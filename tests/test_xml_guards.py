"""K3: a user's ``.vsdx`` map (default rule) and ``.docx`` transcript are refused, before any XML is parsed, when
they are too large, have too many parts, or carry a DOCTYPE/ENTITY declaration. Hostile files are built at run time
in ``ss-tests-*`` temp dirs. Valid files (the demo corpus) must read exactly as before."""
import glob
import hashlib
import json
import tempfile
import unittest
import unittest.mock
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from second_scribe import transcripts as tx
from second_scribe.maps import readers, vsdx_rule
from second_scribe.maps.readers import MapRefused, read_map
from second_scribe.maps.vsdx import ExtractConfig, extract_vsdx
from second_scribe.pipeline import KIT_ROOT

DEMO_VSDX = sorted(glob.glob(str(KIT_ROOT / "demo" / "corpus" / "maps" / "*.vsdx")))
DEMO_DOCX = sorted(glob.glob(str(KIT_ROOT / "demo" / "corpus" / "transcripts" / "*.docx")))
# Digests taken from the code as it was before the preflight existed (18 Oct, base 8e0a6c0).
DOCX_DIGEST = "ad8576c23f7d39525c7f6c20a916e19311a701f649aa87988d3b0615fcf316b2"
VSDX_DIGEST = "0e5a284e40305a8d40c0b5126e900a05b6785f23b85dd2fa7d4579c35b2b7f34"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DOC = (f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}"><w:body>'
       '<w:p><w:r><w:t>Hello there</w:t></w:r></w:p></w:body></w:document>').encode()
EVIL = [b'<!DOCTYPE doc [<!ENTITY x "y">]>', b'<!ENTITY x "y">', b'<!doctype html>', b'<! ENTITY x "y">']


