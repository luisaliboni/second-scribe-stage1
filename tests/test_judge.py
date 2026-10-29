import glob
import json
import os
import re
import tempfile
import types
import unittest
from pathlib import Path

from second_scribe.audit.registry import AuditLog
from second_scribe.judge import (JudgeCache, attested, judge_modality, judge_modality_detailed, modality_items,
                                 prompt_fingerprint)
from second_scribe.llm import MockClient
from second_scribe.prompts import JUDGE_PROMPT_VERSION
from second_scribe.reconstruct import load_definitions, load_system
from second_scribe.transcripts import read_docx_text
from second_scribe.provenance import provenance_for
from second_scribe.schemas import MODALITIES, Edge, MapGraph, Node, Transcript, Turn

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


def parse_items(messages):
    """[(n, element, quote)] from the user message sent to the model."""
    body = next(m for m in messages if m["role"] == "user")["content"].split("ITEMS:", 1)[1]
    body = body.split("\nREMINDER:")[0]
    return [(int(n), e, q) for n, e, q in
            re.findall(r'^(\d+)\. ELEMENT: (.*)\n   QUOTE(?: \((?:participant|interviewer)\))?: "(.*)"$', body, re.M)]


def label_for(quote):
    if "wish" in quote:
        return "wished"
    if "never" in quote:
        return "negated"
    if "cousin" in quote:
        return "other_person"
    if "if " in quote:
        return "hypothetical"
    return "actual"


def keyword_fn(calls=None):
    def fn(messages):
        items = parse_items(messages)
        if calls is not None:
            calls.append(items)
        return json.dumps({"labels": [{"n": n, "modality": label_for(q)} for n, _e, q in items]})
    return fn


def make_items(n, quote="we waited six weeks for the health unit"):
    return [{"element_id": f"n{k}", "element": f"Element {k}", "quote": f"{quote} {k}"} for k in range(n)]


