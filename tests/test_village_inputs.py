"""Village wave 1, run path part a: bad input files are found before any stage and refused once (A1), maps with no
boxes are refused (A2), duplicate stems are refused (A5), ``.mapgraph.json`` maps pair by name (A10), the report's
"analysed" is the profile's (A3), paired files missing from ``roles`` are counted (E13), extra speaker labels are
warned about (E20), and a refused run leaves no folder; a real corpus in a sync folder is refused (E22).
Synthetic corpus copies, oracle clients, temp dirs; no model calls."""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from second_scribe import cli, pipeline, transcripts
from second_scribe.cli import DEMO_CONFIG, main
from second_scribe.transcripts import TranscriptRefused, read_docx_text, read_txt_paras
from tests.test_real_run_validity import fake_build_clients

DEMO_CORPUS = Path(DEMO_CONFIG).parent.parent / "demo" / "corpus"


def make_corpus(root: Path, kind: str = "synthetic", maps_glob="corpus/maps/*.vsdx",
                transcripts_glob="corpus/transcripts/*.docx", **corpus) -> Path:
    """A copy of the demo corpus under ``root/corpus`` and a config for it (``root/config.json``)."""
    if not (root / "corpus").exists():
        shutil.copytree(DEMO_CORPUS, root / "corpus")
    d = json.loads(Path(DEMO_CONFIG).read_text())
    d.update(out_dir=str(root / "run"), base_dir=str(root))
    d["corpus"].update(kind=kind, maps_glob=maps_glob, transcripts_glob=transcripts_glob, synthetic_dir="corpus",
                       **corpus)
    if kind == "real":
        m = root / "participant_id_map.json"
        m.write_text(json.dumps({"_meta": {}, "map": {"QZ": {"canonical_id": "KC99"}}}))
        d["corpus"]["participant_id_map"] = str(m)
    p = root / "config.json"
    p.write_text(json.dumps(d))
    return p


def cli_run(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(list(argv))
    return rc, out.getvalue(), err.getvalue()


def zip_with(path: Path, members: dict) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


class Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)


# ---------------------------------------------------------------------------------------------------------------- A1

class TestDocxAndTxtRefusals(Tmp):
    def test_an_empty_docx(self):
        f = self.root / "a.docx"
        f.write_bytes(b"")
        with self.assertRaisesRegex(TranscriptRefused, r"empty \(0 bytes\)"):
            read_docx_text(f)

    def test_a_pdf_or_png_named_docx(self):
        for head, kind in ((b"%PDF-1.7\n", "a PDF"), (b"\x89PNG\r\n\x1a\n", "a PNG image")):
            f = self.root / "a.docx"
            f.write_bytes(head + b"x" * 50)
            with self.subTest(kind=kind), self.assertRaisesRegex(TranscriptRefused, f"not a readable .docx transcript \\({kind}\\)"):
                read_docx_text(f)

    def test_a_truncated_docx(self):
        f = self.root / "a.docx"
        f.write_bytes((DEMO_CORPUS / "transcripts" / "SYN-OW01.docx").read_bytes()[:700])
        with self.assertRaisesRegex(TranscriptRefused, "not a readable .docx"):
            read_docx_text(f)

    def test_a_zip_without_document_xml(self):
        f = zip_with(self.root / "a.docx", {"other.xml": "<a/>"})
        with self.assertRaisesRegex(TranscriptRefused, "no word/document.xml"):
            read_docx_text(f)

    def test_document_xml_over_the_cap(self):
        f = zip_with(self.root / "a.docx", {"word/document.xml": "<a>" + "x" * 5000 + "</a>"})
        with mock.patch.object(transcripts, "MAX_DOCX_XML_BYTES", 1000), \
                self.assertRaisesRegex(TranscriptRefused, "too large"):
            read_docx_text(f)

    def test_document_xml_not_well_formed(self):
        f = zip_with(self.root / "a.docx", {"word/document.xml": "<a><b></a>"})
        with self.assertRaisesRegex(TranscriptRefused, "not well-formed"):
            read_docx_text(f)

    def test_latin1_and_utf16_txt(self):
        f = self.root / "a.txt"
        f.write_bytes("[PT] café".encode("latin-1"))
        with self.assertRaisesRegex(TranscriptRefused, "not UTF-8"):
            read_txt_paras(f)
        f.write_bytes("[PT] hello".encode("utf-16"))
        with self.assertRaisesRegex(TranscriptRefused, "UTF-16"):
            read_txt_paras(f)

    def test_still_a_value_error(self):
        self.assertTrue(issubclass(TranscriptRefused, ValueError))


