"""Wire-b item 5: the deterministic auditors cover the ladder's work stores (``recon:<rung>``, ``judge_modality:<rung>``)
like the main run's, so a bad rung map or a bad rung judge label blocks a real run too."""
import json
import shutil
import tempfile
import types
import unittest
from pathlib import Path

from second_scribe import audits, pipeline
from second_scribe.audit.registry import AuditLog
from second_scribe.cli import DEMO_CONFIG, build_clients
from second_scribe.pipeline import RunConfig

from tests.test_pipeline_ladder_j import ALL, Base


class TestAuditorsCoverTheRungStores(Base):
    def copy_out(self):
        tmp = tempfile.TemporaryDirectory(prefix="ss-audl-")
        self.addCleanup(tmp.cleanup)
        dst = Path(tmp.name) / "out"
        shutil.copytree(self.lad, dst)
        return dst

    def transcripts(self, out):
        cfg = RunConfig.load(DEMO_CONFIG, out_dir=str(out))
        log = AuditLog(out / "logs" / "aud.jsonl", "a")
        self.addCleanup(log.close)
        r = pipeline._Run(cfg, build_clients("oracle", cfg), log)
        return {p.pid: r.transcript(p) for p in r.people}, cfg

    def recon_audit(self, out):
        trs, cfg = self.transcripts(out)
        return audits.audit_recon_outputs(out / "work", trs, phases=cfg.context["phases"])

    def test_the_clean_run_passes_and_counts_the_rung_maps(self):
        base = audits.audit_recon_outputs(self.base / "work", self.transcripts(self.base)[0],
                                          phases=RunConfig.load(DEMO_CONFIG).context["phases"])
        res = self.recon_audit(self.lad)
        self.assertTrue(res.passed, res.failures)
        # 12 maps x 2 draws x 4 rungs more than the run without a ladder
        self.assertEqual(res.counts["maps_audited"] - base.counts["maps_audited"], 12 * 2 * len(ALL))

    def test_a_rung_map_with_a_quote_that_is_not_in_the_transcript_fails(self):
        out = self.copy_out()
        p = out / "work" / "recon:placebo" / "SYN-OW01__removed__p0__t0.7__r1.json"
        d = json.loads(p.read_text())
        for n in d["map"]["nodes"]:
            n["quote"] = "a sentence that no interviewee ever said in any interview at all"
        for e in d["map"].get("edges", []):
            e["quote"] = "a sentence that no interviewee ever said in any interview at all"      # all > 50% (L16)
        p.write_text(json.dumps(d))
        res = self.recon_audit(out)
        self.assertFalse(res.passed)
        self.assertTrue(any(f.startswith("quote_share_regex:") for f in res.failures), res.failures)
        self.assertTrue(any("placebo" in f for f in res.failures), res.failures)

    def test_a_rung_map_that_breaks_the_schema_fails(self):
        out = self.copy_out()
        p = out / "work" / "recon:rung1" / "SYN-OW02__removed__p0__t0.7__r0.json"
        d = json.loads(p.read_text())
        d["map"]["nodes"][0]["status"] = "maybe"
        p.write_text(json.dumps(d))
        res = self.recon_audit(out)
        self.assertFalse(res.passed)
        self.assertTrue(any(f.startswith("bad_status:") and "rung1" in f for f in res.failures), res.failures)

    def judge_audit(self, out):
        trs, _ = self.transcripts(out)
        return audits.audit_judge_outputs(out / "work", transcripts=trs, prompts_dir=pipeline.PROMPTS_DIR)

    def test_a_rung_judge_item_with_an_unknown_element_fails_and_the_clean_run_passes(self):
        self.assertTrue(self.judge_audit(self.lad).passed)
        out = self.copy_out()
        p = out / "work" / "judge_modality:rung3" / "SYN-OW01__removed__p0__t0.7__r0.json"
        d = json.loads(p.read_text())
        d["labels"]["n999"] = "actual"
        p.write_text(json.dumps(d))
        res = self.judge_audit(out)
        self.assertFalse(res.passed)
        self.assertTrue(any(f.startswith("unknown_element:judge_modality:rung3") for f in res.failures),
                        res.failures)

    def test_a_rung_judge_item_without_its_map_is_an_orphan(self):
        out = self.copy_out()
        (out / "work" / "recon:rung1" / "SYN-OW03__removed__p0__t0.7__r1.json").unlink()
        res = self.judge_audit(out)
        self.assertTrue(any(f.startswith("orphan_judge_item:judge_modality:rung1") for f in res.failures),
                        res.failures)


class TestQuarantineTakesBackTheRungItems(Base):
    def test_a_failed_judge_recheck_takes_back_the_rung_judge_and_align_items_not_the_rung_maps(self):
        tmp = tempfile.TemporaryDirectory(prefix="ss-audq-")
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "out"
        shutil.copytree(self.lad, out)
        ctx = types.SimpleNamespace(cfg=types.SimpleNamespace(out_dir=str(out)), session_start_ns=0, session_id="s1",
                                    log=None, cache_snapshot={})
        n = audits.quarantine_session(ctx, pipeline.STAGE_GROUPS["J"])
        self.assertGreater(n, 0)

        def statuses(store):
            return {json.loads(p.read_text())["status"] for p in (out / "work" / store).glob("*.json")}
        for rung in ALL:
            self.assertEqual(statuses(f"judge_modality:{rung}"), {"quarantined"}, rung)
            self.assertEqual(statuses(f"align:{rung}"), {"quarantined"}, rung)
            self.assertEqual(statuses(f"recon:{rung}"), {"ok"}, rung)          # the Q items stand
        self.assertEqual(statuses("judge_modality"), {"quarantined"})


if __name__ == "__main__":
    unittest.main()