class TestJudge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = AuditLog(Path(self.tmp) / "r.jsonl", "run", clock=lambda: "t")

    def records(self, record_type=None):
        recs = [json.loads(l) for l in (Path(self.tmp) / "r.jsonl").read_text().splitlines()]
        return [r for r in recs if record_type is None or r["record_type"] == record_type]

    def test_45_items_are_three_calls_and_ids_map_back(self):
        calls = []
        items = []
        for k in range(45):
            q = ["we wish there had been a bridge", "we never got a letter", "my cousin saw the doctor",
                 "if the clinic called we would go", "we waited six weeks"][k % 5]
            items.append({"element_id": f"n{k}", "element": f"E{k}", "quote": f"{q} {k}"})
        out = judge_modality(self.log, MockClient(fn=keyword_fn(calls)), items, PROMPTS, item_id="SYN-1")
        self.assertEqual([len(c) for c in calls], [20, 20, 5])
        self.assertEqual(len(out), 45)
        want = ["wished", "negated", "other_person", "hypothetical", "actual"]
        for k in range(45):
            self.assertEqual(out[f"n{k}"], want[k % 5], k)
        self.assertTrue(all(v in MODALITIES for v in out.values()))
        self.assertEqual(len(self.records("call_request")), 3)

    def test_numbering_restarts_per_batch_and_edge_text_is_rendered(self):
        calls = []
        items = [{"element_id": "e:a->b", "element": "Intake → Referral", "quote": "they sent us on"}]
        judge_modality(self.log, MockClient(fn=keyword_fn(calls)), items, PROMPTS, item_id="S")
        self.assertEqual(calls[0], [(1, "Intake → Referral", "they sent us on")])

    def test_prompt_is_the_file_and_braces_in_quotes_survive(self):
        seen = []
        items = [{"element_id": "n0", "element": "X {items} {0}", "quote": "I said {items} and {n} {"}]
        client = MockClient(fn=lambda m: seen.append(m[-1]["content"]) or keyword_fn()(m))
        out = judge_modality(self.log, client, items, PROMPTS, item_id="S")
        self.assertEqual(out, {"n0": "actual"})
        template = (PROMPTS / f"judge_modality.{JUDGE_PROMPT_VERSION}.txt").read_text(encoding="utf-8")   # the default
        self.assertTrue(seen[0].startswith(template.split("{definitions}")[0]))
        self.assertTrue(seen[0].endswith(template.split("{items}")[1]))
        self.assertIn('QUOTE: "I said {items} and {n} {"', seen[0])
        self.assertIn('{"labels": [{"n": <number>, "modality": "<label>"}]}', seen[0])

    def test_request_parameters(self):
        from second_scribe.llm import derive_seed
        judge_modality(self.log, MockClient(fn=keyword_fn()), make_items(25), PROMPTS, item_id="SYN-9")
        reqs = [r["payload"] for r in self.records("call_request")]
        self.assertEqual([(r["temperature"], r["seed"], r["max_tokens"], r["stage"]) for r in reqs],
                         [(0.0, derive_seed("SYN-9", 0), 60 * 20 + 100, "judge:modality"),
                          (0.0, derive_seed("SYN-9", 1), 60 * 5 + 100, "judge:modality")])

    def test_batch_failing_validation_three_times_is_unsupported_and_logged(self):
        def fn(messages):
            items = parse_items(messages)
            if any("poison" in q for _n, _e, q in items):
                return "not json at all"
            return keyword_fn()(messages)
        items = make_items(20) + [{"element_id": "bad", "element": "B", "quote": "poison pill"}]
        out = judge_modality(self.log, MockClient(fn=fn), items, PROMPTS, item_id="S", batch_size=20)
        self.assertEqual(out["bad"], "unsupported")
        self.assertEqual(out["n3"], "actual")
        failed = self.records("judge_failed_batch")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["payload"], {"batch_index": 1, "item_ids": ["bad"]})
        self.assertEqual(len(self.records("call_request")), 1 + 3)
        manifest, _ = self.log.close()
        from second_scribe.audit.verify import verify_run
        self.assertTrue(verify_run(Path(self.tmp) / "r.jsonl", manifest)[0])

    def test_validation_rules(self):
        items = make_items(2)
        replies = {
            "missing n": {"labels": [{"n": 1, "modality": "actual"}]},
            "duplicate n": {"labels": [{"n": 1, "modality": "actual"}, {"n": 1, "modality": "actual"},
                                        {"n": 2, "modality": "actual"}]},
            "unknown n": {"labels": [{"n": 1, "modality": "actual"}, {"n": 3, "modality": "actual"}]},
            "bad label": {"labels": [{"n": 1, "modality": "actual"}, {"n": 2, "modality": "maybe"}]},
            "no labels key": {"answers": []},
        }
        for name, reply in replies.items():
            with self.subTest(name):
                out = judge_modality(self.log, MockClient(fn=lambda m, r=reply: json.dumps(r)), items,
                                     PROMPTS, item_id=name)
                self.assertEqual(out, {"n0": "unsupported", "n1": "unsupported"})

    def test_code_fenced_json_is_accepted_and_retry_can_recover(self):
        replies = iter(["```json\n" + json.dumps({"labels": [{"n": 1, "modality": "wished"}]}) + "\n```"])
        out = judge_modality(self.log, MockClient(fn=lambda m: next(replies)), make_items(1), PROMPTS, item_id="S")
        self.assertEqual(out, {"n0": "wished"})
        bad_then_good = iter(["oops", json.dumps({"labels": [{"n": 1, "modality": "negated"}]})])
        out = judge_modality(self.log, MockClient(fn=lambda m: next(bad_then_good)), make_items(1), PROMPTS,
                             item_id="S2")
        self.assertEqual(out, {"n0": "negated"})
        self.assertEqual(self.records("judge_failed_batch"), [])

    def test_empty_quote_is_unsupported_and_never_sent(self):
        calls = []
        items = [{"element_id": "a", "element": "A", "quote": ""}, {"element_id": "b", "element": "B", "quote": None},
                 {"element_id": "c", "element": "C", "quote": "we wish for a ramp"}]
        out = judge_modality(self.log, MockClient(fn=keyword_fn(calls)), items, PROMPTS, item_id="S")
        self.assertEqual(out, {"a": "unsupported", "b": "unsupported", "c": "wished"})
        self.assertEqual([len(c) for c in calls], [1])

    def test_no_items_no_calls(self):
        client = MockClient(fn=lambda m: self.fail("must not be called"))
        self.assertEqual(judge_modality(self.log, client, [], PROMPTS, item_id="S"), {})

    def test_cache_second_identical_call_makes_zero_model_calls(self):
        cache = JudgeCache(Path(self.tmp) / "cache.json")
        calls = []
        items = make_items(25)
        first = judge_modality(self.log, MockClient(fn=keyword_fn(calls)), items, PROMPTS, item_id="S",
                               cache=cache)
        self.assertEqual(len(calls), 2)
        before = len(self.records("call_request"))
        second = judge_modality(self.log, MockClient(fn=lambda m: self.fail("model called")), items, PROMPTS,
                                item_id="S", cache=cache)
        self.assertEqual(second, first)
        self.assertEqual(len(self.records("call_request")), before)
        # a fresh JudgeCache over the same file sees the stored labels too
        third = judge_modality(self.log, MockClient(fn=lambda m: self.fail("model called")), items, PROMPTS,
                               item_id="S", cache=JudgeCache(Path(self.tmp) / "cache.json"))
        self.assertEqual(third, first)

    def test_cache_only_misses_are_batched(self):
        cache = JudgeCache(Path(self.tmp) / "cache.json")
        items = make_items(30)
        judge_modality(self.log, MockClient(fn=keyword_fn()), items[:10], PROMPTS, item_id="S", cache=cache)
        calls = []
        out = judge_modality(self.log, MockClient(fn=keyword_fn(calls)), items, PROMPTS, item_id="S", cache=cache)
        self.assertEqual([len(c) for c in calls], [20])
        self.assertEqual(sorted(e for _n, e, _q in calls[0]), sorted(f"Element {k}" for k in range(10, 30)))
        self.assertEqual(len(out), 30)

    def test_cache_key_is_the_rendered_prompt_fingerprint_element_and_quote(self):
        import hashlib
        cache = JudgeCache(Path(self.tmp) / "cache.json")
        judge_modality(self.log, MockClient(fn=keyword_fn()),
                       [{"element_id": "n0", "element": "El", "quote": "we wish for a ramp"}], PROMPTS,
                       item_id="S", cache=cache)
        fp = prompt_fingerprint(PROMPTS, JUDGE_PROMPT_VERSION)
        self.assertEqual(fp, prompt_fingerprint(PROMPTS))                 # the default version
        key = hashlib.sha256(f"{fp}\x1fEl\x1fwe wish for a ramp\x1f\x1f".encode("utf-8")).hexdigest()
        self.assertEqual(cache.get(key), "wished")
        self.assertEqual(key, JudgeCache.key("El", "we wish for a ramp", fp))
        # same quote, other element -> miss
        calls = []
        judge_modality(self.log, MockClient(fn=keyword_fn(calls)),
                       [{"element_id": "n0", "element": "Other", "quote": "we wish for a ramp"}], PROMPTS,
                       item_id="S", cache=cache)
        self.assertEqual(len(calls), 1)

    def test_fingerprint_follows_template_definitions_and_system_text_not_file_names(self):
        import shutil
        d1, d2 = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        for d in (d1, d2):
            for f in PROMPTS.glob("*.v2.txt"):
                shutil.copy(f, d / f.name)
        base = prompt_fingerprint(d1, "v2")
        self.assertEqual(prompt_fingerprint(d2, "v2"), base)             # same content, other directory
        self.assertEqual(len(base), 64)
        for name in ("system.judge.v2.txt", "definitions.v2.txt", "judge_modality.v2.txt"):
            f = d2 / name
            old = f.read_text(encoding="utf-8")
            f.write_text(old + " x", encoding="utf-8")
            self.assertNotEqual(prompt_fingerprint(d2, "v2"), base, name)
            f.write_text(old, encoding="utf-8")
        self.assertEqual(prompt_fingerprint(d2, "v2"), base)

    def test_editing_the_prompt_in_place_invalidates_cached_labels(self):
        import shutil
        d = Path(tempfile.mkdtemp())
        for f in PROMPTS.glob("*.txt"):          # the default card (v3) reads the v2 definitions and system message
            shutil.copy(f, d / f.name)
        cache = JudgeCache(Path(self.tmp) / "cache.json")
        items = make_items(1)
        judge_modality(self.log, MockClient(fn=keyword_fn()), items, d, item_id="S", cache=cache)
        f = d / f"judge_modality.{JUDGE_PROMPT_VERSION}.txt"
        f.write_text(f.read_text(encoding="utf-8").replace("REMINDER:", "REMINDER (edited):"), encoding="utf-8")
        calls = []
        judge_modality(self.log, MockClient(fn=keyword_fn(calls)), items, d, item_id="S", cache=cache)
        self.assertEqual(len(calls), 1)

    def test_failed_batch_is_not_cached(self):
        cache = JudgeCache(Path(self.tmp) / "cache.json")
        judge_modality(self.log, MockClient(fn=lambda m: "garbage"), make_items(2), PROMPTS, item_id="S",
                       cache=cache)
        self.assertEqual(cache._data, {})
        calls = []
        out = judge_modality(self.log, MockClient(fn=keyword_fn(calls)), make_items(2), PROMPTS, item_id="S",
                             cache=cache)
        self.assertEqual(len(calls), 1)
        self.assertEqual(out, {"n0": "actual", "n1": "actual"})

    def test_same_element_and_quote_under_two_ids_is_sent_once(self):
        calls = []
        items = [{"element_id": "a", "element": "A", "quote": "we wish for a ramp"},
                 {"element_id": "b", "element": "A", "quote": "we wish  for a ramp"}]
        out = judge_modality(self.log, MockClient(fn=keyword_fn(calls)), items, PROMPTS, item_id="S")
        self.assertEqual(out, {"a": "wished", "b": "wished"})
        self.assertEqual([len(c) for c in calls], [1])

    def test_same_element_and_quote_with_a_different_speaker_is_sent_twice(self):
        calls = []
        items = [{"element_id": "a", "element": "A", "quote": "we wish for a ramp", "speaker": "participant"},
                 {"element_id": "b", "element": "A", "quote": "we wish for a ramp", "speaker": "interviewer",
                  "reply": "Yes."}]
        out = judge_modality(self.log, MockClient(fn=keyword_fn(calls)), items, PROMPTS, item_id="S")
        self.assertEqual(out, {"a": "wished", "b": "wished"})
        self.assertEqual([len(c) for c in calls], [2])


