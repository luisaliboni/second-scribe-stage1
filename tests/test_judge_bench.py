import io
import json
import re
import shutil
import tempfile
import time
import unittest
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from second_scribe.audit.registry import AuditLog
from second_scribe.judge_bench import (DEFAULT_SEEDS, EXCLUDED_KINDS, EXCLUDED_REASONS, FILL_CONCEPTS, FILL_REASON, OracleClient, is_excluded, says_it_happened, corpora, dedupe_items, PASS_ACCURACY, PASS_NOT_JUDGED, bench, both_wrong,
                                       build_modality_set, calibration, main, pass_rule, render_markdown,
                                       score_modality, speechify, wilson, mcnemar_exact_p, cluster_bootstrap_ci)
from second_scribe.llm import ModelResponse, RecordingClient, ReplayClient
from second_scribe.schemas import MODALITIES
from second_scribe.synth.bank import CONCEPTS
from second_scribe.keywords import keyword_modality
from second_scribe.synth.bench_bank import BENCH_TEMPLATES, PER_TEMPLATE, render


def tmpdir(case) -> str:
    """A temp directory removed when ``case`` finishes (bench runs write megabytes of audit log each)."""
    d = tempfile.mkdtemp()
    case.addCleanup(shutil.rmtree, d, True)
    return d


ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "demo" / "corpus"
PROMPTS = ROOT / "prompts"


def planted_counts(corpus=CORPUS) -> Counter:
    """Spoken planted traps per kind in ``corpus`` (read from traps/*.json, so the tests follow a regenerated corpus)."""
    c = Counter()
    for f in (corpus / "traps").glob("*.json"):
        c.update(t["kind"] for t in json.loads(f.read_text()) if t["sentence"])
    return c
_WORD = re.compile(r"[\w']+")


def words(s):
    return [w.lower() for w in _WORD.findall(s)]


class TestSpeechify(unittest.TestCase):
    S = "We never went through the paperwork we redo every year at all."

    def test_deterministic_and_keyed(self):
        a = speechify(self.S, ["the paperwork we redo every year"], "SYN-OW01:C26")
        self.assertEqual(a, speechify(self.S, ["the paperwork we redo every year"], "SYN-OW01:C26"))
        self.assertNotEqual(a, speechify(self.S, ["the paperwork we redo every year"], "SYN-OW02:C26"))
        self.assertNotEqual(a, self.S)

    def test_adds_fillers_a_false_start_and_a_run_on_and_never_removes_words(self):
        out = speechify(self.S, ["the paperwork we redo every year"], "k")
        self.assertTrue(out.startswith("And then "))
        self.assertEqual(len(re.findall(r"\b(?:um|you know|uh),", out)), 2)
        self.assertRegex(out, r"(?i)we never, we never")
        have = Counter(words(out))
        for w, n in Counter(words(self.S)).items():
            self.assertGreaterEqual(have[w], n, w)

    def test_semicolon_becomes_and_then(self):
        s = "My neighbour had the visit from the fire department; that was never part of our story."
        out = speechify(s, ["the visit from the fire department"], "k")
        self.assertNotIn(";", out)
        self.assertIn("department and then that was never", out.replace(" um,", "").replace(" like,", "")
                      .replace(" you know,", ""))

    def test_a_sentence_that_opens_with_the_concept_does_not_repeat_or_cut_it(self):
        wording = "the how-to pages on the city website"
        for key in ("a", "b", "c", "d", "e", "f"):
            out = speechify("The how-to pages on the city website went nowhere.", [wording], key)
            self.assertEqual(out.lower().count(wording), 1, out)       # not repeated by the false start
            self.assertIn(wording, out)                                  # nothing inside the span
            self.assertNotIn("And then The", out)                        # lower-cased once it is not sentence-initial
            self.assertTrue(out.startswith("And "), out)

    def test_no_doubled_then_and_no_filler_after_a_modal_verb(self):
        for key in "abcdefghij":
            out = speechify("Then we dealt with the club for street food sellers.", ["the club for street food sellers"], key)
            self.assertNotRegex(out.lower(), r"\bthen then\b")
            self.assertTrue(out.startswith("And "), out)
            s2 = speechify("I would have loved to have the licensing guide and we could really use it.", [], key)
            self.assertNotRegex(s2, r"\b(would|could|'d)\s+(um|uh|you know),")
            self.assertNotRegex(s2.lower(), r"\blike,")
            s3 = speechify("We checked the first thing; then we waited.", [], key)
            self.assertNotRegex(s3.lower(), r"\bthen then\b|;")
        self.assertNotIn("like,", " ".join(speechify("We saw the visit.", [], k) for k in "abcdefghijklmnop"))

    def test_the_keyword_rule_gives_the_same_label_for_prose_and_speech_of_every_item(self):
        from second_scribe.keywords import keyword_modality
        with corpora(None, DEFAULT_SEEDS) as cs:
            for tag, path, seed in cs:
                items = build_modality_set(path, seed, tag)
                prose = {i["element_id"].rsplit(":", 1)[0]: i for i in items if i["style"] == "prose"}
                for i in items:
                    if i["style"] == "speech":
                        p = prose[i["element_id"].rsplit(":", 1)[0]]
                        self.assertEqual(keyword_modality(p["quote"]), keyword_modality(i["quote"]),
                                         (p["quote"], i["quote"]))

    def test_never_alters_the_concept_wording_for_any_item_in_the_corpus(self):
        items = [it for it in build_modality_set(CORPUS) if it["style"] == "speech"]
        self.assertGreater(len(items), 100)
        for it in items:
            for wording in it["protect"]:
                self.assertIn(wording.lower(), it["quote"].lower(), it["element_id"])
            self.assertNotIn("  ", it["quote"])


class TestModalitySet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = build_modality_set(CORPUS)

    def test_sizes_classes_and_two_styles(self):
        bank = {f"bank_{c}": len(ts) * PER_TEMPLATE for c, ts in BENCH_TEMPLATES.items()}
        pc = planted_counts()
        trap = {**pc, "iv_no_reply": pc["interviewer_voiced"], "iv_no": pc["interviewer_voiced"]}
        per_style = sum(pc.values()) + 2 * pc["interviewer_voiced"] + 60 + 40 + sum(bank.values())
        self.assertEqual(len(self.items), 2 * per_style)
        for style in ("prose", "speech"):
            sub = [i for i in self.items if i["style"] == style]
            self.assertEqual(Counter(i["kind"] for i in sub), Counter({**trap, **bank, "plain": 60, "shuffled": 40}))
            want = Counter({"actual": 60 + sum(pc[k] for k in ("loop", "interviewer_voiced", "mapping_talk",
                                                                "workaround", "dead_end")) + bank["bank_actual"],
                            "unsupported": 40 + pc["interviewer_voiced"],
                            "other_person": pc["other_person"] + bank["bank_other_person"],
                            "wished": pc["wished"] + bank["bank_wished"],
                            "negated": pc["negated"] + pc["interviewer_voiced"] + bank["bank_negated"],
                            "hypothetical": pc["hypothetical"] + bank["bank_hypothetical"]})
            self.assertEqual(Counter(i["expected"] for i in sub), want)
        self.assertEqual(len({i["element_id"] for i in self.items}), len(self.items))
        self.assertTrue(all(i["expected"] in MODALITIES for i in self.items))

    def test_deterministic(self):
        self.assertEqual(self.items, build_modality_set(CORPUS))
        self.assertNotEqual([i["quote"] for i in self.items if i["kind"] == "plain"],
                            [i["quote"] for i in build_modality_set(CORPUS, seed=1) if i["kind"] == "plain"])

    def test_prose_is_the_planted_sentence_and_elements_use_the_ai_wording(self):
        ai = {c.forms[2] for c in CONCEPTS}
        traps = set()
        for f in (CORPUS / "traps").glob("*.json"):
            traps |= {t["sentence"] for t in json.loads(f.read_text()) if t["sentence"]}
        for it in self.items:
            self.assertIn(it["element"], ai)
            if it["style"] == "prose" and it["kind"] not in ("plain", "shuffled") and not it["kind"].startswith("bank_") \
                    and it["kind"] != "iv_no":           # the "No." items are the bench's own questions
                self.assertIn(it["quote"], traps)

    def test_interviewer_items_carry_the_reply_and_participant_items_do_not(self):
        iv = [i for i in self.items if i["speaker"] == "interviewer"]
        pc = planted_counts()
        self.assertEqual(len(iv), 2 * (3 * pc["interviewer_voiced"] + pc["mapping_talk"]))
        self.assertTrue(all(i.get("reply") for i in iv if i["kind"] not in ("iv_no_reply",)))
        self.assertTrue(all("reply" not in i for i in self.items if i["speaker"] == "participant"))

    def test_every_interviewer_question_is_also_asked_without_a_reply_and_with_a_no(self):
        prose = [i for i in self.items if i["style"] == "prose"]
        orig = [i for i in prose if i["kind"] == "interviewer_voiced"]
        bare = {(i["pid"], i["cid"]): i for i in prose if i["kind"] == "iv_no_reply"}
        no = {(i["pid"], i["cid"]): i for i in prose if i["kind"] == "iv_no"}
        self.assertEqual((len(orig), len(bare), len(no)), (6, 6, 6))
        for o in orig:
            k = (o["pid"], o["cid"])
            self.assertEqual((o["quote"], o["element"]), (bare[k]["quote"], bare[k]["element"]))
            self.assertEqual((o["expected"], bare[k]["expected"], no[k]["expected"]),
                             ("actual", "unsupported", "negated"))
            self.assertNotIn("reply", bare[k])                       # the rubric: an interviewer quote without a reply
            self.assertEqual(no[k]["reply"], "No.")
            self.assertTrue(o["reply"] and o["reply"] != "No.")

    def test_shuffled_pairs_never_share_a_domain_cluster(self):
        by_cid = {c.cid: c for c in CONCEPTS}
        self.assertEqual({by_cid[c].cluster for c in ("C11", "C17", "C20")}, {"health"})
        self.assertEqual({by_cid[c].cluster for c in ("C12", "C18")}, {"fire"})
        with corpora(None, DEFAULT_SEEDS) as cs:
            for tag, path, seed in cs:
                for it in build_modality_set(path, seed, tag):
                    if it["kind"] == "shuffled":
                        quote_cid = next(c.cid for c in CONCEPTS if c.forms[1].lower() in it["quote"].lower())
                        self.assertNotEqual(by_cid[quote_cid].cluster or "-", by_cid[it["cid"]].cluster or "+")
                        self.assertNotEqual(quote_cid, it["cid"])

    def test_shuffled_pairs_do_not_talk_about_their_element(self):
        for it in self.items:
            if it["kind"] == "shuffled" and it["style"] == "prose":
                c = next(c for c in CONCEPTS if c.forms[2] == it["element"])
                self.assertNotIn(c.forms[1].lower(), it["quote"].lower())
                self.assertNotIn(c.forms[0].lower(), it["quote"].lower())


