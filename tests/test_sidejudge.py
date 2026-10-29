"""cli sidejudge: the side study on a FINISHED J run (second judge, planted cases, label order, off-record and aligner
calibration, alignment sample, canonicalization order and determinism). The J out_dir is only read; every call goes
into the side study's own hash-chained log; results are aggregates. Synthetic demo files only (corpus kind "real" so
the real-run guards are live), oracle answers behind a ModelConfig, no network."""
import contextlib
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from second_scribe import sidejudge
from second_scribe.audit.registry import AuditLog
from second_scribe.audit.verify import verify_run
from second_scribe.cli import main
from second_scribe.llm import RealGuardRefused
from second_scribe.pipeline import RunConfig
from tests.test_real_run_validity import THINK_OFF, CfgClient, fake_build_clients, real_config

JUDGE = "deepseek-v4-flash-0731-test"
JUDGE2 = "gemma-4-31b-it-test"


def tree_digest(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def records(log: Path) -> list[dict]:
    return [json.loads(l) for l in log.read_text().splitlines() if l.strip()]


class Counting:
    """Counts the calls that reach a client."""

    def __init__(self, inner):
        self.inner, self.cfg, self.n = inner, inner.cfg, 0

    def complete(self, messages, **kw):
        self.n += 1
        return self.inner.complete(messages, **kw)


def side_clients(cfg_path, judge=JUDGE, judge2=JUDGE2, params=None):
    cfg = RunConfig.load(cfg_path)
    base = sidejudge.build_side_clients("oracle", cfg, ["judge", "judge2"])
    p = dict(THINK_OFF) if params is None else params
    return {"judge": Counting(CfgClient(base["judge"], "judge", judge, p)),
            "judge2": Counting(CfgClient(base["judge2"], "judge2", judge2, dict(THINK_OFF)))}


class SideBase(unittest.TestCase):
    """One finished J run (Q, J and M on the demo files, corpus kind real), shared by the tests and never written."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.cfg = real_config(cls.root)
        with mock.patch.dict(os.environ, {"SS_RECON_MODEL": "alpha-recon-1", "SS_JUDGE_MODEL": JUDGE}), \
                mock.patch("second_scribe.cli.build_clients", fake_build_clients), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            rc = main(["run", "--config", str(cls.cfg), "--stages", "all", "--client", "oracle"])
        assert rc == 0, err.getvalue()
        cls.j_out = cls.root / "run"
        cls.j_log = cls.j_out / "logs" / "run.jsonl"
        cls.digest = tree_digest(cls.j_out)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.side = Path(tempfile.mkdtemp(dir=self.root, prefix="side-"))
        self.side_out = self.side / "out"

    def tearDown(self):
        self.assertEqual(tree_digest(self.j_out), self.digest, "the J out_dir was written to")

    def run_side(self, parts, clients=None, **kw):
        clients = clients or side_clients(self.cfg)
        res = sidejudge.run_side(RunConfig.load(self.cfg), self.side_out, parts, clients, **kw)
        return res, clients

    def result(self, part: str) -> dict:
        return json.loads((self.side_out / "results" / f"part{part}.json").read_text())


class TestPart2(SideBase):
    def test_dry_count_calls_nothing_and_writes_nothing(self):
        so = io.StringIO()
        with contextlib.redirect_stdout(so):
            rc = main(["sidejudge", "--config", str(self.cfg), "--out", str(self.side_out), "--dry-count",
                       "--parts", "2"])
        self.assertEqual(rc, 0)
        self.assertIn("part 2 judge2", so.getvalue())
        self.assertFalse(self.side_out.exists())

    def test_relabel_replays_the_j_messages_to_judge2_only_and_resumes_without_asking_twice(self):
        res, clients = self.run_side(["2"])
        self.assertTrue(res["verify"]["ok"], res["verify"])
        self.assertEqual(clients["judge"].n, 0)
        self.assertGreater(clients["judge2"].n, 0)
        j = {(r["payload"]["item_id"], r["payload"]["messages_sha256"]) for r in records(self.j_log)
             if r["record_type"] == "call_request" and r["payload"]["stage"] == "judge:modality"}
        side = [r["payload"] for r in records(self.side_out / "logs" / "sidejudge.jsonl")
                if r["record_type"] == "call_request"]
        self.assertTrue(side)
        for req in side:       # the very messages the main judge got, only the model differs
            self.assertEqual(req["stage"], "judge2:side_relabel")
            self.assertEqual(req["model_id"], JUDGE2)
            self.assertIn((req["item_id"], req["messages_sha256"]), j)
        r = self.result("2")
        self.assertEqual(r["units"]["uncovered"], 0)
        self.assertGreater(r["elements"]["n"], 100)
        self.assertEqual(r["all"]["kappa"], 1.0)          # the same oracle behind both judges
        for arm, h in r["headline"].items():
            self.assertEqual(h["n_participants_without_second_judge"], 0, arm)
            rows = sidejudge._band_rows(h["band"])
            self.assertTrue(rows)
            self.assertTrue(all(v.get("min") == v.get("max") or v.get("same") for _k, v in rows))
        self.assertIn("Part 2", (self.side_out / "results" / "side_report.md").read_text())
        # a second session asks nothing again and the side log still verifies
        res2, clients2 = self.run_side(["2"])
        self.assertEqual(clients2["judge2"].n, 0)
        self.assertTrue(res2["verify"]["ok"])

    def test_a_disagreeing_second_judge_shows_in_kappa_and_the_band(self):
        clients = side_clients(self.cfg)
        inner = clients["judge2"].inner.inner

        class Contrary:
            def __init__(self, inner):
                self.inner = inner

            def complete(self, messages, **kw):
                r = self.inner.complete(messages, **kw)
                d = json.loads(r.text)
                for row in d["labels"]:
                    if row["n"] % 3 == 0:
                        row["modality"] = "unsupported"
                text = json.dumps(d)
                return sidejudge.ModelResponse(text, "stop", {}, 0, hashlib.sha256(text.encode()).hexdigest())
        clients["judge2"].inner.inner = Contrary(inner)
        self.run_side(["2"], clients)
        r = self.result("2")
        self.assertLess(r["all"]["kappa"], 0.9)
        self.assertGreater(r["by_class"]["unsupported"]["n_judge2"], r["by_class"]["unsupported"]["n_judge1"])
        rows = sidejudge._band_rows(r["headline"]["full"]["band"])
        self.assertTrue(any(v.get("min") != v.get("max") for _k, v in rows if "min" in v))


class KeywordJudge:
    """Answers the modality prompt with the kit's keyword rule on each quote (anything else: the inner oracle)."""

    def __init__(self, inner):
        self.inner = inner

    def complete(self, messages, **kw):
        user = next(m["content"] for m in messages if m["role"] == "user")
        if '{"labels"' not in user:
            return self.inner.complete(messages, **kw)
        import re
        from second_scribe.keywords import keyword_modality
        rows = re.findall(r'^(\d+)\. ELEMENT: .*\n\s+QUOTE(?: \(\w+\))?: "(.*)"$', user, re.M)
        text = json.dumps({"labels": [{"n": int(n), "modality": keyword_modality(q)} for n, q in rows]})
        return sidejudge.ModelResponse(text, "stop", {}, 0, hashlib.sha256(text.encode()).hexdigest())


class TestParts3And4(SideBase):
    def test_planted_cases_score_each_judge_per_class(self):
        clients = side_clients(self.cfg)
        clients["judge2"].inner.inner = KeywordJudge(clients["judge2"].inner.inner)
        res, _ = self.run_side(["3"], clients)
        self.assertTrue(res["verify"]["ok"])
        r = self.result("3")
        kinds = r["items"]["by_kind"]
        self.assertGreater(kinds["swap"], 50)
        for c in ("actual", "wished", "negated", "hypothetical", "other_person"):
            self.assertEqual(kinds[f"template:{c}"], 60, c)
            # the keyword rule reads every template sentence right by construction (kept only when it does)
            self.assertEqual(r["judge2"]["by_kind"][f"template:{c}"]["rate"], 1.0, c)
        self.assertEqual(r["judge"]["by_kind"]["swap"]["rate"], 1.0)       # the oracle: another map's label
        self.assertLess(r["judge2"]["by_kind"]["swap"]["rate"], 0.5)       # the keyword rule cannot tell
        self.assertEqual(clients["judge"].n, r["items"]["batches"])
        self.assertEqual(clients["judge2"].n, r["items"]["batches"])
        # a planted sentence never reaches a result file
        planted = [json.loads(l)["payload"]["messages"][-1]["content"]
                   for l in (self.side_out / "logs" / "sidejudge.jsonl").read_text().splitlines()
                   if json.loads(l)["record_type"] == "call_request"]
        self.assertTrue(planted)
        text = (self.side_out / "results" / "part3.json").read_text()
        self.assertNotIn("QUOTE", text)
        res2, clients2 = self.run_side(["3"])
        self.assertEqual(clients2["judge"].n + clients2["judge2"].n, 0)

    def test_reversed_card_on_the_j_batches_for_both_judges(self):
        res, clients = self.run_side(["4"])
        self.assertTrue(res["verify"]["ok"])
        r = self.result("4")
        n = r["sample"]["n_units"]
        self.assertGreater(n, 0)
        reqs = [json.loads(l)["payload"] for l in (self.side_out / "logs" / "sidejudge.jsonl").read_text().splitlines()
                if json.loads(l)["record_type"] == "call_request"]
        rev = [q for q in reqs if q["stage"].endswith(":side_reversed")]
        self.assertEqual(len([q for q in rev if q["stage"] == "judge:side_reversed"]), n)
        self.assertEqual(len([q for q in rev if q["stage"] == "judge2:side_reversed"]), n)
        j = {r_["payload"]["item_id"]: r_["payload"] for r_ in records(self.j_log)
             if r_["record_type"] == "call_request" and r_["payload"]["stage"] == "judge:modality"
             and r_["payload"]["attempt"] == 1}
        for q in rev:       # same items in the same order, same system text and seed; only the card's order differs
            jq = j[q["item_id"]]
            self.assertEqual(q["seed"], jq["seed"])
            self.assertEqual(q["messages"][0], jq["messages"][0])
            self.assertNotEqual(q["messages"][1]["content"], jq["messages"][1]["content"])
            tail = lambda m: m.split("ITEMS:\n", 1)[1].split("\nREMINDER:", 1)[0]
            self.assertEqual(tail(q["messages"][1]["content"]), tail(jq["messages"][1]["content"]))
        self.assertEqual(r["judge"]["forward_vs_reversed"]["agreement"], 1.0)
        self.assertEqual(r["judge"]["changed"], 0)


class TestPart6(SideBase):
    def stages(self):
        return Counter(json.loads(l)["payload"]["stage"]
                       for l in (self.side_out / "logs" / "sidejudge.jsonl").read_text().splitlines()
                       if json.loads(l)["record_type"] == "call_request")

    def test_judge2_paraphrases_first_then_the_judge_checks_planted_copies_and_aligns(self):
        clients = side_clients(self.cfg)
        res, _ = self.run_side(["6"], clients, only_role="judge2")
        self.assertTrue(res["verify"]["ok"])
        self.assertEqual(clients["judge"].n, 0)
        st = self.stages()
        self.assertEqual(set(st), {"judge2:side_paraphrase"})
        start = [json.loads(l)["payload"] for l in (self.side_out / "logs" / "sidejudge.jsonl").read_text().splitlines()
                 if json.loads(l)["record_type"] == "sidejudge_start"][0]
        self.assertEqual(start["paraphrase_prompt_sha256"], sidejudge.PARAPHRASE_PROMPT_SHA256)
        self.assertEqual(self.result("6")["offrecord"]["n_participants"], 0)
        clients2 = side_clients(self.cfg)
        res, _ = self.run_side(["6"], clients2, only_role="judge")
        self.assertEqual(clients2["judge2"].n, 0)
        st = self.stages()
        self.assertGreater(st["judge:side_offrecord"], 0)
        self.assertGreater(st["judge:side_align_cal"], 0)
        r = self.result("6")
        by = r["offrecord"]["by_stratum"]
        self.assertEqual(set(by), {"planted_verbatim", "planted_paraphrase", "lexically_absent"})
        self.assertEqual(by["planted_verbatim"]["judge"]["rate"], 1.0)
        self.assertEqual(by["planted_paraphrase"]["judge"]["rate"], 1.0)     # the oracle knows the bank's forms
        self.assertLess(by["planted_paraphrase"]["lexical_baseline"]["rate"], 0.5)
        self.assertEqual(by["lexically_absent"]["judge"]["rate"], 1.0)
        cal = r["aligncal"]["by_setting"]
        self.assertEqual(cal["primary"]["reorder"]["recall"]["rate"], 1.0)
        self.assertEqual(cal["judge_only"]["paraphrase"]["recall"]["rate"], 1.0)
        self.assertEqual(cal["primary"]["decoy"]["false_match"]["rate"], 0.0)
        self.assertEqual(cal["primary"]["other_label"]["false_match"]["rate"], 0.0)
        # the J run's own off-record and align items are untouched (tearDown checks the tree) and a rerun asks nothing
        clients3 = side_clients(self.cfg)
        self.run_side(["6"], clients3)
        self.assertEqual(clients3["judge"].n + clients3["judge2"].n, 0)


class TestParts7And8(SideBase):
    def test_alignment_sample_to_judge2_and_the_judges_canonicalization_order_and_determinism(self):
        res, clients = self.run_side(["7", "8"])
        self.assertTrue(res["verify"]["ok"])
        j = {(r["payload"]["item_id"], r["payload"]["messages_sha256"]) for r in records(self.j_log)
             if r["record_type"] == "call_request"}
        reqs = [r["payload"] for r in records(self.side_out / "logs" / "sidejudge.jsonl")
                if r["record_type"] == "call_request"]
        al = [q for q in reqs if q["stage"] == "judge2:side_align"]
        det = [q for q in reqs if q["stage"] == "judge:side_determinism"]
        self.assertTrue(al and det)
        for q in al + det:          # the J run's own logged messages
            self.assertIn((q["item_id"], q["messages_sha256"]), j)
        self.assertEqual(len(al), self.result("7")["sample"]["n_units"])
        self.assertEqual(self.result("7")["all"]["same_pairs"]["jaccard"], 1.0)
        r8 = self.result("8")
        self.assertEqual(r8["canon"]["adjusted_rand_index"], 1.0)
        self.assertEqual(r8["determinism"]["all"]["identical"]["rate"], 1.0)
        self.assertEqual(clients["judge2"].n, len(al))
        so = io.StringIO()
        with contextlib.redirect_stdout(so):
            main(["sidejudge", "--config", str(self.cfg), "--out", str(self.side_out), "--dry-count",
                  "--parts", "7,8", "--json"])
        counts = json.loads(so.getvalue())["by_part"]
        self.assertEqual(counts["7"]["judge2"]["remaining"], 0)
        self.assertEqual(counts["8"]["judge"]["remaining"], 0)

    def test_the_cli_runs_every_part_on_the_synthetic_demo_with_the_oracle(self):
        d = json.loads(Path(self.cfg).read_text())
        so, se = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
            rc = main(["sidejudge", "--config", str(self.cfg), "--out", str(self.side_out), "--client", "oracle"])
        self.assertEqual(rc, 0, se.getvalue())
        self.assertIn("parts 2,3,4,6,7,8 done", so.getvalue())
        self.assertTrue(verify_run(self.side_out / "logs" / "sidejudge.jsonl")[0])
        md = (self.side_out / "results" / "side_report.md").read_text()
        for k in ("Part 2", "Part 3", "Part 4", "Part 6", "Part 7", "Part 8"):
            self.assertIn(k, md)
        self.assertEqual(d["corpus"]["kind"], "real")


class TestGuards(SideBase):
    def refused(self, clients, exc=RealGuardRefused):
        with self.assertRaises(exc):
            self.run_side(["2"], clients)
        self.assertEqual(sum(c.n for c in clients.values()), 0)

    def test_judge2_must_be_a_known_other_family_and_never_qwen(self):
        self.refused(side_clients(self.cfg, judge2="qwen3-32b"))
        self.refused(side_clients(self.cfg, judge2="deepseek-v3"))
        self.refused(side_clients(self.cfg, judge2="judge-b"))
        self.refused(side_clients(self.cfg, judge2=JUDGE))

    def test_the_judge_must_be_the_j_runs_instrument(self):
        self.refused(side_clients(self.cfg, judge="deepseek-v4-flash-other"))
        self.refused(side_clients(self.cfg, params={**THINK_OFF, "top_k": 1}))

    def test_a_running_or_unverified_j_run_is_refused(self):
        copy = self.side / "jcopy"
        shutil.copytree(self.j_out, copy / "run")
        d = json.loads(Path(self.cfg).read_text())
        d["out_dir"] = str(copy / "run")
        cfg2 = copy / "config.json"
        cfg2.write_text(json.dumps(d))
        log = AuditLog(copy / "run" / "logs" / "run.jsonl", "x")
        log.append("run_start", {"stages": ["judge_modality"]})       # a session that has not ended
        clients = side_clients(self.cfg)
        with self.assertRaises(sidejudge.SideRefused):
            sidejudge.run_side(RunConfig.load(cfg2), self.side_out, ["2"], clients)
        self.assertEqual(sum(c.n for c in clients.values()), 0)

    def test_the_side_folder_may_not_sit_inside_the_j_out_dir(self):
        clients = side_clients(self.cfg)
        with self.assertRaises(sidejudge.SideRefused):
            sidejudge.run_side(RunConfig.load(self.cfg), self.j_out / "side", ["2"], clients)
        self.assertFalse((self.j_out / "side").exists())


if __name__ == "__main__":
    unittest.main()
