"""R58 + R57 (FABLE review 3.6): the generative bench can present the labels in reversed order and report agreement
between the two orders; the output leads with the C3 floors, the in_transcript (off-record) rows first. Fake models only."""
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from second_scribe import judge_bench as jb
from second_scribe.audit.registry import AuditLog
from second_scribe.cli import main as cli_main
from second_scribe.judge_bench import OracleClient, bench, render_markdown
from second_scribe.llm import ModelResponse
from second_scribe.schemas import MODALITIES

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "demo" / "corpus"
PROMPTS = ROOT / "prompts"


def tmpdir(case):
    d = tempfile.mkdtemp()
    case.addCleanup(shutil.rmtree, d, True)
    return d


class PositionBiased(OracleClient):
    """The oracle, except that every 4th item of a batch gets the label listed FIRST in the prompt (a judge that leans
    on position): 'actual' with the forward card, 'unsupported' with the reversed one."""

    def complete(self, messages, *, temperature, seed, max_tokens, params=None):
        r = super().complete(messages, temperature=temperature, seed=seed, max_tokens=max_tokens, params=params)
        p = messages[-1]["content"]
        if "ITEMS:\n" not in p:
            return r
        card = p.split("Labels (use exactly one per item):\n", 1)[1]
        first = card.split("\n", 1)[0].split(":", 1)[0].lstrip("- ")
        labels = json.loads(r.text)["labels"]
        for row in labels:
            if row["n"] % 4 == 0:
                row["modality"] = first
        return ModelResponse(json.dumps({"labels": labels}), "stop", {}, 0, "biased")


class TestBenchOrder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)

    def run_bench(self, name, client, **kw):
        return bench(client, PROMPTS, CORPUS, AuditLog(Path(self.tmp) / f"{name}.jsonl", name, clock=lambda: "t"),
                     mode="generative", **kw)

    def test_default_is_forward_with_no_order_check(self):
        r = self.run_bench("default", OracleClient(CORPUS))
        self.assertEqual(r["label_order"], "forward")
        self.assertNotIn("order_check", r)
        self.assertEqual(r["modality"]["accuracy"], 1.0)

    def test_both_orders_with_a_position_free_judge_agree_completely(self):
        r = self.run_bench("both_oracle", OracleClient(CORPUS), label_order="both")
        oc = r["order_check"]
        self.assertEqual(r["label_order"], "both")
        self.assertEqual((oc["agreement"], oc["n_disagree"]), (1.0, 0))
        self.assertEqual((oc["accuracy_forward"], oc["accuracy_reversed"]), (1.0, 1.0))
        self.assertEqual(oc["n_judged_both"], oc["n_items"])
        self.assertEqual(oc["first_listed"], {"forward": "actual", "reversed": "unsupported"})
        self.assertEqual(r["modality"]["accuracy"], 1.0)                 # the scored results are the forward ones
        json.dumps(r)
        lines = (Path(self.tmp) / "both_oracle.jsonl").read_text().splitlines()
        self.assertTrue(any(":reversed:" in ln or ":reversed\"" in ln for ln in lines))   # the reversed calls are logged

    def test_a_position_biased_judge_shows_lower_agreement_and_a_first_listed_lean(self):
        r = self.run_bench("both_biased", PositionBiased(CORPUS), label_order="both")
        oc = r["order_check"]
        self.assertLess(oc["agreement"], 1.0)
        self.assertGreater(oc["n_disagree"], 0)
        self.assertGreater(oc["pred_first_listed_share"]["forward"], 0.1)   # many answers are "actual"
        self.assertGreater(oc["pred_first_listed_share"]["reversed"], 0.1)  # ...and many are "unsupported" instead
        self.assertIn("mcnemar_exact_p", oc)

    def test_reversed_alone_scores_the_whole_bench_with_the_reversed_card(self):
        sent = []

        class Spy(OracleClient):
            def complete(self, messages, **kw):
                sent.append(messages[-1]["content"])
                return super().complete(messages, **kw)
        r = self.run_bench("rev", Spy(CORPUS), label_order="reversed")
        self.assertEqual((r["label_order"], r["modality"]["accuracy"]), ("reversed", 1.0))
        cards = [p for p in sent if "Labels (use exactly one per item):" in p]
        self.assertTrue(cards)
        self.assertTrue(all(c.split("Labels (use exactly one per item):\n", 1)[1].startswith("- unsupported:")
                            for c in cards))

    def test_letter_mode_has_no_label_order_and_unknown_orders_are_refused(self):
        log = AuditLog(Path(self.tmp) / "x.jsonl", "x", clock=lambda: "t")
        with self.assertRaises(ValueError):
            bench(OracleClient(CORPUS), PROMPTS, CORPUS, log, mode="logprob", label_order="reversed")
        with self.assertRaises(ValueError):
            bench(OracleClient(CORPUS), PROMPTS, CORPUS, log, mode="generative", label_order="sideways")


