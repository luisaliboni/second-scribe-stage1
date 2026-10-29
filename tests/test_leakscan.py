"""The leak scanner's engine (second_scribe/leakscan.py): folding, names, views of a file, archives and binary
files, git. Fixture data only: the "real" sentences below are invented."""
import bz2
import gzip
import io
import json
import lzma
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

from second_scribe import audits, leakscan
from second_scribe.audits import LeakIndex, audit_leaks, audit_repo
from second_scribe.leakscan import scan_named_bytes
from tests.test_transcripts import make_docx

S1 = "My daughter Tamsin was refused a place because the clinic lost the referral paperwork twice"
S2 = "We drove four hours every Thursday to reach the specialist who finally listened to us"
S3 = "Our daughter’s clinic never sent the referral onward"
S4 = "Café owner told them the permit office never answered the phone"
LEAK = "we drove four hours every Thursday to reach"          # five or more words of S2


def kinds(idx, name, data, **kw):
    hits, reason = scan_named_bytes(idx, name, data, **kw)
    return sorted(k for _l, k, _o in hits), reason


def idx_of(*texts, names=(), stems=(), **kw):
    return LeakIndex.build(list(texts) or [S1, S2, S3, S4], names, stems=stems, **kw)


class TestFolding(unittest.TestCase):
    def setUp(self):
        self.idx = idx_of()

    def hit(self, text):
        return [k for k, _ in self.idx.scan(text)] == ["ngram"]

    def test_invisible_characters_do_not_hide_a_copy(self):
        self.assertTrue(self.hit("we drove four hou​rs every Thursday to reach"))        # zero-width space
        self.assertTrue(self.hit("we drove four hou­rs every Thursday to reach"))        # soft hyphen
        self.assertTrue(self.hit("we dro⁠ve four hours every‍ Thursday to reach"))  # word joiner, ZWJ

    def test_accents_are_folded_whatever_their_form(self):
        self.assertTrue(self.hit("Cafe owner told them the permit office never"))
        self.assertTrue(self.hit("Café owner told them the permit office never"))       # decomposed
        self.assertTrue(self.hit("CAFÉ OWNER TOLD THEM THE PERMIT"))
        self.assertTrue(self.hit("ｗｅ ｄｒｏｖｅ four hours every Thursday to reach"))        # full-width

    def test_look_alike_letters_are_mapped_to_latin(self):
        self.assertTrue(self.hit("we drove four hours every Thurѕday to reach"))              # Cyrillic ѕ
        self.assertTrue(self.hit("wе drоve fоur hоurs every Thursday to rеach"))              # Cyrillic е, о
        self.assertTrue(self.hit("we drove four hours every Thursday tο reach"))              # Greek omicron
        self.assertEqual(len(leakscan.CONFUSABLES), len(set(leakscan.CONFUSABLES)))
        for src, dst in leakscan.CONFUSABLES.items():
            self.assertTrue(dst.isascii() and dst.isalpha(), (src, dst))

    def test_a_hyphenated_line_break_is_joined_and_the_unjoined_text_still_matches(self):
        self.assertTrue(self.hit("the clinic lost the refer-\nral paperwork twice"))
        self.assertTrue(self.hit("the clinic lost the refer-  \r\n  ral paperwork twice"))
        idx = idx_of("Our well-\nknown clinic lost every single record last spring")       # a real hyphen at a line end
        self.assertTrue(idx.scan("our well known clinic lost every single record"))
        self.assertTrue(idx.scan("our wellknown clinic lost every single record"))

    def test_the_stopword_exemption(self):
        idx = idx_of("we were in the kitchen and they were in the garden every day",
                     "Honestly i was waiting for permits all summer long")
        self.assertEqual(idx.scan("we were in the kitchen"), [])                     # 4 of 5 are stopwords
        self.assertEqual(idx.scan("and they were in the"), [])                       # 5 of 5
        self.assertEqual([k for k, _ in idx.scan("so i was waiting for permits all")], ["ngram"])   # 3 stopwords: hits
        self.assertEqual(len(idx._grams), len(set(idx._grams)))
        # the exemption is on both sides: an index of stopword-only text has no grams at all
        self.assertEqual(idx_of("we were in the kitchen", S1).stats["n_ngrams"],
                         idx_of(S1).stats["n_ngrams"])


