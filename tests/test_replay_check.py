"""replay-check (app T20, spec §13): the shipped Try-it data is the recorded run.

An oracle ``demo --record`` run plays the recording (no model: the oracle reads the gold labels). Its own log is the
recorded log and the view built from it is the shipped ``tryit.js``. Each test breaks one thing and expects one reason.
The last class runs the same checks on the copied recording in ``demo/recorded/`` (synthetic text only). Replay
only: no model, no network. Temp dirs only (``tests/__init__`` roots them in ``ss-tests-*``)."""
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from second_scribe import cli, export_view, replay_check as rc
from second_scribe.audit import verify as audit_verify
from second_scribe.audit.registry import AuditLog
from second_scribe.pipeline import KIT_ROOT, RunConfig

DEMO_CONFIG = KIT_ROOT / "configs" / "demo.json"
RECORDED = KIT_ROOT / "demo" / "recorded"
DEMO_HASH = "ccc13208b1fe5e66b5f4bef970af228cb92af9aefca6f21d341a0ccaa18c1ba9"
# literals built at run time, so this file holds no address or home path for the public-tree leak scan to find
LAN_IP = ".".join(("192", "168", "1", "20"))
CGNAT_IP = ".".join(("100", "64", "3", "7"))
HOME = "/" + "Users" + "/someone/corpus"


def quiet(fn, *args, **kw):
    with redirect_stdout(io.StringIO()):
        return fn(*args, **kw)


def demo(out: Path, *extra: str) -> None:
    code = quiet(cli.main, ["demo", "--out", str(out), *extra])
    if code != 0:
        raise AssertionError(f"demo {extra} exited {code}")


def write_view_js(out_dir: Path, dest: Path) -> Path:
    cfg = RunConfig.load(str(DEMO_CONFIG), out_dir=str(out_dir))
    view = export_view.build_view(cfg, source="synthetic", reference_type=None)
    dest.write_text(export_view.view_to_js(view, "tryit"), encoding="utf-8")
    return dest


def records(log: Path) -> list[dict]:
    return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]


def rechain(recs: list[dict], dest: Path) -> Path:
    """The same payloads, in the given order, as a new hash-chained log with its manifest (so it verifies)."""
    log = AuditLog(dest, recs[0]["run_id"])
    for r in recs:
        log.append(r["record_type"], r["payload"])
    log.close()
    return dest


