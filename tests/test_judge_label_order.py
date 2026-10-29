"""R58 (FABLE review 3.6): the generative modality judge can present its six labels (and the six reference examples,
lettered A-F) in reversed order, so label-position bias can be measured on the synthetic traps. Default unchanged."""
import json
import re
import tempfile
import unittest
from pathlib import Path

from second_scribe.audit.registry import AuditLog
from second_scribe.judge import (JudgeCache, judge_modality_detailed, prompt_fingerprint, reverse_label_order,
                                 _render_template)
from second_scribe.llm import MockClient
from second_scribe.prompts import JUDGE_PROMPT_VERSION
from second_scribe.schemas import MODALITIES

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


def bullets(text):
    """The six label lines of the card (the shared definitions above them have bullets of their own)."""
    return re.findall(r"^- (" + "|".join(MODALITIES) + r"):", text, re.M)[-6:]


def examples(text):
    return re.findall(r"^Example ([A-F])\. ELEMENT: (.*?) QUOTE: .*? Label: (\w+)\.$", text, re.M)


class TestReverseLabelOrder(unittest.TestCase):
    def test_v2_labels_and_examples_are_reversed_and_relettered(self):
        fwd, _ = _render_template(PROMPTS, "v2")
        rev = reverse_label_order(fwd)
        self.assertEqual(bullets(fwd), list(MODALITIES))
        self.assertEqual(bullets(rev), list(reversed(MODALITIES)))
        self.assertEqual([e[0] for e in examples(rev)], list("ABCDEF"))                # still lettered A-F in order
        self.assertEqual([e[2] for e in examples(rev)], list(reversed(MODALITIES)))     # first example now "unsupported"
        self.assertEqual([e[1] for e in examples(rev)], list(reversed([e[1] for e in examples(fwd)])))
        self.assertIn("{items}", rev)

    def test_reversing_twice_gives_the_original(self):
        for v in ("v1", "v2", "v3"):
            fwd, _ = _render_template(PROMPTS, v)
            self.assertEqual(reverse_label_order(reverse_label_order(fwd)), fwd)

    def test_only_those_lines_move(self):
        fwd, _ = _render_template(PROMPTS, "v2")
        rev = reverse_label_order(fwd)
        strip = lambda t: sorted(re.sub(r"^Example [A-F]\. ", "Example X. ", l) for l in t.split("\n"))
        self.assertEqual(strip(fwd), strip(rev))

    def test_a_template_without_the_label_block_is_refused(self):
        with self.assertRaises(ValueError):
            reverse_label_order("no labels here {items}")


class TestJudgeCall(unittest.TestCase):
    def run_judge(self, **kw):
        sent = []

        def fn(messages):
            sent.append(next(m for m in messages if m["role"] == "user")["content"])
            return json.dumps({"labels": [{"n": 1, "modality": "actual"}]})
        with tempfile.TemporaryDirectory() as d:
            judge_modality_detailed(AuditLog(Path(d) / "l.jsonl", "t"), MockClient(fn=fn),
                                    [{"element_id": "a", "element": "E", "quote": "we waited six weeks"}], PROMPTS,
                                    item_id="x", **kw)
        return sent[0]

    def test_default_prompt_is_the_forward_prompt(self):
        fwd, _ = _render_template(PROMPTS, JUDGE_PROMPT_VERSION)
        self.assertEqual(bullets(self.run_judge()), list(MODALITIES))
        self.assertEqual(self.run_judge(), self.run_judge(label_order="forward"))
        self.assertEqual(self.run_judge(), fwd.replace("{items}", '1. ELEMENT: E\n   QUOTE: "we waited six weeks"'))

    def test_reversed_prompt_lists_the_labels_backwards(self):
        self.assertEqual(bullets(self.run_judge(label_order="reversed")), list(reversed(MODALITIES)))

    def test_unknown_order_is_refused(self):
        with self.assertRaises(ValueError):
            self.run_judge(label_order="sideways")

    def test_fingerprint_and_cache_keys_differ_by_order(self):
        f, r = prompt_fingerprint(PROMPTS), prompt_fingerprint(PROMPTS, label_order="reversed")
        self.assertEqual(f, prompt_fingerprint(PROMPTS, label_order="forward"))
        self.assertNotEqual(f, r)
        self.assertNotEqual(JudgeCache.key("E", "q", f), JudgeCache.key("E", "q", r))

    def test_a_forward_cache_does_not_answer_the_reversed_run(self):
        with tempfile.TemporaryDirectory() as d:
            cache = JudgeCache(Path(d) / "c.json")
            log = AuditLog(Path(d) / "l.jsonl", "t")
            item = [{"element_id": "a", "element": "E", "quote": "we waited six weeks"}]
            calls = []
            client = MockClient(fn=lambda m: calls.append(1) or json.dumps({"labels": [{"n": 1, "modality": "actual"}]}))
            judge_modality_detailed(log, client, item, PROMPTS, item_id="x", cache=cache)
            judge_modality_detailed(log, client, item, PROMPTS, item_id="x", cache=cache, label_order="reversed")
            self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