def make_results(**over):
    """A results dict that passes every criterion, with ``over`` replacing parts of it."""
    r = {"modality": {"accuracy": 0.95, "recall": {c: 0.9 for c in MODALITIES},
                      "precision": {c: 0.9 for c in MODALITIES}, "style_gap": 0.01},
         "alignment": {"strict": {"f1": 0.9}},
         "off_record": {"recall": 0.9, "mention_recall": 0.9, "n_off_record": 120}, "not_judged_rate": 0.0}
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(r.get(k), dict):
            merged = {**r[k], **v}
            for kk, vv in v.items():
                if isinstance(vv, dict) and isinstance(r[k].get(kk), dict):
                    merged[kk] = {**r[k][kk], **vv}
            r[k] = merged
        else:
            r[k] = v
    return r


def only_the_pool_floor_fails(tc, r):
    """A single-corpus (--corpus) bench scores every metric right but never passes: its off-record set is below the
    100-label floor, and that is the only failed criterion (M-F5-1)."""
    tc.assertFalse(r["pass"]["passed"])
    tc.assertEqual(len(r["pass"]["reasons"]), 1, r["pass"]["reasons"])
    tc.assertIn("fewer than 100", r["pass"]["reasons"][0])


class TestPassRule(unittest.TestCase):
    def test_constants_are_the_preregistered_ones(self):
        from second_scribe import judge_bench as jb
        self.assertEqual((jb.PASS_ACCURACY, jb.PASS_CLASS_RECALL, jb.PASS_STYLE_GAP, jb.PASS_ALIGNMENT_F1,
                          jb.PASS_OFF_RECORD_RECALL, jb.PASS_NOT_JUDGED),
                         (0.90, 0.80, 0.05, 0.85, 0.85, 0.02))
        self.assertEqual(jb.PASS_CLASSES, ("wished", "negated", "other_person", "hypothetical", "unsupported"))

    def test_passes(self):
        self.assertEqual(pass_rule(make_results()), (True, []))

    def test_boundaries_pass_just_beyond_fail(self):
        edge = make_results(modality={"accuracy": 0.90, "recall": {**{c: 0.80 for c in MODALITIES}, "actual": 0.85},
                                                    "precision": {"wished": 0.80}, "style_gap": 0.05},
                            alignment={"strict": {"f1": 0.85}}, off_record={"recall": 0.85, "mention_recall": 0.85, "n_off_record": 100},
                            not_judged_rate=0.02)
        self.assertEqual(pass_rule(edge), (True, []))
        for over, word in [({"modality": {"accuracy": 0.899}}, "accuracy"),
                           ({"modality": {"style_gap": 0.051}}, "style gap"),
                           ({"alignment": {"strict": {"f1": 0.849}}}, "alignment"),
                           ({"off_record": {"recall": 0.849}}, "off-record"),
                           ({"not_judged_rate": 0.021}, "not-judged")]:
            ok, reasons = pass_rule(make_results(**over))
            self.assertFalse(ok)
            self.assertEqual(len(reasons), 1)
            self.assertIn(word, reasons[0])

    def test_each_required_class_is_checked_and_actual_has_its_own_floor(self):
        for c in ("wished", "negated", "other_person", "hypothetical", "unsupported"):
            ok, reasons = pass_rule(make_results(modality={"recall": {c: 0.79}}))
            self.assertFalse(ok)
            self.assertEqual(reasons, [f"recall on {c} 0.790 < 0.8"])
        # actual recall has its own floor (0.85, see tests/test_judge_bench_floors.py)
        self.assertEqual(pass_rule(make_results(modality={"recall": {"actual": 0.1}}))[1],
                         ["recall on actual 0.100 < 0.85"])

    def test_an_uncomputable_metric_fails(self):
        ok, reasons = pass_rule(make_results(modality={"accuracy": None, "recall": {"wished": None},
                                                       "style_gap": None}))
        self.assertFalse(ok)
        self.assertEqual(len(reasons), 3)


def tiny_items():
    mk = lambda i, exp, kind, style: {"element_id": f"i{i}", "expected": exp, "kind": kind, "style": style}
    return [mk(0, "actual", "plain", "prose"), mk(1, "wished", "wished", "prose"), mk(2, "negated", "negated", "prose"),
            mk(3, "actual", "plain", "speech"), mk(4, "wished", "wished", "speech"), mk(5, "negated", "negated", "speech")]