class TestPreflight(Tmp):
    def break_files(self):
        c = self.root / "corpus"
        (c / "transcripts" / "SYN-OW01.docx").write_bytes(b"")
        (c / "transcripts" / "SYN-OW02.docx").write_bytes(b"%PDF-1.4 not a docx")
        (c / "maps" / "SYN-OF01.vsdx").write_bytes(b"")
        (c / "maps" / "SYN-OF02.vsdx").write_bytes(b"PK\x03\x04junk")
        zip_with(c / "transcripts" / "SYN-OW03.docx", {"word/document.xml": "<a><b></a>"})

    def test_the_triage_repro_is_one_refusal_with_exit_2(self):
        cfg = make_corpus(self.root)
        (self.root / "corpus" / "transcripts" / "SYN-OW01.docx").write_bytes(b"")
        rc, _out, err = cli_run("run", "--config", str(cfg), "--client", "oracle")
        self.assertEqual(rc, 2, err)
        self.assertEqual(err.strip(), "refused: 1 input file cannot be read: SYN-OW01.docx: the file is empty (0 bytes)")
        self.assertNotIn("Traceback", err)

    def test_every_bad_file_is_counted_three_are_shown(self):
        cfg = make_corpus(self.root)
        self.break_files()
        rc, _out, err = cli_run("run", "--config", str(cfg), "--client", "oracle")
        self.assertEqual(rc, 2, err)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertTrue(err.startswith("refused: 5 input files cannot be read: "), err)
        self.assertIn("SYN-OF01.vsdx: the map file is empty (0 bytes)", err)
        self.assertIn("SYN-OF02.vsdx: not a readable Visio (.vsdx) file", err)
        self.assertIn("SYN-OW01.docx: the file is empty (0 bytes)", err)
        self.assertTrue(err.strip().endswith("; and 2 more"), err)
        self.assertFalse((self.root / "run").exists())        # nothing written

    def test_the_check_lists_every_problem(self):
        cfg = pipeline.RunConfig.load(make_corpus(self.root))
        self.break_files()
        with self.assertRaises(pipeline.InputsRefused) as cm:
            pipeline.check_inputs(cfg)
        probs = cm.exception.problems
        self.assertEqual(len(probs), 5)
        self.assertIn("SYN-OW02.docx: not a readable .docx transcript (a PDF)", probs)
        self.assertIn("SYN-OW03.docx: word/document.xml is not well-formed XML (a damaged file)", probs)

    def test_a_real_corpus_is_named_by_position(self):
        cfg = make_corpus(self.root, kind="real")
        self.break_files()
        rc, _out, err = cli_run("run", "--config", str(cfg), "--client", "oracle")
        self.assertEqual(rc, 2, err)
        self.assertIn("map #1: the map file is empty (0 bytes)", err)
        self.assertIn("map #2: not a readable Visio (.vsdx) file", err)
        self.assertIn("transcript #6: the file is empty (0 bytes)", err)
        self.assertNotIn("SYN-", err)
        self.assertNotIn(str(self.root), err)

    def test_the_pipeline_checks_before_any_stage_when_called_directly(self):
        cfg = pipeline.RunConfig.load(make_corpus(self.root))
        (self.root / "corpus" / "maps" / "SYN-OW04.vsdx").write_bytes(b"%PDF-1.4")
        log = mock.MagicMock()
        with self.assertRaises(pipeline.InputsRefused):
            pipeline.run(cfg, ["mapping_talk_confirm"], {}, log=log)
        log.append.assert_not_called()

    def test_the_demo_passes_the_check_with_no_warning(self):
        check = pipeline.check_inputs(pipeline.RunConfig.load(DEMO_CONFIG))
        self.assertEqual(len(check.pairing.participants), 12)
        self.assertEqual(check.warnings, [])