class TestNames(unittest.TestCase):
    def names(self, *names, stems=()):
        return idx_of(names=names, stems=stems)

    def n(self, idx, text):
        return sum(1 for k, _ in idx.scan(text) if k == "name")

    def test_initials_match_capitals_only_with_their_variants(self):
        idx = self.names("XY")
        for hit in ("XY", "XY2", "XY12", "X.Y.", "X Y", "(XY)", "[XY] said", "XY's"):
            self.assertEqual(self.n(idx, hit), 1, hit)
        for miss in ("xy", "Xy", "xyz", "boxy", "XYZ", "XY123", "AXY"):
            self.assertEqual(self.n(idx, miss), 0, miss)

    def test_ids_match_in_capitals_only_with_any_separator_space_or_zero_padding(self):
        idx = self.names("K07")
        for hit in ("K07", "K7", "K-07", "K_7", "K007", "K 07", "(K 7)"):
            self.assertEqual(self.n(idx, hit), 1, hit)
        for miss in ("K70", "K077", "Q7", "UP7", "k7", "k07", "k-7", "k 7", "full__k7__t0"):
            self.assertEqual(self.n(idx, miss), 0, miss)
        idx = self.names("KC12", "QZ-OW01")
        self.assertEqual(self.n(idx, "KC12 and KC-12 and KC 12 and QZ_OW_1 and QZOW01"), 5)
        self.assertEqual(self.n(idx, "pc12 and qz_ow_1"), 0)

    def test_public_and_generic_names_are_never_names(self):
        idx = self.names("I1", "I2", "I3", "pt", "iv", "SYN-OW01", "Interviewer", "Participant", "Interviewee", "Caregiver",
                         "Provider", "Researcher", "Participant 3", "Caregiver_2", "QZ")
        self.assertEqual(self.n(idx, "I1 I2 I3 PT IV SYN-OW01 Interviewer Participant interviewee caregiver provider "
                                     "researcher Participant 3 Caregiver_2"), 0)
        self.assertEqual(self.n(idx, "see QZ"), 1)                                   # the rest still works
        self.assertEqual(idx.stats["n_names"], 1)

    def test_a_full_name_matches_with_any_joiner(self):
        idx = self.names("Marguerite Tremblay")
        for hit in ("Marguerite Tremblay", "marguerite.tremblay", "Marguerite_Tremblay", "MARGUERITE-TREMBLAY",
                    "marguerite   tremblay"):
            self.assertEqual(self.n(idx, hit), 1, hit)
        self.assertEqual(self.n(idx, "marguerite and tremblay"), 0)

    def test_file_name_stems_give_only_id_shaped_parts_never_words_or_acronyms(self):
        idx = self.names(stems=("XY_map_copy", "20260512_JD_AB_interview", "KC07-notes"))
        self.assertEqual(self.n(idx, "KC07 and KC-7"), 2)
        self.assertEqual(self.n(idx, "see the 20260512_JD_AB_interview file"), 1)                  # the whole stem: one name
        for word in ("XY", "JD", "AB", "map", "copy", "interview", "notes", "20260512", "xy", "jd"):
            self.assertEqual(self.n(idx, f"a {word} b"), 0, word)

    def test_code_identifiers_do_not_match_lowercase_initials(self):
        idx = self.names("AB", stems=("2026_AB_CD_x",))
        code = "ab = 1\ntab = 2\nlab_ab_x = lab + kab\nclass Slab: pass\nab_test(1)\n"
        self.assertEqual(self.n(idx, code), 0)
        self.assertEqual(self.n(idx, "ID AB here"), 1)

    def test_variants_hidden_by_encoding_are_found(self):
        idx = self.names("XY", "Jose")
        self.assertEqual(self.n(idx, "&#88;&#89; and &#x58;Y"), 2)                    # entity-encoded
        self.assertEqual(self.n(idx, "X​Y"), 1)                                  # zero-width space inside
        self.assertEqual(self.n(idx, "ХY"), 1)                                   # Cyrillic Х
        self.assertEqual(self.n(idx, "José and JOSÉ and José and jose"), 4)  # accents, any case: a name

    def test_interviewer_codes_pids_and_labels_come_from_the_config(self):
        d = Path(tempfile.mkdtemp())
        from tests.test_audits import real_like_config
        cfg = real_like_config(d)
        c = json.loads(Path(cfg).read_text())
        c["corpus"]["interviewers"] = {"SYN-OW01": "KL"}
        c["corpus"]["interviewer_labels"] = {"*": ["iv", "MQ"]}
        Path(cfg).write_text(json.dumps(c))
        from second_scribe.pipeline import RunConfig
        idx = LeakIndex.from_config(RunConfig.load(cfg))
        for token in ("KL", "MQ", "KC99", "QZ"):
            self.assertEqual(self.n(idx, f"a {token} b"), 1, token)
        for public in ("IV", "PT", "I1"):                     # public speaker labels and interviewer codes: exempt
            self.assertEqual(self.n(idx, f"a {public} b"), 0, public)
        self.assertEqual(self.n(idx, "a SYN-OW01 b"), 1)          # a pid from corpus.roles is never dropped
        self.assertEqual(idx.stats["id_map_entries"], 1)
        self.assertEqual(idx.problems, [])                        # SYN-* ids are not public words: no collision


