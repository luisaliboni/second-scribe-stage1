"""K2: ``.txt`` transcripts, the transcript reader dispatch and the speaker-label candidates Setup offers."""
import contextlib
import glob
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from second_scribe import leakscan, smoke_q
from second_scribe.cli import main as cli_main
from second_scribe.pipeline import KIT_ROOT, RunConfig
from second_scribe.provenance import locate_quote
from second_scribe.transcripts import (TRANSCRIPT_SUFFIXES, label_candidates, parse_paragraphs, read_docx_text,
                                       read_transcript_paras, read_txt_paras)

DEMO_CONFIG = KIT_ROOT / "configs" / "demo.json"
DEMO_CORPUS = KIT_ROOT / "demo" / "corpus"

WHOLE_WORD = [
    "Interviewer: Thank you for joining. Can you tell me how the licence process started for you?",
    "Participant: I went to the city office first and they sent me to the health department.",
    "Interviewer: What happened at the health department?",
    "Participant 3 (00:04:12): They booked an inspection, but nobody told me about the fire permit.",
    "Interviewer   (00:05:01):   And the fire permit, when did you hear of it?",
    "Participant: Only when the inspector came. I wished there had been one checklist.",
]


def _write(d: Path, name: str, data: bytes) -> Path:
    p = d / name
    p.write_bytes(data)
    return p


