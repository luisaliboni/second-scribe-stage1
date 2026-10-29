"""cli validate-nomodel: no-model checks on a finished J out_dir (keyword arm, robustness / leave-one-out,
wrong-transcript null, approximate-quote diffs). Synthetic demo (replay) and the offline oracle only; the run's
out_dir is only read; outputs are aggregates only."""
import contextlib
from collections import Counter
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from second_scribe import metrics, nomodel, pipeline, textnorm
from second_scribe.cli import DEMO_CONFIG, main
from second_scribe.keywords import keyword_modality
from tests.test_real_run_validity import Base


def tree_digest(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def cli(*argv):
    so, se = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
        rc = main(list(argv))
    return rc, so.getvalue(), se.getvalue()


class DemoBase(unittest.TestCase):
    """One replayed demo run (every stage) shared by the tests of a class; its config points at that out_dir."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        rc, _o, err = cli("demo", "--out", str(cls.root / "run"), "--client", "replay")
        assert rc == 0, err
        d = json.loads(Path(DEMO_CONFIG).read_text())
        d.update(out_dir=str(cls.root / "run"), base_dir=str(Path(DEMO_CONFIG).parent))
        cls.cfg_path = cls.root / "config.json"
        cls.cfg_path.write_text(json.dumps(d))
        cls.cfg = pipeline.RunConfig.load(cls.cfg_path)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def validate(self, out: Path, *extra):
        return cli("validate-nomodel", "--config", str(self.cfg_path), "--out", str(out), *extra)

    def out_dir(self, name: str) -> Path:
        p = self.root / "v" / name
        shutil.rmtree(p, ignore_errors=True)
        return p


class TestAgreement(unittest.TestCase):
    def test_counts_kappa_classes_and_confusion(self):
        rows = [{"judge": j, "keyword": k} for j, k in
                [("actual", "actual")] * 6 + [("actual", "negated")] * 2 + [("wished", "wished")] * 2]
        a = nomodel.agreement(rows)
        self.assertEqual((a["n"], a["n_agree"]), (10, 8))
        self.assertAlmostEqual(a["agreement"], 0.8)
        self.assertAlmostEqual(a["cohen_kappa"], metrics.cohen_kappa([r["judge"] for r in rows],
                                                                     [r["keyword"] for r in rows]))
        self.assertEqual(a["confusion"], {"actual": {"actual": 6, "negated": 2}, "wished": {"wished": 2}})
        w = a["classes"]["wished"]
        self.assertEqual((w["n_judge"], w["n_keyword"], w["n_both"]), (2, 2, 2))
        self.assertEqual(w["cohen_kappa"], 1.0)
        n = a["classes"]["negated"]
        self.assertEqual((n["n_judge"], n["n_keyword"], n["keyword_given_judge"], n["judge_given_keyword"]),
                         (0, 2, None, 0.0))
        self.assertIsNone(a["classes"]["hypothetical"]["cohen_kappa"])      # neither side used it: undefined

    def test_empty(self):
        a = nomodel.agreement([])
        self.assertEqual((a["n"], a["agreement"], a["cohen_kappa"]), (0, None, None))

    def test_spearman_and_quantiles(self):
        self.assertAlmostEqual(nomodel.spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)
        self.assertAlmostEqual(nomodel.spearman([1, 2, 3, 4], [4, 3, 2, 1]), -1.0)
        self.assertIsNone(nomodel.spearman([1, 2], [1, 2]))
        self.assertIsNone(nomodel.spearman([1, 1, 1], [1, 2, 3]))
        self.assertEqual(nomodel._quantile([3, 1, 2], 0.5), 2)
        self.assertEqual(nomodel._quantile([1, 2, 3, 4], 0.25), 1.75)


class TestKeyword(DemoBase):
    def test_keyword_check_reports_agreement_and_both_headlines(self):
        before = tree_digest(self.root / "run")
        out = self.out_dir("kw")
        rc, so, err = self.validate(out, "--checks", "keyword")
        self.assertEqual(rc, 0, err)
        self.assertEqual(tree_digest(self.root / "run"), before)          # the run is only read
        k = json.loads((out / "aggregates" / "keyword.json").read_text())
        agg = json.loads((self.root / "run" / "aggregates" / "aggregates.json").read_text())
        # the judge headline is M's own, recomputed by the same code with the same parameters
        self.assertEqual(k["headline_judge"], agg["headline"])
        self.assertEqual(k["other_arms_judge"], agg["other_arms"])
        self.assertEqual(k["headline_keyword"]["modality_source"], "keyword")
        self.assertEqual(set(k["comparison"]), {"headline", "full"})
        self.assertIn("leak_rate", k["comparison"]["headline"])
        a = k["agreement"]["all"]
        # every element the judge answered for in every main spec, once
        n = 0
        ctx = nomodel.Context(self.cfg)
        for p in ctx.p_list:
            for s in ctx.keys:
                if p.ai.get(s) is not None:
                    n += sum(1 for eid in p.modality[s] if eid not in metrics._nj(p, s))
        self.assertEqual(a["n"], n)
        self.assertEqual(sum(sum(v.values()) for v in a["confusion"].values()), n)
        self.assertEqual(sum(v["n"] for v in k["agreement"]["by_spec"].values()), n)
        self.assertEqual(k["agreement"]["by_cue"]["cue"]["n"] + k["agreement"]["by_cue"]["no_cue"]["n"], n)
        self.assertIn("keyword:", so)
        self.assertTrue((out / "logs" / "run.manifest.json").exists())
        rc, _o, err = cli("verify", str(out / "logs" / "run.jsonl"))
        self.assertEqual(rc, 0, err)

    def test_keyword_labels_are_the_classifier_on_each_quote(self):
        ctx = nomodel.Context(self.cfg)
        rows = nomodel.element_rows(ctx.p_list, ctx.keys, ctx.min_words)
        self.assertTrue(rows)
        p = ctx.p_list[rows[0]["i"]]
        g = p.ai[rows[0]["spec"]]
        quotes = {n.node_id: n.quote for n in g.nodes}
        quotes.update({f"e:{e.source}->{e.target}": e.quote for e in g.edges})
        mine = [r for r in rows if r["i"] == rows[0]["i"] and r["spec"] == rows[0]["spec"]]
        kw = metrics.with_keyword_modality(p).modality[rows[0]["spec"]]
        for eid, q in quotes.items():
            self.assertEqual(kw[eid], keyword_modality(q))
        self.assertTrue(all("pid" not in r for r in mine))

    def test_outputs_are_deterministic(self):
        a, b = self.out_dir("d1"), self.out_dir("d2")
        self.assertEqual(self.validate(a)[0], 0)                     # every check
        self.assertEqual(self.validate(b)[0], 0)
        names = sorted(p.name for p in (a / "aggregates").glob("*.json"))
        self.assertEqual(names, sorted(f"{c}.json" for c in nomodel.CHECKS))
        for n in names:
            self.assertEqual((a / "aggregates" / n).read_bytes(), (b / "aggregates" / n).read_bytes(), n)

    def test_out_inside_the_run_is_refused(self):
        rc, _o, err = self.validate(self.root / "run" / "nomodel")
        self.assertEqual(rc, 2)
        self.assertIn("outside", err)
        self.assertFalse((self.root / "run" / "nomodel").exists())
        rc, _o, err = self.validate(self.out_dir("x"), "--checks", "nope")
        self.assertEqual(rc, 2)


class TestRealKindGuards(Base):
    """A corpus of kind "real" (the synthetic files, oracle answers): the aggregate-only guard and the leak guard run
    before any write; no participant id, label or quote reaches the outputs or the console."""

    def setUp(self):
        super().setUp()
        for g in ("Q", "J", "M"):
            rc, err = self.run_all(g)
            self.assertEqual(rc, 0, err)
        self.cfg_obj = pipeline.RunConfig.load(self.cfg)

    def validate(self, out, *extra):
        return cli("validate-nomodel", "--config", str(self.cfg), "--out", str(out), *extra)

    def test_outputs_carry_no_id_label_or_quote(self):
        before = tree_digest(self.out / "run")
        out = self.out / "nomodel"
        rc, so, err = self.validate(out)
        self.assertEqual(rc, 0, err)
        self.assertEqual(tree_digest(self.out / "run"), before)
        ctx = nomodel.Context(self.cfg_obj)
        bad = {s for s in nomodel.forbidden_strings(ctx) if len(s) >= 4}
        texts = [p.read_text() for p in sorted((out / "aggregates").glob("*.json"))] + [so, err]
        self.assertTrue(texts[0])
        for t in texts:
            for s in bad:
                self.assertNotIn(f'"{s}"', t)
        for p in ctx.r.people:
            self.assertTrue(all(p.pid not in t for t in texts))

    def test_a_leaking_output_is_refused_and_not_written(self):
        ctx = nomodel.Context(self.cfg_obj)
        label = ctx.p_list[0].endorsed.nodes[0].text
        real = nomodel.CHECK_FUNCS["keyword"]
        with mock.patch.dict(nomodel.CHECK_FUNCS, {"keyword": lambda c: {**real(c), "oops": label}}):
            out = self.out / "leak"
            rc, so, err = self.validate(out, "--checks", "keyword")
        self.assertEqual(rc, 1)
        self.assertFalse((out / "aggregates" / "keyword.json").exists())
        self.assertNotIn(label, so + err)
        self.assertTrue(err.startswith("error: ValueError at report.py"), err)     # the aggregate-only guard


if __name__ == "__main__":
    unittest.main()


class TestLooSummary(unittest.TestCase):
    def test_min_max_sign_and_flag_changes(self):
        ref = {"x": 0.2, "flag": True, "gone": 0.1}
        runs = [{"x": 0.3, "flag": True, "gone": None}, {"x": -0.1, "flag": False, "gone": 0.1}, None]
        s = nomodel.loo_summary(ref, runs)
        self.assertEqual((s["n_loo"], s["n_failed"]), (3, 1))
        x = s["values"]["x"]
        self.assertEqual((x["all"], x["min"], x["max"], x["n_sign_change"]), (0.2, -0.1, 0.3, 1))
        self.assertAlmostEqual(x["max_abs_change"], 0.3)
        self.assertEqual(s["values"]["flag"], {"all": True, "n_flag_change": 1, "n_missing": 0})
        self.assertEqual(s["values"]["gone"]["n_missing"], 1)

    def test_distribution(self):
        d = nomodel.distribution([0.9, None, 0.5, 0.7])
        self.assertEqual((d["n"], d["n_undefined"], d["min"], d["median"], d["max"]), (4, 1, 0.5, 0.7, 0.9))
        self.assertAlmostEqual(d["share_kappa_below_floor"], 1 / 3)
        self.assertEqual(d["values"], [0.5, 0.7, 0.9])


class TestRobustness(DemoBase):
    def test_per_interview_per_role_and_leave_one_out(self):
        out = self.out_dir("rob")
        rc, _so, err = self.validate(out, "--checks", "robustness")
        self.assertEqual(rc, 0, err)
        r = json.loads((out / "aggregates" / "robustness.json").read_text())
        ctx = nomodel.Context(self.cfg)
        n = len(ctx.p_list)
        pi = r["per_interview"]["all_specs"]
        self.assertEqual(pi["n_interviews"], n)
        k = pi["cohen_kappa"]
        self.assertEqual(len(k["values"]) + k["n_undefined"], n)
        self.assertEqual(k["values"], sorted(k["values"]))
        roles = Counter(p.role for p in ctx.p_list)
        self.assertEqual({role: v["n_participants"] for role, v in r["per_role"].items()}, dict(roles))
        self.assertEqual(sum(v["cohen_kappa"]["n"] for v in pi["by_role"].values()), n)
        for src in ("judge", "keyword"):
            loo = r["loo"][src]
            self.assertEqual((loo["n_loo"], loo["n_failed"]), (n, 0))
        # one left-out run by hand: inside [min, max] of every number; "all" = the full list at the same parameters
        ref, _ = ctx.headlines(ctx.p_list, "judge", other_arms=False, **nomodel.LOO_DEFAULTS)
        one, _ = ctx.headlines(ctx.p_list[1:], "judge", other_arms=False, **nomodel.LOO_DEFAULTS)
        ref_s, one_s = nomodel.headline_scalars(ref), nomodel.headline_scalars(one)
        for name, v in r["loo"]["judge"]["values"].items():
            self.assertEqual(v["all"], ref_s[name], name)
            if isinstance(one_s[name], float):
                self.assertLessEqual(v["min"], one_s[name] + 1e-12, name)
                self.assertGreaterEqual(v["max"], one_s[name] - 1e-12, name)
        self.assertIsNotNone(r["loo"]["agreement"]["all"])

    def test_point_estimates_do_not_depend_on_the_resample_counts(self):
        ctx = nomodel.Context(self.cfg)
        a, _ = ctx.headlines(ctx.p_list, "keyword", other_arms=False, **nomodel.LOO_DEFAULTS)
        b, _ = ctx.headlines(ctx.p_list, "keyword", other_arms=False)
        sa, sb = nomodel.headline_scalars(a), nomodel.headline_scalars(b)
        self.assertEqual({k: v for k, v in sa.items() if not isinstance(v, bool)},
                         {k: v for k, v in sb.items() if not isinstance(v, bool)})


def fake_ctx(rows, shifts=1, out_dir="/nonexistent"):
    """A stand-in context for the null and diff checks: quote rows given directly (units = [(marker, text)])."""
    from types import SimpleNamespace
    people = []
    for j, (units, maps, labels) in enumerate(rows):
        endorsed = SimpleNamespace(nodes=[SimpleNamespace(text=t) for t in labels])
        people.append({"j": j, "person": SimpleNamespace(pid=f"X{j}"), "units": units, "transcript": None,
                       "maps": maps, "endorsed": endorsed})
    return SimpleNamespace(cfg=SimpleNamespace(out_dir=out_dir), null_shifts=shifts, _quote_units=people,
                           min_words=8)


Q_A = "we waited six months for the assessment and then we gave up"


class TestNull(DemoBase):
    def test_own_tiers_match_m_and_the_rule_is_the_run_s(self):
        out = self.out_dir("null")
        rc, so, err = self.validate(out, "--checks", "null")
        self.assertEqual(rc, 0, err)
        r = json.loads((out / "aggregates" / "null.json").read_text())
        agg = json.loads((self.root / "run" / "aggregates" / "aggregates.json").read_text())
        for arm in ("full", "removed"):
            self.assertEqual(r["by_arm"][arm]["own"], agg["quote_type"]["by_arm"][arm]["quote_tiers"], arm)
        self.assertTrue(r["quote_rule_matches_run"])
        a = r["all"]
        self.assertEqual(sum(a["null"].values()), a["n_quotes"])
        self.assertEqual(r["n_shifts"], 1)
        self.assertIn("wrong transcript:", so)

    def test_a_quote_in_another_transcript_is_counted_and_the_bar_applied(self):
        rows = [([("P", Q_A)], [("removed__p0__t0__r0", "removed", [("n1", "node", Q_A)])], []),
                ([("P", Q_A + ", it was awful")], [], []),
                ([("P", "the school called us back the next week")],
                 [("removed__p0__t0__r0", "removed", [("n1", "node", "the school called us back the next week")])],
                 [])]
        r = nomodel.check_null(fake_ctx(rows))
        a = r["all"]
        self.assertEqual(a["own"], {"exact": 2, "approximate": 0, "unfound": 0})
        # quote of 0 found in 1's transcript (exact); quote of 2 looked up in 0's: unfound
        self.assertEqual(a["null"], {"exact": 1, "approximate": 0, "unfound": 1})
        self.assertEqual(a["null_located_share"], 0.5)
        self.assertFalse(a["null_ok"])
        self.assertFalse(r["quote_rule_matches_run"])        # no run log here: nothing recorded
        r2 = nomodel.check_null(fake_ctx(rows, shifts=2))
        self.assertEqual(r2["n_shifts"], 2)
        self.assertEqual(sum(r2["all"]["null"].values()), 4)


SPAN = "we waited six months for the assessment and then we gave up"


class TestDiffOps(unittest.TestCase):
    def ops(self, span, quote, labels=()):
        stems = {x for t in labels for x in textnorm.content_tokens(t)}
        return nomodel.diff_ops(quote, [textnorm.tokens(span)], stems)

    def test_substitutions_numbers_and_stopwords(self):
        ops = self.ops(SPAN, "we waited 6 months for an assessment and then we gave up")
        self.assertEqual([o["type"] for o in ops], ["substitution", "substitution"])
        six, the = ops
        self.assertTrue(six["flags"]["number"] and six["flags"]["guard_word_free_substitution"])
        self.assertFalse(six["flags"]["stopword_only"])
        self.assertTrue(the["flags"]["stopword_only"])
        self.assertNotIn("6", json.dumps(ops))          # no word in the output, only types, sizes and flags
        self.assertNotIn("months", json.dumps(ops))

    def test_insertion_deletion_person_label_negation_and_stem(self):
        ins, = self.ops(SPAN, "we waited six months for the assessment and then we really gave up")
        self.assertEqual((ins["type"], ins["n_words"]), ("insertion", 1))
        dele, = self.ops(SPAN, "we waited six months for assessment and then we gave up")
        self.assertEqual(dele["type"], "deletion")
        self.assertTrue(dele["flags"]["stopword_only"])
        per, = self.ops("my son was seen at the clinic last spring", "my daughter was seen at the clinic last spring")
        self.assertTrue(per["flags"]["person_word"])
        lab, = self.ops(SPAN, "we waited six months for the evaluation and then we gave up", labels=["Evaluation wait"])
        self.assertTrue(lab["flags"]["endorsed_label_word"])
        neg, = self.ops("they did not call the office back", "they did not ring the office back")
        self.assertTrue(neg["flags"]["negation_adjacent"])
        self.assertFalse(neg["flags"]["guard_word"])
        stem_, = self.ops("we were waiting for the call", "we were waited for the call")
        self.assertTrue(stem_["flags"]["same_stem"])

    def test_block_counts(self):
        q1 = self.ops(SPAN, "we waited 6 months for an assessment and then we gave up")
        q2 = self.ops(SPAN, "we waited six months for assessment and then we gave up")
        b = nomodel.diff_block([q1, q2])
        self.assertEqual(b["n_quotes"], 2)
        self.assertEqual(b["ops"]["substitution"]["n_changes"], 2)
        self.assertEqual(b["ops"]["deletion"]["n_changes"], 1)
        self.assertEqual(b["flags"]["number"], {"n_changes": 1, "n_quotes_with": 1})
        self.assertEqual(b["words_per_quote"]["2"], 1)
        self.assertEqual(b["words_per_quote"]["1"], 1)
        self.assertEqual(b["n_quotes_all_minor"], 1)          # the dropped "the" only


class TestDiffsOnARun(DemoBase):
    """A copy of the demo run where one AI quote has one word changed: it becomes approximate and the diff audit
    counts one guard-free substitution."""

    def test_one_changed_word_is_one_substitution(self):
        import re
        run = self.root / "run-approx"
        shutil.rmtree(run, ignore_errors=True)
        shutil.copytree(self.root / "run", run)
        d = json.loads(self.cfg_path.read_text())
        d["out_dir"] = str(run)
        cfg = self.root / "config-approx.json"
        cfg.write_text(json.dumps(d))
        item = sorted((run / "work" / "recon").glob("*__full__p0__t0__r0.json"))[0]
        obj = json.loads(item.read_text())
        node = max((n for n in obj["map"]["nodes"] if n.get("quote")), key=lambda n: len(n["quote"]))
        toks = textnorm.tokens(node["quote"])
        self.assertGreaterEqual(len(toks), 14)
        word = next(t for t in toks[2:] if t not in textnorm.STOPWORDS and not textnorm._is_guard(t)
                    and t not in nomodel.NUMBER_WORDS and t not in nomodel.PERSON_WORDS)
        node["quote"] = re.sub(rf"\b{re.escape(word)}\b", "zebra", node["quote"], count=1, flags=re.I)
        item.write_text(json.dumps(obj))
        out = self.out_dir("diffs")
        rc, so, err = cli("validate-nomodel", "--config", str(cfg), "--out", str(out), "--checks", "null,diffs")
        self.assertEqual(rc, 0, err)
        nul = json.loads((out / "aggregates" / "null.json").read_text())
        self.assertEqual(nul["all"]["own"]["approximate"], 1)
        r = json.loads((out / "aggregates" / "diffs.json").read_text())
        a = r["all"]
        self.assertEqual(a["n_quotes"], 1)
        self.assertEqual(a["ops"]["substitution"], {"n_changes": 1, "n_words": 1})
        self.assertEqual(a["flags"]["guard_word_free_substitution"]["n_quotes_with"], 1)
        self.assertEqual(sum(v["n_quotes"] for v in r["by_eligible"].values()), 1)
        self.assertEqual(set(r["by_arm"]), {"full"})
        self.assertNotIn("zebra", (out / "aggregates" / "diffs.json").read_text() + so)