class TestScoring(unittest.TestCase):
    def test_accuracy_recall_confusion_style_and_not_judged(self):
        labels = {"i0": "actual", "i1": "wished", "i2": "actual", "i3": "actual", "i4": "actual", "i5": "unsupported"}
        s = score_modality(tiny_items(), labels, {"i5"})
        self.assertEqual((s["n"], s["n_judged"], s["n_not_judged"]), (6, 5, 1))
        self.assertAlmostEqual(s["accuracy"], 3 / 5)                    # i5 is out of the denominator
        self.assertAlmostEqual(s["accuracy_incl_not_judged"], 3 / 6)
        self.assertEqual(s["recall"]["actual"], 1.0)
        self.assertEqual(s["recall"]["wished"], 0.5)
        self.assertEqual(s["recall"]["negated"], 0.0)                   # i2 judged, i5 not
        self.assertIsNone(s["recall"]["hypothetical"])                  # no such items
        self.assertEqual(s["confusion"]["negated"]["actual"], 1)
        self.assertEqual(s["confusion"]["wished"]["actual"], 1)
        self.assertEqual(sum(sum(r.values()) for r in s["confusion"].values()), 5)
        self.assertAlmostEqual(s["by_style"]["prose"], 2 / 3)
        self.assertAlmostEqual(s["by_style"]["speech"], 1 / 2)
        self.assertAlmostEqual(s["style_gap"], abs(2 / 3 - 1 / 2))
        self.assertEqual(s["by_kind"]["plain"], 1.0)
        self.assertEqual([r["judged"] for r in s["items"]], [True] * 5 + [False])

    def test_style_gap_is_absolute(self):
        labels = {"i0": "actual", "i1": "wished", "i2": "actual", "i3": "actual", "i4": "wished", "i5": "negated"}
        s = score_modality(tiny_items(), labels, set())                 # speech 3/3, prose 2/3
        self.assertAlmostEqual(s["style_gap"], 1 / 3)

    def test_calibration_hand_computed(self):
        def d(label, p):
            rest = (1 - p) / 5
            return {m: (p if m == label else rest) for m in MODALITIES}
        # four items at confidence 0.8, three right: one bin with acc 0.75, conf 0.8 -> ECE 0.05;
        # Brier(top) = (3 * 0.2^2 + 1 * 0.8^2) / 4 = 0.19
        rows = [(d("actual", 0.8), "actual")] * 3 + [(d("actual", 0.8), "wished")]
        c = calibration(rows)
        self.assertAlmostEqual(c["ece"], 0.05)
        self.assertAlmostEqual(c["brier_top"], 0.19)
        self.assertEqual(c["n"], 4)
        # confident and right: zero error; confident and wrong: ECE = confidence
        self.assertAlmostEqual(calibration([(d("wished", 1.0), "wished")])["ece"], 0.0)
        self.assertAlmostEqual(calibration([(d("wished", 1.0), "actual")])["ece"], 1.0)
        self.assertIsNone(calibration([])["ece"])

    def test_both_wrong_counts_agreeing_errors_per_kind(self):
        def res(preds):
            items = [{"id": f"i{k}", "expected": e, "kind": kind, "style": "prose", "pred": p, "judged": j}
                     for k, (e, kind, p, j) in enumerate(preds)]
            return {"modality": {"items": items}}
        a = res([("wished", "wished", "actual", True), ("wished", "wished", "actual", True),
                 ("wished", "wished", "wished", True), ("negated", "negated", "actual", True),
                 ("negated", "negated", "unsupported", False)])
        b = res([("wished", "wished", "actual", True), ("wished", "wished", "negated", True),
                 ("wished", "wished", "actual", True), ("negated", "negated", "actual", True),
                 ("negated", "negated", "unsupported", True)])
        bw = both_wrong(a, b)
        # wished: agree on item 0 (both wrong) only; negated: item 3 agrees and is wrong, item 4 not judged by a
        self.assertEqual(bw["wished"], {"n": 3, "n_agree": 1, "both_wrong_agree": 1, "share": 1 / 3,
                                        "share_of_agreements_wrong": 1.0})
        self.assertEqual(bw["negated"], {"n": 1, "n_agree": 1, "both_wrong_agree": 1, "share": 1.0,
                                         "share_of_agreements_wrong": 1.0})
        self.assertEqual(bw["all"], {"n": 4, "n_agree": 2, "both_wrong_agree": 2, "share": 0.5,
                                     "share_of_agreements_wrong": 1.0})
        # two judges that agree and are right: no agreement is wrong; never agreeing: None
        right = res([("wished", "wished", "wished", True)])
        self.assertEqual(both_wrong(right, right)["wished"]["share_of_agreements_wrong"], 0.0)
        c1, c2 = res([("wished", "wished", "actual", True)]), res([("wished", "wished", "negated", True)])
        self.assertIsNone(both_wrong(c1, c2)["wished"]["share_of_agreements_wrong"])

    def test_both_wrong_refuses_results_on_different_items(self):
        row = lambda i: {"id": i, "expected": "actual", "kind": "plain", "style": "prose", "pred": "actual", "judged": True}
        with self.assertRaises(ValueError):
            both_wrong({"modality": {"items": [row("a"), row("b")]}}, {"modality": {"items": [row("a"), row("c")]}})

    def test_mcnemar_exact_p(self):
        self.assertEqual(mcnemar_exact_p(0, 0), 1.0)
        self.assertAlmostEqual(mcnemar_exact_p(5, 0), 0.0625)
        self.assertAlmostEqual(mcnemar_exact_p(10, 2), 2 * 79 / 4096)
        self.assertEqual(mcnemar_exact_p(3, 3), 1.0)
        self.assertEqual(mcnemar_exact_p(2, 7), mcnemar_exact_p(7, 2))

    def test_style_gap_reports_the_discordant_pairs(self):
        mk = lambda base, style, exp="wished": {"element_id": f"{base}:{style}", "base": base, "expected": exp,
                                                "kind": "wished", "style": style}
        items = [mk(f"b{i}", st) for i in range(4) for st in ("prose", "speech")]
        labels = {i["element_id"]: "wished" for i in items}
        labels["b0:speech"] = labels["b1:speech"] = "actual"       # prose right, speech wrong: twice
        labels["b2:prose"] = "actual"                              # speech right, prose wrong: once
        d = score_modality(items, labels, set())["style_discordant"]
        self.assertEqual((d["prose_right_speech_wrong"], d["speech_right_prose_wrong"], d["n_pairs"]), (2, 1, 4))
        self.assertAlmostEqual(d["mcnemar_exact_p"], mcnemar_exact_p(2, 1))
        # a pair with one side not judged is not a pair
        d2 = score_modality(items, labels, {"b0:prose"})["style_discordant"]
        self.assertEqual(d2["n_pairs"], 3)
        self.assertIsNone(score_modality(tiny_items(), {i["element_id"]: i["expected"] for i in tiny_items()},
                                         set())["style_discordant"])


def distinct_requests():
    return len({(i["element"], i["quote"], i["speaker"], i.get("reply")) for i in dedupe_items(build_modality_set(CORPUS))})


