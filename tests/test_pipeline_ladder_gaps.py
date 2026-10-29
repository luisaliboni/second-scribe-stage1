"""Final review fixes C-1 / F2: a ladder rung whose judge labels or alignments are missing (the judge was down, or
an item fell back) is an INCOMPLETE rung, never a rung of parse failures. Synthetic demo corpus, the oracle, temp dirs."""
import json
import unittest
from unittest import mock

from second_scribe import metrics, pipeline
from second_scribe.llm import TransportFailure
from tests.test_pipeline_ladder_q import ALL, N_PEOPLE, Base

STAGES = ("Q", "J", "M")


class Down:
    model_label = "oracle"

    def complete(self, messages, **kw):
        raise TransportFailure("model call to 127.0.0.1:1 failed after 4 attempts: ConnectionRefusedError")


class Gaps(Base):
    def cfg1(self):
        return self.cfg(ladder_cfg={"rungs": ALL, "draws": 1, "n_perm": 50, "B": 50})

    def aggregates(self):
        return json.loads((self.out / "aggregates" / "aggregates.json").read_text(encoding="utf-8"))

    def item(self, stage, name):
        return self.out / "work" / stage / f"{name}.json"

    def damage(self, stage, count, status):
        files = sorted((self.out / "work" / stage).glob("*.json"))
        hit = [f for f in files if "__ai_" in f.name or stage.startswith("judge")][:count]
        self.assertEqual(len(hit), count)
        for f in hit:
            d = json.loads(f.read_text(encoding="utf-8"))
            d["status"] = status
            f.write_text(json.dumps(d), encoding="utf-8")
        return hit


class TestJudgeOutageOnARung(Gaps):
    """C-1 (probe_judge_outage.py): the judge is down for the placebo rung's maps."""

    def run_down(self):
        orig = pipeline.rung_view

        def view(r, key):
            v = orig(r, key)
            if key == "placebo":
                v.clients = {**v.clients, "judge": Down()}
            return v
        with mock.patch.object(pipeline, "rung_view", view):
            self.go(self.cfg1(), stages=STAGES)
        return self.aggregates()

    def test_the_outage_is_counted_and_the_run_and_the_ladder_are_incomplete(self):
        agg = self.run_down()
        placebo_items = list((self.out / "work" / "align:placebo").glob("*.json"))
        self.assertEqual({json.loads(f.read_text())["status"] for f in placebo_items}, {"timeout_excluded"})
        out = agg["outage"]
        self.assertEqual(out["timeout_excluded_items"], len(placebo_items))
        self.assertEqual(out["by_stage"], {"align:placebo": len(placebo_items)})
        self.assertEqual(agg["headline"]["interpretation"], "incomplete_run")
        lad = agg["ladder"]
        self.assertIs(lad["complete"], False)

    def test_no_verdict_and_no_contrast_rests_on_the_missing_rung(self):
        """Every map has a gap in the placebo rung, so no map is left in any arm: no report, no verdict (it used to be
        'null' with a remedy-placebo difference of +1.0 and CI [1, 1]: the placebo's maps scored as all missed)."""
        lad = self.run_down()["ladder"]
        self.assertIsNone(lad["report"])
        self.assertEqual(lad["per_rung"]["placebo"]["j_inputs_missing"], N_PEOPLE)
        self.assertEqual(lad["per_rung"]["rung3"]["j_inputs_missing"], 0)


class TestMissingRungInputsAreNotParseFailures(Gaps):
    """F2: a rung map whose judge item or AI-endorsed alignment is missing, timed out or fell back."""

    def baseline(self):
        cfg = self.cfg1()
        self.go(cfg, stages=STAGES)
        return self.aggregates()["ladder"]

    def rerun_metrics(self):
        self.go(self.cfg1(), stages=("M",))
        return self.aggregates()["ladder"]

    def test_a_timed_out_rung_judge_item_leaves_parse_failures_unchanged_and_the_ladder_incomplete(self):
        base = self.baseline()
        self.assertIs(base["complete"], True)
        before = base["report"]["rungs"]["remedy"]["parse_failures"]
        self.damage("judge_modality:rung3", 2, "timeout_excluded")
        lad = self.rerun_metrics()
        self.assertEqual(lad["report"]["rungs"]["remedy"]["parse_failures"], before)
        self.assertIs(lad["complete"], False)
        self.assertEqual(lad["per_rung"]["rung3"]["j_inputs_missing"], 2)
        self.assertEqual(lad["report"]["verdict"]["verdict"], "incomplete")
        self.assertEqual(self.aggregates()["outage"]["by_stage"].get("judge_modality:rung3"), 2)
        self.assertEqual(self.aggregates()["headline"]["interpretation"], "incomplete_run")

    def test_the_two_maps_are_dropped_from_every_arm_so_the_pairing_holds(self):
        self.baseline()
        self.damage("judge_modality:rung3", 2, "timeout_excluded")
        rep = self.rerun_metrics()["report"]
        for rung in ("remedy", "placebo", "rung2", "rung1"):
            with self.subTest(rung=rung):
                self.assertEqual(rep["rungs"][rung]["n_maps"], N_PEOPLE - 2)

    def test_an_alignment_that_fell_back_makes_the_ladder_incomplete_not_a_parse_failure(self):
        base = self.baseline()
        before = base["report"]["rungs"]["placebo"]["parse_failures"]
        hit = self.damage("align:placebo", 1, "fallback")
        self.assertTrue(hit[0].exists())
        lad = self.rerun_metrics()
        self.assertEqual(lad["report"]["rungs"]["placebo"]["parse_failures"], before)
        self.assertIs(lad["complete"], False)
        self.assertEqual(lad["per_rung"]["placebo"]["j_inputs_missing"], 1)

    def test_a_recon_that_failed_is_still_a_parse_failure(self):
        base = self.baseline()
        self.assertIs(base["complete"], True)
        files = sorted((self.out / "work" / "recon:rung3").glob("*.json"))[:1]
        d = json.loads(files[0].read_text())
        d.update(status="failed", map=None)
        files[0].write_text(json.dumps(d))
        lad = self.rerun_metrics()
        self.assertIs(lad["complete"], True)
        self.assertEqual(lad["report"]["rungs"]["remedy"]["parse_failures"],
                         base["report"]["rungs"]["remedy"]["parse_failures"] + 1)