class TestSafetyNet(Tmp):
    """A read error that gets past the preflight is one ``refused:`` line with exit 2, never a traceback."""

    def raising(self, exc, kind="synthetic"):
        cfg = make_corpus(self.root, kind=kind)
        with mock.patch("second_scribe.cli.cmd_run", side_effect=exc):
            return cli_run("run", "--config", str(cfg), "--client", "oracle")

    def test_reader_errors(self):
        from second_scribe.maps.mermaid import MermaidUnsupported
        from second_scribe.maps.readers import MapRefused
        from xml.etree.ElementTree import ParseError
        for exc in (MapRefused("unreadable", "the CSV file is empty"), MermaidUnsupported("unreadable", "subgraph"),
                    zipfile.BadZipFile("File is not a zip file"), ParseError("not well-formed"),
                    TranscriptRefused("transcript is not UTF-8 text")):
            with self.subTest(exc=type(exc).__name__):
                rc, _out, err = self.raising(exc)
                self.assertEqual(rc, 2)
                self.assertTrue(err.startswith("refused: "), err)
                self.assertEqual(len(err.strip().splitlines()), 1)

    def test_an_os_error_names_its_path_only_for_a_synthetic_corpus(self):
        exc = FileNotFoundError(2, "No such file or directory", "/secret/case-x.docx")
        rc, _out, err = self.raising(exc)
        self.assertEqual((rc, err.strip()), (2, "refused: /secret/case-x.docx: No such file or directory"))
        rc, _out, err = self.raising(FileNotFoundError(2, "No such file or directory", "/secret/case-x.docx"), "real")
        self.assertEqual((rc, err.strip()), (2, "refused: No such file or directory"))

    def test_an_edited_pinned_prompt(self):
        from second_scribe import ladder
        import inspect
        self.assertIn('"' + cli.VALIDATE_RUNGS_PREFIX + ' "', inspect.getsource(ladder).replace("\n", ""))
        exc = RuntimeError(cli.VALIDATE_RUNGS_PREFIX + " reconstruct.p0.v1.txt: differs from the pinned sha256 in "
                                                       "ladder.PINNED_SHA256")
        rc, _out, err = self.raising(exc)
        self.assertEqual(rc, 2)
        self.assertTrue(err.startswith("refused: reconstruct prompts are pinned (ladder.PINNED_SHA256); an edited "
                                       "prompt file does not match"), err)


# ---------------------------------------------------------------------------------------------------------------- A2