class OracleRecording(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Path(tempfile.mkdtemp())
        cls.fixtures = cls.d / "fixtures.json"
        demo(cls.d / "rec", "--client", "oracle", "--record", str(cls.fixtures))
        cls.log = cls.d / "rec" / "logs" / "run.jsonl"
        cls.view_js = write_view_js(cls.d / "rec", cls.d / "tryit.js")

    def check(self, *, fixtures=None, log=None, view_js=None):
        return rc.replay_check(DEMO_CONFIG, fixtures or self.fixtures, log or self.log, view_js or self.view_js)

    def edited_fixtures(self, edit) -> Path:
        data = json.loads(self.fixtures.read_text(encoding="utf-8"))
        edit(data)
        p = Path(tempfile.mkdtemp()) / "fixtures.json"
        p.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        return p


class TestOracleRecording(OracleRecording):
    def test_demo_fixtures_pass(self):
        self.assertEqual(self.check(), (True, []))

    def test_reordered_recorded_log_fails_request(self):
        recs = records(self.log)
        idx = [i for i, r in enumerate(recs) if r["record_type"] == "call_request"
               and i + 1 < len(recs) and recs[i + 1]["record_type"] == "call_response"]
        i = idx[0]
        j = next(k for k in idx[1:] if recs[k]["payload"]["messages_sha256"] != recs[i]["payload"]["messages_sha256"])
        swapped = recs[:i] + recs[j:j + 2] + recs[i + 2:j] + recs[i:i + 2] + recs[j + 2:]
        log = rechain(swapped, Path(tempfile.mkdtemp()) / "run.jsonl")
        self.assertTrue(audit_verify.verify_run(log)[0])          # the recording itself still verifies
        n = [r["record_type"] for r in recs].count("call_request")
        n_before = [r["record_type"] for r in recs[:i]].count("call_request")
        ok, reasons = self.check(log=log)
        self.assertFalse(ok)
        self.assertIn(f"request_mismatch:{n_before}", reasons)
        self.assertGreater(n, n_before)

    def test_edited_fixture_fails_request(self):
        log = records(self.log)
        key = next(r["payload"]["messages_sha256"] for r in log if r["record_type"] == "call_request"
                   and r["payload"]["stage"].startswith("recon:"))

        def edit(data):
            rec = data[key]
            target = rec["calls"][0] if isinstance(rec, dict) and "calls" in rec else rec
            target["text"] = "not a map"
        ok, reasons = self.check(fixtures=self.edited_fixtures(edit))
        self.assertFalse(ok)
        self.assertTrue(any(r.startswith("request_mismatch:") for r in reasons), reasons)

    def test_model_id_swap_fails(self):
        def edit(data):
            data["_meta"]["models"]["judge"] = "some-other-judge"
        ok, reasons = self.check(fixtures=self.edited_fixtures(edit))
        self.assertFalse(ok)
        first_judge = next(n for n, r in enumerate(r for r in records(self.log) if r["record_type"] == "call_request")
                           if r["payload"]["stage"].startswith("judge:"))
        self.assertIn(f"model_mismatch:{first_judge}", reasons)
        self.assertFalse(any(r.startswith("request_mismatch") for r in reasons), reasons)

    def test_unused_fixture_fails(self):
        multi = []

        def edit(data):
            key = next(k for k, v in sorted(data.items()) if isinstance(v, dict) and "calls" in v)
            data[key]["calls"].append({"text": "never asked", "finish_reason": "stop", "usage": {}})
            data["f" * 64] = {"text": "never asked either", "finish_reason": "stop", "usage": {}}
            multi.append((key, len(data[key]["calls"]) - 1))
        ok, reasons = self.check(fixtures=self.edited_fixtures(edit))
        self.assertFalse(ok)
        key, i = multi[0]
        self.assertIn(f"unused_fixture:{key}:{i}", reasons)
        self.assertIn(f"unused_fixture:{'f' * 64}:0", reasons)
        self.assertEqual(len([r for r in reasons if r.startswith("unused_fixture:")]), 2, reasons)

    def test_determinism_re_asks_count_as_served(self):
        """The audits' re-asks are served by the client but not logged: the oracle recording holds them, and a clean
        check proves the count includes them (more fixture calls than logged requests)."""
        data = json.loads(self.fixtures.read_text(encoding="utf-8"))
        n_fixture_calls = sum(len(v["calls"]) if isinstance(v, dict) and "calls" in v else 1
                              for k, v in data.items() if k != "_meta")
        n_logged = [r["record_type"] for r in records(self.log)].count("call_request")
        self.assertGreater(n_fixture_calls, n_logged)
        self.assertEqual(self.check(), (True, []))

    def test_tampered_work_item_fails_with_force(self):
        real_replay = rc._replay

        def replay_then_tamper(cfg, fixtures):
            served = real_replay(cfg, fixtures)
            item = sorted((Path(cfg.out_dir) / "work" / "recon").glob("*.json"))[0]
            obj = json.loads(item.read_text(encoding="utf-8"))
            obj["tampered"] = True
            item.write_text(json.dumps(obj), encoding="utf-8")
            return served
        with mock.patch.object(rc, "_replay", replay_then_tamper):
            ok, reasons = self.check()
        self.assertFalse(ok)
        self.assertTrue(any(r.startswith("work_integrity:") and "changed after it was logged" in r for r in reasons),
                        reasons)

    def test_view_byte_difference_fails(self):
        text = self.view_js.read_text(encoding="utf-8")
        p = Path(tempfile.mkdtemp()) / "tryit.js"
        p.write_text(text.replace('"source": "synthetic"', '"source": "synthetic" '), encoding="utf-8")  # bytes only
        self.assertEqual(self.check(view_js=p), (False, ["view_differs"]))
        doc = json.loads(text.split(" = ", 2)[2].rstrip(";"))
        doc["draws"] = doc.get("draws", 0) + 1
        p.write_text(export_view.view_to_js(doc, "tryit"), encoding="utf-8")        # canonical bytes, other data
        self.assertEqual(self.check(view_js=p), (False, ["view_differs"]))

    def test_view_from_another_replay_passes(self):
        """A fresh replay differs only in the per-replay fields (request-id suffix, record hashes, chain head,
        checked_at); everything else is byte-identical, so a view built from any replay of the recording passes."""
        out = Path(tempfile.mkdtemp())
        demo(out, "--client", "replay", "--fixtures", str(self.fixtures))
        other = write_view_js(out, out / "tryit.js")
        self.assertNotEqual(other.read_bytes(), self.view_js.read_bytes())
        self.assertEqual(self.check(view_js=other), (True, []))

    def test_unverified_recorded_log_fails(self):
        d = Path(tempfile.mkdtemp())
        shutil.copyfile(self.log, d / "run.jsonl")                       # no manifest next to it
        ok, reasons = self.check(log=d / "run.jsonl")
        self.assertFalse(ok)
        self.assertIn("recorded_log_unverified", reasons)

    def test_cli_exit_codes(self):
        args = ["replay-check", "--config", str(DEMO_CONFIG), "--fixtures", str(self.fixtures),
                "--recorded-log", str(self.log), "--view-js", str(self.view_js)]
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(args), 0)
        bad = Path(tempfile.mkdtemp()) / "tryit.js"
        bad.write_text(self.view_js.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(args[:-1] + [str(bad)]), 1)
        self.assertEqual(buf.getvalue().splitlines(), ["view_differs"])