class TestTransportErrorColumn(Gaps):
    """M-3: ``per_rung.transport_errors`` counts attempts that ended in a transport error, not every call_error."""

    def test_an_http_400_is_not_a_transport_error(self):
        marker = "how-to pages on the city website with the help of a friend"

        def refuse(messages):
            if any(marker in (m.get("content") or "") for m in messages):
                return RuntimeError("model call to 127.0.0.1:1 failed: HTTP 400: the request exceeds the available "
                                    "context size")
        self.go(self.cfg1(), stages=STAGES, fail=refuse)
        per_rung = self.aggregates()["ladder"]["per_rung"]
        for rung in ALL:
            with self.subTest(rung=rung):
                self.assertEqual(per_rung[rung]["transport_errors"], 0)


class TestNotJudgedShareOfARung(Gaps):
    """F3: the 10% not-judged rule of the main headline applies to the rungs too."""

    def leave_unjudged(self, rung, per_item):
        """The judge answered but left ``per_item`` labels of every item of the rung unjudged (the judge's own cache
        would otherwise serve the oracle's labels, so the stored items are rewritten)."""
        for f in (self.out / "work" / f"judge_modality:{rung}").glob("*.json"):
            d = json.loads(f.read_text(encoding="utf-8"))
            d["not_judged"] = sorted(d["labels"])[:per_item]
            f.write_text(json.dumps(d), encoding="utf-8")

    def test_a_rung_the_judge_left_mostly_unjudged_is_reported_not_interpreted(self):
        self.go(self.cfg1(), stages=STAGES)
        self.leave_unjudged("rung3", 4)
        self.go(self.cfg1(), stages=("M",))
        lad = self.aggregates()["ladder"]
        self.assertIs(lad["complete"], True)
        self.assertGreater(lad["per_rung"]["rung3"]["not_judged_share"], pipeline.NOT_JUDGED_LIMIT)
        self.assertEqual(lad["per_rung"]["placebo"]["not_judged_share"], 0.0)
        self.assertEqual(lad["interpretation"], "reported_not_interpreted")
        self.assertEqual(lad["report"]["verdict"]["verdict"], "reported_not_interpreted")
        self.assertEqual(lad["report"]["verdict"]["reason_codes"], ["not_judged_over_limit"])

    def test_a_clean_ladder_is_interpreted_and_keeps_its_verdict(self):
        self.go(self.cfg1(), stages=STAGES)
        lad = self.aggregates()["ladder"]
        self.assertEqual(lad["interpretation"], "interpreted")
        self.assertEqual({r["not_judged_share"] for r in lad["per_rung"].values()}, {0.0})
        self.assertNotIn(lad["report"]["verdict"]["verdict"], ("incomplete", "reported_not_interpreted"))

    def test_an_incomplete_ladder_is_incomplete_run(self):
        self.go(self.cfg1(), stages=STAGES)
        self.damage("judge_modality:rung3", 1, "timeout_excluded")
        self.go(self.cfg1(), stages=("M",))
        self.assertEqual(self.aggregates()["ladder"]["interpretation"], "incomplete_run")


class TestLadderScriptSource(unittest.TestCase):
    """F4: the ladder's script-conditional gap rests on the same primary source as the headline."""

    def src(self, name, family):
        return metrics.ScriptSource(name, family, {})

    def test_the_first_other_family_source_is_the_primary_else_the_own_script(self):
        own = self.src("own", "own")
        judge_alias = self.src("judgescript", "own")        # configured first, but its family is not established
        other = self.src("gemma", "other")
        self.assertIs(pipeline.primary_script_source([judge_alias, other], own), other)
        self.assertIs(pipeline.primary_script_source([judge_alias], own), own)
        self.assertIs(pipeline.primary_script_source(None, own), own)
        self.assertIs(pipeline.primary_script_source([], own), own)

    def test_the_ladder_section_records_which_script_it_rests_on(self):
        tc = Gaps("aggregates")
        tc.setUp()
        try:
            tc.go(tc.cfg1(), stages=STAGES)
            self.assertEqual(tc.aggregates()["ladder"]["script_source"], {"name": "own", "family": "own"})
        finally:
            tc.doCleanups()


if __name__ == "__main__":
    unittest.main()
