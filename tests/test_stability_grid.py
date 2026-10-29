"""A1: the claim-stability grid varies the prompt variant and temperature WITHIN the primary's arm (p1, p2, T0.7 of
the same arm). The other arm is computed and reported separately, never mixed into the primary's grid. Synthetic
corpus, oracle clients."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from second_scribe.cli import DEMO_CONFIG, main as cli_main


def cross_arm(paths, needle):
    """Paths that name ``needle``, except the `leakage` block, which is by design the full-vs-removed contrast (the
    mapping-talk ablation) and names both arms in every headline, and the `not_judged` block, which lists the twin
    specs the leakage block reads (F1b round 1)."""
    return [p for p in paths if needle in p and not p.startswith(("/leakage", "/not_judged"))]


def walk(x, path=""):
    if isinstance(x, dict):
        for k, v in x.items():
            yield f"{path}/{k}"
            yield from walk(v, f"{path}/{k}")
    elif isinstance(x, list):
        for i, v in enumerate(x):
            yield from walk(v, f"{path}[{i}]")
    elif isinstance(x, str):
        yield f"{path}={x}"


class TestStabilityGrid(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._t = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._t.cleanup)
        out = Path(cls._t.name)
        d = json.loads(Path(DEMO_CONFIG).read_text())
        d.update(out_dir=str(out / "run"), base_dir=str(Path(DEMO_CONFIG).parent))
        d["specs"].append({"arm": "removed", "variant": "p0", "temperature": 0.7, "replicate": 0})
        d["specs"].append({"arm": "full", "variant": "p0", "temperature": 0.7, "replicate": 0})
        d["stats"] = {"B": 20, "n_perm": 5, "n_derangements": 2, "n_perm_script": 20}
        cfg = out / "c.json"
        cfg.write_text(json.dumps(d))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = cli_main(["run", "--config", str(cfg), "--stages", "all", "--client", "oracle"])
        assert rc == 0
        cls.agg = json.loads((out / "run" / "aggregates" / "aggregates.json").read_text())

    def test_the_primary_headline_varies_only_within_its_arm(self):
        h = self.agg["headline"]
        self.assertEqual(h["primary_spec"], "removed__p0__t0__r0")
        self.assertEqual(h["variation_specs"], ["removed__p1__t0__r0", "removed__p2__t0__r0", "removed__p0__t0.7__r0"])
        self.assertEqual(cross_arm(walk(h), "full__"), [])          # no full-arm spec in the primary's grid or rows

    def test_the_other_arm_is_reported_separately_with_its_own_primary(self):
        other = self.agg["other_arms"]
        self.assertEqual(set(other), {"full"})
        self.assertEqual(other["full"]["primary_spec"], "full__p0__t0__r0")
        self.assertEqual(other["full"]["variation_specs"], ["full__p1__t0__r0", "full__p2__t0__r0", "full__p0__t0.7__r0"])
        self.assertEqual(cross_arm(walk(other["full"]), "removed__"), [])
        self.assertEqual(other["full"]["n_participants"], self.agg["headline"]["n_participants"])

    def test_every_spec_still_has_its_pooled_rows(self):
        self.assertEqual(len(self.agg["specs"]), 8)
        self.assertEqual(set(self.agg["buckets"]["by_spec"]), set(self.agg["specs"]))


class TestPrimaryFullArm(unittest.TestCase):
    def test_a_full_arm_primary_gets_the_removed_arm_as_the_other(self):
        import tempfile as _tf
        with _tf.TemporaryDirectory() as t:
            out = Path(t)
            d = json.loads(Path(DEMO_CONFIG).read_text())
            d.update(out_dir=str(out / "run"), base_dir=str(Path(DEMO_CONFIG).parent))
            d["specs"] = [s for s in d["specs"] if s["variant"] == "p0"][::-1]       # full p0 first, removed p0 second
            d["stats"] = {"B": 20, "n_perm": 5, "n_derangements": 2, "n_perm_script": 20}
            cfg = out / "c.json"
            cfg.write_text(json.dumps(d))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli_main(["run", "--config", str(cfg), "--stages", "all", "--client", "oracle"]), 0)
            agg = json.loads((out / "run" / "aggregates" / "aggregates.json").read_text())
            self.assertEqual(agg["headline"]["variation_specs"], [])
            self.assertEqual(set(agg["other_arms"]), {"removed"})


class TestGridIsReported(unittest.TestCase):
    """Review fix I-2: the grid the claims rest on is the primary arm's p0/p1/p2 at T0 and p0 at T0.7; a missing spec
    is reported and the stability claims fail closed (rule 7), never silently shrunk."""

    def run_with(self, extra_specs):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        out = Path(t.name)
        d = json.loads(Path(DEMO_CONFIG).read_text())
        d.update(out_dir=str(out / "run"), base_dir=str(Path(DEMO_CONFIG).parent))
        d["specs"] += extra_specs
        d["stats"] = {"B": 20, "n_perm": 5, "n_derangements": 2, "n_perm_script": 20}
        cfg = out / "c.json"
        cfg.write_text(json.dumps(d))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli_main(["run", "--config", str(cfg), "--stages", "all", "--client", "oracle"]), 0)
        return json.loads((out / "run" / "aggregates" / "aggregates.json").read_text())

    def test_a_grid_without_the_temperature_spec_is_reported_and_the_claims_fail_closed(self):
        agg = self.run_with([])                              # the demo config has no T0.7 spec
        g = agg["headline"]["stability_grid"]
        self.assertEqual(g["missing"], ["removed__p0__t0.7__r0"])
        self.assertIn("removed__p1__t0__r0", g["present"])
        self.assertEqual(len(g["expected"]), 4)
        pull = agg["headline"]["pull_toward_script"]
        self.assertIs(pull["claim_pulled_toward_script"], False)
        self.assertIs(pull["in_transcript_only"]["claim_pulled_toward_script"], False)
        self.assertEqual(pull["claim_blocked"], "grid_incomplete")
        self.assertEqual(agg["other_arms"]["full"]["stability_grid"]["missing"], ["full__p0__t0.7__r0"])

    def test_a_complete_grid_reports_nothing_missing_and_blocks_nothing(self):
        agg = self.run_with([{"arm": a, "variant": "p0", "temperature": 0.7, "replicate": 0}
                             for a in ("removed", "full")])
        for h in (agg["headline"], agg["other_arms"]["full"]):
            self.assertEqual(h["stability_grid"]["missing"], [])
            self.assertEqual(h["stability_grid"]["expected"], h["stability_grid"]["present"])
        self.assertNotIn("claim_blocked", agg["headline"]["pull_toward_script"])


class TestTemplateHasTheWholeGrid(unittest.TestCase):
    def test_the_real_template_lists_p0_p1_p2_and_the_temperature_spec_for_both_arms(self):
        from second_scribe.pipeline import KIT_ROOT
        specs = json.loads((Path(KIT_ROOT) / "configs" / "real.template.json").read_text())["specs"]
        have = {(s["arm"], s["variant"], s["temperature"], s["replicate"]) for s in specs}
        for arm in ("removed", "full"):
            for v, t in (("p0", 0.0), ("p1", 0.0), ("p2", 0.0), ("p0", 0.7)):
                self.assertIn((arm, v, t, 0), have)
        self.assertEqual((specs[0]["arm"], specs[0]["variant"], specs[0]["temperature"]), ("removed", "p0", 0.0))


if __name__ == "__main__":
    unittest.main()