class TestNoBoxes(Tmp):
    """Every reader refuses a map with zero boxes, as Mermaid already did."""

    def refused(self, path, rule=None):
        from second_scribe.maps.readers import MapRefused, read_map
        with self.assertRaises(MapRefused) as cm:
            read_map(path, "x", None, rule)
        return cm.exception

    def test_the_triage_repro_a_vsdx_without_pages(self):
        from second_scribe.maps.readers import MapRefused
        from second_scribe.maps.vsdx import extract_vsdx
        src = zipfile.ZipFile(DEMO_CORPUS / "maps" / "SYN-OW01.vsdx")
        f = self.root / "x.vsdx"
        with zipfile.ZipFile(f, "w") as z:
            for n in src.namelist():
                if not n.startswith("visio/pages/page"):
                    z.writestr(n, src.read(n))
        with self.assertRaises(MapRefused) as cm:
            extract_vsdx(f, "x")
        self.assertEqual((cm.exception.code, cm.exception.detail), ("unreadable", "the map has no boxes"))
        self.assertEqual(self.refused(f).detail, "the map has no boxes")
        from second_scribe.maps.readers import MissingRule
        self.assertEqual(self.refused(f, MissingRule("red_fill")).detail, "the map has no boxes")   # opt-in rule

    def test_a_header_only_csv(self):
        f = self.root / "x.csv"
        f.write_text("kind,id,label,phase,status,missing_kind,from,to\n")
        self.assertEqual(self.refused(f).detail, "the map has no boxes")

    def test_an_xlsx_with_an_empty_boxes_sheet(self):
        from tests.test_reader_table import _write_zip, _xlsx_parts
        hdr = [f'<c r="{chr(65 + i)}1" t="inlineStr"><is><t>{h}</t></is></c>'
               for i, h in enumerate(("kind", "id", "label", "phase", "status", "missing_kind"))]
        d, f = _write_zip(_xlsx_parts([hdr]))
        self.addCleanup(d.cleanup)
        self.assertEqual(self.refused(f).detail, "the map has no boxes")

    def test_drawio_excalidraw_and_mapgraph_with_no_boxes(self):
        from tests.test_reader_drawio import model, mxfile
        (self.root / "x.drawio").write_text(mxfile(model()))
        (self.root / "x.excalidraw").write_text(json.dumps({"type": "excalidraw", "version": 2, "elements": []}))
        from second_scribe.schemas import MapGraph
        (self.root / "x.mapgraph.json").write_text(MapGraph(map_id="x", origin="endorsed").to_json())
        for name in ("x.drawio", "x.excalidraw", "x.mapgraph.json"):
            with self.subTest(name=name):
                self.assertEqual(self.refused(self.root / name).code, "unreadable")

    def test_the_apps_mode_c_reference_is_empty_on_purpose(self):
        from second_scribe import reference
        from second_scribe.maps.readers import read_map
        f = self.root / "x.mapgraph.json"
        f.write_text(reference.empty_reference("x").to_json())
        self.assertEqual(read_map(f, "x").nodes, [])

    def test_the_preflight_refuses_a_map_with_no_boxes(self):
        cfg = make_corpus(self.root, maps_glob="corpus/maps/*")
        (self.root / "corpus" / "maps" / "SYN-OW05.vsdx").unlink()
        (self.root / "corpus" / "maps" / "SYN-OW05.csv").write_text("kind,id,label,phase,status,missing_kind,from,to\n")
        rc, _out, err = cli_run("run", "--config", str(cfg), "--client", "oracle")
        self.assertEqual(rc, 2, err)
        self.assertEqual(err.strip(), "refused: 1 input file cannot be read: SYN-OW05.csv: the map has no boxes")


# ---------------------------------------------------------------------------------------------------------------- A5

class TestDuplicateStems(Tmp):
    def add_twins(self):
        c = self.root / "corpus"
        (c / "transcripts" / "SYN-OW03.txt").write_text("[IV] hello\n[PT] hi\n")
        shutil.copyfile(c / "maps" / "SYN-OW05.vsdx", c / "maps" / "SYN-OW05.drawio")

    def test_the_triage_repro_is_refused(self):
        cfg = make_corpus(self.root, transcripts_glob="corpus/transcripts/*")
        self.add_twins()
        rc, _out, err = cli_run("run", "--config", str(cfg), "--client", "oracle")
        self.assertEqual(rc, 2, err)
        self.assertEqual(err.strip(), "refused: 1 participant id matches more than one file in transcripts_glob: "
                                      "SYN-OW03: SYN-OW03.docx and SYN-OW03.txt. Keep one file per participant, or "
                                      "list each participant's files in corpus.pairs")
        self.assertFalse((self.root / "run").exists())

    def test_maps_too_and_a_real_corpus_by_position(self):
        cfg = make_corpus(self.root, kind="real", maps_glob="corpus/maps/*")
        self.add_twins()
        rc, _out, err = cli_run("run", "--config", str(cfg), "--client", "oracle")
        self.assertEqual(rc, 2, err)
        self.assertIn("in maps_glob: map #10 and map #11", err)      # SYN-OW05.drawio, SYN-OW05.vsdx (sorted)
        self.assertNotIn("SYN-", err)

    def test_explicit_pairs_settle_it(self):
        c = self.root / "corpus"
        pairs = {pid: [f"corpus/maps/{pid}.vsdx", f"corpus/transcripts/{pid}.docx"]
                 for pid in json.loads(Path(DEMO_CONFIG).read_text())["corpus"]["roles"]}
        cfg = make_corpus(self.root, pairs=pairs)
        self.add_twins()
        self.assertEqual(len(pipeline.check_inputs(pipeline.RunConfig.load(cfg)).pairing.participants), 12)
        self.assertTrue((c / "transcripts" / "SYN-OW03.txt").exists())