class TestBench(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.runs = {}

    @classmethod
    def run_bench(cls, name, client, mode):
        log = AuditLog(Path(cls.tmp) / f"{name}.jsonl", name, clock=lambda: "t")
        cls.runs[name] = bench(client, PROMPTS, CORPUS, log, mode=mode)
        return cls.runs[name]

    def test_oracle_generative_scores_one_everywhere_and_passes(self):
        r = self.run_bench("oracle_gen", OracleClient(CORPUS), "generative")
        only_the_pool_floor_fails(self, r)
        m = r["modality"]
        self.assertEqual((m["accuracy"], m["style_gap"], r["not_judged_rate"], r["unparsed_rate"]), (1.0, 0.0, 0.0, 0.0))
        self.assertTrue(all(v == 1.0 for v in m["recall"].values()), m["recall"])
        self.assertTrue(all(v == 1.0 for v in m["by_kind"].values()))
        for k in ("strict", "lenient"):
            self.assertEqual((r["alignment"][k]["precision"], r["alignment"][k]["recall"], r["alignment"][k]["f1"]),
                             (1.0, 1.0, 1.0))
        self.assertEqual(r["alignment"]["ai_only"]["f1"], 1.0)
        self.assertEqual((r["off_record"]["recall"], r["off_record"]["precision"]), (1.0, 1.0))
        self.assertGreater(r["off_record"]["n_off_record"], 0)
        self.assertNotIn("calibration", m)
        self.assertGreater(r["timing"]["modality"]["calls"], 0)

    def test_oracle_logprob_scores_one_everywhere_passes_and_is_calibrated(self):
        r = self.run_bench("oracle_lp", OracleClient(CORPUS), "logprob")
        only_the_pool_floor_fails(self, r)
        m = r["modality"]
        self.assertEqual((m["accuracy"], r["not_judged_rate"]), (1.0, 0.0))
        self.assertEqual(r["judge_stats"]["calls"], distinct_requests())      # one call per distinct item
        self.assertLess(m["calibration"]["ece"], 0.02)               # confidence ~0.99, always right
        self.assertLess(m["calibration"]["brier_top"], 0.001)
        self.assertEqual(m["calibration"]["n"], m["n_judged"])

    def test_always_actual_fails_with_the_right_reasons(self):
        r = self.run_bench("always_actual", OracleClient(CORPUS, fixed_label="actual"), "generative")
        ok, reasons = r["pass"]["passed"], r["pass"]["reasons"]
        self.assertFalse(ok)
        text = "\n".join(reasons)
        self.assertIn("modality accuracy", text)
        for c in ("wished", "negated", "other_person", "hypothetical", "unsupported"):
            self.assertIn(f"recall on {c} 0.000", text)
        self.assertNotIn("recall on actual", text)
        self.assertNotIn("style gap", text)                          # both styles equally wrong
        self.assertNotIn("alignment", text)                          # the helper prompts are still answered
        self.assertEqual(r["modality"]["recall"]["actual"], 1.0)
        scored_wished = [i for i in dedupe_items(build_modality_set(CORPUS)) if i["expected"] == "wished" and not is_excluded(i)]
        self.assertEqual(r["modality"]["confusion"]["wished"]["actual"], len(scored_wished))    # trap + bank sentences, two styles
        self.assertEqual(pass_rule(r), (False, reasons))

    def test_logprob_confidence_of_a_known_size_gives_the_expected_ece(self):
        base = OracleClient(CORPUS)

        class Hedging:
            def complete(self, messages, **kw):
                resp = base.complete(messages, **kw)
                if resp.logprobs is None:
                    return resp
                import math
                return ModelResponse(resp.text, resp.finish_reason, {}, 0, "h",
                                     logprobs=[[resp.text, math.log(0.7)], ["F" if resp.text != "F" else "A", math.log(0.3)]])
        r = self.run_bench("hedge", Hedging(), "logprob")
        self.assertEqual(r["modality"]["accuracy"], 1.0)
        self.assertAlmostEqual(r["modality"]["calibration"]["ece"], 0.3, places=6)    # 70% sure, 100% right
        self.assertAlmostEqual(r["modality"]["calibration"]["brier_top"], 0.09, places=6)

    def test_a_server_without_logprobs_judges_nothing_in_logprob_mode(self):
        r = self.run_bench("nolp", OracleClient(CORPUS, with_logprobs=False), "logprob")
        self.assertEqual(r["not_judged_rate"], 1.0)
        self.assertIsNone(r["modality"]["accuracy"])
        self.assertFalse(r["pass"]["passed"])
        self.assertIn(f"not-judged rate 1.000 > {PASS_NOT_JUDGED:g}", r["pass"]["reasons"])
        self.assertEqual(r["judge_stats"]["no_logprobs"], r["modality"]["n_all"])

    def test_keyword_floor_arm_and_the_claims_table(self):
        r = self.runs.get("oracle_gen") or self.run_bench("oracle_gen", OracleClient(CORPUS), "generative")
        fl = r["keyword_floor"]
        self.assertLess(fl["modality"]["accuracy"], 1.0)
        self.assertGreater(fl["modality"]["accuracy"], 0.3)
        self.assertEqual(fl["not_judged_rate"], 0.0)
        self.assertLess(fl["alignment"]["strict"]["recall"], 0.2)           # lexical only: exact but thin
        self.assertEqual(fl["alignment"]["strict"]["precision"], 1.0)
        arms = r["arms"]
        self.assertIs(arms["modality accuracy"]["ai_only"], True)
        self.assertEqual(arms["modality accuracy"]["keyword_only"], fl["modality"]["accuracy"] >= PASS_ACCURACY)
        self.assertIs(arms["alignment F1 (strict)"]["keyword_only"], False)
        self.assertIs(arms["alignment F1 (strict)"]["combined"], True)
        self.assertIsNone(arms["modality accuracy"]["combined"])
        self.assertIsNone(arms["off-record recall"]["ai_only"])

    def test_results_are_json_and_markdown_renders(self):
        r = self.runs.get("oracle_lp") or self.run_bench("oracle_lp", OracleClient(CORPUS), "logprob")
        json.dumps(r)
        md = render_markdown(r)
        for needle in ("Pass rule (pooled): FAIL", "Modality accuracy", "Confusion matrix", "ECE", "keyword/lexical only",
                       "Style gap", "Off-record recall"):
            self.assertIn(needle, md)

    def test_logprob_replay_round_trip_reproduces_the_results_exactly(self):
        # a mock server that returns real top_logprobs (not just text): record, then replay offline
        base = OracleClient(CORPUS)

        class Noisy:
            def complete(self, messages, **kw):
                resp = base.complete(messages, **kw)
                if resp.logprobs is None:
                    return resp
                other = "F" if resp.text != "F" else "A"
                return ModelResponse(resp.text, resp.finish_reason, {"completion_tokens": 1}, 5, "h",
                                     logprobs=[[resp.text, -0.4], [" " + other, -1.2], ["The", -2.0]])
        path = Path(self.tmp) / "fx.json"
        recorder = RecordingClient(Noisy(), str(path), merge=False)
        live = self.run_bench("rec", recorder, "logprob")
        recorder.save()
        fx = json.loads(path.read_text())
        self.assertTrue(any(isinstance(v, dict) and v.get("logprobs") and v["logprobs"][1][0].startswith(" ")
                            for v in fx.values()))
        replayed = self.run_bench("rep", ReplayClient(str(path)), "logprob")
        for k in ("modality", "alignment", "off_record", "not_judged_rate", "keyword_floor", "arms", "pass"):
            self.assertEqual(live[k], replayed[k], k)
        self.assertGreater(live["modality"]["calibration"]["ece"], 0.0)

    def test_batches_mix_the_labels_and_the_order_is_deterministic(self):
        seen = []
        base = OracleClient(CORPUS)

        class Spy:
            def complete(self, messages, **kw):
                resp = base.complete(messages, **kw)
                p = messages[-1]["content"]
                if "ITEMS:\n" in p:
                    body = p.split("ITEMS:\n", 1)[1].split("\nREMINDER:")[0]
                    from second_scribe.judge_bench import _ITEM_RE
                    seen.append([base._label(e, q, r) for _n, e, q, r in _ITEM_RE.findall(body)])
                return resp
        runs = []
        for k in range(2):
            seen.clear()
            self.run_bench(f"mix{k}", Spy(), "generative")
            runs.append([list(b) for b in seen if len(b) == 20])
        self.assertEqual(runs[0], runs[1])                                   # deterministic
        full = runs[0]
        self.assertGreater(len(full), 10)
        mixed = sum(len(set(b)) > 1 for b in full)
        self.assertGreaterEqual(mixed, len(full) - 1)                        # (almost) no single-label batch
        self.assertGreater(sum(len(set(b)) >= 3 for b in full), len(full) // 2)

    def test_timing_reports_calls_per_second_and_drops_tokens_per_second_for_one_token_calls(self):
        for mode in ("logprob", "generative"):
            r = self.runs.get("oracle_lp" if mode == "logprob" else "oracle_gen") or \
                self.run_bench("oracle_lp" if mode == "logprob" else "oracle_gen", OracleClient(CORPUS), mode)
            self.assertIn("calls_per_s", r["timing"]["modality"])
            if mode == "logprob":
                self.assertIsNone(r["timing"]["modality"]["tokens_per_s"])
                self.assertIsNone(r["timing"]["all"]["tokens_per_s"])

    def test_bad_mode(self):
        log = AuditLog(Path(self.tmp) / "bad.jsonl", "bad", clock=lambda: "t")
        with self.assertRaises(ValueError):
            bench(OracleClient(CORPUS), PROMPTS, CORPUS, log, mode="nope")


class TestWilson(unittest.TestCase):
    def test_known_values(self):
        lo, hi = wilson(50, 100)
        self.assertAlmostEqual(lo, 0.4038, places=3)
        self.assertAlmostEqual(hi, 0.5962, places=3)
        lo, hi = wilson(10, 10)
        self.assertAlmostEqual(lo, 0.7225, places=3)
        self.assertEqual(hi, 1.0)
        lo, hi = wilson(0, 10)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 0.2775, places=3)
        self.assertIsNone(wilson(0, 0))

    def test_narrows_with_n(self):
        a, b = wilson(9, 10), wilson(90, 100)
        self.assertGreater(a[1] - a[0], b[1] - b[0])


class TestMappingTalkExcluded(unittest.TestCase):
    def test_excluded_from_accuracy_recall_and_gap_but_reported_by_kind(self):
        items = tiny_items() + [{"element_id": "m0", "expected": "actual", "kind": "mapping_talk", "style": "prose"},
                                {"element_id": "m1", "expected": "actual", "kind": "mapping_talk", "style": "speech"}]
        labels = {i["element_id"]: i["expected"] for i in items}
        labels.update({"m0": "unsupported", "m1": "unsupported"})            # wrong on the excluded kind only
        s = score_modality(items, labels, set())
        self.assertEqual((s["accuracy"], s["style_gap"], s["recall"]["actual"]), (1.0, 0.0, 1.0))
        self.assertEqual((s["n"], s["n_all"], s["n_excluded"]), (6, 8, 2))
        self.assertEqual(s["recall_n"]["actual"], 2)                          # m0, m1 not in the class
        self.assertEqual((s["by_kind"]["mapping_talk"], s["by_kind_n"]["mapping_talk"]), (0.0, 2))
        self.assertEqual(s["by_kind_ci"]["mapping_talk"], wilson(0, 2))
        self.assertEqual([r["excluded"] for r in s["items"]], [False] * 6 + [True, True])
        self.assertEqual(sum(sum(r.values()) for r in s["confusion"].values()), 6)

    def test_not_judged_excluded_items_still_count_in_the_not_judged_rate_over_all_items(self):
        items = tiny_items() + [{"element_id": "m0", "expected": "actual", "kind": "mapping_talk", "style": "prose"}]
        labels = {i["element_id"]: i["expected"] for i in items}
        s = score_modality(items, labels, {"m0"})
        self.assertEqual((s["n_not_judged"], s["n_not_judged_all"], s["n_all"]), (0, 1, 7))

    def test_both_wrong_all_leaves_out_excluded_kinds_but_lists_them(self):
        row = lambda i, kind, pred, ex: {"id": i, "expected": "actual", "kind": kind, "style": "prose", "pred": pred,
                                         "judged": True, "excluded": ex}
        r = {"modality": {"items": [row("a", "mapping_talk", "wished", True), row("b", "plain", "actual", False)]}}
        bw = both_wrong(r, r)
        self.assertEqual(bw["mapping_talk"]["both_wrong_agree"], 1)
        self.assertEqual((bw["all"]["n"], bw["all"]["both_wrong_agree"]), (1, 0))

    def test_workaround_dead_end_and_planted_hypothetical_are_excluded_with_reasons(self):
        self.assertEqual(EXCLUDED_KINDS, ("mapping_talk", "workaround", "other_person", "dead_end", "hypothetical"))
        self.assertEqual(set(EXCLUDED_REASONS), set(EXCLUDED_KINDS))
        items = build_modality_set(CORPUS)
        self.assertEqual({i["expected"] for i in items if i["kind"] == "dead_end"}, {"actual"})
        self.assertTrue(any(i["kind"] == "hypothetical" for i in items))
        self.assertTrue(any(i["kind"] == "bank_hypothetical" for i in items))      # the bank's own are scored
        row = lambda i, kind: {"element_id": i, "expected": "actual", "kind": kind, "style": "prose"}
        its = [row("a", "workaround"), row("b", "dead_end"), row("c", "plain")]
        s = score_modality(its, {"a": "unsupported", "b": "unsupported", "c": "actual"}, set())
        self.assertEqual((s["accuracy"], s["n"], s["n_excluded"]), (1.0, 1, 2))
        self.assertEqual((s["by_kind"]["workaround"], s["by_kind"]["dead_end"]), (0.0, 0.0))     # still by kind
        m = score_modality(items, {i["element_id"]: i["expected"] for i in items}, set())
        for kind in ("dead_end", "hypothetical"):
            tids = {i["template"] for i in items if i["kind"] == kind}
            self.assertTrue(tids)
            for tid in tids:
                self.assertTrue(m["by_template"][tid]["excluded"])
                self.assertEqual(m["by_template"][tid]["reason"], EXCLUDED_REASONS[kind])

    def test_a_judge_wrong_only_on_mapping_talk_still_passes(self):
        keys = {(it["element"], " ".join(it["quote"].split())) for it in build_modality_set(CORPUS)
                if it["kind"] == "mapping_talk"}

        class WrongOnMappingTalk(OracleClient):
            def _label(self, element, quote, reply=""):
                return ("unsupported" if (element, " ".join(quote.split())) in keys
                        else super()._label(element, quote, reply))
        with tempfile.TemporaryDirectory() as tmp:
            r = bench(WrongOnMappingTalk(CORPUS), PROMPTS, CORPUS, AuditLog(Path(tmp) / "l.jsonl", "l"), mode="logprob")
        only_the_pool_floor_fails(self, r)
        self.assertEqual(r["modality"]["accuracy"], 1.0)
        self.assertEqual(r["modality"]["by_kind"]["mapping_talk"], 0.0)
        # mapping_talk, workaround, other_person, dead_end, hypothetical, two styles, distinct sentences only (a sentence
        # of the same template and concept drawn for two participants is scored once)
        kinds = len(dedupe_items([i for i in build_modality_set(CORPUS)
                                  if i["kind"] in ("mapping_talk", "workaround", "other_person", "dead_end",
                                                   "hypothetical")]))
        # the corpus fills are all safe since the F6 gold fix (tests/test_synth_gold.py), so no item is excluded one by one
        contradicting = len([i for i in build_modality_set(CORPUS) if i.get("exclude")])
        self.assertEqual(contradicting, 0)
        self.assertEqual(r["modality"]["n_excluded"], kinds + contradicting)
        self.assertIn("mapping_talk", render_markdown(r))


class TestPlantedOtherPersonExcluded(unittest.TestCase):
    def test_planted_other_person_is_not_scored_and_is_listed_by_template_with_the_reason(self):
        items = build_modality_set(CORPUS)
        planted = [i for i in items if i["kind"] == "other_person"]
        self.assertEqual(len(planted), 2 * planted_counts()["other_person"])      # two styles
        self.assertTrue(all(i["template"].startswith("trap:other_person:") for i in planted))
        bank = [i for i in items if i["kind"] == "bank_other_person"]
        self.assertTrue(bank)
        # a judge that gets only the planted ones wrong is still perfect on everything scored
        preds = {i["element_id"]: ("actual" if i["kind"] == "other_person" else i["expected"]) for i in items}
        m = score_modality(items, preds, set(), n_boot=50)
        self.assertEqual(m["recall"]["other_person"], 1.0)
        self.assertEqual(m["recall_n"]["other_person"], len(bank))
        self.assertEqual(m["accuracy"], 1.0)
        self.assertEqual(m["n_excluded"], len([i for i in items if is_excluded(i)]))
        ex = {t: v for t, v in m["by_template"].items() if v.get("excluded")}
        self.assertTrue({i["template"] for i in planted} <= set(ex))
        for v in ex.values():
            self.assertIsNone(v["recall"])
            self.assertTrue(v["reason"])
        self.assertIn("negated outranks other_person", ex[planted[0]["template"]]["reason"])
        self.assertFalse(any(v.get("excluded") for t, v in m["by_template"].items() if t.startswith("bank:")))
        self.assertEqual(m["by_kind"]["other_person"], 0.0)      # still reported by kind

    def test_markdown_lists_the_excluded_templates(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = bench(OracleClient(CORPUS), PROMPTS, CORPUS, AuditLog(Path(tmp) / "l.jsonl", "l"), mode="logprob")
        md = render_markdown(r)
        self.assertIn("## Templates excluded from scoring", md)
        self.assertIn("trap:other_person:0", md)
        self.assertIn("other_person", r["modality"]["excluded_kinds"])


class TestPooledSeeds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.r = bench(OracleClient(seeds=(0, 1, 2)), PROMPTS, None, AuditLog(Path(cls.tmp) / "l.jsonl", "l"),
                      mode="logprob", seeds=(0, 1, 2))

    def test_default_seeds_are_zero_to_four(self):
        self.assertEqual(DEFAULT_SEEDS, (0, 1, 2, 3, 4))

    def test_pooled_counts_are_the_sum_of_the_per_seed_counts(self):
        r, ps = self.r, self.r["per_seed"]
        self.assertEqual(r["seeds"], ["0", "1", "2"])
        self.assertEqual(list(ps), ["0", "1", "2"])
        self.assertEqual(r["n_items"], sum(p["modality"]["n_all"] for p in ps.values()))
        self.assertEqual(r["modality"]["n"], sum(p["modality"]["n"] for p in ps.values()))
        for c in MODALITIES:
            self.assertEqual(r["modality"]["recall_n"][c], sum(p["modality"]["recall_n"][c] for p in ps.values()), c)
        for name in ("strict", "lenient", "ai_only", "lexical_only"):
            for k in ("tp", "n_truth"):
                self.assertEqual(r["alignment"][name][k], sum(p["alignment"][name][k] for p in ps.values()), (name, k))
        # pooled off-record = the per-seed counts + the extra corpora added to reach the minimum size
        self.assertEqual(r["off_record"]["counts"],
                         {k: sum(p["off_record"]["counts"][k] for p in ps.values()) + r["off_record"]["extra_counts"][k]
                          for k in ("tp", "fp", "fn", "tn")})
        self.assertGreater(r["modality"]["n"], 3 * 280)                       # roughly 3x one corpus

    def test_pooled_corpora_are_not_copies_of_each_other(self):
        ps = self.r["per_seed"]
        self.assertGreaterEqual(len({p["modality"]["recall_n"]["wished"] for p in ps.values()}
                                    | {p["modality"]["recall_n"]["negated"] for p in ps.values()}), 2)

    def test_pass_rule_is_on_the_pooled_set_and_each_seed_has_its_own_verdict(self):
        self.assertEqual(self.r["pass"], {"passed": True, "reasons": []})
        self.assertEqual(self.r["pass"], dict(zip(("passed", "reasons"), pass_rule(self.r))))
        self.assertTrue(all(p["pass"]["passed"] for p in self.r["per_seed"].values()))
        self.assertNotIn("items", self.r["per_seed"]["0"]["modality"])

    def test_every_rate_has_a_wilson_interval_that_contains_it(self):
        r, m = self.r, self.r["modality"]
        self.assertEqual(m["accuracy_ci"], wilson(m["n_judged"], m["n_judged"]))
        pairs = [(m["accuracy"], m["accuracy_ci"])] + [(m["recall"][c], m["recall_ci"][c]) for c in MODALITIES] \
            + [(m["by_style"][s], m["by_style_ci"][s]) for s in ("prose", "speech")] \
            + [(m["by_kind"][k], m["by_kind_ci"][k]) for k in m["by_kind"]] \
            + [(r["alignment"]["strict"]["precision"], r["alignment"]["strict"]["precision_ci"]),
               (r["alignment"]["strict"]["recall"], r["alignment"]["strict"]["recall_ci"]),
               (r["off_record"]["recall"], r["off_record"]["recall_ci"]),
               (r["off_record"]["precision"], r["off_record"]["precision_ci"]),
               (r["not_judged_rate"], r["not_judged_rate_ci"])]
        for v, ci in pairs:
            self.assertLessEqual(ci[0] - 1e-12, v)
            self.assertLessEqual(v, ci[1] + 1e-12)
        self.assertEqual(r["off_record"]["recall_ci"], wilson(r["off_record"]["n_off_record"], r["off_record"]["n_off_record"]))

    def test_markdown_shows_intervals_n_per_class_and_the_per_seed_table(self):
        md = render_markdown(self.r)
        for needle in ("[95% CI]", "Recall, wished (n=", "Per corpus", "| 0 |", "| 2 |", "Corpora pooled: 0, 1, 2",
                       "mapping_talk"):
            self.assertIn(needle, md)
        json.dumps(self.r)


class TestSeedsOption(unittest.TestCase):
    def test_cli_seeds_option_pools_those_corpora(self):
        out = Path(tmpdir(self))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--mode", "logprob", "--out", str(out), "--client", "oracle", "--seeds", "1,3"]), 0)
        res = json.loads((out / "bench.logprob.json").read_text())
        self.assertEqual((res["seeds"], list(res["per_seed"])), (["1", "3"], ["1", "3"]))

    def test_bad_seeds_is_a_usage_error(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            main(["--mode", "logprob", "--out", tmpdir(self), "--seeds", "a,b"])
        self.assertEqual(cm.exception.code, 2)

    def test_default_cli_pools_five_seeds(self):
        out = Path(tmpdir(self))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--mode", "logprob", "--out", str(out), "--client", "oracle"]), 0)
        res = json.loads((out / "bench.logprob.json").read_text())
        self.assertEqual(res["seeds"], ["0", "1", "2", "3", "4"])
        self.assertTrue(res["pass"]["passed"])


class TestBenchBank(unittest.TestCase):
    def fills(self, template):
        # C19's own wording holds an "if", a cue in every sentence it is slotted into
        return [render(template, c.forms[1]) for c in CONCEPTS if c.cid != "C19"]

    def test_a_dozen_distinct_templates_per_class_each_with_one_slot(self):
        self.assertEqual(set(BENCH_TEMPLATES), {"wished", "negated", "hypothetical", "other_person", "actual"})
        for label, templates in BENCH_TEMPLATES.items():
            self.assertGreaterEqual(len(templates), 12, label)
            self.assertEqual(len(set(templates)), len(templates), label)
            for tpl in templates:
                self.assertEqual(tpl.count("{x}"), 1, tpl)
                self.assertTrue(tpl.rstrip().endswith((".", "?")), tpl)

    def test_templates_do_not_repeat_the_corpus_builders_own_templates(self):
        from second_scribe.synth.bank import ORDINARY_TURNS, TRAP_TEMPLATES
        own = {t for ts in TRAP_TEMPLATES.values() for t in ts} | set(ORDINARY_TURNS)
        for templates in BENCH_TEMPLATES.values():
            self.assertFalse(own & set(templates))

    def test_at_least_four_templates_per_class_carry_no_cue_word_and_a_keyword_rule_gets_them_wrong(self):
        for label in ("wished", "negated", "hypothetical", "other_person"):
            blind = [t for t in BENCH_TEMPLATES[label]
                     if all(keyword_modality(x) == "actual" for x in self.fills(t))]
            self.assertGreaterEqual(len(blind), 4, label)        # no cue at all: the rule says "actual"
            wrong = [t for t in BENCH_TEMPLATES[label] if all(keyword_modality(x) != label for x in self.fills(t))]
            self.assertGreaterEqual(len(wrong), len(blind), label)
        # and the experienced sentences include ones with a cue word, which the rule gets wrong the other way
        cued = [t for t in BENCH_TEMPLATES["actual"] if all(keyword_modality(x) != "actual" for x in self.fills(t))]
        self.assertGreaterEqual(len(cued), 4)

    def test_labels_stay_clear_under_the_precedence(self):
        # structural lint behind "one clearly correct label": precedence is actual > wished > negated >
        # other_person > hypothetical, so an other_person sentence must not deny anything about the speaker (that
        # would be a negation), a negated one must not wish or name another person, a hypothetical one must not
        # claim it happened, and a wished one must not claim it happened
        deny = re.compile(r"\b(not us|not ours|we didn't|we did not|never part|not me|we never|we were)\b", re.I)
        for t in BENCH_TEMPLATES["other_person"]:
            self.assertIsNone(deny.search(t), t)
        wish = re.compile(r"\b(wish|hope|hoping|needed|wanted|should have|would like|would have loved|if only|longed)\b", re.I)
        other = re.compile(r"\b(my (cousin|friend|neighbour|brother|sister|mother|father)|someone else|another owner)\b", re.I)
        for t in BENCH_TEMPLATES["negated"]:
            self.assertIsNone(wish.search(t), t)
            self.assertIsNone(other.search(t), t)
        happened = re.compile(r"\b(we (got|had|went through|dealt with|finished|did)|took us|happened to us)\b", re.I)
        for t in BENCH_TEMPLATES["wished"]:
            self.assertIsNone(happened.search(t), t)
        conditional = re.compile(r"\b(might|may|could|would|if|had we|perhaps|maybe|possibly|guessing|assuming|depend"
                                 r"|should take|who knows|in another world)\b", re.I)
        for t in BENCH_TEMPLATES["hypothetical"]:
            self.assertIsNotNone(conditional.search(t), t)
        for t in BENCH_TEMPLATES["actual"]:
            self.assertIsNone(wish.search(t) or other.search(t), t)

    def test_items_are_templates_times_concepts_with_template_ids(self):
        items = [i for i in build_modality_set(CORPUS) if i["style"] == "prose" and i["kind"].startswith("bank_")]
        by_t = Counter(i["template"] for i in items)
        self.assertEqual(len(by_t), sum(len(t) for t in BENCH_TEMPLATES.values()))
        self.assertEqual(set(by_t.values()), {PER_TEMPLATE})
        for i in items:
            label, k = i["template"].split(":")[1], int(i["template"].split(":")[2])
            self.assertEqual(i["expected"], label)
            concept = next(c for c in CONCEPTS if c.forms[2] == i["element"])
            self.assertEqual(i["quote"], render(BENCH_TEMPLATES[label][k], concept.forms[1]))
        other_seed = {(i["template"], i["cid"]) for i in build_modality_set(CORPUS, seed=3)
                      if i["style"] == "prose" and i["kind"].startswith("bank_")}
        self.assertNotEqual(other_seed, {(i["template"], i["cid"]) for i in items})

    def test_every_item_has_a_template_and_the_keyword_rule_cannot_solve_the_bench(self):
        items = dedupe_items([i for i in build_modality_set(CORPUS) if i["kind"] not in EXCLUDED_KINDS])
        self.assertTrue(all(i.get("template") for i in items))
        acc = sum(keyword_modality(i["quote"]) == i["expected"] for i in items) / len(items)
        self.assertLess(acc, 0.7)
        wished = [i for i in items if i["expected"] == "wished"]
        self.assertLess(sum(keyword_modality(i["quote"]) == "wished" for i in wished) / len(wished), 0.6)


class TestBankFills(unittest.TestCase):
    # independent of ``says_it_happened``: any first-person word, or a finite past-tense verb in a relative clause
    FIRST_PERSON = re.compile(r"\b(we|our|us|i|my)\b", re.I)
    PAST_EVENT = re.compile(r"\b(who|when|where|which|that)\b[^.]*?\b(\w+ed|came|went|did|had|got|took|saw|made|ran|gave)\b", re.I)
    EXPECTED_OUT = {"C04", "C05", "C07", "C14", "C22", "C25", "C26", "C30",         # "we ..."
                    "C17", "C18", "C20", "C23", "C24"}                                # event clause without a subject, "our"

    def test_fill_concepts_exclude_every_wording_that_says_it_happened_to_the_speaker(self):
        out = {c.cid for c in CONCEPTS} - {c.cid for c in FILL_CONCEPTS}
        self.assertEqual(out, self.EXPECTED_OUT)
        for c in FILL_CONCEPTS:
            self.assertIsNone(self.FIRST_PERSON.search(c.forms[1]), c.cid)
            self.assertIsNone(self.PAST_EVENT.search(c.forms[1]), (c.cid, c.forms[1]))
        for c in CONCEPTS:                       # and the lint is not vacuous: every excluded concept trips it
            if c.cid in self.EXPECTED_OUT:
                self.assertTrue(self.FIRST_PERSON.search(c.forms[1]) or self.PAST_EVENT.search(c.forms[1]), c.cid)
        self.assertTrue(says_it_happened("the visit when the health unit checked the truck over"))
        self.assertTrue(says_it_happened("the health official who came out to the truck"))
        self.assertFalse(says_it_happened("the letter saying the land could be used for vending"))

    def test_no_non_actual_bank_sentence_says_the_concept_happened_or_uses_our_in_the_fill(self):
        for seed in (0, 1, 2, 3, 4):
            items = [i for i in build_modality_set(CORPUS, seed=seed)
                     if i["style"] == "prose" and i["kind"].startswith("bank_") and i["expected"] != "actual"]
            self.assertTrue(items)
            for i in items:
                concept = next(c for c in CONCEPTS if c.cid == i["cid"])
                self.assertIn(concept, FILL_CONCEPTS, i["quote"])
                self.assertIsNone(re.search(r"\bour\b", concept.forms[1], re.I), i["quote"])
                self.assertIsNone(self.PAST_EVENT.search(concept.forms[1]), i["quote"])

    def test_actual_bank_sentences_may_use_every_concept(self):
        cids = {i["cid"] for seed in range(5) for i in build_modality_set(CORPUS, seed=seed)
                if i["style"] == "prose" and i["kind"] == "bank_actual"}
        self.assertTrue(cids - {c.cid for c in FILL_CONCEPTS})             # some excluded concepts appear in actual

    def test_planted_wished_and_negated_items_with_a_contradicting_fill_are_excluded_one_by_one(self):
        items = []
        for seed in (0, 1, 2):
            items += build_modality_set(CORPUS, seed=seed)
        flagged = [i for i in items if i.get("exclude")]
        # since the F6 gold fix the corpus never fills a planted wished/negated sentence with a contradicting wording,
        # so nothing is flagged; the rule below still holds item by item (and would catch a regression)
        self.assertEqual({i["exclude"] for i in flagged}, set())
        for i in items:
            planted = i["kind"] in ("wished", "negated", "iv_no")        # iv_no: the bench's "No." questions
            if planted:
                self.assertEqual(bool(i.get("exclude")), i["cid"] not in {c.cid for c in FILL_CONCEPTS}, i["quote"])
            else:
                self.assertFalse(i.get("exclude"), i["kind"])
        # the corpus has no contradicting fill any more, so flag one planted pair by hand to keep the per-item
        # exclusion path (both styles, reason, scoring) covered
        pick = next(i for i in items if i["kind"] == "wished")
        for i in items:
            if i["base"] == pick["base"]:                    # the planted sentence is the same in every seed's item set
                i["exclude"] = FILL_REASON
        flagged = [i for i in items if i.get("exclude")]
        self.assertTrue(flagged)
        both_styles = {(i["seed"], i["base"]) for i in flagged}
        self.assertEqual(len(flagged), 2 * len(both_styles))                # the speech twin is excluded with it
        preds = {i["element_id"]: ("actual" if i.get("exclude") else i["expected"]) for i in items}
        m = score_modality(items, preds, set())
        self.assertEqual(m["accuracy"], 1.0)                                # wrong only where excluded
        self.assertEqual(m["n_excluded"], len([i for i in items if is_excluded(i)]))
        self.assertEqual(m["excluded_items"][FILL_REASON]["n"], len(flagged))
        self.assertEqual(m["excluded_items"][FILL_REASON]["accuracy"], 0.0)
        self.assertTrue(all(r["excluded"] for r in m["items"] if r["id"] in {i["element_id"] for i in flagged}))
        self.assertFalse(any(v.get("excluded") and v["reason"] == FILL_REASON for v in m["by_template"].values()))


class TestClusterBootstrapAndDistinct(unittest.TestCase):
    def test_perfect_clusters_give_a_point_and_it_is_deterministic(self):
        self.assertEqual(cluster_bootstrap_ci([(5, 5), (7, 7), (3, 3)], 200), [1.0, 1.0])
        self.assertIsNone(cluster_bootstrap_ci([], 200))
        c = [(9, 10), (4, 10), (10, 10), (0, 10)]
        self.assertEqual(cluster_bootstrap_ci(c, 300, seed=1), cluster_bootstrap_ci(c, 300, seed=1))

    def test_templates_that_differ_widen_the_interval_far_beyond_the_item_level_one(self):
        clusters = [(10, 10)] * 6 + [(0, 10)] * 6                # recall .5; items look like n=120
        lo, hi = cluster_bootstrap_ci(clusters, 1000)
        wlo, whi = wilson(60, 120)
        self.assertLess(lo, 0.5)
        self.assertGreater(hi, 0.5)
        self.assertGreater(hi - lo, 2 * (whi - wlo))
        self.assertLessEqual(0.0, lo)
        self.assertLessEqual(hi, 1.0)

    def test_dedupe_keeps_the_first_of_a_repeated_sentence_per_style(self):
        row = lambda i, style, q, el="E": {"element_id": i, "style": style, "element": el, "prose": q,
                                            "quote": q if style == "prose" else "um " + q}
        items = [row("a", "prose", "S1"), row("b", "speech", "S1"), row("c", "prose", "S1"), row("d", "speech", "S1"),
                 row("e", "prose", "S1", el="F"), row("f", "prose", "S2")]
        self.assertEqual([i["element_id"] for i in dedupe_items(items)], ["a", "b", "e", "f"])
        with_reply = [{**row("g", "prose", "S3"), "reply": "Yes."}, {**row("h", "prose", "S3"), "reply": "No."}]
        self.assertEqual(len(dedupe_items(with_reply)), 2)

    def test_bench_scores_distinct_sentences_and_reports_per_template_recall(self):
        base = OracleClient(CORPUS)
        target = "bank:negated:03"            # "We skipped {x} entirely."

        class WrongOnOneTemplate(OracleClient):
            def __init__(self):
                super().__init__(CORPUS)
                self.bad = {(it["element"], " ".join(it["quote"].split()))
                            for it in build_modality_set(CORPUS) if it["template"] == target}

            def _label(self, element, quote, reply=""):
                return "actual" if (element, " ".join(quote.split())) in self.bad else super()._label(element, quote, reply)
        with tempfile.TemporaryDirectory() as tmp:
            r = bench(WrongOnOneTemplate(), PROMPTS, CORPUS, AuditLog(Path(tmp) / "l.jsonl", "l"), mode="logprob")
        m = r["modality"]
        self.assertEqual(m["by_template"][target]["recall"], 0.0)
        self.assertEqual(m["by_template"][target]["n"], 2 * PER_TEMPLATE)
        self.assertEqual(m["by_template"][target]["text"], "We skipped {x} entirely.")
        self.assertEqual(m["by_template"]["bank:negated:00"]["recall"], 1.0)
        used = {i["template"] for i in build_modality_set(CORPUS)
                if i["expected"] == "negated" and not i.get("exclude") and i["kind"] not in EXCLUDED_KINDS}   # scored ones only
        self.assertEqual(m["n_templates"]["negated"], len(used))                 # bank + the trap templates the corpus used + iv_no
        self.assertGreaterEqual(len(used), len(BENCH_TEMPLATES["negated"]) + 3)
        self.assertLess(m["recall"]["negated"], 1.0)
        self.assertLess(m["recall_ci"]["negated"][0], m["recall"]["negated"])
        self.assertEqual(m["recall_ci"]["wished"], [1.0, 1.0])
        for c in MODALITIES:
            self.assertEqual(m["recall_n"][c], 2 * m["distinct_n"][c], c)       # two styles of each distinct sentence
        self.assertEqual(m["recall_ci_item"]["negated"], wilson(round(m["recall"]["negated"] * m["recall_n"]["negated"]),
                                                                 m["recall_n"]["negated"]))
        md = render_markdown(r)
        for needle in ("Weakest templates", "bank:negated:03", "cluster bootstrap", "item-level Wilson"):
            self.assertIn(needle, md)


class TestOverHTTP(unittest.TestCase):
    """The whole path against a local OpenAI-compatible server: request body (logprobs asked for per call),
    response parsing (first-token top_logprobs) and the bench numbers."""

    def test_logprob_and_generative_modes_through_a_local_server(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        oracle = OracleClient(seeds=(0, 1))
        bodies = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                bodies.append(body)
                time.sleep(0.003)           # latency is whole milliseconds; keep it above zero
                resp = oracle.complete(body["messages"], temperature=0, seed=None, max_tokens=body["max_tokens"])
                choice = {"message": {"content": resp.text}, "finish_reason": resp.finish_reason}
                if resp.logprobs and body.get("logprobs"):
                    choice["logprobs"] = {"content": [{"token": resp.text, "logprob": resp.logprobs[0][1],
                                                       "top_logprobs": [{"token": t, "logprob": lp, "bytes": None}
                                                                        for t, lp in resp.logprobs]}]}
                raw = json.dumps({"choices": [choice],
                                  "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.0001}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        env = {"SS_JUDGE_URL": f"http://127.0.0.1:{srv.server_port}/v1", "SS_JUDGE_MODEL": "m",
               "SS_JUDGE_PARAMS": '{"provider": {"order": ["p"], "allow_fallbacks": false}}'}
        for mode in ("logprob", "generative"):
            out = Path(tmpdir(self))
            with mock.patch.dict("os.environ", env, clear=True), redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--mode", mode, "--out", str(out), "--seeds", "0,1"]), 0)
            res = json.loads((out / f"bench.{mode}.json").read_text())
            self.assertEqual(res["pass"], {"passed": True, "reasons": []}, mode)
            self.assertEqual(res["timing"]["modality"]["cost_usd"] > 0, True)
            self.assertIsNotNone(res["timing"]["modality"]["calls_per_s"])
            self.assertEqual(res["timing"]["modality"]["tokens_per_s"] is None, mode == "logprob")
            log = [json.loads(l) for l in (out / f"bench.{mode}.audit.jsonl").read_text().splitlines()]
            req = next(r["payload"] for r in log if r["record_type"] == "call_request"
                       and r["payload"]["stage"].startswith("judge:modality"))
            self.assertEqual(req["params"]["provider"], {"order": ["p"], "allow_fallbacks": False})  # provider recorded
            self.assertEqual("logprobs" in req["params"], mode == "logprob")
        letter = [b for b in bodies if b["max_tokens"] == 1]
        self.assertTrue(letter and all(b["logprobs"] is True and b["top_logprobs"] == 10 and b["temperature"] == 0
                                       and b["provider"]["order"] == ["p"] for b in letter))


class TestMain(unittest.TestCase):
    def test_oracle_run_writes_json_markdown_log_and_fixtures(self):
        out = Path(tmpdir(self)) / "o"
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--mode", "logprob", "--out", str(out), "--client", "oracle"])
        self.assertEqual(code, 0)
        res = json.loads((out / "bench.logprob.json").read_text())
        self.assertTrue(res["pass"]["passed"])
        self.assertIn("Pass rule (pooled): PASS", (out / "bench.logprob.md").read_text())
        self.assertTrue((out / "bench.logprob.audit.jsonl").exists())
        self.assertTrue(json.loads((out / "fixtures.logprob.json").read_text()))
        self.assertIn("PASS", buf.getvalue())

    def test_http_without_env_is_a_clear_error_and_prints_no_secret(self):
        err = io.StringIO()
        with mock.patch.dict("os.environ", {}, clear=True), redirect_stderr(err):
            code = main(["--mode", "generative", "--out", tmpdir(self)])
        self.assertEqual(code, 2)
        self.assertIn("SS_JUDGE_URL", err.getvalue())

    def test_http_client_uses_the_env_config_and_the_key_is_not_printed(self):
        env = {"SS_JUDGE_URL": "http://127.0.0.1:9/v1", "SS_JUDGE_MODEL": "m", "SS_JUDGE_KEY_ENV": "MY_KEY_VAR",
               "MY_KEY_VAR": "sk-secret-123", "SS_JUDGE_PARAMS": '{"provider": {"order": ["x"]}}'}
        seen = {}

        def fake_bench(client, prompts, corpus, log, **kw):
            seen["cfg"] = client.cfg
            return {"mode": kw["mode"], "pass": {"passed": False, "reasons": ["stub"]}, "n_items": 0,
                    "modality": {"n_judged": 0, "n_not_judged": 0, "accuracy": None, "recall": {}, "by_style": {},
                                 "style_gap": None, "confusion": {}, "by_kind": {}},
                    "alignment": {"strict": {"f1": None}, "lenient": {}}, "off_record": {}, "arms": {},
                    "timing": {"modality": {}}, "not_judged_rate": None}
        buf, err = io.StringIO(), io.StringIO()
        with mock.patch.dict("os.environ", env, clear=True), mock.patch("second_scribe.judge_bench.bench", fake_bench), \
                mock.patch("second_scribe.judge_bench.render_markdown", lambda r: "md"), \
                redirect_stdout(buf), redirect_stderr(err):
            code = main(["--mode", "generative", "--out", tmpdir(self)])
        self.assertEqual(code, 0)
        self.assertEqual((seen["cfg"].model_id, seen["cfg"].api_key_env, seen["cfg"].params),
                         ("m", "MY_KEY_VAR", {"provider": {"order": ["x"]}}))
        self.assertNotIn("sk-secret-123", buf.getvalue() + err.getvalue())


class TestBenchSubcommandOfTheCli(unittest.TestCase):
    """``python -m second_scribe.cli bench ...`` is the bench's own command line, reachable from the kit's one CLI."""

    def test_the_cli_passes_its_arguments_to_the_bench(self):
        from second_scribe.cli import main as cli_main
        with mock.patch("second_scribe.judge_bench.main", return_value=0) as m:
            self.assertEqual(cli_main(["bench", "--mode", "logprob", "--out", "X", "--seeds", "1"]), 0)
        m.assert_called_once_with(["--mode", "logprob", "--out", "X", "--seeds", "1"])

    def test_the_exit_code_of_the_bench_is_the_exit_code_of_the_cli(self):
        from second_scribe.cli import main as cli_main
        with mock.patch("second_scribe.judge_bench.main", return_value=2):
            self.assertEqual(cli_main(["bench", "--mode", "logprob", "--out", "X"]), 2)

    def test_a_real_bench_runs_through_the_cli_on_the_oracle(self):
        from second_scribe.cli import main as cli_main
        out = Path(tmpdir(self))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli_main(["bench", "--mode", "logprob", "--out", str(out), "--client", "oracle",
                                       "--seeds", "1"]), 0)
        self.assertTrue((out / "bench.logprob.json").exists())

    def test_the_help_lists_the_subcommand(self):
        from second_scribe.cli import main as cli_main
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
            cli_main(["--help"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("bench", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
