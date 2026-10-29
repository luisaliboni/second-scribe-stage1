import contextlib
import io
import json
import math
import os
import tempfile
import unittest
from pathlib import Path

from second_scribe.audit.manifest import manifest_path_for, write_manifest
from second_scribe.audit.registry import (AuditLog, CanonicalizationError, GENESIS_HASH,
                                          redact)
from second_scribe.audit.verify import main as _verify_main
from second_scribe.audit.verify import (verify_internal_chain, verify_manifest_chain,
                                        verify_run)


def cli_main(argv):
    """The verify command with its printed result line kept out of the test output."""
    with contextlib.redirect_stdout(io.StringIO()):
        return _verify_main(argv)


def _counter_clock():
    n = {"i": 0}

    def clock():
        n["i"] += 1
        return f"2026-07-03T00:00:{n['i']:02d}+00:00"

    return clock


def _fixed_clock():
    return "2026-07-03T00:00:00+00:00"


class TestAuditHashChain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "run.jsonl")

    def _write_sample(self):
        reg = AuditLog(self.path, run_id="run-1", clock=_counter_clock())
        reg.append("run_config", {"cell": "c1", "seed": 1})
        reg.append("prompt_sent", {"agent_id": "a1", "text": "hello world"})
        reg.append("response", {"agent_id": "a1", "model_id": "model-A", "text": "hi there"})
        return reg

    def test_internal_chain_verifies(self):
        reg = self._write_sample()
        self.assertEqual(reg.head()[0], 3)
        ok, detail = verify_internal_chain(self.path)
        self.assertTrue(ok, detail)
        self.assertEqual(detail["records"], 3)
        self.assertEqual(detail["head_hash"], reg.head()[1])

    def test_empty_log_head_is_genesis(self):
        reg = AuditLog(self.path, run_id="run-1", clock=_counter_clock())
        self.assertEqual(reg.head(), (0, GENESIS_HASH))

    def test_tamper_payload_detected(self):
        self._write_sample()
        with open(self.path, encoding="utf-8") as fh:
            lines = fh.readlines()
        rec = json.loads(lines[1])
        rec["payload"]["text"] = "TAMPERED"  # edit content, keep old hash
        lines[1] = json.dumps(rec) + "\n"
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        ok, detail = verify_internal_chain(self.path)
        self.assertFalse(ok)
        self.assertEqual(detail["reason"], "hash_mismatch")
        self.assertEqual(detail["seq"], 1)

    def test_deleted_line_detected(self):
        self._write_sample()
        with open(self.path, encoding="utf-8") as fh:
            lines = fh.readlines()
        del lines[1]  # remove the middle record
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        ok, detail = verify_internal_chain(self.path)
        self.assertFalse(ok)
        self.assertIn(detail["reason"], {"seq_gap", "broken_link"})

    def test_resume_continues_chain(self):
        reg = self._write_sample()
        head = reg.head()
        reg2 = AuditLog(self.path, run_id="run-1", clock=_counter_clock())
        self.assertEqual(reg2.head(), head)
        reg2.append("response", {"agent_id": "a2", "text": "more"})
        ok, detail = verify_internal_chain(self.path)
        self.assertTrue(ok, detail)
        self.assertEqual(detail["records"], 4)

    def test_two_writers_on_one_file_stay_chained(self):
        a = AuditLog(self.path, run_id="run-1", clock=_counter_clock())
        b = AuditLog(self.path, run_id="run-1", clock=_counter_clock())
        a.append("x", {"n": 1})
        b.append("x", {"n": 2})
        a.append("x", {"n": 3})
        ok, detail = verify_internal_chain(self.path)
        self.assertTrue(ok, detail)
        self.assertEqual(detail["records"], 3)

    def test_api_key_redacted_in_payload(self):
        reg = AuditLog(self.path, run_id="run-1", clock=_counter_clock())
        reg.append("run_config", {"api_key": "sk-secret-should-not-appear", "model": "m"})
        with open(self.path, encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("sk-secret-should-not-appear", raw)
        self.assertIn("***REDACTED***", raw)
        ok, _ = verify_internal_chain(self.path)
        self.assertTrue(ok)

    def test_redact_keeps_non_string_secretish_fields(self):
        out = redact({"max_tokens": 5, "api_key_present": True, "nested": [{"Authorization": "Bearer x"}]})
        self.assertEqual(out["max_tokens"], 5)
        self.assertIs(out["api_key_present"], True)
        self.assertEqual(out["nested"][0]["Authorization"], "***REDACTED***")

    def test_non_string_key_rejected(self):
        reg = AuditLog(self.path, run_id="run-1", clock=_counter_clock())
        with self.assertRaises(CanonicalizationError):
            reg.append("run_config", {1: "numeric key is ambiguous"})

    def test_nan_and_inf_rejected_and_nothing_written(self):
        reg = AuditLog(self.path, run_id="run-1", clock=_counter_clock())
        with self.assertRaises(CanonicalizationError):
            reg.append("run_config", {"v": math.nan})
        with self.assertRaises(CanonicalizationError):
            reg.append("run_config", {"v": math.inf})
        self.assertEqual(reg.head(), (0, GENESIS_HASH))
        reg.append("run_config", {"v": 1})
        self.assertTrue(verify_internal_chain(self.path)[0])

    def test_refuses_resume_on_tampered_chain(self):
        p = Path(self.tmp) / "r.jsonl"
        log = AuditLog(p, "run1", clock=lambda: "t")
        log.append("x", {"a": 1})
        lines = p.read_text().splitlines()
        rec = json.loads(lines[0])
        rec["payload"]["a"] = 2
        p.write_text(json.dumps(rec) + "\n")
        with self.assertRaises(RuntimeError):
            AuditLog(p, "run1", clock=lambda: "t")


class TestManifestAnchor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "run.jsonl")

    def _build(self, run_id="run-1", texts=("a", "b", "c")):
        if os.path.exists(self.path):
            os.remove(self.path)
        reg = AuditLog(self.path, run_id=run_id, clock=_fixed_clock)
        for t in texts:
            reg.append("response", {"agent_id": "agent", "model_id": "model-A", "text": t})
        _ok, detail = verify_internal_chain(self.path)
        seq, head = reg.head()
        mpath, mhash = write_manifest(
            self.path, run_id, head, seq,
            prev_manifest_hash=None, closed_at_iso=_fixed_clock(),
        )
        return reg, mpath, mhash, detail

    def test_valid_run_passes(self):
        self._build()
        ok, detail = verify_run(self.path)
        self.assertTrue(ok, detail)
        self.assertEqual(detail["records"], 3)

    def test_close_writes_manifest_that_verifies(self):
        reg = AuditLog(self.path, run_id="run-1", clock=_fixed_clock)
        reg.append("x", {"a": 1})
        mpath, mhash = reg.close()
        self.assertEqual(mpath, manifest_path_for(self.path))
        ok, detail = verify_run(self.path, mpath, expected_manifest_hash=mhash)
        self.assertTrue(ok, detail)
        self.assertEqual(detail["records"], 1)
        self.assertEqual(detail["orphan_requests"], [])
        self.assertEqual(detail["errors"], [])

    def test_close_links_previous_manifest(self):
        reg = AuditLog(self.path, run_id="run-1", clock=_fixed_clock)
        reg.append("x", {"a": 1})
        mpath, _ = reg.close(prev_manifest_hash="abc123")
        with open(mpath) as fh:
            self.assertEqual(json.load(fh)["prev_manifest_hash"], "abc123")

    def test_manifest_missing_fails(self):
        self._build()
        os.remove(manifest_path_for(self.path))
        ok, detail = verify_run(self.path)
        self.assertFalse(ok)
        self.assertEqual(detail["reason"], "manifest_missing")

    def test_end_truncation_fails(self):
        self._build()
        with open(self.path, encoding="utf-8") as fh:
            lines = fh.readlines()
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.writelines(lines[:-1])  # drop the last record
        ok, detail = verify_run(self.path)
        self.assertFalse(ok)
        self.assertEqual(detail["reason"], "count_mismatch")

    def test_emptied_file_fails(self):
        self._build()
        open(self.path, "w").close()  # truncate to empty
        ok, detail = verify_run(self.path)
        self.assertFalse(ok)
        self.assertIn(detail["reason"], {"count_zero", "count_mismatch"})

    def test_forward_rewrite_without_manifest_update_fails(self):
        self._build(texts=("a", "b", "c"))
        self._rewrite_only_chain(("x", "y", "z"))
        ok, detail = verify_run(self.path)
        self.assertFalse(ok)
        self.assertEqual(detail["reason"], "head_mismatch")

    def test_forward_rewrite_with_forged_manifest_needs_external_anchor(self):
        _reg, _mpath, mhash_original, _d = self._build(texts=("a", "b", "c"))
        reg2 = self._rewrite_only_chain(("x", "y", "z"))
        seq, head = reg2.head()
        write_manifest(self.path, "run-1", head, seq,
                       prev_manifest_hash=None, closed_at_iso=_fixed_clock())
        ok, _ = verify_run(self.path)       # inherent limit without an external anchor
        self.assertTrue(ok)
        ok2, detail2 = verify_run(self.path, expected_manifest_hash=mhash_original)
        self.assertFalse(ok2)
        self.assertEqual(detail2["reason"], "manifest_hash_mismatch")

    def _rewrite_only_chain(self, texts):
        os.remove(self.path)
        reg = AuditLog(self.path, run_id="run-1", clock=_fixed_clock)
        for t in texts:
            reg.append("response", {"agent_id": "agent", "model_id": "model-A", "text": t})
        return reg

    # --- orphan detection ---
    def test_orphan_request_fails(self):
        p = Path(self.tmp) / "r.jsonl"
        log = AuditLog(p, "run1", clock=lambda: "2026-10-15T00:00:00Z")
        log.append("call_request", {"request_id": "q1", "stage": "recon"})
        mpath, mhash = log.close()
        ok, d = verify_run(p, mpath)
        self.assertFalse(ok)
        self.assertEqual(d["orphan_requests"], [0])

    def test_orphans_allowed_for_crash_inspection(self):
        p = Path(self.tmp) / "r.jsonl"
        log = AuditLog(p, "run1", clock=lambda: "t")
        log.append("call_request", {"request_id": "q1", "stage": "recon"})
        mpath, _ = log.close()
        ok, d = verify_run(p, mpath, allow_orphans=True)
        self.assertTrue(ok, d)
        self.assertEqual(d["orphan_requests"], [0])

    def test_paired_requests_are_not_orphans(self):
        p = Path(self.tmp) / "r.jsonl"
        log = AuditLog(p, "run1", clock=lambda: "t")
        log.append("call_request", {"request_id": "q1"})
        log.append("call_request", {"request_id": "q2"})
        log.append("call_response", {"request_id": "q1", "text": "ok"})
        log.append("call_error", {"request_id": "q2", "error": "boom"})
        mpath, _ = log.close()
        ok, d = verify_run(p, mpath)
        self.assertTrue(ok, d)
        self.assertEqual(d["orphan_requests"], [])

    def test_response_before_request_does_not_pair(self):
        p = Path(self.tmp) / "r.jsonl"
        log = AuditLog(p, "run1", clock=lambda: "t")
        log.append("call_response", {"request_id": "q1", "text": "early"})
        log.append("call_request", {"request_id": "q1"})
        mpath, _ = log.close()
        ok, d = verify_run(p, mpath)
        self.assertFalse(ok)
        self.assertEqual(d["orphan_requests"], [1])

    def test_request_without_request_id_is_an_orphan(self):
        p = Path(self.tmp) / "r.jsonl"
        log = AuditLog(p, "run1", clock=lambda: "t")
        log.append("call_request", {"stage": "recon"})                 # no request_id at all
        log.append("call_request", {"request_id": "q1"})
        log.append("call_response", {"request_id": "q1", "text": "ok"})
        log.append("call_request", "not-a-dict")                       # payload not even an object
        mpath, _ = log.close()
        ok, d = verify_run(p, mpath)
        self.assertFalse(ok)
        self.assertEqual(d["orphan_requests"], [0, 3])
        self.assertIn("orphan_requests", d["errors"])

    # --- CLI ---
    def test_cli_pass(self):
        self._build()
        self.assertEqual(cli_main([self.path]), 0)

    def test_cli_not_found_exit_2(self):
        self.assertEqual(cli_main([os.path.join(self.tmp, "nope.jsonl")]), 2)

    def test_cli_truncated_fails_exit_1(self):
        self._build()
        with open(self.path, encoding="utf-8") as fh:
            lines = fh.readlines()
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.writelines(lines[:-1])
        self.assertEqual(cli_main([self.path]), 1)

    def test_cli_orphan_fails_exit_1(self):
        log = AuditLog(self.path, "run1", clock=_fixed_clock)
        log.append("call_request", {"request_id": "q1"})
        log.close()
        self.assertEqual(cli_main([self.path]), 1)