class TestRequestComparison(unittest.TestCase):
    """The per-index comparison on its own: no replay needed."""
    META = {"recon": "model-r", "judge": "model-j"}

    def recs(self, *calls, models=None):
        out = [{"record_type": "run_start", "payload": {"models": models or {}}}]
        for stage, sha, model in calls:
            p = {"stage": stage, "messages_sha256": sha}
            if model is not None:
                p["model_id"] = model
            out.append({"record_type": "call_request", "payload": p})
        return out

    def test_same_sequence_passes(self):
        a = self.recs(("recon:full", "a", "model-r"), ("judge:align", "b", "model-j"))
        b = self.recs(("recon:full", "a", None), ("judge:align", "b", None), models=self.META)
        self.assertEqual(rc.compare_requests(a, b, self.META), [])

    def test_swapped_models_fail_at_the_first_call(self):
        a = self.recs(("recon:full", "a", "model-r"), ("judge:align", "b", "model-j"))
        b = self.recs(("recon:full", "a", None), ("judge:align", "b", None), models=self.META)
        swapped = {"recon": "model-j", "judge": "model-r"}
        self.assertEqual(rc.compare_requests(a, b, swapped), ["model_mismatch:0"])

    def test_a_shorter_replay_is_a_request_mismatch(self):
        a = self.recs(("recon:full", "a", "model-r"), ("judge:align", "b", "model-j"))
        b = self.recs(("recon:full", "a", None), models=self.META)
        self.assertEqual(rc.compare_requests(a, b, self.META), ["request_mismatch:1"])


