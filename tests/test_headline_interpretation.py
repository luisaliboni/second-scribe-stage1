"""F1b item 7: the headline's `interpretation` is the metrics block `headline.not_judged.interpretation`, computed
over EVERY spec the headline uses, not the primary spec's alone. Synthetic corpus and fake clients only."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from second_scribe.cli import DEMO_CONFIG, main as cli_main


class TestHeadlineInterpretation(unittest.TestCase):
    def run_clean(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)
        d = json.loads(Path(DEMO_CONFIG).read_text())
        d.update(out_dir=str(self.out / "run"), base_dir=str(Path(DEMO_CONFIG).parent))
        d["stats"] = {"B": 20, "n_perm": 5, "n_derangements": 2, "n_perm_script": 20}
        self.cfg = self.out / "c.json"
        self.cfg.write_text(json.dumps(d))
        self.cli("run", "--config", str(self.cfg), "--stages", "all", "--client", "oracle")
        return self.agg()

    def cli(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli_main(list(argv)), 0)

    def agg(self):
        return json.loads((self.out / "run" / "aggregates" / "aggregates.json").read_text())

    def test_a_non_primary_spec_with_too_many_unjudged_elements_flags_the_headline(self):
        clean = self.run_clean()
        self.assertEqual(clean["headline"]["interpretation"], "interpreted")
        self.assertEqual(clean["headline"]["not_judged"]["interpretation"], "interpreted")
        # a stability variation of the SAME arm (removed, p1): every element of every participant is not judged.
        # (Edited in the work store, then the metrics stage is redone: the oracle's variations share the primary's
        # judge-cache entries, so no live failure can single one spec out.)
        victim = "removed__p1__t0__r0"
        for f in (self.out / "run" / "work" / "judge_modality").glob(f"*__{victim}.json"):
            d = json.loads(f.read_text())
            d["not_judged"] = sorted(d["labels"])
            f.write_text(json.dumps(d))
        self.cli("run", "--config", str(self.cfg), "--stages", "M", "--client", "oracle")
        flagged = self.agg()
        primary = flagged["primary_spec"]
        by = flagged["judging"]["by_spec"]
        self.assertEqual(by[primary]["interpretation"], "interpreted")             # the primary alone is fine
        self.assertEqual(by[victim]["interpretation"], "reported_not_interpreted")
        self.assertEqual(flagged["headline"]["not_judged"]["interpretation"], "reported_not_interpreted")
        self.assertEqual(flagged["headline"]["interpretation"], "reported_not_interpreted")


if __name__ == "__main__":
    unittest.main()