class TestManifestChainWalker(unittest.TestCase):
    """Cross-run manifest-chain walker: prev_manifest_hash links are actively verified."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _run_manifest(self, name, run_id, texts, prev_manifest_hash):
        path = os.path.join(self.tmp, name)
        reg = AuditLog(path, run_id=run_id, clock=_fixed_clock)
        for t in texts:
            reg.append("response", {"agent_id": "agent", "model_id": "model-A", "text": t})
        return reg.close(prev_manifest_hash=prev_manifest_hash)

    def _three_runs(self):
        m1p, m1h = self._run_manifest("r1.jsonl", "run-1", ("a", "b"), None)
        m2p, m2h = self._run_manifest("r2.jsonl", "run-2", ("c", "d"), m1h)
        m3p, m3h = self._run_manifest("r3.jsonl", "run-3", ("e", "f"), m2h)
        return [m1p, m2p, m3p], [m1h, m2h, m3h]

    def test_valid_chain_passes(self):
        paths, _hashes = self._three_runs()
        ok, detail = verify_manifest_chain(paths)
        self.assertTrue(ok, detail)
        self.assertEqual(detail["count"], 3)

    def test_empty_list_fails_closed(self):
        ok, detail = verify_manifest_chain([])
        self.assertFalse(ok)
        self.assertEqual(detail["reason"], "no_manifests")

    def test_broken_link_fails(self):
        m1p, _ = self._run_manifest("r1.jsonl", "run-1", ("a",), None)
        m2p, _ = self._run_manifest("r2.jsonl", "run-2", ("c",), "deadbeef")
        ok, detail = verify_manifest_chain([m1p, m2p])
        self.assertFalse(ok)
        self.assertEqual(detail["reason"], "prev_manifest_mismatch")
        self.assertEqual(detail["index"], 1)

    def test_reordered_runs_fail(self):
        paths, _hashes = self._three_runs()
        ok, detail = verify_manifest_chain([paths[0], paths[2], paths[1]])
        self.assertFalse(ok)
        self.assertEqual(detail["reason"], "prev_manifest_mismatch")

    def test_missing_manifest_fails(self):
        paths, _hashes = self._three_runs()
        os.remove(paths[1])
        ok, detail = verify_manifest_chain(paths)
        self.assertFalse(ok)
        self.assertEqual(detail["reason"], "manifest_missing")

    def test_wrong_genesis_anchor_fails(self):
        paths, _hashes = self._three_runs()
        ok, detail = verify_manifest_chain(paths, genesis_prev="not-the-genesis")
        self.assertFalse(ok)
        self.assertEqual(detail["reason"], "prev_manifest_mismatch")
        self.assertEqual(detail["index"], 0)


class TestCrashRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / "r.jsonl"

    def test_resume_closes_orphan_requests_and_verifies(self):
        log = AuditLog(self.path, "run1", clock=lambda: "t")
        log.append("call_request", {"request_id": "q1", "stage": "recon"})
        log.append("call_request", {"request_id": "q2"})
        log.append("call_response", {"request_id": "q2", "text": "ok"})
        # process dies here: no close(), q1 never answered
        log2 = AuditLog(self.path, "run1", clock=lambda: "t2")
        recs = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual(len(recs), 4)
        self.assertEqual(recs[3]["record_type"], "call_error")
        self.assertEqual(recs[3]["payload"], {"request_id": "q1",
                                              "error": "abandoned: process restarted before response"})
        mpath, _ = log2.close()
        ok, d = verify_run(self.path, mpath)       # no allow_orphans
        self.assertTrue(ok, d)
        self.assertEqual(d["orphan_requests"], [])

    def test_resume_without_orphans_appends_nothing_and_is_idempotent(self):
        log = AuditLog(self.path, "run1", clock=lambda: "t")
        log.append("call_request", {"request_id": "q1"})
        log.append("call_response", {"request_id": "q1", "text": "ok"})
        AuditLog(self.path, "run1", clock=lambda: "t")
        self.assertEqual(len(self.path.read_text().splitlines()), 2)
        log.append("call_request", {"request_id": "q2"})
        AuditLog(self.path, "run1", clock=lambda: "t")
        AuditLog(self.path, "run1", clock=lambda: "t")      # second resume finds nothing left to close
        self.assertEqual(len(self.path.read_text().splitlines()), 4)

    def test_resume_cannot_close_a_request_without_id(self):
        log = AuditLog(self.path, "run1", clock=lambda: "t")
        log.append("call_request", {"stage": "recon"})
        log2 = AuditLog(self.path, "run1", clock=lambda: "t")
        self.assertEqual(len(self.path.read_text().splitlines()), 1)   # nothing it can honestly pair
        mpath, _ = log2.close()
        self.assertFalse(verify_run(self.path, mpath)[0])

    def test_resume_still_refuses_tampered_chain_before_recovering(self):
        log = AuditLog(self.path, "run1", clock=lambda: "t")
        log.append("call_request", {"request_id": "q1"})
        rec = json.loads(self.path.read_text().splitlines()[0])
        rec["payload"]["request_id"] = "other"
        self.path.write_text(json.dumps(rec) + "\n")
        with self.assertRaises(RuntimeError):
            AuditLog(self.path, "run1", clock=lambda: "t")
        self.assertEqual(len(self.path.read_text().splitlines()), 1)


class TestCorruptFilesFailCleanly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / "r.jsonl"
        log = AuditLog(self.path, "run1", clock=lambda: "t")
        log.append("x", {"a": 1})
        log.append("x", {"a": 2})
        self.mpath, _ = log.close()

    def _corrupt_log_with_bad_utf8(self):
        raw = self.path.read_bytes()
        self.path.write_bytes(raw.replace(b'"a":2', b'"a":\xff2'))

    def test_non_utf8_log_fails_cleanly_in_verify(self):
        self._corrupt_log_with_bad_utf8()
        ok, d = verify_run(self.path, self.mpath)
        self.assertFalse(ok)
        self.assertEqual(d["reason"], "log_invalid")
        self.assertIn("log_invalid", d["errors"])
        self.assertFalse(verify_internal_chain(self.path)[0])
        self.assertEqual(cli_main([str(self.path)]), 1)

    def test_non_utf8_log_refuses_resume_with_runtime_error(self):
        self._corrupt_log_with_bad_utf8()
        with self.assertRaises(RuntimeError):
            AuditLog(self.path, "run1", clock=lambda: "t")

    def test_non_finite_number_in_log_fails_cleanly(self):
        rec = json.loads(self.path.read_text().splitlines()[0])
        line = json.dumps(rec).replace('{"a": 1}', '{"a": NaN}')
        self.path.write_text(line + "\n")
        ok, d = verify_run(self.path, self.mpath)
        self.assertFalse(ok)
        self.assertEqual(d["reason"], "log_invalid")
        with self.assertRaises(RuntimeError):
            AuditLog(self.path, "run1", clock=lambda: "t")

    def _assert_manifest_invalid(self, content: bytes):
        Path(self.mpath).write_bytes(content)
        ok, d = verify_run(self.path, self.mpath)
        self.assertFalse(ok)
        self.assertEqual(d["reason"], "manifest_invalid")
        self.assertIn("manifest_invalid", d["errors"])
        with self.assertRaises(RuntimeError):
            AuditLog(self.path, "run1", clock=lambda: "t")
        ok, d = verify_manifest_chain([self.mpath])
        self.assertFalse(ok)
        self.assertEqual(d["reason"], "manifest_invalid")
        self.assertEqual(cli_main([str(self.path)]), 1)

    def test_corrupt_manifest_json(self):
        self._assert_manifest_invalid(b"{not json")

    def test_manifest_missing_a_field(self):
        m = json.loads(Path(self.mpath).read_text())
        del m["head_hash"]
        self._assert_manifest_invalid(json.dumps(m).encode())

    def test_manifest_not_an_object(self):
        self._assert_manifest_invalid(b"[1, 2]")

    def test_manifest_non_utf8(self):
        self._assert_manifest_invalid(b'{"run_id": "\xff"}')

    def test_manifest_with_non_finite_number(self):
        m = Path(self.mpath).read_text().replace('"record_count":2', '"record_count":NaN')
        self._assert_manifest_invalid(m.encode())

    def test_valid_manifest_still_allows_resume(self):
        AuditLog(self.path, "run1", clock=lambda: "t")    # no raise


if __name__ == "__main__":
    unittest.main()