class TestTxtReader(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def test_suffixes(self):
        self.assertEqual(TRANSCRIPT_SUFFIXES, (".docx", ".txt"))

    def test_txt_whole_word_labels_after_confirmation(self):
        p = _write(self.d, "Q01.txt", "\n".join(WHOLE_WORD).encode("utf-8"))
        paras = read_txt_paras(p)
        self.assertEqual(paras, WHOLE_WORD)
        t = parse_paragraphs(paras, {"participant"}, "Q01", interviewer_labels={"interviewer"})
        self.assertEqual(t.speaker_format, "first_token")
        speakers = [x.speaker for x in t.turns]
        self.assertEqual(speakers, ["interviewer", "participant"] * 3)
        self.assertIn("nobody told me about the fire permit", t.turns[3].text)
        # Before the user confirms labels the kit cannot tell these speakers apart: Setup must ask.
        self.assertEqual(parse_paragraphs(paras, {"pt"}, "Q01", interviewer_labels={"iv"}).speaker_format, "none")

    def test_txt_bom_nbsp_curly(self):
        lines = ["Interviewer: Where did you go first?",
                 "Participant: I said “the city’s office was closed” and went home.",
                 "Interviewer: And then?",
                 "Participant: Then I called the inspector’s line."]
        p = _write(self.d, "Q02.txt", b"\xef\xbb\xbf" + "\r\n".join(lines).encode("utf-8") + b"\r\n")
        paras = read_txt_paras(p)
        self.assertEqual(paras[:4], lines)                       # BOM gone, CRLF split, NBSP and curly kept
        self.assertFalse(paras[0].startswith("﻿"))
        t = parse_paragraphs(paras, {"participant"}, "Q02", interviewer_labels={"interviewer"})
        self.assertEqual(t.speaker_format, "first_token")
        prov = locate_quote("I said \"the city's office was closed\" and went home", t, "n1", "node")
        self.assertTrue(prov.found)
        self.assertEqual(prov.speaker, "participant")
        self.assertEqual(prov.turn_idx, 1)

    def test_txt_line_endings(self):
        p = _write(self.d, "Q03.txt", b"a one\rb two\r\nc three\nd four")
        self.assertEqual(read_txt_paras(p), ["a one", "b two", "c three", "d four"])
        self.assertEqual(read_txt_paras(_write(self.d, "empty.txt", b"")), [])

    def test_txt_not_utf8_refused(self):
        p = _write(self.d, "Q04.txt", "Participant: café SECRET".encode("latin-1"))
        with self.assertRaises(ValueError) as cm:
            read_txt_paras(p)
        self.assertEqual(str(cm.exception), "transcript is not UTF-8 text")
        p16 = _write(self.d, "Q05.txt", "Participant: hello".encode("utf-16"))
        with self.assertRaises(ValueError):
            read_txt_paras(p16)

    def test_txt_oversize_refused(self):
        import second_scribe.transcripts as tr
        p = _write(self.d, "Q06.txt", b"Participant: " + b"x" * 64)
        old = tr.MAX_TRANSCRIPT_BYTES
        tr.MAX_TRANSCRIPT_BYTES = 16
        try:
            with self.assertRaises(ValueError) as cm:
                read_txt_paras(p)
        finally:
            tr.MAX_TRANSCRIPT_BYTES = old
        self.assertNotIn("xxxx", str(cm.exception))

    def test_unicode_file_name_reads(self):
        p = _write(self.d, "entrevista_José_ü_中.txt", "\n".join(WHOLE_WORD).encode("utf-8"))
        self.assertEqual(read_transcript_paras(p), WHOLE_WORD)
        self.assertEqual(read_transcript_paras(str(p)), WHOLE_WORD)

    def test_dispatch(self):
        docx = sorted(glob.glob(str(DEMO_CORPUS / "transcripts" / "*.docx")))[0]
        self.assertEqual(read_transcript_paras(docx), read_docx_text(docx))
        up = _write(self.d, "Q07.TXT", b"Participant: hi")
        self.assertEqual(read_transcript_paras(up), ["Participant: hi"])
        for name in ("Q08.doc", "Q08.rtf", "Q08.pdf", "Q08"):
            with self.assertRaises(ValueError):
                read_transcript_paras(_write(self.d, name, b"x"))


class TestLabelCandidates(unittest.TestCase):
    def test_label_candidates_orders_by_count(self):
        paras = ["[PT] one", "[PT] two", "[PT] three", "[IV] four", "[IV] five",
                 "MR: six", "", "  ", "[...] seven", "[00:01] eight"]
        self.assertEqual(label_candidates(paras), [("pt", 3), ("iv", 2), ("MR", 1)])
        self.assertEqual(label_candidates(paras, top=2), [("pt", 3), ("iv", 2)])
        self.assertEqual(label_candidates([]), [])

    def test_label_candidates_offers_word_before_timestamp(self):
        paras = ["Participant 3 (00:04:12): first", "Interviewer (00:04:30): q",
                 "Participant 3 (00:05:12): second", "Participant 3 (00:06:00): third",
                 "Interviewer (00:06:30): q2"]
        cands = label_candidates(paras)
        self.assertEqual(cands[0], ("Participant", 3))
        self.assertEqual(cands[1], ("Interviewer", 2))

    def test_label_candidates_ties_keep_first_seen(self):
        self.assertEqual(label_candidates(["B: x", "A: y", "A: z", "B: w"]), [("B", 2), ("A", 2)])


def _quiet(argv):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return cli_main(argv)


class TestTxtCorpus(unittest.TestCase):
    """The demo corpus written as .txt runs to the same aggregates as the .docx original."""

    @classmethod
    def setUpClass(cls):
        root = Path(tempfile.mkdtemp())
        corpus = root / "corpus"
        shutil.copytree(DEMO_CORPUS, corpus)          # keeps meta.json: the copy stays synthetic
        for docx in sorted((corpus / "transcripts").glob("*.docx")):
            docx.with_suffix(".txt").write_text("\n".join(read_docx_text(docx)), encoding="utf-8")
        d = json.loads(DEMO_CONFIG.read_text(encoding="utf-8"))
        d["corpus"]["maps_glob"] = str(corpus / "maps" / "*.vsdx")
        d["corpus"]["transcripts_glob"] = str(corpus / "transcripts" / "*.txt")
        d["corpus"]["synthetic_dir"] = str(corpus)
        d["out_dir"] = str(root / "out-config")
        cls.txt_config = root / "config.txt.json"
        cls.txt_config.write_text(json.dumps(d, indent=2), encoding="utf-8")
        cls.root = root

    def test_txt_corpus_gives_same_aggregates(self):
        out_docx, out_txt = self.root / "out-docx", self.root / "out-txt"
        self.assertEqual(_quiet(["demo", "--out", str(out_docx), "--client", "oracle"]), 0)
        self.assertEqual(_quiet(["demo", "--out", str(out_txt), "--client", "oracle",
                                 "--config", str(self.txt_config)]), 0)
        a = json.loads((out_docx / "aggregates.json").read_text(encoding="utf-8"))
        b = json.loads((out_txt / "aggregates.json").read_text(encoding="utf-8"))
        self.assertEqual(a["n_analysed"], 12)
        self.assertEqual(b["n_analysed"], 12)
        self.assertEqual(b["buckets"]["primary"], a["buckets"]["primary"])
        self.assertEqual(b["absences"]["primary"], a["absences"]["primary"])
        self.assertEqual(b["mapping_talk"], a["mapping_talk"])
        self.assertNotEqual(b["config_sha256"], a["config_sha256"])    # the input files differ, so the hash does

    def test_txt_corpus_smoke_q_and_leakscan_read_txt(self):
        cfg_docx = RunConfig.load(DEMO_CONFIG, out_dir=str(Path(tempfile.mkdtemp()) / "o"))
        cfg_txt = RunConfig.load(self.txt_config, out_dir=str(Path(tempfile.mkdtemp()) / "o"))
        self.assertEqual(smoke_q.padded_paragraphs(cfg_txt, 400), smoke_q.padded_paragraphs(cfg_docx, 400))
        li_docx = leakscan.LeakIndex.from_config(cfg_docx)
        li_txt = leakscan.LeakIndex.from_config(cfg_txt)
        self.assertEqual(li_txt.problems, li_docx.problems)
        self.assertNotIn("transcript_unreadable", li_txt.problems)
        self.assertEqual(li_txt._grams, li_docx._grams)


if __name__ == "__main__":
    unittest.main()