# --------------------------------------------------------------------------------------------------------------- A10

class TestMapgraphStems(Tmp):
    def mapgraph_corpus(self, **kw):
        from second_scribe.maps.readers import map_stem, read_map
        cfg = make_corpus(self.root, maps_glob="corpus/maps/*.mapgraph.json", **kw)
        maps = self.root / "corpus" / "maps"
        for f in sorted(maps.glob("*.vsdx")):
            g = read_map(f, map_stem(f))
            (maps / f"{f.stem}.mapgraph.json").write_text(g.to_json())
        return cfg

    def test_map_stem(self):
        from second_scribe.maps.readers import map_stem
        self.assertEqual([map_stem(n) for n in ("SYN-PAIR01.mapgraph.json", "SYN-PAIR01.vsdx",
                                              "SYN-PAIR01.MAPGRAPH.JSON", "a.b.csv", "SYN-PAIR01")],
                         ["SYN-PAIR01", "SYN-PAIR01", "SYN-PAIR01", "a.b", "SYN-PAIR01"])

    def test_the_triage_repro_pairs_by_name(self):
        cfg = self.mapgraph_corpus()
        rc, _out, err = cli_run("run", "--config", str(cfg), "--stages", "Q", "--client", "oracle")
        self.assertEqual(rc, 0, err)
        start = json.loads((self.root / "run" / "logs" / "run.jsonl").read_text().splitlines()[0])
        self.assertEqual(start["payload"]["n_participants"], 12)

    def test_a_missing_map_names_the_pattern_and_the_nearest_names(self):
        cfg = make_corpus(self.root)
        maps = self.root / "corpus" / "maps"
        (maps / "SYN-OF01.vsdx").rename(maps / "SYN_OF01.vsdx")
        rc, _out, err = cli_run("run", "--config", str(cfg), "--client", "oracle")
        self.assertEqual(rc, 2, err)
        self.assertIn("participant SYN-OF01 has no map file: no file named SYN-OF01.<map suffix: .vsdx .drawio "
                      ".excalidraw .mmd .csv .xlsx .mapgraph.json> matched maps_glob (corpus/maps/*.vsdx); nearest "
                      "names: SYN_OF01.vsdx", err)
        self.assertTrue(err.startswith("refused: the corpus is incomplete: "), err)
        self.assertFalse((self.root / "run").exists())

    def test_a_real_corpus_names_the_position_in_roles(self):
        cfg = make_corpus(self.root, kind="real")
        (self.root / "corpus" / "transcripts" / "SYN-OW07.docx").unlink()
        rc, _out, err = cli_run("run", "--config", str(cfg), "--client", "oracle")
        self.assertEqual(rc, 2, err)
        self.assertIn("participant #12 of corpus.roles has no transcript file that transcripts_glob matched", err)
        self.assertNotIn("SYN-", err)


# ---------------------------------------------------------------------------------------------------------------- A3