class TestFloorsFirst(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, tmp, True)
        cls.r = bench(OracleClient(CORPUS), PROMPTS, CORPUS, AuditLog(Path(tmp) / "l.jsonl", "l", clock=lambda: "t"),
                      mode="generative", label_order="both")

    def test_c3_floors_lists_in_transcript_first_with_the_pass_constants(self):
        rows = jb.c3_floors(self.r)
        self.assertEqual([x["floor"].split(":")[0] for x in rows],
                         ["in_transcript"] * 3 + ["modality"] * 2)
        self.assertEqual([x["rule"] for x in rows], [f">= {jb.PASS_OFF_RECORD_MIN_N:g}",
                         f">= {jb.PASS_OFF_RECORD_MENTION_RECALL:g}", f">= {jb.PASS_OFF_RECORD_RECALL:g}",
                         f">= {jb.PASS_ACTUAL_RECALL:g}", f">= {jb.PASS_WISHED_PRECISION:g}"])
        self.assertEqual((jb.PASS_OFF_RECORD_MIN_N, jb.PASS_OFF_RECORD_MENTION_RECALL, jb.PASS_ACTUAL_RECALL,
                          jb.PASS_WISHED_PRECISION), (100, 0.85, 0.85, 0.80))
        self.assertTrue(all(x["ok"] for x in rows[1:]))       # the oracle clears every rate floor
        self.assertFalse(rows[0]["ok"])                       # one corpus: the pool floor of 100 is not met, and says so

    def test_c3_floors_survives_missing_values(self):
        rows = jb.c3_floors({"off_record": {}, "modality": {}})
        self.assertEqual([x["ok"] for x in rows], [None] * 5)

    def test_markdown_leads_with_the_floors_in_transcript_first_and_has_the_order_section(self):
        md = render_markdown(self.r)
        i_off = md.index("in_transcript: off-record mention recall")
        i_act = md.index("modality: recall on actual")
        self.assertLess(md.index("## C3 floors"), i_off)
        self.assertLess(md.index("in_transcript: off-record set size"), i_off)
        self.assertLess(i_off, i_act)
        self.assertLess(i_act, md.index("Modality accuracy (item-level Wilson)"))
        self.assertIn("## Label-order check", md)
        self.assertIn("Agreement between the two orders", md)
        self.assertIn("## Per corpus", md)

    def test_cli_generative_runs_end_to_end_and_prints_in_transcript_first(self):
        out = Path(tmpdir(self))
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_main(["bench", "--mode", "generative", "--out", str(out), "--client", "oracle",
                             "--seeds", "1", "--label-order", "both"])
        self.assertEqual(code, 0)
        lines = buf.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("in_transcript: off-record set size"), lines[:2])
        self.assertTrue(lines[1].startswith("in_transcript: off-record mention recall"))
        self.assertTrue(any(ln.startswith("label order forward vs reversed: agreement 1.000") for ln in lines), lines)
        res = json.loads((out / "bench.generative.json").read_text())
        self.assertEqual(res["mode"], "generative")
        self.assertIn("order_check", res)
        self.assertTrue((out / "bench.generative.md").exists())

    def test_cli_refuses_a_label_order_in_letter_mode(self):
        out = Path(tmpdir(self))
        with redirect_stdout(io.StringIO()):
            code = cli_main(["bench", "--mode", "logprob", "--out", str(out), "--client", "oracle", "--seeds", "1",
                             "--label-order", "reversed"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
