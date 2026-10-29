"""Review fixes (F1b round 1, I-3 + I-1): the headline's not_judged block and `interpretation` cover EVERY spec the
headline reads (the same-arm grid, the full/removed twins its leakage block reads) and the quote-shuffled re-judge of
the primary. Synthetic corpus, oracle clients."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from second_scribe.cli import DEMO_CONFIG, main as cli_main

PRIMARY = "removed__p0__t0__r0"
TWIN = "full__p0__t0__r0"


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)
        d = json.loads(Path(DEMO_CONFIG).read_text())
        d.update(out_dir=str(self.out / "run"), base_dir=str(Path(DEMO_CONFIG).parent))
        d["stats"] = {"B": 20, "n_perm": 5, "n_derangements": 2, "n_perm_script": 20}
        self.cfg = self.out / "c.json"
        self.cfg.write_text(json.dumps(d))
        self.cli("run", "--config", str(self.cfg), "--stages", "all", "--client", "oracle")

    def cli(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli_main(list(argv)), 0)

    def agg(self):
        return json.loads((self.out / "run" / "aggregates" / "aggregates.json").read_text())

    def unjudge(self, stage, spec):
        files = list((self.out / "run" / "work" / stage).glob(f"*__{spec}.json"))
        self.assertTrue(files, stage)
        for f in files:
            d = json.loads(f.read_text())
            d["not_judged"] = sorted(d["labels"] or {})
            f.write_text(json.dumps(d))

    def redo_metrics(self):
        self.cli("run", "--config", str(self.cfg), "--stages", "M", "--client", "oracle")
        return self.agg()


class TestHeadlineReadsTheTwinArm(Base):
    def test_the_clean_run_is_interpreted_everywhere(self):
        a = self.agg()
        self.assertEqual(a["headline"]["interpretation"], "interpreted")
        self.assertEqual(a["other_arms"]["full"]["interpretation"], "interpreted")

    def test_an_unjudged_twin_arm_spec_flags_the_headline_that_reads_it(self):
        self.unjudge("judge_modality", TWIN)
        a = self.redo_metrics()
        self.assertEqual(a["judging"]["by_spec"][TWIN]["interpretation"], "reported_not_interpreted")
        self.assertEqual(a["judging"]["by_spec"][PRIMARY]["interpretation"], "interpreted")
        h = a["headline"]
        self.assertIn(TWIN, h["not_judged"]["by_spec"])                      # the block lists what the leakage reads
        self.assertEqual(h["not_judged"]["interpretation"], "reported_not_interpreted")
        self.assertEqual(h["interpretation"], "reported_not_interpreted")
        self.assertEqual(a["other_arms"]["full"]["interpretation"], "reported_not_interpreted")

    def test_the_other_arms_interpretation_follows_its_own_specs(self):
        self.unjudge("judge_modality", "removed__p1__t0__r0")                 # a removed-arm variation
        a = self.redo_metrics()
        self.assertEqual(a["headline"]["interpretation"], "reported_not_interpreted")
        # the full-arm headline reads its removed twin of the same variant, so it is flagged through the leakage block
        self.assertEqual(a["other_arms"]["full"]["interpretation"], "reported_not_interpreted")


class TestHeadlineReadsTheShuffledControl(Base):
    def test_an_unjudged_shuffled_rejudge_flags_the_headline(self):
        self.unjudge("judge_shuffled", PRIMARY)
        a = self.redo_metrics()
        self.assertGreater(a["judging"]["by_spec"][PRIMARY]["shuffled"]["not_judged_share"], 0.10)
        self.assertEqual(a["judging"]["by_spec"][PRIMARY]["interpretation"], "reported_not_interpreted")
        self.assertEqual(a["headline"]["interpretation"], "reported_not_interpreted")


if __name__ == "__main__":
    unittest.main()