class TestAnalysedIsTheProfiles(Tmp):
    def agg(self):
        return json.loads((self.root / "run" / "aggregates" / "aggregates.json").read_text())

    def report_line(self):
        return (self.root / "run" / "aggregates" / "report.md").read_text().splitlines()[4]

    def test_the_triage_repro_no_speaker_labels(self):
        import re
        from second_scribe import profile_base
        from second_scribe.synth.docx_writer import write_docx
        cfg = make_corpus(self.root)
        doc = self.root / "corpus" / "transcripts" / "SYN-OW01.docx"
        write_docx(doc, [re.sub(r"^\[[A-Z]+\]\s*|^[A-Z]+ \d+:\d\d\s*", "", x) for x in read_docx_text(doc)])
        rc, _out, err = cli_run("run", "--config", str(cfg), "--stages", "all", "--client", "oracle")
        self.assertEqual(rc, 0, err)
        agg = self.agg()
        self.assertEqual((agg["n_participants"], agg["n_analysed"]), (12, 11))
        self.assertEqual(agg["exclusions"], {"no_speaker_labels": 1})
        self.assertEqual(agg["not_analysed"], {"no_speaker_labels": 1})
        self.assertNotIn("item_exclusions", agg)
        self.assertEqual(self.report_line(), "- run: `demo`, corpus: synthetic, participants 12, analysed 11, "
                                             "excluded 1 (no speaker labels: 1)")
        self.assertIn("stage Q: participants 12, analysed 11, excluded 1 (no speaker labels: 1)", err)
        ctx = profile_base.open_context(cfg)          # the profile's own count: the same 11 of 12
        self.assertEqual((len(ctx.analysed), len(ctx.run.people)), (agg["n_analysed"], agg["n_participants"]))

    def test_an_empty_transcript_has_its_own_reason(self):
        pairs = {pid: [f"corpus/maps/{pid}.vsdx", f"corpus/transcripts/{pid}.docx"]
                 for pid in json.loads(Path(DEMO_CONFIG).read_text())["corpus"]["roles"]}
        pairs["SYN-OF02"][1] = "corpus/extra/SYN-OF02.txt"
        cfg = make_corpus(self.root, pairs=pairs)
        (self.root / "corpus" / "extra").mkdir()
        (self.root / "corpus" / "extra" / "SYN-OF02.txt").write_bytes(b"")
        rc, _out, err = cli_run("run", "--config", str(cfg), "--stages", "all", "--client", "oracle")
        self.assertEqual(rc, 0, err)
        agg = self.agg()
        self.assertEqual(agg["n_analysed"], 11)
        self.assertEqual(agg["exclusions"], {"empty_transcript": 1})
        self.assertEqual(agg["not_analysed"], {"empty_transcript": 1})
        self.assertIn("excluded 1 (empty transcript: 1)", self.report_line())

    def test_the_run_line(self):
        from second_scribe.report import _run_line
        base = {"run_id": "r", "corpus_kind": "synthetic", "n_participants": 12, "n_analysed": 12}
        self.assertEqual(_run_line(base), "- run: `r`, corpus: synthetic, participants: 12 (analysed 12)")
        self.assertEqual(_run_line({**base, "item_exclusions": {"too_long_excluded": {"recon": 2}}}),
                         "- run: `r`, corpus: synthetic, participants 12, analysed 12, excluded 0; items too long "
                         "excluded: recon 2")
        self.assertEqual(_run_line({**base, "n_analysed": 9, "not_analysed": {"recon_failed": 1,
                                                                             "in_transcript_too_long": 2}}),
                         "- run: `r`, corpus: synthetic, participants 12, analysed 9, excluded 3 (in transcript too "
                         "long: 2, recon failed: 1)")


# --------------------------------------------------------------------------------------------------------------- E13

