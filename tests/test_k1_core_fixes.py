"""Submission 2.4, step K1: small defects found by the village wave 3, each fixed without changing any result.

U04 verify refuses an empty --expect-manifest-hash; U25 an empty --out is refused; U24 a bad config / fixtures file,
an unknown stage and --client http without its environment variables are refusals, not tracebacks; U23 a UTF-8 BOM
in a JSON config / id map is read; U03 a deeply nested .docx is refused fast; U06 a label spelled in another case
warns; U05 byte-identical transcripts warn (console and report, only then). Offline, temp dirs, no model call."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from second_scribe import cli, pipeline, transcripts
from second_scribe.cli import DEMO_CONFIG, main
from second_scribe.transcripts import TranscriptRefused, parse_paragraphs, read_docx_text
from tests.test_village_inputs import DEMO_CORPUS, make_corpus


def cli_run(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(list(argv))
    return rc, out.getvalue(), err.getvalue()


class Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)


class TestU04EmptyManifestHash(Tmp):
    def test_empty_and_blank_values_are_refused_and_never_pass(self):
        log = self.root / "run.jsonl"
        for value in ("", "   "):
            with self.subTest(value=value), mock.patch.object(cli.audit_verify, "main", return_value=0) as m:
                rc, out, err = cli_run("verify", str(log), "--expect-manifest-hash", value)
                self.assertEqual(rc, 2)
                self.assertTrue(err.startswith("refused: --expect-manifest-hash is empty"), err)
                self.assertNotIn("PASS", out + err)
                m.assert_not_called()                     # nothing was checked, so nothing could pass

    def test_a_non_empty_value_is_passed_on_unchanged(self):
        with mock.patch.object(cli.audit_verify, "main", return_value=0) as m, \
                mock.patch.object(cli, "_validity", return_value=0):
            rc, _out, _err = cli_run("verify", "x.jsonl", "--expect-manifest-hash", "ab12")
        self.assertEqual(rc, 0)
        m.assert_called_once_with(["x.jsonl", "--manifest-hash", "ab12"])

    def test_without_the_option_nothing_changes(self):
        with mock.patch.object(cli.audit_verify, "main", return_value=0) as m, \
                mock.patch.object(cli, "_validity", return_value=0):
            self.assertEqual(cli_run("verify", "x.jsonl")[0], 0)
        m.assert_called_once_with(["x.jsonl"])


class TestU25EmptyOut(Tmp):
    def test_an_empty_or_blank_out_is_refused_by_every_command_that_takes_one(self):
        cases = [("demo", "--out"), ("smoke-q", "--out", "--config", "c.json"), ("phantom", "--out"),
                 ("pool", "--out", "--profiles", "p.json"), ("model-check", "--out", "--stages", "Q"),
                 ("certificate", "--out"), ("fork", "--out", "--config", "c.json"),
                 ("widen", "--out", "--pilot-config", "p.json", "--config", "c.json"),
                 ("sidejudge", "--out", "--config", "c.json"), ("validate-nomodel", "--out", "--config", "c.json"),
                 ("calibrate", "--out", "--config", "c.json", "--profile", "p.json"),
                 ("export-profile", "--out-dir", "--config", "c.json", "--profile", "p.json")]
        for cmd, flag, *rest in cases:
            for blank in ("", "  "):
                with self.subTest(cmd=cmd, blank=blank), mock.patch.object(pipeline, "run") as ran:
                    rc, out, err = cli_run(cmd, *rest, flag, blank)
                    self.assertEqual((rc, out), (2, ""))
                    self.assertTrue(err.startswith(f"refused: {flag} is empty"), err)
                    ran.assert_not_called()

    def test_equals_form_and_the_bench(self):
        rc, _out, err = cli_run("bench", "--mode", "generative", "--out", "")
        self.assertEqual(rc, 2)
        self.assertIn("refused: --out is empty", err)
        rc, _out, err = cli_run("bench", "--mode=generative", "--out=")
        self.assertEqual(rc, 2)

    def test_a_demo_with_an_empty_out_writes_nothing_where_it_stands(self):
        here = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, here)
        rc, _out, err = cli_run("demo", "--out", "", "--client", "oracle")
        self.assertEqual(rc, 2)
        self.assertEqual(list(self.root.iterdir()), [])


class TestU24CleanRefusals(Tmp):
    def refused(self, *argv, mention=()):
        rc, out, err = cli_run(*argv)
        self.assertEqual(rc, 2, (out, err))
        self.assertTrue(err.startswith("refused: "), err)
        self.assertNotIn("Traceback", err)
        for word in mention:
            self.assertIn(word, err)
        self.assertEqual(list(self.root.glob("demo-out*")), [])           # nothing was written
        return err

    def test_a_missing_config_is_named(self):
        for cmd in ("demo", "run", "report"):
            argv = [cmd, "--config", str(self.root / "nope.json")] + (["--out", str(self.root / "demo-out")]
                                                                      if cmd == "demo" else [])
            with self.subTest(cmd=cmd):
                self.refused(*argv, mention=("nope.json", "No such file"))

    def test_a_binary_incomplete_or_mistyped_config(self):
        binary = self.root / "binary.txt"
        binary.write_bytes(b"\xbe\xff\x00\x81\x82" * 20)
        incomplete = self.root / "incomplete.json"
        incomplete.write_text('{"run_id": "x"}')
        mistyped = self.root / "mistyped.json"
        mistyped.write_text('{"run_id": 5, "corpus": "x", "out_dir": []}')
        a_list = self.root / "list.json"
        a_list.write_text("[1, 2]")
        for f in (binary, incomplete, mistyped, a_list):
            for cmd in ("demo", "run", "report"):
                argv = [cmd, "--config", str(f)] + (["--out", str(self.root / "demo-out")] if cmd == "demo" else [])
                with self.subTest(file=f.name, cmd=cmd):
                    self.refused(*argv, mention=(f.name,))

    def test_a_config_the_kit_refuses_for_its_content_keeps_its_message(self):
        d = json.loads(Path(DEMO_CONFIG).read_text())
        d["corpus"]["kind"] = "Real"
        f = self.root / "c.json"
        f.write_text(json.dumps(d))
        self.refused("run", "--config", str(f), mention=("corpus.kind must be one of",))

    def test_bad_fixtures_files(self):
        bad = {"truncated.json": b'{"a":', "binary.json": b"\xbe\xff\x00\x81", "deep.json": b"[" * 100000 + b"]" * 100000,
               "list.json": b"[1]", "meta_list.json": b'{"_meta": [1]}'}
        for name, data in bad.items():
            f = self.root / name
            f.write_bytes(data)
            with self.subTest(name=name):
                self.refused("demo", "--out", str(self.root / "demo-out"), "--client", "replay", "--fixtures", str(f),
                             mention=(name, "fixtures"))
        self.refused("demo", "--out", str(self.root / "demo-out"), "--client", "replay",
                     "--fixtures", str(self.root / "missing.json"), mention=("missing.json", "fixtures"))

    def test_an_unknown_stage(self):
        for argv in (["--stages", "Z"], ["--stages", "Q,Z"], ["--only", "../../x"]):
            with self.subTest(argv=argv):
                self.refused("run", "--config", str(DEMO_CONFIG), "--client", "oracle", *argv, mention=("unknown stage",))
        err = self.refused("run", "--config", str(DEMO_CONFIG), "--stages", "Z", mention=("Q, J, M, all",))
        self.assertIn("recon, prior_only", err)                           # the stage names are listed

    def test_http_without_the_endpoint_variables_names_the_missing_one(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("SS_")}
        with mock.patch.dict(os.environ, env, clear=True):
            err = self.refused("demo", "--out", str(self.root / "demo-out"), "--client", "http",
                               mention=("SS_RECON_URL", "README"))
        with mock.patch.dict(os.environ, {**env, "SS_RECON_URL": "http://localhost:1/v1", "SS_RECON_MODEL": "m",
                                          "SS_RECON_TIMEOUT_S": "soon"}, clear=True):
            self.refused("demo", "--out", str(self.root / "demo-out"), "--client", "http", mention=("endpoint settings",))

    def test_a_record_request_with_a_bad_config_is_a_refusal_too(self):
        f = self.root / "bad.json"
        f.write_text("{")
        self.refused("run", "--config", str(f), "--record", str(self.root / "r.json"))


class TestU23Bom(Tmp):
    def test_a_config_with_a_bom_loads_and_hashes_like_the_same_config_without(self):
        plain = make_corpus(self.root / "a")
        bom = self.root / "b.json"
        bom.write_bytes(b"\xef\xbb\xbf" + plain.read_bytes())
        cfg_a, cfg_b = pipeline.RunConfig.load(plain), pipeline.RunConfig.load(bom, base_dir=str(plain.parent))
        self.assertEqual(cfg_a.sha256(), cfg_b.sha256())

    def test_a_cli_run_with_a_bom_config_is_not_refused_for_it(self):
        plain = make_corpus(self.root / "a")
        bom = self.root / "a" / "bom.json"
        bom.write_bytes(b"\xef\xbb\xbf" + plain.read_bytes())
        rc, _out, err = cli_run("run", "--config", str(bom), "--stages", "bogus")
        self.assertEqual(rc, 2)
        self.assertIn("unknown stage", err)              # got past the config, to the stage names

    def test_an_id_map_with_a_bom_is_read_by_the_leak_index(self):
        from second_scribe import audits
        cfg_path = make_corpus(self.root / "r", kind="real")
        cfg = pipeline.RunConfig.load(cfg_path)
        idmap = Path(cfg.corpus["participant_id_map"])
        idmap.write_bytes(b"\xef\xbb\xbf" + idmap.read_bytes())
        index = audits.LeakIndex.from_config(cfg)
        self.assertEqual(index.stats.get("id_map_entries"), 1)


_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def docx_with_body(path: Path, body: str) -> Path:
    xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{_W_NS}"><w:body>{body}</w:body></w:document>'
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", xml)
    return path


def nested(n: int, text: str = "hello") -> str:
    return "<w:p>" * n + f"<w:r><w:t>{text}</w:t></w:r>" + "</w:p>" * n


def reference_read_docx_text(path) -> list[str]:
    """The extraction loop as it was before U03 (the guard must leave its output untouched for ordinary documents)."""
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    W = "{" + _W_NS + "}"
    out = []
    for p in root.iter(W + "p"):
        parts = []
        for el in p.iter():
            if el.tag == W + "t":
                parts.append(el.text or "")
            elif el.tag in (W + "tab", W + "br"):
                parts.append(" ")
        out.append("".join(parts))
    return out


class TestU03NestedParagraphs(Tmp):
    def test_a_deeply_nested_docx_is_refused_fast(self):
        for n in (33, 5000, 200000):
            f = docx_with_body(self.root / f"deep{n}.docx", nested(n))
            started = time.perf_counter()
            with self.subTest(n=n), self.assertRaisesRegex(TranscriptRefused, "nests paragraphs more than 32 levels"):
                read_docx_text(f)
            self.assertLess(time.perf_counter() - started, 1.0, n)

    def test_the_refusal_reaches_the_cli_as_one_line(self):
        cfg_path = make_corpus(self.root)
        target = sorted((self.root / "corpus" / "transcripts").glob("*.docx"))[0]
        docx_with_body(target, nested(5000))
        started = time.perf_counter()
        rc, _out, err = cli_run("run", "--config", str(cfg_path), "--stages", "Q", "--client", "oracle")
        self.assertEqual(rc, 2)
        self.assertIn("1 input file cannot be read", err)
        self.assertIn("nests paragraphs more than 32 levels", err)
        self.assertNotIn("Traceback", err)
        self.assertLess(time.perf_counter() - started, 20.0)

    def test_ordinary_nesting_is_read_and_the_text_is_what_it_was(self):
        body = ("<w:p><w:r><w:t>one</w:t></w:r><w:r><w:tab/><w:t>two</w:t></w:r></w:p>"
                "<w:p><w:r><w:drawing><w:txbxContent>" + nested(3, "in a text box") + "</w:txbxContent></w:drawing></w:r></w:p>")
        f = docx_with_body(self.root / "ok.docx", body)
        self.assertEqual(read_docx_text(f), reference_read_docx_text(f))
        edge = docx_with_body(self.root / "edge.docx", nested(transcripts.MAX_PARAGRAPH_NESTING))
        self.assertEqual(read_docx_text(edge), reference_read_docx_text(edge))    # 32 deep is still read

    def test_every_demo_transcript_reads_exactly_as_before(self):
        files = sorted((DEMO_CORPUS / "transcripts").glob("*.docx"))
        self.assertTrue(files)
        for f in files:
            self.assertEqual(read_docx_text(f), reference_read_docx_text(f), f.name)


LABELLED = ["Interviewer: How did you start?", "Participant: We began with a loan.", "Interviewer: And then?",
            "PARTICIPANT: We joined the fund.", "Interviewer: Why that one?", "Participant : It was cheap.",
            "Interviewer: Who helped?", "Participant: My sister.", "Interviewer: Thanks.", "Participant: Welcome."]


class TestU06LabelLookalikes(Tmp):
    def parse(self):
        return parse_paragraphs(LABELLED, {"participant"}, "T1", interviewer_labels={"interviewer"})

    def test_a_label_in_another_case_or_with_a_space_before_the_colon_is_reported(self):
        found = transcripts.label_lookalikes(LABELLED, {"participant"}, "T1", {"interviewer"})
        self.assertEqual(dict(found), {"PARTICIPANT": 1, "Participant :": 1})

    def test_the_turns_are_parsed_exactly_as_before(self):
        t = self.parse()
        self.assertEqual(t.speaker_format, "first_token")
        self.assertEqual([(x.speaker, x.text) for x in t.turns], [
            ("interviewer", "How did you start?"), ("participant", "We began with a loan."),
            ("interviewer", "And then? PARTICIPANT: We joined the fund."),      # the upper-case line is still glued
            ("interviewer", "Why that one?"), ("participant", ": It was cheap."),   # and the stray colon is still there
            ("interviewer", "Who helped?"), ("participant", "My sister."), ("interviewer", "Thanks."),
            ("participant", "Welcome.")])

    def test_a_sentence_that_opens_with_the_same_word_is_not_reported(self):
        paras = ["AM: one", "BO: two", "AM: three", "BO: four", "Am I sure about this?", "AM: five", "BO: six"]
        self.assertEqual(parse_paragraphs(paras, set(), "T").speaker_format, "first_token")
        self.assertEqual(len(transcripts.label_lookalikes(paras, set(), "T")), 0)

    def test_clean_transcripts_report_nothing(self):
        clean = [x.replace("PARTICIPANT:", "Participant:").replace("Participant :", "Participant:") for x in LABELLED]
        self.assertEqual(len(transcripts.label_lookalikes(clean, {"participant"}, "T", {"interviewer"})), 0)
        for f in sorted((DEMO_CORPUS / "transcripts").glob("*.docx")):                # bracket labels: nothing
            self.assertEqual(len(transcripts.label_lookalikes(read_docx_text(f), {"pt"}, f.stem, {"iv"})), 0, f.name)

    def test_the_warning_reaches_the_console_through_check_inputs(self):
        from second_scribe.synth.docx_writer import write_docx
        cfg_path = make_corpus(self.root, participant_labels={"*": ["Participant"]},
                               interviewer_labels={"*": ["Interviewer"]})
        target = sorted((self.root / "corpus" / "transcripts").glob("*.docx"))[0]
        write_docx(target, LABELLED)
        check = pipeline.check_inputs(pipeline.RunConfig.load(cfg_path))
        ours = [w for w in check.warnings if "look like a speaker label" in w]
        self.assertEqual(len(ours), 1)
        self.assertIn(f"{target.name}: 2 lines (PARTICIPANT, Participant :)", ours[0])
        rc, _out, err = cli_run("report", "--config", str(cfg_path))          # the CLI prints it on stderr as a warning
        self.assertIn("warning: 1 transcript has lines that look like a speaker label", err)

    def test_a_real_corpus_warning_never_names_the_spelling(self):
        from second_scribe.synth.docx_writer import write_docx
        cfg_path = make_corpus(self.root, kind="real", participant_labels={"*": ["Participant"]},
                               interviewer_labels={"*": ["Interviewer"]})
        for f in (self.root / "corpus" / "transcripts").glob("*.docx"):
            write_docx(f, LABELLED)
        check = pipeline.check_inputs(pipeline.RunConfig.load(cfg_path))
        ours = [w for w in check.warnings if "look like a speaker label" in w]
        self.assertEqual(len(ours), 1)
        self.assertIn("transcript #1: 2 lines (2 spellings)", ours[0])
        self.assertNotIn("PARTICIPANT", ours[0])


class TestU05DuplicateTranscripts(Tmp):
    def duplicated_corpus(self, kind="synthetic"):
        cfg_path = make_corpus(self.root, kind=kind)
        files = sorted((self.root / "corpus" / "transcripts").glob("*.docx"))
        shutil.copyfile(files[0], files[1])                                    # the same interview filed twice
        return cfg_path, files

    def test_duplicate_transcripts_are_found_by_content_not_by_name(self):
        cfg_path, files = self.duplicated_corpus()
        people = pipeline.load_participants(pipeline.RunConfig.load(cfg_path))
        self.assertEqual(pipeline.duplicate_transcripts(people), [[files[0].stem, files[1].stem]])

    def test_no_duplicates_no_warning_no_line(self):
        cfg_path = make_corpus(self.root)
        check = pipeline.check_inputs(pipeline.RunConfig.load(cfg_path))
        self.assertEqual([w for w in check.warnings if "byte-identical" in w], [])
        self.assertEqual(pipeline.duplicate_transcripts(check.pairing.participants), [])

    def test_the_console_warning_names_the_files_of_a_synthetic_corpus_and_the_positions_of_a_real_one(self):
        cfg_path, files = self.duplicated_corpus()
        check = pipeline.check_inputs(pipeline.RunConfig.load(cfg_path))
        (w,) = [w for w in check.warnings if "byte-identical" in w]
        self.assertIn(f"{files[0].name} = {files[1].name}", w)
        self.assertIn("nothing is excluded", w)
        rc, _out, err = cli_run("report", "--config", str(cfg_path))
        self.assertIn("warning: 1 transcript is a byte-identical copy of another one", err)
        cfg_path, files = self.duplicated_corpus_real()
        check = pipeline.check_inputs(pipeline.RunConfig.load(cfg_path))
        (w,) = [w for w in check.warnings if "byte-identical" in w]
        self.assertIn("transcript #1 = transcript #2", w)
        self.assertNotIn(files[0].stem, w)

    def duplicated_corpus_real(self):
        self.root = self.root / "real"
        self.root.mkdir()
        return self.duplicated_corpus(kind="real")

    def test_the_run_report_gets_one_line_only_when_duplicates_exist(self):
        cfg_path, _files = self.duplicated_corpus()
        rc, _out, err = cli_run("run", "--config", str(cfg_path), "--client", "oracle", "--stages", "all")
        self.assertEqual(rc, 0, err)
        self.assertIn("warning: 1 transcript is a byte-identical copy of another one", err)
        out_dir = Path(pipeline.RunConfig.load(cfg_path).out_dir)
        agg = json.loads((out_dir / "aggregates" / "aggregates.json").read_text())
        self.assertEqual(agg["duplicate_transcripts"], {"groups": 1, "copies": 1})
        self.assertEqual(agg["n_participants"], 12)                                # nothing was excluded
        report = (out_dir / "aggregates" / "report.md").read_text()
        (line,) = [x for x in report.splitlines() if x.startswith("- duplicate transcripts")]
        self.assertIn("1 transcript file(s) are byte-identical copies of another (1 group(s))", line)

    def test_the_report_of_a_run_without_duplicates_has_no_such_line(self):
        from second_scribe import report as report_mod
        base = {"run_id": "x", "corpus_kind": "synthetic", "n_participants": 2, "n_analysed": 2, "config_sha256": "0" * 64,
                "prompt_set_sha256": "0" * 64, "models": {}, "primary_spec": "s", "primary_setting": "p",
                "exclusions": {}, "specs": [], "items": {}, "buckets": {"by_spec": {}}, "absences": {"by_spec": {}},
                "judging": {"by_spec": {}}}
        self.assertNotIn("duplicate transcripts", report_mod.report_markdown(base))
        with_dups = report_mod.report_markdown({**base, "duplicate_transcripts": {"groups": 1, "copies": 2}})
        self.assertIn("- duplicate transcripts: 2 transcript file(s) are byte-identical copies of another (1 group(s))",
                      with_dups)


if __name__ == "__main__":
    unittest.main()
