"""Task 14: the app log (K7), a hash-chained log of the app's own actions, re-closed after every append."""
import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path

from second_scribe.audit.applog import APP_RECORD_TYPES, AppLog
from second_scribe.audit.manifest import manifest_path_for
from second_scribe.audit.verify import verify_run
from second_scribe.cli import DEMO_CONFIG
from second_scribe.cli import main as cli_main

HEAD = "ab" * 32
# the run_start payload keys of a study config at the branch point f8e4ed9 (no tool block), plus K6's code_sha256
STUDY_RUN_START_KEYS = {"code_clean", "code_commit", "code_sha256", "config_sha256", "corpus_kind", "ladder_order",
                        "models", "n_participants", "prompt_manifest_problems", "prompt_set_sha256", "quote_rule",
                        "quote_rule_sha256", "stages"}


def _log_path():
    return Path(tempfile.mkdtemp()) / "logs" / "app.jsonl"


def _lines(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class TestAppLog(unittest.TestCase):
    def test_record_types(self):
        self.assertEqual(APP_RECORD_TYPES, (
            "project_created", "import", "removed", "labels_confirmed", "missing_rule", "map_confirmed", "context_set",
            "reference_set", "sync_folder_ack", "optin", "declaration", "model_check", "coding_locked", "reveal",
            "run_started", "run_linked", "export"))

    def test_manifest_current_after_each_append(self):
        path = _log_path()
        log = AppLog(path, "proj-1")
        for n in range(1, 6):
            rec = log.append("import", {"n": n})
            self.assertEqual(rec["seq"], n - 1)
            ok, detail = verify_run(path)
            self.assertTrue(ok, detail)
            self.assertEqual(detail["records"], n)
            self.assertEqual(log.head(), (n, rec["hash"]))
        self.assertTrue(Path(manifest_path_for(path)).name == "app.manifest.json")

    def test_manifests_chain(self):
        path = _log_path()
        log = AppLog(path, "proj-1")
        log.append("import", {"n": 1})
        first = json.loads(Path(manifest_path_for(path)).read_text(encoding="utf-8"))
        self.assertIsNone(first["prev_manifest_hash"])
        log.append("import", {"n": 2})
        second = json.loads(Path(manifest_path_for(path)).read_text(encoding="utf-8"))
        from second_scribe.audit.manifest import compute_manifest_hash
        self.assertEqual(second["prev_manifest_hash"], compute_manifest_hash(first))

    def test_unknown_type_refused(self):
        path = _log_path()
        log = AppLog(path, "proj-1")
        with self.assertRaises(ValueError):
            log.append("call_request", {"x": 1})
        with self.assertRaises(ValueError):
            log.append("run_start", {})
        self.assertFalse(path.exists())
        self.assertEqual(log.head(), (0, "0" * 64))

    def test_non_dict_payload_refused(self):
        log = AppLog(_log_path(), "proj-1")
        with self.assertRaises(TypeError):
            log.append("import", ["not", "a", "dict"])

    def test_threaded_appends_chain(self):
        path = _log_path()
        log = AppLog(path, "proj-1")
        errors = []

        def worker(t):
            try:
                for i in range(10):
                    log.append("optin", {"thread": t, "i": i})
            except Exception as exc:          # pragma: no cover - surfaced by the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(errors, [])
        ok, detail = log.verify()
        self.assertTrue(ok, detail)
        self.assertEqual(detail["records"], 80)
        self.assertEqual([r["seq"] for r in _lines(path)], list(range(80)))
        self.assertEqual(log.head()[0], 80)

    def test_reopen_continues_chain(self):
        path = _log_path()
        AppLog(path, "proj-1").append("project_created", {"name": "p"})
        again = AppLog(path, "proj-1")
        again.append("import", {"file": "a.docx"})
        again.append("export", {"what": "report"})
        ok, detail = again.verify()
        self.assertTrue(ok, detail)
        self.assertEqual(detail["records"], 3)
        self.assertEqual([r["record_type"] for r in again.records()], ["project_created", "import", "export"])

    def test_records_filter(self):
        log = AppLog(_log_path(), "proj-1")
        self.assertEqual(log.records(), [])
        log.append("import", {"n": 1})
        log.append("export", {"n": 2})
        log.append("import", {"n": 3})
        self.assertEqual([r["payload"]["n"] for r in log.records("import")], [1, 3])
        self.assertEqual(len(log.records()), 3)

    def test_clock_used(self):
        log = AppLog(_log_path(), "proj-1", clock=lambda: "2026-10-18T00:00:00+00:00")
        self.assertEqual(log.append("import", {})["ts"], "2026-10-18T00:00:00+00:00")

    def test_tampered_line_fails_verify(self):
        path = _log_path()
        log = AppLog(path, "proj-1")
        log.append("import", {"name": "aaaa"})
        log.append("export", {"name": "bbbb"})
        self.assertTrue(log.verify()[0])
        raw = path.read_bytes()
        self.assertEqual(raw.count(b'"aaaa"'), 1)
        path.write_bytes(raw.replace(b'"aaaa"', b'"aaab"'))
        ok, detail = log.verify()
        self.assertIs(ok, False)
        self.assertTrue(detail["errors"], detail)
        with self.assertRaises(RuntimeError):      # nothing is appended onto a broken chain
            log.append("import", {"name": "cccc"})

    def test_truncated_log_not_reanchored(self):
        path = _log_path()
        log = AppLog(path, "proj-1")
        for n in range(3):
            log.append("import", {"n": n})
        lines = path.read_bytes().splitlines(keepends=True)
        path.write_bytes(b"".join(lines[:2]))           # drop the last record: the chain alone still verifies
        self.assertFalse(log.verify()[0])
        with self.assertRaises(RuntimeError):
            log.append("import", {"n": 9})
        self.assertFalse(log.verify()[0])              # the manifest still names 3 records

    def test_emptied_log_not_restarted(self):
        path = _log_path()
        log = AppLog(path, "proj-1")
        log.append("import", {"n": 0})
        path.unlink()
        with self.assertRaises(RuntimeError):
            AppLog(path, "proj-1").append("import", {"n": 1})
        self.assertFalse(path.exists())

    def test_verify_missing_log(self):
        ok, detail = AppLog(_log_path(), "proj-1").verify()
        self.assertIs(ok, False)
        self.assertEqual(detail["reason"], "log_missing")


class TestRunStartAppLogHead(unittest.TestCase):
    @staticmethod
    def _run_start(tool):
        out = Path(tempfile.mkdtemp()) / "run"
        out.mkdir()
        d = json.loads(Path(DEMO_CONFIG).read_text(encoding="utf-8"))
        d["out_dir"] = str(out)
        d["base_dir"] = str(Path(DEMO_CONFIG).parent)
        if tool is not None:
            d["tool"] = tool
        cfg = out / "config.json"
        cfg.write_text(json.dumps(d), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = cli_main(["run", "--config", str(cfg), "--stages", "Q", "--client", "oracle"])
        assert rc == 0, rc
        starts = [r for r in _lines(out / "logs" / "run.jsonl") if r["record_type"] == "run_start"]
        assert len(starts) == 1, starts
        return starts[0]["payload"]

    def test_run_start_carries_app_log_head(self):
        head = {"records": 3, "head": HEAD}
        with_tool = self._run_start({"app_log_head": head})
        self.assertEqual(with_tool["app_log_head"], head)
        without = self._run_start(None)
        self.assertNotIn("app_log_head", without)
        self.assertEqual(set(without), STUDY_RUN_START_KEYS)          # study records unchanged
        # the field is the only addition, besides V7's reading3_prompt_sha256 that every tool run records (T16)
        strip = lambda p: {k: v for k, v in p.items() if k not in ("app_log_head", "config_sha256",
                                                                   "reading3_prompt_sha256")}
        self.assertEqual(strip(with_tool), strip(without))

    def test_tool_without_app_log_head_adds_nothing(self):
        p = self._run_start({"reference_type": "A"})
        self.assertNotIn("app_log_head", p)
        # V7 (T16): every tool run records reading3_prompt_sha256; fix H: a tool run that declares one records its
        # reference_type
        self.assertEqual(set(p), STUDY_RUN_START_KEYS | {"reading3_prompt_sha256", "reference_type"})
        self.assertEqual(p["reference_type"], "A")


if __name__ == "__main__":
    unittest.main()