class TestNotInRoles(Tmp):
    def add_extra_pair(self):
        c = self.root / "corpus"
        shutil.copyfile(c / "maps" / "SYN-OW01.vsdx", c / "maps" / "SYN-ZZ01.vsdx")
        shutil.copyfile(c / "transcripts" / "SYN-OW01.docx", c / "transcripts" / "SYN-ZZ01.docx")

    def test_a_paired_file_outside_roles_is_warned_and_counted(self):
        cfg = make_corpus(self.root)
        self.add_extra_pair()
        rc, _out, err = cli_run("run", "--config", str(cfg), "--stages", "all", "--client", "oracle")
        self.assertEqual(rc, 0, err)
        self.assertIn("warning: 1 paired file has no entry in corpus.roles and is not analysed: SYN-ZZ01 "
                      "(counted as not_in_roles)", err)
        agg = json.loads((self.root / "run" / "aggregates" / "aggregates.json").read_text())
        self.assertEqual(agg["exclusions"], {"not_in_roles": 1})
        self.assertEqual((agg["n_participants"], agg["n_analysed"]), (12, 12))
        self.assertNotIn("not_analysed", agg)

    def test_a_real_corpus_names_positions(self):
        cfg = make_corpus(self.root, kind="real")
        self.add_extra_pair()
        check = pipeline.check_inputs(pipeline.RunConfig.load(cfg))
        self.assertEqual(check.warnings, ["1 paired file has no entry in corpus.roles and is not analysed: map #13 and "
                                          "transcript #13 (counted as not_in_roles)"])


# --------------------------------------------------------------------------------------------------------------- E20

class TestLabelsInNeitherSet(Tmp):
    def add_a_third_speaker(self):
        from second_scribe.synth.docx_writer import write_docx
        doc = self.root / "corpus" / "transcripts" / "SYN-OF02.docx"
        paras = read_docx_text(doc)
        write_docx(doc, paras[:4] + ["[MOD] One more question from me.", "[MOD] And a second one."] + paras[4:])

    def test_a_synthetic_corpus_names_the_file_and_the_label(self):
        cfg = make_corpus(self.root)
        self.add_a_third_speaker()
        check = pipeline.check_inputs(pipeline.RunConfig.load(cfg))
        self.assertEqual(check.warnings, [
            "1 transcript has turns opened by a speaker label in neither participant_labels nor interviewer_labels; "
            "they are read as interviewer turns (two-party interviews are assumed): SYN-OF02.docx: 2 turns (mod)"])

    def test_a_real_corpus_counts_labels_by_position(self):
        cfg = make_corpus(self.root, kind="real")
        self.add_a_third_speaker()
        check = pipeline.check_inputs(pipeline.RunConfig.load(cfg))
        self.assertEqual(len(check.warnings), 1)
        self.assertTrue(check.warnings[0].endswith(": transcript #2: 2 turns (1 label)"), check.warnings[0])
        self.assertNotIn("mod", check.warnings[0])

    def test_no_warning_without_interviewer_labels(self):
        cfg = make_corpus(self.root, interviewer_labels={})
        self.add_a_third_speaker()
        self.assertEqual(pipeline.check_inputs(pipeline.RunConfig.load(cfg)).warnings, [])

    def test_the_cli_prints_it_and_the_run_goes_on(self):
        cfg = make_corpus(self.root)
        self.add_a_third_speaker()
        rc, _out, err = cli_run("run", "--config", str(cfg), "--stages", "Q", "--client", "oracle")
        self.assertEqual(rc, 0, err)
        self.assertIn("warning: 1 transcript has turns opened by a speaker label in neither", err)
        recs = [json.loads(x)["record_type"] for x in (self.root / "run" / "logs" / "run.jsonl").read_text().splitlines()]
        self.assertNotIn("speaker_labels", " ".join(recs))         # Ruling R4: no new log record


# --------------------------------------------------------------------------------------------------------------- E22

