"""Integrity residual of I-F1-5: a hand edit of a work item or of a judge cache label that keeps its request ids
valid must make `verify` fail, because the log (hash chained) carries the sha256 of every item as written. Also D-2
(a real run whose work store is missing fails) and D-3 (a real-run call with no model id is flagged).
Synthetic corpus, fake clients, temp dirs only."""
import json
import shutil
import types
import unittest
from pathlib import Path

from second_scribe import audits, pipeline
from second_scribe.audit import verify as audit_verify
from second_scribe.audit.registry import AuditLog
from tests.test_real_run_validity import Base


def records(log: Path, rtype: str) -> list[dict]:
    return [json.loads(l)["payload"] for l in log.read_text().splitlines() if json.loads(l)["record_type"] == rtype]


class TestWorkItemsAreLogged(Base):
    def test_every_item_and_cache_entry_has_a_work_item_record_with_its_hash(self):
        self.assertEqual(self.run_all()[0], 0)
        last = {(r["stage"], r["name"]): r["sha256"] for r in records(self.log, "work_item")}
        files = sorted(self.work.glob("*/*.json"))
        self.assertTrue(files)
        for f in files:
            obj = json.loads(f.read_text())
            self.assertEqual(last[(f.parent.name, f.stem)], audit_verify.canonical_sha256(obj), f.name)
        cache = json.loads((self.work / "judge_cache.json").read_text())
        self.assertTrue(cache)
        for key, v in cache.items():
            self.assertEqual(last[("cache:judge_cache", key)], audit_verify.canonical_sha256(v))

    def test_the_hash_is_not_stored_in_the_item(self):
        self.assertEqual(self.run_all("Q")[0], 0)
        item = json.loads(next((self.work / "recon").glob("*.json")).read_text())
        self.assertNotIn("sha256", item)


class TestContentEditsAreCaught(Base):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.run_all()[0], 0)
        self.assertEqual(self.cli("verify", str(self.log))[0], 0)       # untouched: PASS, so a failure below is real

    def test_edit_of_a_recon_item_with_valid_request_ids_fails(self):
        f = next((self.work / "recon").glob("*.json"))
        d = json.loads(f.read_text())
        self.assertTrue(d["request_ids"])
        d["map"]["nodes"][0]["text"] = d["map"]["nodes"][0]["text"] + " (edited)"
        f.write_text(json.dumps(d))
        rc, _ = self.cli("verify", str(self.log))
        self.assertEqual(rc, 1)

    def test_reformatting_without_a_content_change_still_passes(self):
        f = next((self.work / "recon").glob("*.json"))
        f.write_text(json.dumps(json.loads(f.read_text()), indent=4))
        self.assertEqual(self.cli("verify", str(self.log))[0], 0)

    def test_a_deleted_item_fails(self):
        next((self.work / "align").glob("*.json")).unlink()
        self.assertEqual(self.cli("verify", str(self.log))[0], 1)

    def test_an_item_added_by_hand_or_left_over_fails(self):
        f = next((self.work / "recon").glob("*.json"))
        shutil.copy(f, f.with_name("stale__copy.json"))
        self.assertEqual(self.cli("verify", str(self.log))[0], 1)

    def test_edit_of_a_judge_cache_label_with_its_request_id_fails(self):
        p = self.work / "judge_cache.json"
        cache = json.loads(p.read_text())
        key = next(k for k, v in cache.items() if isinstance(v, dict))
        cache[key]["modality"] = "wished" if cache[key]["modality"] != "wished" else "actual"
        p.write_text(json.dumps(cache))
        self.assertEqual(self.cli("verify", str(self.log))[0], 1)

    def test_a_cache_entry_added_by_hand_fails(self):
        p = self.work / "judge_cache.json"
        cache = json.loads(p.read_text())
        cache["f" * 64] = next(v for v in cache.values() if isinstance(v, dict))
        p.write_text(json.dumps(cache))
        self.assertEqual(self.cli("verify", str(self.log))[0], 1)


class TestQuarantineStaysVerifiable(Base):
    def test_quarantine_rewrites_and_cache_restores_are_logged(self):
        self.assertEqual(self.run_all()[0], 0)
        cfg = pipeline.RunConfig.load(self.cfg)
        log = AuditLog(self.log, cfg.run_id)
        ctx = types.SimpleNamespace(cfg=cfg, log=log, session_id="s1", session_start_ns=0,
                                    cache_snapshot={"judge_cache.json": None, "judge2_cache.json": None})
        n = audits.quarantine_session(ctx, only=("recon", "judge_modality"))
        log.close()
        self.assertGreater(n, 0)
        self.assertFalse((self.work / "judge_cache.json").exists())
        rc, _ = self.cli("verify", str(self.log))
        self.assertEqual(rc, 0)
        # and a further hand edit of a quarantined item is still caught
        f = next((self.work / "recon").glob("*.json"))
        d = json.loads(f.read_text())
        d["map"] = None
        f.write_text(json.dumps(d))
        self.assertEqual(self.cli("verify", str(self.log))[0], 1)


class TestMissingWorkStoreAndModelId(Base):
    def test_D2_a_real_run_whose_work_store_is_gone_fails(self):
        self.assertEqual(self.run_all()[0], 0)
        shutil.move(str(self.work), str(self.out / "elsewhere"))
        rc, _ = self.cli("verify", str(self.log))
        self.assertEqual(rc, 1)

    def test_D2_message_names_the_missing_store(self):
        self.assertEqual(self.run_all("Q")[0], 0)
        shutil.rmtree(self.work)
        self.assertIn("work store not found", audit_verify.work_integrity_problem(self.log, self.work))

    def _log(self, kind):
        d = Path(self._tmp.name) / f"mini-{kind}"
        (d / "work").mkdir(parents=True)
        (d / "logs").mkdir()
        log = AuditLog(d / "logs" / "run.jsonl", "r")
        log.append("run_start", {"corpus_kind": kind})
        log.append("call_request", {"request_id": "recon:full:p:0:x", "stage": "recon:full", "item_id": "p"})
        log.close()
        return d

    def test_D3_a_real_run_call_without_a_model_id_is_flagged(self):
        d = self._log("real")
        self.assertIn("no model id", audit_verify.work_integrity_problem(d / "logs" / "run.jsonl", d / "work"))

    def test_D3_synthetic_logs_are_not_held_to_it(self):
        d = self._log("synthetic")
        self.assertIsNone(audit_verify.work_integrity_problem(d / "logs" / "run.jsonl", d / "work"))


if __name__ == "__main__":
    unittest.main()