class TestJudgeCache(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "sub" / "cache.json"
        self.fp = prompt_fingerprint(PROMPTS, "v2")

    def test_get_put_roundtrip_and_atomic_file(self):
        c = JudgeCache(self.path)
        key = JudgeCache.key("El", "we wish for a ramp", self.fp, "interviewer", "Yes.")
        self.assertIsNone(c.get(key))
        c.put(key, "wished")
        self.assertEqual(c.get(key), "wished")
        self.assertEqual(json.loads(self.path.read_text()), {key: "wished"})
        self.assertEqual(os.listdir(self.path.parent), ["cache.json"])    # no tmp file left behind
        self.assertEqual(JudgeCache(self.path).get(key), "wished")

    def test_key_changes_with_every_part(self):
        base = JudgeCache.key("El", "q", self.fp, "interviewer", "Yes.")
        self.assertEqual(base, JudgeCache.key("El", " q ", self.fp, "interviewer", "Yes."))   # whitespace squashed
        for other in (JudgeCache.key("E2", "q", self.fp, "interviewer", "Yes."),
                      JudgeCache.key("El", "q2", self.fp, "interviewer", "Yes."),
                      JudgeCache.key("El", "q", "0" * 64, "interviewer", "Yes."),
                      JudgeCache.key("El", "q", self.fp, "participant", "Yes."),
                      JudgeCache.key("El", "q", self.fp, "interviewer", "No."),
                      JudgeCache.key("El", "q", self.fp)):
            self.assertNotEqual(base, other)
        # a reply is only shown (and keyed) for an interviewer quote
        self.assertEqual(JudgeCache.key("El", "q", self.fp, "participant", "Yes."),
                         JudgeCache.key("El", "q", self.fp, "participant", "No."))

    def test_put_rejects_unknown_modality(self):
        with self.assertRaises(ValueError):
            JudgeCache(self.path).put("k", "maybe")

    def test_corrupt_file_raises_rather_than_silently_resetting(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json")
        with self.assertRaises(ValueError):
            JudgeCache(self.path)

    def test_non_object_file_raises(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("[1, 2]")
        with self.assertRaises(ValueError):
            JudgeCache(self.path)


class TestAttestedAndItems(unittest.TestCase):
    def test_attested(self):
        self.assertIs(attested("wished", "wished"), True)
        self.assertIs(attested("experienced", "wished"), False)
        self.assertIs(attested("experienced", "actual"), True)
        self.assertIs(attested("wished", "actual"), False)
        for m in ("negated", "hypothetical", "other_person", "unsupported"):
            self.assertIs(attested("experienced", m), False)
            self.assertIs(attested("wished", m), False)

    def test_modality_items_for_nodes_and_edges(self):
        ai = MapGraph("SYN-1", "ai",
                      nodes=[Node("n1", "Health unit", quote="we waited"), Node("n2", "Clinic", quote="we went"),
                             Node("n3", "No quote")],
                      edges=[Edge("n1", "n2", quote="then the clinic"), Edge("n2", "n3")])
        self.assertEqual(modality_items(ai), [
            {"element_id": "n1", "element": "Health unit", "quote": "we waited"},
            {"element_id": "n2", "element": "Clinic", "quote": "we went"},
            {"element_id": "e:n1->n2", "element": "Health unit → Clinic", "quote": "then the clinic"},
        ])


def _words(text):
    return re.findall(r"[a-z0-9']+", text.lower().replace("\u2019", "'"))


def _grams(words, n=5):
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def _demo_corpus_grams():
    demo = Path(__file__).resolve().parent.parent / "demo" / "corpus"
    docs = []
    for f in sorted(glob.glob(str(demo / "transcripts" / "*.docx"))):
        docs += read_docx_text(f)
    for f in sorted(glob.glob(str(demo / "traps" / "*.json"))):
        docs += [t["sentence"] for t in json.loads(Path(f).read_text(encoding="utf-8"))]
    grams = set()
    for d in docs:
        grams |= _grams(_words(d))
    return docs, grams


def _reference_examples(template):
    """[(label, element, quote)] from the 'Example X.' lines of the rubric card."""
    return [(m.group(3), m.group(1), m.group(2)) for m in re.finditer(
        r'^Example [A-Z]\. ELEMENT: (.*?)\. QUOTE: "(.*)" Label: (\w+)\.$', template, re.M)]


class TestJudgeV2(unittest.TestCase):
    """Task 5B: system message, rubric card with shared definitions, reference examples, UNPARSED."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = AuditLog(Path(self.tmp) / "r.jsonl", "run", clock=lambda: "t")
        self.template = (PROMPTS / "judge_modality.v2.txt").read_text(encoding="utf-8")

    def records(self, record_type=None):
        recs = [json.loads(l) for l in (Path(self.tmp) / "r.jsonl").read_text().splitlines()]
        return [r for r in recs if record_type is None or r["record_type"] == record_type]

    def test_every_call_has_a_nonempty_system_message_first(self):
        seen = []
        client = MockClient(fn=lambda m: seen.append([dict(x) for x in m]) or keyword_fn()(m))
        judge_modality(self.log, client, make_items(25), PROMPTS, item_id="S")
        self.assertEqual(len(seen), 2)
        for m in seen:
            self.assertEqual([x["role"] for x in m], ["system", "user"])
            self.assertEqual(m[0]["content"], load_system(PROMPTS, "judge"))
            self.assertTrue(m[0]["content"].strip())

    def test_system_message_is_one_short_paragraph(self):
        sysmsg = load_system(PROMPTS, "judge")
        self.assertLess(len(sysmsg.split()), 70)
        self.assertNotIn("\n\n", sysmsg)

    def test_rubric_card_uses_the_shared_definitions_text(self):
        seen = []
        client = MockClient(fn=lambda m: seen.append(m[-1]["content"]) or keyword_fn()(m))
        judge_modality(self.log, client, make_items(1), PROMPTS, item_id="S")
        defs = load_definitions(PROMPTS)
        self.assertEqual(defs, (PROMPTS / "definitions.v2.txt").read_text(encoding="utf-8").strip())
        self.assertIn(defs, seen[0])
        self.assertNotIn("{definitions}", seen[0])

    def test_all_six_labels_are_defined_and_each_has_one_reference_example(self):
        for lab in MODALITIES:
            self.assertRegex(self.template, rf"(?m)^- {lab}: ", lab)
        ex = _reference_examples(self.template)
        self.assertEqual(sorted(l for l, _e, _q in ex), sorted(MODALITIES))
        self.assertIn("for reference only", self.template.lower())
        self.assertIn("not items to judge", self.template.lower())
        # reference examples come before the real items, in a different domain
        self.assertLess(self.template.index("Example A."), self.template.index("ITEMS:"))
        self.assertIn("lease", self.template.lower())

    def test_reference_examples_are_not_parsed_as_items(self):
        seen = []
        client = MockClient(fn=lambda m: seen.append(m) or keyword_fn()(m))
        judge_modality(self.log, client, make_items(2), PROMPTS, item_id="S")
        self.assertEqual([n for n, _e, _q in parse_items(seen[0])], [1, 2])

    def test_no_five_word_sequence_of_a_reference_example_is_in_the_demo_corpus(self):
        docs, corpus = _demo_corpus_grams()
        self.assertGreater(len(docs), 100)
        self.assertGreater(len(corpus), 500)
        # the check can see a planted sentence (guards against an empty or mis-parsed corpus)
        planted = _grams(_words(docs[0]))
        self.assertTrue(planted and planted <= corpus)
        for lab, element, quote in _reference_examples(self.template):
            shared = _grams(_words(f"{element} . {quote}")) & corpus
            self.assertFalse(shared, f"reference example {lab!r} shares a 5-word run with the corpus: {sorted(shared)[:2]}")

    def test_reply_failing_strict_parsing_after_retries_is_logged_as_unparsed(self):
        stats = {}
        items = make_items(3)
        out = judge_modality(self.log, MockClient(fn=lambda m: "Sure! The labels are: actual, actual, actual"),
                             items, PROMPTS, item_id="S", stats=stats)
        self.assertEqual(out, {f"n{k}": "unsupported" for k in range(3)})     # downstream value unchanged
        recs = self.records("judge_unparsed")
        self.assertEqual([r["payload"] for r in recs], [{"batch_index": 0, "n_items": 3}])
        self.assertEqual(len(self.records("judge_failed_batch")), 1)
        self.assertEqual(stats["unparsed_batches"], 1)
        self.assertEqual(stats["unparsed_items"], ["n0", "n1", "n2"])
        self.assertEqual(stats["batches"], 1)

    def test_unparsed_is_counted_per_batch_and_clean_batches_are_not(self):
        def fn(messages):
            if any("poison" in q for _n, _e, q in parse_items(messages)):
                return '{"labels": []}'
            return keyword_fn()(messages)
        items = make_items(20) + [{"element_id": "bad", "element": "B", "quote": "poison pill"}]
        stats = {}
        judge_modality(self.log, MockClient(fn=fn), items, PROMPTS, item_id="S", batch_size=20, stats=stats)
        self.assertEqual([r["payload"] for r in self.records("judge_unparsed")], [{"batch_index": 1, "n_items": 1}])
        self.assertEqual((stats["batches"], stats["unparsed_batches"]), (2, 1))

    def test_a_batch_that_never_got_a_reply_is_failed_but_not_unparsed(self):
        def boom(messages):
            raise ConnectionError("down")
        stats = {}
        out = judge_modality(self.log, MockClient(fn=boom), make_items(2), PROMPTS, item_id="S", stats=stats)
        self.assertEqual(out, {"n0": "unsupported", "n1": "unsupported"})
        self.assertEqual(len(self.records("judge_failed_batch")), 1)
        self.assertEqual(self.records("judge_unparsed"), [])
        self.assertEqual(stats["unparsed_batches"], 0)
        self.assertEqual(stats["failed_batches"], 1)

    def test_retry_after_bad_then_empty_reply_recovers_and_is_not_unparsed(self):
        seen = []
        replies = iter(["```json\nnot json\n```", "", None])

        def fn(messages):
            seen.append([x["role"] for x in messages])
            r = next(replies)
            return r if r is not None else json.dumps({"labels": [{"n": 1, "modality": "actual"}]})
        out = judge_modality(self.log, MockClient(fn=fn), make_items(1), PROMPTS, item_id="S")
        self.assertEqual(out, {"n0": "actual"})
        self.assertEqual(seen[1], ["system", "user", "assistant", "user"])
        self.assertEqual(seen[2], ["system", "user", "assistant", "user"])   # empty reply: messages unchanged
        self.assertEqual(self.records("judge_unparsed"), [])

    def test_v1_prompt_still_runs_for_the_record_without_a_system_message(self):
        seen = []
        client = MockClient(fn=lambda m: seen.append([dict(x) for x in m]) or keyword_fn()(m))
        out = judge_modality(self.log, client, make_items(2), PROMPTS, item_id="S", prompt_version="v1")
        self.assertEqual(len(out), 2)
        self.assertEqual([x["role"] for x in seen[0]], ["user"])
        self.assertIn("Return only JSON:", seen[0][0]["content"])
        cache = JudgeCache(Path(self.tmp) / "c.json")
        judge_modality(self.log, client, make_items(1), PROMPTS, item_id="S", cache=cache, prompt_version="v1")
        self.assertIsNotNone(cache.get(JudgeCache.key("Element 0", "we waited six weeks for the health unit 0",
                                                      prompt_fingerprint(PROMPTS, "v1"))))
        self.assertNotEqual(prompt_fingerprint(PROMPTS, "v1"), prompt_fingerprint(PROMPTS, "v2"))




def _items_body(messages):
    return next(m for m in messages if m["role"] == "user")["content"].split("ITEMS:", 1)[1].split("\nREMINDER:")[0]


class TestJudgeRubricRound1(unittest.TestCase):
    """Fix round 1: precedence, interviewer quotes with the participant's reply, not-judged ids."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = AuditLog(Path(self.tmp) / "r.jsonl", "run", clock=lambda: "t")
        self.template = (PROMPTS / "judge_modality.v2.txt").read_text(encoding="utf-8")

    def records(self, record_type):
        recs = [json.loads(l) for l in (Path(self.tmp) / "r.jsonl").read_text().splitlines()]
        return [r for r in recs if r["record_type"] == record_type]

    def run_one(self, items):
        seen = []
        client = MockClient(fn=lambda m: seen.append(m) or json.dumps(
            {"labels": [{"n": k, "modality": "actual"} for k in range(1, len(items) + 1)]}))
        judge_modality(self.log, client, items, PROMPTS, item_id="S")
        return _items_body(seen[0])

    def test_rubric_lines_unsupported_precedence_and_interviewer_rule(self):
        t = self.template
        self.assertIn("- unsupported: the quote does not talk about this element, or talks about it without "
                      "the participant saying any of the above.", t)
        self.assertIn("If more than one label could fit: use actual if the quote says it happened to or existed for "
                      "the participant; otherwise wished if it fits the wished definition above; otherwise negated, "
                      "then other_person, then hypothetical. Use unsupported only when none fits.", t)
        self.assertIn("When the quote is the interviewer's words, label what the participant's REPLY affirms about "
                      "the element (a plain \"yes\" affirms the interviewer's statement). If an interviewer quote "
                      "has no REPLY, use unsupported.", t)
        self.assertLess(t.index("- unsupported:"), t.index("If more than one label could fit"))
        self.assertLess(t.index("If more than one label could fit"), t.index("Reference examples"))

    def test_label_lines_say_participant_and_match_the_definitions(self):
        t = self.template
        for line in (
                "- actual: the quote says the element is experienced, as defined above: it happened to the "
                "participant or existed for them.",
                "- wished: the quote says the element is wished, as defined above: the participant wanted or needed "
                "it, or says it should have existed for them, and does not say that it happened or existed for them.",
                "- negated: the participant says it did NOT happen or did NOT exist for them.",
                "- hypothetical: the participant talks about it only as a possibility, a condition or a guess; it "
                "only MIGHT have happened.",
                "- other_person: the participant says it happened only to SOMEONE ELSE, not to them."):
            self.assertIn(line, t)
        self.assertNotIn("the speaker", t)

    def test_card_tells_the_judge_to_use_the_reply_and_repeats_it_in_the_reminder(self):
        t = self.template
        self.assertIn("Judge only from the words of the quote, and, when the quote is the interviewer's, the "
                      "participant's REPLY.", t)
        self.assertIn("REMINDER: Judge each item only from its own quote and reply. ", t)
        self.assertNotIn("only from the words of the quote.", t)

    def test_reference_examples_d_and_f_were_reworded(self):
        ex = {l: (e, q) for l, e, q in _reference_examples(self.template)}
        self.assertEqual(ex["hypothetical"][1],
                         "If the landlord raises the rent next year, I suppose we might look for another place.")
        self.assertEqual(ex["unsupported"], ("Broker fee", "The kitchen had a lovely view over the river."))

    def test_interviewer_quote_renders_both_the_question_and_the_reply(self):
        body = self.run_one([{"element_id": "a", "element": "Plan review",
                              "quote": "So the engineer came next?", "speaker": "interviewer", "reply": "Yes."}])
        self.assertIn('1. ELEMENT: Plan review\n   QUOTE (interviewer): "So the engineer came next?"\n'
                      '   REPLY (participant): "Yes."', body)

    def test_participant_and_unknown_speakers_render_their_tags(self):
        body = self.run_one([
            {"element_id": "a", "element": "A", "quote": "we waited", "speaker": "participant", "reply": "ignored"},
            {"element_id": "b", "element": "B", "quote": "somebody said", "speaker": "unknown"},
            {"element_id": "c", "element": "C", "quote": "no speaker key"}])
        self.assertIn('1. ELEMENT: A\n   QUOTE (participant): "we waited"\n2.', body)
        self.assertNotIn("ignored", body)
        self.assertIn('2. ELEMENT: B\n   QUOTE: "somebody said"\n3.', body)
        self.assertIn('3. ELEMENT: C\n   QUOTE: "no speaker key"', body)
        self.assertNotIn("REPLY", body)

    def test_reply_is_truncated_to_sixty_words_and_missing_reply_adds_no_line(self):
        long = " ".join(f"w{k}" for k in range(100))
        body = self.run_one([
            {"element_id": "a", "element": "A", "quote": "q one", "speaker": "interviewer", "reply": long},
            {"element_id": "b", "element": "B", "quote": "q two", "speaker": "interviewer", "reply": None}])
        reply_line = next(l for l in body.splitlines() if l.strip().startswith("REPLY"))
        self.assertEqual(reply_line.split('"')[1].split(), [f"w{k}" for k in range(60)])
        self.assertEqual(body.count("REPLY"), 1)

    def test_speaker_and_reply_are_part_of_the_cache_key(self):
        cache = JudgeCache(Path(self.tmp) / "c.json")
        base = {"element_id": "a", "element": "El", "quote": "so it came next?"}
        for extra in ({}, {"speaker": "interviewer", "reply": "Yes."}, {"speaker": "interviewer", "reply": "No."},
                      {"speaker": "participant"}):
            calls = []
            client = MockClient(fn=lambda m, c=calls: c.append(1) or json.dumps(
                {"labels": [{"n": 1, "modality": "actual"}]}))
            judge_modality(self.log, client, [{**base, **extra}], PROMPTS, item_id="S", cache=cache)
            self.assertEqual(len(calls), 1, extra)          # each variant is a cache miss the first time
        calls = []
        judge_modality(self.log, MockClient(fn=lambda m: self.fail("cache must serve this")),
                       [{**base, "speaker": "interviewer", "reply": "Yes."}], PROMPTS, item_id="S", cache=cache)

    def test_modality_items_fill_speaker_and_next_participant_reply(self):
        T = Transcript("S", [Turn(0, "interviewer", "Then the engineer came next, right?"),
                             Turn(1, "participant", "Yes."),
                             Turn(2, "participant", "We waited six weeks for the health unit."),
                             Turn(3, "interviewer", "And the clinic?"),
                             Turn(4, "participant", "Never went.")], "synthetic")
        ai = MapGraph("S", "ai",
                      nodes=[Node("n1", "Engineer", quote="Then the engineer came next, right?"),
                             Node("n2", "Health unit", quote="We waited six weeks for the health unit."),
                             Node("n3", "Clinic", quote="And the clinic?"),
                             Node("n4", "Ghost", quote="not in the transcript at all")],
                      edges=[Edge("n1", "n2", quote="Then the engineer came next, right?")])
        prov = provenance_for(ai, T)
        by_id = {i["element_id"]: i for i in modality_items(ai, provenance=prov, transcript=T)}
        self.assertEqual((by_id["n1"]["speaker"], by_id["n1"]["reply"]), ("interviewer", "Yes."))
        self.assertEqual(by_id["n2"]["speaker"], "participant"); self.assertNotIn("reply", by_id["n2"])
        self.assertEqual((by_id["n3"]["speaker"], by_id["n3"]["reply"]), ("interviewer", "Never went."))
        self.assertNotIn("speaker", by_id["n4"])                    # not found: no speaker claimed
        self.assertEqual(by_id["e:n1->n2"]["reply"], "Yes.")
        # provenance is computed when only the transcript is given
        again = {i["element_id"]: i for i in modality_items(ai, transcript=T)}
        self.assertEqual(again["n1"]["reply"], "Yes.")
        # an interviewer quote with no later participant turn gets no reply
        T2 = Transcript("S", [Turn(0, "participant", "ok then"), Turn(1, "interviewer", "Anything else at all?")],
                        "synthetic")
        ai2 = MapGraph("S", "ai", nodes=[Node("n1", "X", quote="Anything else at all?")])
        self.assertNotIn("reply", modality_items(ai2, transcript=T2)[0])

    def test_two_interviewer_questions_in_a_row_the_reply_belongs_to_the_last_one(self):
        T = Transcript("S", [Turn(0, "interviewer", "Did the first office send you away?"),
                             Turn(1, "interviewer", "And did the second one help?"),
                             Turn(2, "participant", "Yes, it did."),
                             Turn(3, "interviewer", "Was there a third step?"),
                             Turn(4, "participant", "No.")], "synthetic")
        ai = MapGraph("S", "ai", nodes=[Node("n1", "First office", quote="Did the first office send you away?"),
                                        Node("n2", "Second office", quote="And did the second one help?"),
                                        Node("n3", "Third step", quote="Was there a third step?")])
        by_id = {i["element_id"]: i for i in modality_items(ai, transcript=T)}
        self.assertEqual(by_id["n1"]["speaker"], "interviewer")
        self.assertNotIn("reply", by_id["n1"])            # earlier question: no REPLY, so unsupported
        self.assertEqual(by_id["n2"]["reply"], "Yes, it did.")
        self.assertEqual(by_id["n3"]["reply"], "No.")
        # a participant turn between, then another interviewer turn: still the last interviewer turn only
        T2 = Transcript("S", [Turn(0, "participant", "ok"), Turn(1, "interviewer", "Q one here?"),
                              Turn(2, "unknown", "mm"), Turn(3, "participant", "Sure.")], "synthetic")
        ai2 = MapGraph("S", "ai", nodes=[Node("n1", "X", quote="Q one here?")])
        self.assertEqual(modality_items(ai2, transcript=T2)[0]["reply"], "Sure.")

    def test_judge_modality_detailed_reports_not_judged_ids(self):
        def fn(messages):
            if any("poison" in q for _n, _e, q in parse_items(messages)):
                return "not json"
            return keyword_fn()(messages)
        items = make_items(20) + [{"element_id": "bad", "element": "B", "quote": "poison pill"},
                                  {"element_id": "empty", "element": "E", "quote": ""}]
        labels, not_judged = judge_modality_detailed(self.log, MockClient(fn=fn), items, PROMPTS, item_id="S",
                                                     batch_size=20)
        self.assertEqual(not_judged, {"bad"})            # failed batch only; empty quote is unsupported by rule
        self.assertEqual(labels["bad"], "unsupported")
        self.assertEqual(labels["empty"], "unsupported")
        self.assertEqual(labels["n3"], "actual")
        plain = judge_modality(self.log, MockClient(fn=fn), items, PROMPTS, item_id="S", batch_size=20)
        self.assertEqual(plain, labels)

    def test_not_judged_includes_transport_failed_batches_and_is_empty_when_all_judged(self):
        def boom(messages):
            raise ConnectionError("down")
        labels, nj = judge_modality_detailed(self.log, MockClient(fn=boom), make_items(2), PROMPTS, item_id="S")
        self.assertEqual(nj, {"n0", "n1"})
        labels, nj = judge_modality_detailed(self.log, MockClient(fn=keyword_fn()), make_items(2), PROMPTS,
                                             item_id="S2")
        self.assertEqual(nj, set())

    def test_prereg_note_states_the_label_convention_and_precedence(self):
        doc = (Path(__file__).resolve().parent.parent / "docs" / "prereg-judge-labels.md").read_text(encoding="utf-8")
        low = doc.lower()
        for phrase in ("interviewer", "reply", "affirms", "precedence", "actual", "wished", "negated",
                       "other_person", "hypothetical", "unsupported"):
            self.assertIn(phrase, low)
        self.assertLess(low.index("actual"), low.index("wished"))
        self.assertEqual(len([p for p in doc.split("\n\n") if p.strip() and not p.startswith("#")]), 1)




class TestCacheProvenance(unittest.TestCase):
    """The cache key names the model and its params; each entry names the call that produced it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.log = AuditLog(Path(self.tmp) / "r.jsonl", "run", clock=lambda: "t")
        self.cache = JudgeCache(Path(self.tmp) / "cache.json")

    def client(self, model_id, params=None, calls=None):
        c = MockClient(fn=keyword_fn(calls))
        c.cfg = types.SimpleNamespace(model_id=model_id, params=params or {}, api_key_env=None)
        return c

    def judge(self, client, items, **kw):
        return judge_modality_detailed(self.log, client, items, PROMPTS, item_id="S", cache=self.cache, **kw)

    def test_another_model_or_other_params_never_hit_the_cache(self):
        items = make_items(3)
        self.judge(self.client("model-a", {"top_p": 1}), items)
        for model_id, params in (("model-b", {"top_p": 1}), ("model-a", {"top_p": 0.5})):
            calls = []
            self.judge(self.client(model_id, params, calls), items)
            self.assertEqual(len(calls), 1, (model_id, params))
        calls = []
        self.judge(self.client("model-a", {"top_p": 1}, calls), items)          # same model, same params: all hits
        self.assertEqual(calls, [])

    def test_secret_params_do_not_split_the_cache_by_their_value(self):
        items = make_items(2)
        self.judge(self.client("m", {"api_key": "one"}), items)
        calls = []
        self.judge(self.client("m", {"api_key": "two"}, calls), items)
        self.assertEqual(calls, [])

    def test_an_entry_names_the_call_that_produced_its_label(self):
        items = make_items(3)
        self.judge(self.client("m"), items)
        request_ids = {r["payload"]["request_id"] for r in map(json.loads, (Path(self.tmp) / "r.jsonl").read_text().splitlines())
                       if r["record_type"] == "call_request"}
        data = json.loads((Path(self.tmp) / "cache.json").read_text())
        self.assertEqual(len(data), 3)
        for entry in data.values():
            self.assertIn(entry["modality"], MODALITIES)
            self.assertIn(entry["request_id"], request_ids)

    def test_a_cache_served_run_reports_the_calls_behind_the_labels(self):
        items = make_items(3)
        self.judge(self.client("m"), items)
        st = {}
        noted = []
        self.log.note_request_ids = noted.extend            # what the work store's tracker provides
        self.judge(self.client("m", calls=[]), items, stats=st)
        self.assertEqual((st["batches"], st["cache_hits"]), (0, 3))
        self.assertEqual(len(st["cache_request_ids"]), 1)    # one batch call produced the three labels
        self.assertEqual(noted, st["cache_request_ids"])

    def test_a_run_with_no_cache_hits_reports_none(self):
        st = {}
        self.judge(self.client("m"), make_items(2), stats=st)
        self.assertEqual((st["cache_hits"], st["cache_request_ids"]), (0, []))

    def test_an_entry_stored_without_a_request_id_still_serves_but_names_no_call(self):
        items = make_items(1)
        key = JudgeCache.key("Element 0", "we waited six weeks for the health unit 0", prompt_fingerprint(PROMPTS))
        self.cache.put(key, "wished")                          # the legacy shape: a bare modality
        st = {}
        out = self.judge(MockClient(fn=lambda m: self.fail("called")), items, stats=st)
        self.assertEqual(out[0], {"n0": "wished"})
        self.assertEqual((st["cache_hits"], st["cache_request_ids"]), (1, []))
        self.assertIsNone(self.cache.request_id(key))


if __name__ == "__main__":
    unittest.main()
