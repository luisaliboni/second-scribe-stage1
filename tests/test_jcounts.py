"""cli jcounts: counts, seconds per call and a projection for a judge pilot made by fork. Counts only."""
import contextlib
import io
import json
import os

from second_scribe import jcounts
from second_scribe.cli import main
from tests.test_fork import PILOT, ForkBase


def rec(rt, ts, **pl):
    return {"record_type": rt, "ts": f"2026-10-16T10:00:{ts:06.3f}+00:00", "payload": pl}


class TestCallStats(ForkBase):
    def test_seconds_from_latency_or_from_the_log_gap_and_error_tallies(self):
        recs = [rec("call_request", 0, request_id="a", stage="judge:modality"),
                rec("call_response", 9, request_id="a", latency_ms=2000, validation_error=None,
                    usage={"prompt_tokens": 100, "completion_tokens": 10}),
                rec("call_request", 10, request_id="b", stage="judge:modality"),
                rec("call_response", 14, request_id="b", latency_ms=None, validation_error="bad json"),
                rec("call_request", 20, request_id="c", stage="judge:align:x"),
                rec("call_error", 21, request_id="c", transport=True, error="refused")]
        s = jcounts.call_stats(recs)
        m = s["judge:modality"]
        self.assertEqual((m["calls"], m["replies"], m["invalid_replies"]), (2, 2, 1))
        self.assertEqual(m["seconds_median"], 3.0)          # 2.0 (latency) and 4.0 (gap in the log)
        self.assertEqual(m["seconds_p90"], 4.0)
        self.assertEqual(m["prompt_tokens_median"], 100)
        a = s["judge:align"]
        self.assertEqual((a["calls"], a["errors"], a["transport_errors"], a["seconds_mean"]), (1, 1, 1, None))


class TestPilotCounts(ForkBase):
    def test_after_a_pilot_j_and_m_the_counts_verify_and_project_without_text(self):
        self.q_phase()
        rc, _o, err, pilot = self.fork("--pairs", ",".join(PILOT))
        self.assertEqual(rc, 0, err)
        os.environ.pop("SS_RECON_MODEL")
        for g in ("J", "M"):
            rc, err = self.run_cfg(pilot / "config.json", g)
            self.assertEqual(rc, 0, err)
        res = jcounts.report(pilot / "config.json", self.cfg)
        self.assertEqual(res["verify"], "PASS")
        self.assertEqual((res["pilot_participants"], res["full_participants"]), (3, 12))
        self.assertEqual(res["calls"]["judge:in_transcript"]["calls"], 3)
        self.assertTrue(all(s.startswith("judge:") for s in res["calls"]))      # nothing of the Q prefix is counted
        self.assertEqual(res["projection"]["judge:in_transcript"]["calls"], 12)
        self.assertEqual(res["outcomes"]["in_transcript"]["items"], 3)
        self.assertGreater(res["outcomes"]["judge_modality"]["elements"], 0)
        self.assertGreaterEqual(res["projected_hours_total"], 0)
        self.assertIn("preflight", res["overhead"])
        self.assertEqual([s["stages"][0] for s in res["sessions"]], ["in_transcript", "metrics"])
        so = io.StringIO()
        with contextlib.redirect_stdout(so):
            rc = main(["jcounts", "--config", str(pilot / "config.json"), "--full-config", str(self.cfg)])
        self.assertEqual(rc, 0)
        text = so.getvalue()
        self.assertIn("TOTAL ~", text)
        words = set()
        for f in (pilot / "out" / "work").rglob("*.json"):
            d = json.loads(f.read_text())
            for n in ((d.get("map") or {}).get("nodes") or []):
                words.update(t for t in (n.get("text"), n.get("quote")) if t and len(t) > 6)
        self.assertTrue(words)
        self.assertFalse([w for w in words if w in text])
        with contextlib.redirect_stdout(io.StringIO()) as so2:
            main(["jcounts", "--config", str(pilot / "config.json"), "--full-config", str(self.cfg), "--json"])
        self.assertEqual(json.loads(so2.getvalue())["verify"], "PASS")