class TestPrivacyScan(unittest.TestCase):
    def write(self, name: str, text: str) -> Path:
        p = Path(tempfile.mkdtemp()) / name
        p.write_text(text, encoding="utf-8")
        return p

    def test_privacy_scan_flags_lan_ip_and_home_path(self):
        p = self.write("run.jsonl", json.dumps({"payload": {"endpoint_host": f"{LAN_IP}:11434",
                                                            "note": f"saved under {HOME}/x.docx"}}) + "\n")
        q = self.write("fixtures.json", json.dumps({"k": {"text": f"see http://{CGNAT_IP}:8080/v1"}}))
        reasons = rc.privacy_scan([p, q])
        self.assertIn("privacy:run.jsonl:ipv4", reasons)
        self.assertIn("privacy:run.jsonl:home_path", reasons)
        self.assertIn("privacy:run.jsonl:endpoint_host", reasons)
        self.assertIn("privacy:fixtures.json:ipv4", reasons)
        self.assertIn("privacy:fixtures.json:url_host", reasons)
        for r in reasons:                                   # a reason names the file and the kind, never the text
            self.assertNotIn(LAN_IP, r)
            self.assertNotIn("someone", r)

    def test_windows_home_and_keys_are_flagged(self):
        p = self.write("tryit.js", json.dumps({"a": "C:\\" + "Users\\me", "b": "sk-" + "A1b2C3d4" * 4}))
        self.assertEqual(rc.privacy_scan([p]), ["privacy:tryit.js:home_path", "privacy:tryit.js:secret"])

    def test_loopback_endpoints_pass(self):
        p = self.write("run.jsonl", "\n".join(json.dumps({"payload": {"endpoint_host": h}})
                                              for h in ("127.0.0.1:18142", "localhost:8080")) + "\n")
        q = self.write("fixtures.json", json.dumps({"k": {"text": "version 1.2 of the form; call at 10.30 am"}}))
        self.assertEqual(rc.privacy_scan([p, q]), [])

    def test_a_scan_failure_fails_replay_check(self):
        d = Path(tempfile.mkdtemp())
        bad = d / "fixtures.json"
        bad.write_text(json.dumps({"_meta": {"models": {}}, "k": {"text": f"from {LAN_IP}"}}), encoding="utf-8")
        ok, reasons = rc.replay_check(DEMO_CONFIG, bad, d / "run.jsonl", d / "tryit.js")
        self.assertFalse(ok)
        self.assertIn("privacy:fixtures.json:ipv4", reasons)


class TestForceFlag(unittest.TestCase):
    """``work_integrity_problem(force=False)`` is today's behaviour (a synthetic log passes untouched);
    ``force=True`` runs the work-store checks on a synthetic log too."""
    @classmethod
    def setUpClass(cls):
        cls.out = Path(tempfile.mkdtemp())
        demo(cls.out, "--client", "oracle")
        cls.log = cls.out / "logs" / "run.jsonl"
        cls.work = cls.out / "work"

    def test_clean_synthetic_run_passes_both_ways(self):
        self.assertIsNone(audit_verify.work_integrity_problem(self.log, self.work))
        self.assertIsNone(audit_verify.work_integrity_problem(self.log, self.work, force=True))

    def test_tampered_synthetic_item_only_caught_with_force(self):
        work = Path(tempfile.mkdtemp()) / "work"
        shutil.copytree(self.work, work)
        item = sorted((work / "recon").glob("*.json"))[0]
        obj = json.loads(item.read_text(encoding="utf-8"))
        obj["tampered"] = True
        item.write_text(json.dumps(obj), encoding="utf-8")
        self.assertIsNone(audit_verify.work_integrity_problem(self.log, work))
        problem = audit_verify.work_integrity_problem(self.log, work, force=True)
        self.assertIn("changed after it was logged", problem or "")

    def test_force_on_a_missing_store(self):
        self.assertIsNone(audit_verify.work_integrity_problem(self.log, self.out / "nowhere"))
        self.assertIn("work store not found", audit_verify.work_integrity_problem(self.log, self.out / "nowhere",
                                                                                   force=True) or "")

    def test_demo_hash_unchanged(self):
        self.assertEqual(RunConfig.load(str(DEMO_CONFIG)).sha256(), DEMO_HASH)


class TestRecordedRun(unittest.TestCase):
    """The copied recording of the re-recorded Try-it run (18 Oct; synthetic corpus; loopback endpoints)."""
    FIXTURES = RECORDED / "demo-live-fixtures.v2.json"
    LOG = RECORDED / "run.jsonl"

    def test_recorded_files_are_present(self):
        for name in ("demo-live-fixtures.v2.json", "run.jsonl", "run.manifest.json"):
            self.assertTrue((RECORDED / name).is_file(), name)

    def test_recorded_run_passes(self):
        out = Path(tempfile.mkdtemp())
        demo(out, "--client", "replay", "--fixtures", str(self.FIXTURES))
        view_js = write_view_js(out, out / "tryit.js")
        self.assertEqual(rc.replay_check(DEMO_CONFIG, self.FIXTURES, self.LOG, view_js), (True, []))

    def test_recorded_files_pass_the_privacy_scan(self):
        self.assertEqual(rc.privacy_scan([self.FIXTURES, self.LOG, RECORDED / "run.manifest.json"]), [])


if __name__ == "__main__":
    unittest.main()