class TestNoFolderBeforeTheChecks(Tmp):
    def test_a_run_refused_inside_the_pipeline_leaves_no_folder(self):
        cfg = make_corpus(self.root, kind="real")
        with mock.patch.object(pipeline, "prompt_manifest_problem", return_value="x.txt: edited"), \
                mock.patch.dict("os.environ", {"SS_RECON_MODEL": "m1", "SS_JUDGE_MODEL": "m2"}), \
                mock.patch("second_scribe.cli.build_clients", fake_clients):
            rc, _out, err = cli_run("run", "--config", str(cfg), "--client", "oracle")
        self.assertEqual(rc, 2, err)
        self.assertTrue(err.startswith("refused: refusing to run: the prompt files differ"), err)
        self.assertFalse((self.root / "run").exists())

    def test_a_refused_demo_leaves_no_folder(self):
        cfg = make_corpus(self.root)
        (self.root / "corpus" / "maps" / "SYN-OW02.vsdx").write_bytes(b"")
        out = self.root / "demo-out"
        rc, _out, err = cli_run("demo", "--out", str(out), "--config", str(cfg), "--client", "oracle")
        self.assertEqual(rc, 2, err)
        self.assertFalse(out.exists())


def fake_clients(kind, cfg, fixtures=None, roles=None):
    """The oracle, with model ids and params as a live client has them (a real corpus's guard needs them)."""
    return fake_build_clients(kind, cfg, fixtures, roles)


class TestSyncFolders(Tmp):
    def synced(self, where: str):
        """A real corpus with the out_dir (``out``) or the corpus (``corpus``) inside a OneDrive folder."""
        base = self.root / "OneDrive - Example" if where == "corpus" else self.root
        base.mkdir(exist_ok=True)
        cfg = make_corpus(base, kind="real")
        if where == "out":
            d = json.loads(cfg.read_text())
            d["out_dir"] = str(self.root / "Dropbox" / "run")
            cfg.write_text(json.dumps(d))
        return cfg

    def run_cli(self, cfg, *extra):
        with mock.patch.dict("os.environ", {"SS_RECON_MODEL": "m1", "SS_JUDGE_MODEL": "m2"}), \
                mock.patch("second_scribe.cli.build_clients", fake_clients):
            return cli_run("run", "--config", str(cfg), "--stages", "Q", "--client", "oracle", *extra)

    def test_out_dir_in_a_sync_folder_is_refused(self):
        rc, _out, err = self.run_cli(self.synced("out"))
        self.assertEqual(rc, 2, err)
        self.assertEqual(err.strip(), "refused: a real corpus must stay out of sync folders: the output folder (out_dir) "
                                      "is in a Dropbox folder. A sync client copies every file to its provider's "
                                      "servers. Move them, or pass --allow-synced-folder to go on anyway")
        self.assertFalse((self.root / "Dropbox").exists())

    def test_the_corpus_in_a_sync_folder_is_refused_and_named_by_kind_only(self):
        rc, _out, err = self.run_cli(self.synced("corpus"))
        self.assertEqual(rc, 2, err)
        self.assertIn("a map's folder is in a OneDrive folder; a transcript's folder is in a OneDrive folder", err)
        self.assertNotIn("Example", err)

    def test_the_flag_lets_it_go_on_with_a_warning(self):
        rc, _out, err = self.run_cli(self.synced("out"), "--allow-synced-folder")
        self.assertEqual(rc, 0, err)
        self.assertIn("warning: --allow-synced-folder: the output folder (out_dir) is in a Dropbox folder", err)

    def test_a_synthetic_corpus_is_not_checked(self):
        cfg = make_corpus(self.root)
        d = json.loads(cfg.read_text())
        d["out_dir"] = str(self.root / "Dropbox" / "run")
        cfg.write_text(json.dumps(d))
        rc, _out, err = cli_run("run", "--config", str(cfg), "--stages", "Q", "--client", "oracle")
        self.assertEqual(rc, 0, err)


if __name__ == "__main__":
    unittest.main()