class TestPublicNames(unittest.TestCase):
    """Speaker labels and file stems may be exempt as public; names of the id map, roles and pairs never are."""

    def config(self, id_map=None, **corpus):
        from tests.test_audits import real_like_config
        from second_scribe.pipeline import RunConfig
        d = Path(tempfile.mkdtemp())
        path = real_like_config(d, **corpus)
        if id_map is not None:
            m = d / "participant_id_map.json"
            m.write_text(json.dumps({"_meta": {}, "map": id_map}))
        return RunConfig.load(path)

    def test_an_id_map_name_that_is_a_demo_label_is_kept_and_fails_closed(self):
        for key, val in (("PT", "KC99"), ("QZ", "PT"), ("QZ", "I3"), ("I3", "KC99"), ("QZ", "PT01"), ("QZ", "MAP")):
            idx = LeakIndex.from_config(self.config({key: {"canonical_id": val}}))
            self.assertEqual(idx.problems, ["public_name_collision"], (key, val))
            self.assertEqual(idx.stats["public_name_collisions"], 1)
            hit = key if key in ("PT", "I3") else val
            self.assertEqual([k for k, _ in idx.scan(f"a {hit} b")], ["name"], (key, val))      # kept, not dropped
            r = audit_leaks([self.write_file(idx)], idx)
            self.assertFalse(r.passed)
            self.assertEqual(r.failures, ["index_problem:public_name_collision"])
            self.assertNotIn(hit, json.dumps(r.failures))                                       # count only

    def write_file(self, idx) -> str:
        p = Path(tempfile.mkdtemp()) / "a.txt"
        p.write_text("fine words only")
        return str(p)

    def test_roles_and_pairs_names_are_never_dropped_either(self):
        idx = LeakIndex.from_config(self.config({"QZ": {"canonical_id": "KC99"}}, roles={"PT": "owner", "SYN-OW01": "owner"}))
        self.assertEqual(idx.problems, ["public_name_collision"])
        self.assertEqual([k for k, _ in idx.scan("a PT b")], ["name"])

    def test_a_speaker_label_and_an_interviewer_code_that_are_public_are_exempt_and_counted(self):
        cfg = self.config({"QZ": {"canonical_id": "KC99"}}, interviewer_labels={"*": ["I1", "Interviewer", "MQ"]},
                          participant_labels={"*": ["pt", "Participant"]}, interviewers={"SYN-OW01": "I1", "SYN-OW02": "I2"})
        idx = LeakIndex.from_config(cfg)
        self.assertEqual(idx.problems, [])
        for public in ("I1", "I2", "pt", "Interviewer", "Participant"):
            self.assertEqual(idx.scan(f"a {public} b"), [], public)
        self.assertEqual([k for k, _ in idx.scan("a MQ b")], ["name"])
        # I1, Interviewer, pt, Participant (labels) + 2 codes + the 12 demo file stems
        self.assertEqual(idx.stats["names_public_exempt"], 4 + 2 + 12)
        f = Path(tempfile.mkdtemp()) / "a.txt"
        f.write_text("fine words only")
        r = audit_leaks([str(f)], idx)
        self.assertTrue(r.passed, r.failures)
        self.assertEqual(r.counts["index_names_public_exempt"], 18)

    def test_a_non_numeric_interviewer_code_is_a_name_not_an_exemption(self):
        idx = LeakIndex.from_config(self.config({"QZ": {"canonical_id": "KC99"}}, interviewers={"SYN-OW01": "KL"}))
        self.assertEqual([k for k, _ in idx.scan("a KL b")], ["name"])


class TestViews(unittest.TestCase):
    def setUp(self):
        self.idx = idx_of()
        self.dir = Path(tempfile.mkdtemp())

    def found(self, name, data, kind="ngram", **kw):
        got, reason = kinds(self.idx, name, data if isinstance(data, bytes) else data.encode("utf-8"), **kw)
        self.assertIsNone(reason)
        return got.count(kind)

    def test_json_escapes_in_any_json_shaped_file(self):
        text = json.dumps({"note": "daughter’s clinic never sent the", "who": "Café owner told them the permit"})
        self.assertIn("\\u2019", text)
        self.assertEqual(self.found("a.json", text), 2)
        self.assertEqual(self.found("a.txt", text), 2)               # any file that parses as JSON
        self.assertEqual(self.found("a.json", json.dumps({"x": LEAK}, ensure_ascii=False)), 1)    # once, not twice

    def test_jsonl_and_notebook_sources_split_into_fragments(self):
        lines = "\n".join(json.dumps({"t": s}) for s in ("fine", "daughter’s clinic never sent the referral"))
        self.assertEqual(self.found("a.jsonl", lines), 1)
        nb = json.dumps({"cells": [{"source": ["we drove four hours every Th", "ursday to reach\n"]}]})
        self.assertEqual(self.found("a.ipynb", nb), 1)

    def test_html_entities_and_tags(self):
        self.assertEqual(self.found("a.html", "<p>daughter&rsquo;s clinic never sent the referral</p>"), 1)
        self.assertEqual(self.found("a.html", "<p>we drove four <b>hours</b> every Thursday to reach</p>"), 1)
        self.assertEqual(self.found("a.html", "we dr<i>o</i>ve four hours every Thursday to reach"), 1)   # mid-word run split
        self.assertEqual(self.found("a.html", "we drove four hours every&#32;Thursday&nbsp;to reach"), 1)
        self.assertEqual(self.found("a.txt", "we drove four hours every&#32;Thursday to reach"), 1)
        self.assertEqual(self.found("a.html", "we drove four hours every Thursday to reach"), 1)        # once only

    def test_word_files_with_runs_split_anywhere_and_other_parts(self):
        p = self.dir / "t.docx"
        make_docx(p, [["we dro", "ve four hours every Th", "ursday to reach"]])
        got, reason = kinds(self.idx, p.name, p.read_bytes())
        self.assertEqual((got.count("ngram"), reason), (1, None))
        with zipfile.ZipFile(self.dir / "h.docx", "w") as z:
            z.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>fine</w:t></w:r></w:p></w:body></w:document>")
            z.writestr("word/header1.xml", "<w:hdr><w:p><w:r><w:t>we drove four hours every </w:t></w:r>"
                                           "<w:r><w:t>Thursday to reach</w:t></w:r></w:p></w:hdr>")
            z.writestr("word/comments.xml", "<w:comments><w:comment><w:p><w:r><w:t>Tamsin was refused</w:t></w:r></w:p>"
                                            "</w:comment></w:comments>")
        got, reason = kinds(self.idx, "h.docx", (self.dir / "h.docx").read_bytes())
        self.assertEqual(got.count("ngram"), 1)

    def test_spreadsheet_strings(self):
        with zipfile.ZipFile(self.dir / "s.xlsx", "w") as z:
            z.writestr("xl/sharedStrings.xml", f"<sst><si><t>fine</t></si><si><t>{LEAK}</t></si></sst>")
        got, _ = kinds(self.idx, "s.xlsx", (self.dir / "s.xlsx").read_bytes())
        self.assertEqual(got.count("ngram"), 1)

    def test_rtf_and_tex_control_words(self):
        rtf = r"{\rtf1\ansi we drove four hours every {\b Thursday} to \b0 reach}"
        self.assertEqual(self.found("a.rtf", rtf), 1)
        self.assertEqual(self.found("a.rtf", r"{\rtf1 Caf\'e9 owner told them the permit office}"), 1)
        self.assertEqual(self.found("a.tex", r"\textbf{we drove} four hours every \emph{Thursday} to reach"), 1)

    def test_a_long_string_is_a_hit_only_in_aggregate_json(self):
        data = json.dumps({"k": "y" * 41}).encode()
        self.assertEqual(kinds(self.idx, "a.json", data, aggregate_json=True)[0], ["long_string"])
        self.assertEqual(kinds(self.idx, "a.json", data)[0], [])