class _NoParse(unittest.TestCase):
    """Any XML parse fails the test: a refusal must come before it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-xmlguards-")
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        boom = lambda *a, **k: self.fail("XML was parsed before the refusal")  # noqa: E731
        for name in ("fromstring", "XML", "parse", "iterparse"):
            p = unittest.mock.patch.object(ET, name, boom)
            p.start()
            self.addCleanup(p.stop)

    def zip_of(self, name, members, base=None):
        p = self.dir / name
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
            if base:
                with zipfile.ZipFile(base) as b:
                    for i in b.infolist():
                        if i.filename in dict(members):          # an override replaces the base part
                            continue
                        z.writestr(i.filename, b.read(i.filename))
            for n, data in members:
                z.writestr(n, data)
        return p

    def sparse(self, name, size):
        p = self.dir / name
        with open(p, "wb") as f:
            f.truncate(size)
        return p


class TestVsdx(_NoParse):
    def read(self, p):
        return read_map(p, "SYN-X", ExtractConfig())

    def refused(self, p, *words):
        with self.assertRaises(MapRefused) as cm:
            self.read(p)
        self.assertEqual(cm.exception.code, "unreadable")
        for w in words:
            self.assertIn(w, cm.exception.detail)

    def test_oversize_file(self):
        p = self.sparse("big.vsdx", readers.MAX_MAP_BYTES + 1)
        self.refused(p, "larger than 50 MB")

    def test_too_many_members(self):
        p = self.dir / "many.vsdx"
        with zipfile.ZipFile(p, "w", zipfile.ZIP_STORED) as z:
            for i in range(vsdx_rule.MAX_MEMBERS + 1):
                z.writestr(f"m{i}.bin", b"")
        self.refused(p, "too many parts")

    def test_doctype_or_entity_member(self):
        for i, evil in enumerate(EVIL):
            for member in ("visio/pages/page1.xml", "visio/extra.xml", "docProps/x.rels"):
                p = self.zip_of(f"dt{i}.vsdx", [(member, evil + b"<a/>")], base=DEMO_VSDX[0])
                with self.subTest(evil=evil, member=member):
                    self.refused(p, "DOCTYPE or ENTITY")

    def test_doctype_utf16_member(self):
        p = self.zip_of("u16.vsdx", [("visio/extra.xml", '<!DOCTYPE a><a/>'.encode("utf-16"))], base=DEMO_VSDX[0])
        self.refused(p, "DOCTYPE or ENTITY")

    def test_bomb_like_oversize_member(self):
        n = vsdx_rule.MAX_MEMBER_BYTES + 1
        p = self.zip_of("bomb.vsdx", [("visio/pages/page1.xml", b"<a>" + b" " * n)], base=DEMO_VSDX[0])
        self.assertLess(p.stat().st_size, 1024 * 1024)            # tiny on disk, huge unpacked
        self.refused(p, "larger than 20 MB")

    def test_total_unpacked_cap(self):
        with unittest.mock.patch.object(vsdx_rule, "MAX_TOTAL_BYTES", 1000):
            self.refused(DEMO_VSDX[0], "XML is larger than")

    def test_duplicate_part_names(self):
        # z.read(name) would return the later copy, so a clean first copy must not hide a hostile one
        p = self.dir / "dup.vsdx"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("visio/pages/page1.xml", EVIL[0] + b"<a/>")
            z.writestr("visio/pages/page1.xml", b"<a/>")
        self.refused(p, "same part twice")

    def test_not_a_zip(self):
        p = self.dir / "junk.vsdx"
        p.write_bytes(b"not a zip at all")
        self.refused(p, "not a readable Visio")

    def test_preflight_runs_before_extract(self):
        p = self.zip_of("dt.vsdx", [("visio/extra.xml", EVIL[0])], base=DEMO_VSDX[0])
        with unittest.mock.patch.object(readers, "extract_vsdx",
                                        side_effect=AssertionError("extract_vsdx reached")):
            self.refused(p, "DOCTYPE or ENTITY")


class TestVsdxValid(unittest.TestCase):
    def test_demo_files_read_identically(self):
        self.assertEqual(len(DEMO_VSDX), 12)
        graphs = []
        for p in DEMO_VSDX:
            g = read_map(p, "M").to_json()
            self.assertEqual(g, extract_vsdx(p, "M").to_json())
            graphs.append(g)
        self.assertEqual(hashlib.sha256("\n".join(graphs).encode()).hexdigest(), VSDX_DIGEST)


class TestDocx(_NoParse):
    def refused(self, p, *words):
        with self.assertRaises(ValueError) as cm:
            tx.read_transcript_paras(p)
        for w in words:
            self.assertIn(w, str(cm.exception))

    def docx(self, name, members=()):
        return self.zip_of(name, [("[Content_Types].xml", b"<Types/>"), ("word/document.xml", DOC), *members])

    def test_oversize_file(self):
        p = self.sparse("big.docx", tx.MAX_TRANSCRIPT_BYTES + 1)
        self.refused(p, "larger than 20 MB")

    def test_too_many_members(self):
        p = self.dir / "many.docx"
        with zipfile.ZipFile(p, "w", zipfile.ZIP_STORED) as z:
            z.writestr("word/document.xml", DOC)
            for i in range(tx.MAX_DOCX_MEMBERS + 1):
                z.writestr(f"m{i}.bin", b"")
        self.refused(p, "more than 10000 parts")

    def test_doctype_or_entity_member(self):
        for i, evil in enumerate(EVIL):
            for member in ("word/document.xml", "word/styles.xml", "word/_rels/document.xml.rels"):
                if member == "word/document.xml":
                    p = self.zip_of(f"dt{i}.docx", [("word/document.xml", evil + DOC)])
                else:
                    p = self.docx(f"dt{i}.docx", [(member, evil + b"<a/>")])
                with self.subTest(evil=evil, member=member):
                    self.refused(p, "DOCTYPE or ENTITY")

    def test_doctype_utf16_member(self):
        p = self.docx("u16.docx", [("word/styles.xml", '<!ENTITY a "b"><a/>'.encode("utf-16"))])
        self.refused(p, "DOCTYPE or ENTITY")

    def test_bomb_like_oversize_member(self):
        n = tx.MAX_TRANSCRIPT_BYTES + 1
        p = self.zip_of("bomb.docx", [("word/document.xml", b"<a>" + b" " * n)])
        self.assertLess(p.stat().st_size, 1024 * 1024)
        self.refused(p, "too large")

    def test_total_unpacked_cap(self):
        p = self.docx("tot.docx")
        with unittest.mock.patch.object(tx, "MAX_DOCX_XML_BYTES", 10):
            self.refused(p, "too large when unpacked")

    def test_duplicate_part_names(self):
        p = self.dir / "dup.docx"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("word/document.xml", EVIL[0] + DOC)
            z.writestr("word/document.xml", DOC)
        self.refused(p, "same part twice")

    def test_not_a_zip_and_no_document(self):
        p = self.dir / "junk.docx"
        p.write_bytes(b"not a zip")
        self.refused(p, "not a readable .docx")
        self.refused(self.zip_of("nodoc.docx", [("[Content_Types].xml", b"<Types/>")]), "not a readable .docx")

    def test_read_docx_text_itself_is_guarded(self):
        p = self.zip_of("dt.docx", [("word/document.xml", EVIL[0] + DOC)])
        with self.assertRaises(ValueError):
            tx.read_docx_text(p)


class TestDocxValid(unittest.TestCase):
    def test_demo_files_read_identically(self):
        self.assertEqual(len(DEMO_DOCX), 12)
        out = []
        for p in DEMO_DOCX:
            paras = tx.read_transcript_paras(p)
            with zipfile.ZipFile(p) as z:                           # an unguarded reference parse
                root = ET.fromstring(z.read("word/document.xml"))
            ref = []
            for para in root.iter(f"{{{W}}}p"):
                parts = []
                for el in para.iter():
                    if el.tag == f"{{{W}}}t":
                        parts.append(el.text or "")
                    elif el.tag in (f"{{{W}}}tab", f"{{{W}}}br"):
                        parts.append(" ")
                ref.append("".join(parts))
            self.assertEqual(paras, ref)
            out.append(paras)
        self.assertEqual(hashlib.sha256(json.dumps(out, ensure_ascii=False).encode()).hexdigest(), DOCX_DIGEST)

    def test_minimal_docx_reads(self):
        with tempfile.TemporaryDirectory(prefix="ss-tests-xmlguards-") as d:
            p = Path(d) / "ok.docx"
            with zipfile.ZipFile(p, "w") as z:
                z.writestr("word/document.xml", DOC)
            self.assertEqual(tx.read_transcript_paras(p), ["Hello there"])


if __name__ == "__main__":
    unittest.main()