class TestFailClosed(unittest.TestCase):
    def setUp(self):
        self.idx = idx_of()
        self.dir = Path(tempfile.mkdtemp())

    def zip_of(self, members: dict) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, data in members.items():
                z.writestr(name, data)
        return buf.getvalue()

    def test_nested_archives_are_opened_and_their_depth_is_bounded(self):
        inner = self.zip_of({"a.txt": LEAK})
        outer = self.zip_of({"in.zip": inner, "b.txt": "fine"})
        got, reason = kinds(self.idx, "o.zip", self.zip_of({"o2.zip": outer}))
        self.assertEqual((got, reason), (["ngram"], None))
        deep = LEAK.encode()
        for _ in range(6):
            deep = self.zip_of({"x.zip": deep})
        self.assertEqual(kinds(self.idx, "d.zip", deep), ([], "nested_too_deep"))

    def test_compressed_files_and_tarballs_are_opened(self):
        for name, data in (("a.txt.gz", gzip.compress(LEAK.encode())), ("a.txt.bz2", bz2.compress(LEAK.encode())),
                           ("a.txt.xz", lzma.compress(LEAK.encode()))):
            self.assertEqual(kinds(self.idx, name, data), (["ngram"], None), name)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo("x/a.txt")
            info.size = len(LEAK)
            tf.addfile(info, io.BytesIO(LEAK.encode()))
        self.assertEqual(kinds(self.idx, "a.tar", buf.getvalue()), (["ngram"], None))
        self.assertEqual(kinds(self.idx, "a.gz", b"\x1f\x8bnot really gzip")[1], "bad_compressed")

    def test_an_encrypted_zip_is_unscannable(self):
        raw = bytearray(self.zip_of({"a.txt": LEAK}))
        i = raw.index(b"PK\x01\x02")
        raw[i + 8] |= 1                                         # general purpose flag: encrypted
        self.assertEqual(kinds(self.idx, "e.zip", bytes(raw)), ([], "encrypted"))

    def test_binary_and_unreadable_formats_fail_closed(self):
        self.assertEqual(kinds(self.idx, "x.dat", b"abc\x00\x01\x02" * 200)[1], "binary")
        self.assertEqual(kinds(self.idx, "x.7z", b"7z\xbc\xaf\x27\x1c" + b"\0" * 20)[1], "unreadable_format")
        self.assertEqual(kinds(self.idx, "x.bin", b"\xd0\xcf\x11\xe0" + b"\0" * 20)[1], "unreadable_format")
        self.assertEqual(kinds(self.idx, "x.pdf", b"%PDF-1.4 stuff")[1], "unreadable_format")
        self.assertEqual(kinds(self.idx, "x.txt", b"ab \x81\x8d\x8f\x90 cd " * 50)[1], "undecodable")
        unknown_member = self.zip_of({"a.txt": "fine", "word/vba.xyz": b"\x00\x01\x02" * 100})
        self.assertEqual(kinds(self.idx, "m.docx", unknown_member)[1], "binary")
        known_member = self.zip_of({"a.txt": "fine", "word/media/i.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 50})
        self.assertEqual(kinds(self.idx, "m.docx", known_member), ([], None))

    def test_other_encodings_are_decoded(self):
        text = LEAK
        for enc in ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be", "utf-16", "utf-32", "utf-8-sig"):
            self.assertEqual(kinds(self.idx, "a.txt", text.encode(enc)), (["ngram"], None), enc)
        # a text that is not UTF-8 is read as cp1252 too
        data = ("Café owner told them the permit office never. " + LEAK).encode("cp1252")
        self.assertEqual(kinds(self.idx, "a.txt", data)[0].count("ngram"), 2)

    def test_hits_in_other_encodings_report_a_byte_offset(self):
        data = ("xx " + LEAK).encode("utf-16-le")
        hits, _ = scan_named_bytes(self.idx, "a.txt", data)
        self.assertEqual(hits[0][2], 6)

    @staticmethod
    def png(*chunks) -> bytes:
        out = b"\x89PNG\r\n\x1a\n"
        for kind, body in chunks:
            out += len(body).to_bytes(4, "big") + kind + body + b"\0\0\0\0"
        return out

    def test_image_metadata_is_scanned(self):
        t = self.png((b"tEXt", b"Comment\x00" + LEAK.encode()))
        z = self.png((b"zTXt", b"Comment\x00\x00" + zlib.compress(LEAK.encode())))
        i = self.png((b"iTXt", b"Comment\x00\x01\x00en\x00\x00" + zlib.compress(LEAK.encode())))
        for name, data in (("a.png", t), ("b.png", z), ("c.png", i)):
            self.assertEqual(kinds(self.idx, name, data), (["ngram"], None), name)
        jpg = b"\xff\xd8\xff\xfe\x00\x40" + LEAK.encode() + b"\xff\xd9"
        self.assertEqual(kinds(self.idx, "a.jpg", jpg), (["ngram"], None))
        self.assertEqual(kinds(self.idx, "a.png", self.png((b"IDAT", b"\x00\x01\x02")))[0], [])

    def test_an_svg_without_text_elements_is_unverifiable_unless_its_data_is_scanned(self):
        outlines = b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0L9 9"/></svg>'
        self.assertEqual(kinds(self.idx, "f.svg", outlines)[0], ["unverifiable_svg"])
        self.assertEqual(kinds(self.idx, "f.svg", b"<svg><text>fine</text></svg>")[0], [])
        (self.dir / "f.svg").write_bytes(outlines)
        self.assertEqual([f.split(":")[0] for f in audit_leaks([str(self.dir / "f.svg")], self.idx).failures],
                         ["unverifiable_svg"])
        (self.dir / "f.json").write_text("{}")
        r = audit_leaks([str(self.dir / "f.svg"), str(self.dir / "f.json")], self.idx)
        self.assertTrue(r.passed, r.failures)

    def test_unscannable_reasons_are_counted_and_fail_the_scan(self):
        (self.dir / "x.dat").write_bytes(b"abc\x00\x01\x02" * 200)
        (self.dir / "y.7z").write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\0" * 20)
        r = audit_leaks([str(self.dir)], self.idx)
        self.assertFalse(r.passed)
        self.assertEqual(r.counts["unscannable_reasons"], {"binary": 1, "unreadable_format": 1})
        self.assertEqual(r.counts["files_unscannable"], 2)

    def test_the_cli_reports_any_unexpected_error_as_2_never_as_hits(self):
        from unittest import mock
        from tests.test_audits import cli, real_like_config
        cfg = real_like_config(self.dir / "cfgdir" if (self.dir / "cfgdir").mkdir() is None else self.dir)
        f = self.dir / "a.txt"
        f.write_text("fine")
        with mock.patch.object(audits, "audit_leaks", side_effect=RuntimeError("boom")):
            rc, _o, err = cli(["audit-leaks", "--paths", str(f), "--real-config", cfg])
        self.assertEqual(rc, 2)
        self.assertNotIn("boom", err)


class TestIndexSources(unittest.TestCase):
    def setUp(self):
        from tests.test_audits import real_like_config
        self.dir = Path(tempfile.mkdtemp())
        self.cfg_path = real_like_config(self.dir)

    def cfg(self):
        from second_scribe.pipeline import RunConfig
        return RunConfig.load(self.cfg_path)

    def test_the_work_files_are_indexed_ai_labels_and_quotes_included(self):
        work = self.dir / "work" / "recon"
        work.mkdir(parents=True)
        label = "the model wrote this long invented label about a clinic referral"
        (work / "P__x.json").write_text(json.dumps({"status": "ok", "map": {"nodes": [
            {"node_id": "n1", "text": label, "meta": {"spec": "full__p0__t0__r0"}}]}}))
        idx = LeakIndex.from_config(self.cfg())
        self.assertEqual([k for k, _ in idx.scan("see: invented label about a clinic referral")], ["ngram"])
        self.assertEqual(idx.scan("full__p0__t0__r0 and p0 t0 r0 full p0"), [])      # the kit's own identifiers: not text

    def test_a_real_corpus_must_name_its_id_map(self):
        c = json.loads(Path(self.cfg_path).read_text())
        del c["corpus"]["participant_id_map"]
        Path(self.cfg_path).write_text(json.dumps(c))
        with self.assertRaises(leakscan.IndexUnavailable) as cm:
            LeakIndex.from_config(self.cfg())
        self.assertEqual(cm.exception.code, "id_map_required")
        # a synthetic corpus does not need one
        c["corpus"]["kind"] = "synthetic"
        Path(self.cfg_path).write_text(json.dumps(c))
        self.assertEqual(LeakIndex.from_config(self.cfg()).stats["id_map_entries"], 0)

    def test_an_unreadable_or_empty_id_map_is_a_problem_and_the_count_is_reported(self):
        c = json.loads(Path(self.cfg_path).read_text())
        m = self.dir / "empty_map.json"
        m.write_text(json.dumps({"map": {}}))
        c["corpus"]["participant_id_map"] = str(m)
        Path(self.cfg_path).write_text(json.dumps(c))
        idx = LeakIndex.from_config(self.cfg())
        self.assertEqual(idx.problems, ["id_map_empty"])
        f = self.dir / "a.txt"
        f.write_text("fine")
        r = audit_leaks([str(f)], idx)
        self.assertFalse(r.passed)
        m.write_text(json.dumps({"map": {"QZ": {"canonical_id": "KC99"}, "WV": {"canonical_id": "KC98"}}}))
        r = audit_leaks([str(f)], LeakIndex.from_config(self.cfg()))
        self.assertTrue(r.passed)
        self.assertEqual(r.counts["index_id_map_entries"], 2)

    def test_headers_footers_and_comments_of_a_word_transcript_are_indexed(self):
        d = Path(tempfile.mkdtemp())
        p = d / "XY_t.docx"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                                            "<w:body><w:p><w:r><w:t>ordinary body words here</w:t></w:r></w:p></w:body></w:document>")
            z.writestr("word/footer1.xml", "<w:ftr><w:p><w:r><w:t>footer note about the secret clinic</w:t></w:r></w:p></w:ftr>")
            z.writestr("word/comments.xml", "<w:comments><w:comment><w:p><w:r><w:t>reviewer remark on the "
                                            "hidden kitchen</w:t></w:r></w:p></w:comment></w:comments>")
        from second_scribe.pipeline import RunConfig
        c = json.loads(Path(self.cfg_path).read_text())
        c["corpus"].update(transcripts_glob=str(d / "*.docx"), maps_glob=str(d / "*.vsdx"))
        Path(self.cfg_path).write_text(json.dumps(c))
        idx = LeakIndex.from_config(RunConfig.load(self.cfg_path))
        for phrase in ("note about the secret clinic", "remark on the hidden kitchen"):
            self.assertEqual([k for k, _ in idx.scan(phrase + " today")], ["ngram"], phrase)


# ----------------------------------------------------------------------------------------- git

class TestRepo(unittest.TestCase):
    """What ``git push`` publishes: the index, HEAD, every unpushed commit, every path and message."""

    def setUp(self):
        self.idx = idx_of(names=["KC12"])
        self.repo = Path(tempfile.mkdtemp())
        self.git("init", "-q")

    def git(self, *args, repo=None):
        return subprocess.run(["git", "-C", str(repo or self.repo), "-c", "user.name=t", "-c", "user.email=t@example.test",
                               *args], check=True, capture_output=True, text=True).stdout

    def commit(self, msg="c"):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)

    def audit(self):
        return audit_repo(self.repo, self.idx)

    def test_a_clean_repo_passes(self):
        (self.repo / "a.md").write_text("fine words only")
        self.commit()
        r = self.audit()
        self.assertTrue(r.passed, r.failures)
        self.assertEqual((r.counts["commits_scanned"], r.counts["blobs_scanned"], r.counts["dirty_entries"]), (1, 1, 0))

    def test_committed_then_cleaned_is_caught_in_history(self):
        (self.repo / "a.md").write_text(LEAK)
        self.commit("add")
        (self.repo / "a.md").write_text("clean now")
        self.commit("clean")
        r = self.audit()
        self.assertFalse(r.passed)
        self.assertEqual(r.counts["ngram_hits"], 1)

    def test_committed_then_removed_in_a_later_commit_is_caught(self):
        (self.repo / "a.md").write_text(LEAK)
        (self.repo / "b.md").write_text("fine")
        self.commit("add")
        self.git("rm", "-q", "a.md")
        self.git("commit", "-q", "-m", "remove")
        self.assertEqual(self.audit().counts["ngram_hits"], 1)

    def test_staged_but_cleaned_in_the_working_tree_is_caught_and_the_dirty_tree_refused(self):
        (self.repo / "a.md").write_text("fine")
        self.commit()
        (self.repo / "a.md").write_text(LEAK)
        self.git("add", "a.md")
        (self.repo / "a.md").write_text("fine again")
        r = self.audit()
        self.assertEqual(r.counts["ngram_hits"], 1)                         # the index blob
        self.assertTrue(any(f.startswith("dirty_tree") for f in r.failures), r.failures)

    def test_an_id_only_in_a_file_name_is_caught_without_printing_the_name(self):
        (self.repo / "KC12_notes.md").write_text("fine words only")
        self.commit()
        r = self.audit()
        self.assertEqual(r.counts["name_hits"], 1)
        self.assertNotIn("KC12", json.dumps(r.failures))
        self.assertFalse(r.passed)

    def test_a_commit_message_is_scanned(self):
        (self.repo / "a.md").write_text("fine")
        self.commit(f"notes: {LEAK}")
        r = self.audit()
        self.assertEqual(r.counts["ngram_hits"], 1)
        self.assertTrue(r.failures[0].startswith("ngram:commit message"))

    def test_with_an_upstream_the_unpushed_commits_are_scanned(self):
        remote = Path(tempfile.mkdtemp())
        self.git("init", "-q", "--bare", repo=remote)
        (self.repo / "a.md").write_text("fine")
        self.commit("one")
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "-q", "-u", "origin", "HEAD")
        self.assertTrue(self.audit().passed)
        (self.repo / "a.md").write_text(LEAK)
        self.commit("two")
        (self.repo / "a.md").write_text("fine")
        self.commit("three")
        r = audit_repo(self.repo, self.idx, since_upstream=True)
        self.assertEqual((r.counts["has_upstream"], r.counts["commits_scanned"], r.counts["ngram_hits"]), (1, 2, 1))
        # the default is the whole history: a leak that was already pushed is found too
        (self.repo / "b.md").write_text(LEAK + " again and more")
        self.commit("four")
        self.git("push", "-q")
        (self.repo / "b.md").write_text("fine")
        self.commit("five")
        self.assertEqual(audit_repo(self.repo, self.idx, since_upstream=True).counts["ngram_hits"], 0)
        self.assertEqual(self.audit().counts["ngram_hits"], 2)

    def merge_with(self, branch_files: dict, main_files: dict, resolved: str):
        """main and a branch each change a.md; the merge is resolved by hand to ``resolved`` (a blob that exists only in the
        merge commit)."""
        (self.repo / "a.md").write_text("base line\n")
        self.commit("base")
        base = self.git("rev-parse", "--abbrev-ref", "HEAD").strip()
        self.git("checkout", "-q", "-b", "side")
        for name, text in branch_files.items():
            (self.repo / name).write_text(text)
        self.commit("side")
        self.git("checkout", "-q", base)
        for name, text in main_files.items():
            (self.repo / name).write_text(text)
        self.commit("main")
        merge = subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@example.test",
                                "merge", "side", "-m", "merge"], capture_output=True, text=True)
        self.assertNotEqual(merge.returncode, 0)                          # a conflict, resolved by hand
        (self.repo / "a.md").write_text(resolved)
        self.commit("resolved")

    def test_a_blob_that_exists_only_in_a_conflict_resolved_merge_is_caught_even_after_cleaning(self):
        self.merge_with({"a.md": "side line\n"}, {"a.md": "main line\n"}, f"{LEAK}\n")
        (self.repo / "a.md").write_text("clean now\n")
        self.commit("clean")
        r = self.audit()
        self.assertEqual(r.counts["ngram_hits"], 1)
        self.assertFalse(r.passed)

    def test_a_file_added_only_in_a_merge_and_later_removed_is_caught(self):
        self.merge_with({"a.md": "side line\n"}, {"a.md": "main line\n"}, "resolved\n")
        # an evil merge: a new file that no parent has, committed with the merge, removed later
        (self.repo / "evil.md").write_text(LEAK)
        self.git("add", "-A")
        self.git("commit", "-q", "--amend", "--no-edit")
        self.git("rm", "-q", "evil.md")
        self.git("commit", "-q", "-m", "remove")
        self.assertEqual(self.audit().counts["ngram_hits"], 1)

    def test_an_annotated_tag_message_is_scanned_and_a_lightweight_tag_is_not_content(self):
        (self.repo / "a.md").write_text("fine")
        self.commit()
        self.git("tag", "light")
        self.assertTrue(self.audit().passed)
        self.git("tag", "-a", "v1", "-m", f"release notes: {LEAK}")
        r = self.audit()
        self.assertEqual((r.counts["tags_scanned"], r.counts["ngram_hits"]), (1, 1))
        self.assertTrue(r.failures[0].startswith("ngram:tag message"))

    def test_a_git_lfs_pointer_is_unscannable(self):
        (self.repo / "big.bin").write_text("version https://git-lfs.github.com/spec/v1\noid sha256:" + "a" * 64 + "\nsize 9\n")
        self.commit()
        r = self.audit()
        self.assertFalse(r.passed)
        self.assertEqual(r.counts["unscannable_reasons"], {"lfs_pointer": 1})

    def test_an_svg_without_text_elements_fails_the_repo_scan(self):
        (self.repo / "f.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0L9 9"/></svg>')
        (self.repo / "f.json").write_text("{}")                  # a sibling data file does not excuse it in a repo
        self.commit()
        r = self.audit()
        self.assertFalse(r.passed)
        self.assertEqual(r.counts["unverifiable_svg_hits"], 1)

    def test_a_file_renamed_and_renamed_back_is_caught_by_its_middle_name(self):
        (self.repo / "a.md").write_text("fine words only")
        self.commit("one")
        self.git("mv", "a.md", "KC12_notes.md")
        self.commit("rename")
        self.git("mv", "KC12_notes.md", "a.md")
        self.commit("back")
        self.assertEqual(self.audit().counts["name_hits"], 1)                # the bytes never changed; the name did

    def test_a_renamed_directory_is_caught_by_its_old_name_and_each_path_under_it(self):
        (self.repo / "KC12").mkdir()
        (self.repo / "KC12" / "x.md").write_text("one")
        (self.repo / "KC12" / "y.md").write_text("two")
        self.commit("one")
        self.git("mv", "KC12", "other")
        self.commit("rename")
        r = self.audit()
        self.assertEqual(r.counts["name_hits"], 3)                           # KC12, KC12/x.md, KC12/y.md
        self.assertTrue(all("path of object" in f for f in r.failures))
        self.assertNotIn("KC12", json.dumps(r.failures))

    def test_a_path_that_exists_only_in_a_merge_with_the_bytes_of_another_file_is_caught(self):
        self.merge_with({"a.md": "side line\n"}, {"a.md": "main line\n"}, "resolved\n")
        (self.repo / "KC12_copy.md").write_text("resolved\n")               # same bytes as a.md at the merge
        self.git("add", "-A")
        self.git("commit", "-q", "--amend", "--no-edit")
        self.git("rm", "-q", "KC12_copy.md")
        self.git("commit", "-q", "-m", "remove")
        self.assertEqual(self.audit().counts["name_hits"], 1)

    def test_the_attribution_trailer_of_a_commit_is_not_a_leak(self):
        (self.repo / "a.md").write_text("fine")
        self.commit("work\n\nCo-Authored-By: Claude <" + "noreply" + "@" + "anthropic.com>\nClaude-Session: https://claude.ai/code/x")
        self.assertTrue(self.audit().passed)

    def test_untracked_files_are_not_scanned_and_do_not_make_the_tree_dirty(self):
        (self.repo / "a.md").write_text("fine")
        self.commit()
        (self.repo / "untracked.md").write_text(LEAK)
        self.assertTrue(self.audit().passed)

    def test_aggregate_json_rules_apply_to_blobs_under_aggregates(self):
        (self.repo / "aggregates").mkdir()
        (self.repo / "aggregates" / "a.json").write_text(json.dumps({"k": "z" * 41}))
        (self.repo / "other.json").write_text(json.dumps({"k": "z" * 41}))
        self.commit()
        self.assertEqual(self.audit().counts["long_string_hits"], 1)

    def test_an_empty_repo_and_a_missing_repo(self):
        self.assertTrue(self.audit().passed)
        with self.assertRaises(subprocess.CalledProcessError):
            audit_repo(Path(tempfile.mkdtemp()), self.idx)

    def test_the_cli_exits_2_when_the_folder_is_not_a_repo(self):
        from tests.test_audits import cli, real_like_config
        d = Path(tempfile.mkdtemp())
        rc, _o, _e = cli(["audit-leaks", "--repo", str(Path(tempfile.mkdtemp())), "--real-config", real_like_config(d)])
        self.assertEqual(rc, 2)


class TestThePublicTree(unittest.TestCase):
    def test_the_actual_tree_of_the_kit_has_no_hit_for_readme_style_ids_and_roles(self):
        """A leak scan of what the kit publishes (the real ``git ls-files`` tree), with ids and codes written the way the
        README and the demo write them, finds nothing: the name list must not block the public tree. The fixture
        initials are not in the tree; the ids and initials are built so that this file does not contain them."""
        root = Path(__file__).resolve().parent.parent
        listed = [f for f in subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True,
                                            check=True).stdout.splitlines() if f]
        self.assertGreater(len(listed), 50)
        ids = [f"P{i:02d}" for i in range(1, 13)] + [f"P{i}" for i in (1, 2, 3)] + [f"PC{i:02d}" for i in (1, 7, 12)]
        generic = ["Interviewer", "Participant", "Interviewee", "Caregiver", "Provider", "Researcher", "Participant 3"]
        demo = ["I1", "I2", "I3", "pt", "iv", "SYN-OW01", "SYN-OF03"]
        idx = LeakIndex.build([" ".join(f"w{i}x" for i in range(8))],      # a sentence that is in no file
                              ids + ["I1", "I2", "IN" + "T", "H" + "J", "V" + "W", "p1", "p2"] + generic + demo,
                              stems=(f"P{1:02d}_interview", f"P{12}-notes", "SYN-OW02"))
        res = audit_leaks([str(root / f) for f in listed], idx)
        self.assertEqual([f for f in res.failures if f.startswith("name:")], [])
        self.assertEqual(res.counts["name_hits"], 0)
        self.assertEqual(res.failures, [], res.failures[:5])
        self.assertEqual(res.counts["files_scanned"], len(listed))


if __name__ == "__main__":
    unittest.main()
