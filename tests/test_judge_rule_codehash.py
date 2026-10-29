"""K4 / K6 (app plan Task 15): the configurable judge-family rule, the code hash, and the chain fields.

- ``guard_real_clients(..., judge_rule="study")`` keeps today's checks: no Qwen-family judge, no judge that is the
  reconstructor's own model. ``"two_family"`` (a tool config's ``tool.judge_rule``) drops the Qwen ban and asks
  instead that each judge's family be known and differ from the reconstructor's; the same-name check stays.
- ``codehash.code_sha256()`` hashes the package source without git; ``codehash.git_commit()`` is HEAD or None.
- every ``run_start`` carries ``code_sha256``; a tool run's ``aggregates.json`` carries ``chain {head, records,
  verified}`` (a study config's aggregates stay byte for byte as they were: the head changes with every run).
- a study config (no ``tool`` block) on a real corpus still refuses every non-local endpoint, on every role.
"""
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.addr import ip
from second_scribe import codehash, pipeline
from second_scribe.audit.registry import AuditLog
from second_scribe.cli import DEMO_CONFIG
from second_scribe.cli import main as cli_main
from second_scribe.llm import EgressRefused, HTTPClient, ModelConfig, RealGuardRefused, guard_real_clients
from second_scribe.pipeline import RunConfig, run

OFF = {"chat_template_kwargs": {"enable_thinking": False}}
TWO_FAMILY_MSG = "real corpus: the judge must be a different model family from the reconstructor"
# Historical 1.0 oracle proof (Python 3.11) and current 1.0/2.0 oracle runs (3.13)
# agree after rounding floats to 12 decimal places: 16 structural-distance values
# differed by less than 1e-15. All fields/types remain; only declared_on is fixed.
BASE_DEMO_AGGREGATES_SHA = "ead93afead36659ceb77cc32ec50a6051e4874ab5e52181a07e65bd229a8cab3"
HEX64 = re.compile(r"[0-9a-f]{64}")


def _round_aggregate_floats(value):
    """Ignore sub-picounit float noise without dropping fields or coercing other types."""
    if isinstance(value, float):
        return round(value, 12)
    if isinstance(value, dict):
        return {k: _round_aggregate_floats(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_round_aggregate_floats(v) for v in value]
    return value


def http(model, name, host):
    return HTTPClient(ModelConfig(name=name, endpoint=f"http://{host}:8000/v1", model_id=model, params=dict(OFF)))


def no_dns(host, port):
    raise AssertionError("no lookup expected")


def check(recon, judge, judge2=None, **kw):
    clients = {"recon": http(recon, "recon", ip(10, 0, 0, 1)), "judge": http(judge, "judge", ip(10, 0, 0, 2))}
    if judge2:
        clients["judge2"] = http(judge2, "judge2", ip(10, 0, 0, 3))
    guard_real_clients(clients, resolver=no_dns, **kw)


class TestJudgeRule(unittest.TestCase):
    def test_study_rule_still_bans_qwen_judge(self):
        for kw in ({}, {"judge_rule": "study"}, {"judge_rule": "study", "families": {"judge": "gemma"}}):
            with self.assertRaises(RealGuardRefused, msg=kw) as cm:
                check("gemma3:4b", "qwen3:8b", **kw)
            self.assertIn("Qwen", str(cm.exception))
            check("qwen3:8b", "gemma3:4b", **kw)       # the study's normal case still passes

    def test_two_family_allows_qwen_judge_with_gemma_recon(self):
        check("gemma3:4b", "qwen3:8b", judge_rule="two_family")
        check("gemma3:4b", "qwen3:8b", "llama3.1:8b", judge_rule="two_family")
        check("gemma3:4b", "qwen3:8b", judge_rule="two_family", families={"recon": "gemma", "judge": "qwen"})

    def test_two_family_refuses_same_family(self):
        cases = (({"recon": "gemma", "judge": "gemma"}), None, ({"recon": "Gemma", "judge": " gemma "}))
        for fams in cases:
            with self.assertRaises(RealGuardRefused, msg=fams) as cm:
                check("gemma3:4b", "gemma2:9b", judge_rule="two_family", families=fams)
            self.assertEqual(str(cm.exception), TWO_FAMILY_MSG)
        with self.assertRaises(RealGuardRefused) as cm:          # the second judge is held to the rule too
            check("gemma3:4b", "qwen3:8b", "gemma2:9b", judge_rule="two_family")
        self.assertEqual(str(cm.exception), TWO_FAMILY_MSG)
        with self.assertRaises(RealGuardRefused):                # a declaration can make a known pair the same family
            check("gemma3:4b", "qwen3:8b", judge_rule="two_family", families={"recon": "x", "judge": "x"})

    def test_two_family_unknown_family_refused(self):
        for recon, judge, fams in (("my-alias", "other-alias", None), ("gemma3:4b", "judge", None),
                                   ("recon", "qwen3:8b", None), ("my-alias", "other-alias", {"recon": "gemma"}),
                                   ("my-alias", "other-alias", {"recon": "", "judge": "llama"})):
            with self.assertRaises(RealGuardRefused, msg=(recon, judge, fams)) as cm:
                check(recon, judge, judge_rule="two_family", families=fams)
            self.assertEqual(str(cm.exception), TWO_FAMILY_MSG)
        # declared families stand in for ids the table does not know (aliases)
        check("my-alias", "other-alias", judge_rule="two_family", families={"recon": "gemma", "judge": "llama"})

    def test_same_name_refused_in_both_rules(self):
        for recon, judge in (("deepseek-j", "deepseek-j"), ("org/model-x", "MODEL-X")):
            with self.assertRaises(RealGuardRefused) as cm:
                check(recon, judge)
            self.assertIn("reconstructor's own model", str(cm.exception))
            with self.assertRaises(RealGuardRefused) as cm:    # declared different families: the name check still holds
                check(recon, judge, judge_rule="two_family", families={"recon": "gemma", "judge": "llama"})
            self.assertIn("reconstructor's own model", str(cm.exception))

    def test_unknown_rule_refused(self):
        with self.assertRaises(RealGuardRefused):
            check("qwen3:8b", "gemma3:4b", judge_rule="anything")

    def test_endpoint_and_thinking_checks_unchanged_under_two_family(self):
        public = HTTPClient(ModelConfig("judge", "https://api.example-public.test/v1", "qwen3:8b", params=dict(OFF)))
        with self.assertRaises(EgressRefused):
            guard_real_clients({"recon": http("gemma3:4b", "recon", ip(10, 0, 0, 1)), "judge": public},
                               resolver=lambda h, p: [ip(104, 18, 0, 1)], judge_rule="two_family")
        thinking_on = HTTPClient(ModelConfig("judge", f"http://{ip(10, 0, 0, 2)}:8000/v1", "qwen3:8b"))
        with self.assertRaises(RealGuardRefused) as cm:
            guard_real_clients({"recon": http("gemma3:4b", "recon", ip(10, 0, 0, 1)), "judge": thinking_on},
                               resolver=no_dns, judge_rule="two_family")
        self.assertIn("thinking", str(cm.exception))


def real_config(out: Path, tool=None) -> Path:
    d = json.loads(Path(DEMO_CONFIG).read_text())
    d.update(out_dir=str(out / "run"), base_dir=str(Path(DEMO_CONFIG).parent))
    d["corpus"]["kind"] = "real"
    d.pop("prereg_required", None)
    m = out / "participant_id_map.json"
    m.write_text(json.dumps({"_meta": {}, "map": {"QZ": {"canonical_id": "KC99"}}}))
    d["corpus"]["participant_id_map"] = str(m)
    if tool is not None:
        d["tool"] = tool
    p = out / "config.json"
    p.write_text(json.dumps(d))
    return p


def _open_log(cfg):
    (Path(cfg.out_dir) / "logs").mkdir(parents=True, exist_ok=True)
    return AuditLog(Path(cfg.out_dir) / "logs" / "run.jsonl", cfg.run_id)


class TestCallSite(unittest.TestCase):
    def _guard_kwargs(self, tool):
        cfg = RunConfig.load(real_config(Path(tempfile.mkdtemp()), tool))
        log = _open_log(cfg)
        try:
            with mock.patch("second_scribe.pipeline.guard_real_clients",
                            side_effect=RealGuardRefused("stop here")) as g, self.assertRaises(RealGuardRefused):
                run(cfg, [], {}, log=log)
        finally:
            log.close()
        self.assertEqual(g.call_count, 1)
        return g.call_args.kwargs

    def test_study_config_calls_the_study_rule(self):
        self.assertEqual(self._guard_kwargs(None), {"judge_rule": "study", "families": None,
                                                    "remote_endpoints": None})      # T18 (K13): None for a study

    def test_tool_config_passes_its_rule_and_families(self):
        fams = {"recon": "gemma", "judge": "qwen"}
        self.assertEqual(self._guard_kwargs({"judge_rule": "two_family", "model_families": fams}),
                         {"judge_rule": "two_family", "families": fams, "remote_endpoints": None,
                          "endpoint_optins": []})                       # audit K7: a tool config passes its opt-ins
        self.assertEqual(self._guard_kwargs({"reference_type": "A"}),
                         {"judge_rule": "study", "families": None, "remote_endpoints": None, "endpoint_optins": []})

    def test_study_config_refuses_every_remote_endpoint(self):
        """Analysis session, 18 Oct (binding): a study config (no ``tool`` block) on a real corpus refuses every
        non-local endpoint, on each role, for OpenAI-compatible hosts and for Anthropic's, before any request. Pinned
        on today's guard so the remote allow-list (T18) cannot open egress for a study config."""
        anthropic = "https://api.anthropic.com/v1"
        remotes = ("https://api.openai.com/v1", "https://openrouter.ai/api/v1", f"http://{ip(203, 0, 113, 5)}:8000/v1",
                   anthropic)
        local = {"recon": ("qwen3:8b", ip(10, 0, 0, 1)), "judge": ("gemma3:4b", ip(10, 0, 0, 2)),
                 "judge2": ("llama3.1:8b", ip(10, 0, 0, 3))}

        def remote_client(role, url, model):
            if url == anthropic:      # a client of the Anthropic kind: the guard reads only its cfg
                cfg = types.SimpleNamespace(name=role, endpoint=url, model_id="claude-sonnet-x", params=dict(OFF),
                                            provider="anthropic")
                return types.SimpleNamespace(cfg=cfg, complete=mock.Mock(side_effect=AssertionError("sent")))
            return HTTPClient(ModelConfig(name=role, endpoint=url, model_id=model, params=dict(OFF)))

        cfg = RunConfig.load(real_config(Path(tempfile.mkdtemp())))
        self.assertEqual(cfg.tool, {})
        n = 0
        for role in ("recon", "judge", "judge2"):
            for url in remotes:
                clients = {r: http(m, r, h) for r, (m, h) in local.items()}
                clients[role] = remote_client(role, url, local[role][0])
                log = _open_log(cfg)
                try:
                    sent = AssertionError("a request was sent")
                    with mock.patch("second_scribe.llm._OPENER.open", side_effect=sent) as uo, \
                            mock.patch("second_scribe.llm._resolve", return_value=[ip(104, 18, 0, 1)]), \
                            self.assertRaises(EgressRefused, msg=(role, url)) as cm:
                        run(cfg, ["Q", "J"], clients, log=log)
                finally:
                    log.close()
                self.assertIn(f"the {role} endpoint is refused", str(cm.exception))
                self.assertEqual(uo.call_count, 0)
                n += 1
        self.assertEqual(n, 12)
        self.assertFalse((Path(cfg.out_dir) / "work").exists())


class TestCodeHash(unittest.TestCase):
    def _copy_kit(self) -> Path:
        root = Path(tempfile.mkdtemp())
        ignore = shutil.ignore_patterns("__pycache__")
        shutil.copytree(codehash.KIT_ROOT / "second_scribe", root / "second_scribe", ignore=ignore)
        shutil.copytree(codehash.KIT_ROOT / "prompts", root / "prompts", ignore=ignore)
        return root

    def test_code_sha256_stable_and_changes(self):
        real = codehash.code_sha256()
        self.assertRegex(real, HEX64)
        self.assertEqual(codehash.code_sha256(), real)
        root = self._copy_kit()
        with mock.patch.object(codehash, "KIT_ROOT", root):
            self.assertEqual(codehash.code_sha256(), real)              # the place does not enter the hash
            pyc = root / "second_scribe" / "__pycache__"
            pyc.mkdir(exist_ok=True)
            (pyc / "x.cpython-310.pyc").write_bytes(b"\0")
            (pyc / "stray.json").write_text("{}")                      # any suffix inside a cache is left out
            (root / "second_scribe" / "notes.md").write_text("not code")
            self.assertEqual(codehash.code_sha256(), real)              # caches and other suffixes are left out
            target = root / "second_scribe" / "families.py"
            data = bytearray(target.read_bytes())
            data[-2] = (data[-2] + 1) % 256
            target.write_bytes(bytes(data))
            changed = codehash.code_sha256()
            self.assertNotEqual(changed, real)
            for sub, name in (("prompts", "MANIFEST.json"), ("second_scribe", "app/strings/en.json")):
                p = root / sub / name
                with self.subTest(p=name):
                    before = codehash.code_sha256()
                    p.write_bytes(p.read_bytes() + b" ")
                    self.assertNotEqual(codehash.code_sha256(), before)
            self.assertEqual(codehash.code_sha256(root=root), codehash.code_sha256())    # explicit root = patched

    def test_code_sha256_mirrors_the_prompt_set_hash(self):
        root = self._copy_kit()
        files = {}
        for sub in ("second_scribe", "prompts"):
            for p in (root / sub).rglob("*"):
                if p.is_file() and p.suffix in (".py", ".json", ".txt") and "__pycache__" not in p.parts:
                    files[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
        lines = "\n".join(f"{n} {files[n]}" for n in sorted(files)).encode("utf-8")
        self.assertEqual(codehash.code_sha256(root=root), hashlib.sha256(lines).hexdigest())
        self.assertIn("prompts/MANIFEST.json", files)
        self.assertIn("second_scribe/codehash.py", files)

    def test_git_commit_none_without_git(self):
        root = self._copy_kit()
        self.assertIsNone(codehash.git_commit(root=root))              # not a git checkout
        with mock.patch.object(codehash, "KIT_ROOT", root):
            self.assertIsNone(codehash.git_commit())
        with mock.patch("second_scribe.codehash.subprocess.run", side_effect=FileNotFoundError("git")):
            self.assertIsNone(codehash.git_commit())                   # no git binary
        with mock.patch("second_scribe.codehash.subprocess.run",
                        side_effect=subprocess.TimeoutExpired(["git"], 5)) as r:
            self.assertIsNone(codehash.git_commit())                   # a hung git
        self.assertEqual(r.call_args.kwargs.get("timeout"), 5)

    def test_git_commit_is_head_of_the_kit_itself(self):
        head = subprocess.run(["git", "-C", str(codehash.KIT_ROOT), "rev-parse", "HEAD"], capture_output=True,
                              text=True)
        if head.returncode != 0:
            self.skipTest("the kit is not a git checkout here")
        self.assertEqual(codehash.git_commit(), head.stdout.strip())
        # a kit unpacked inside someone else's repository does not report that repository's commit
        outer = Path(tempfile.mkdtemp())
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(outer)}
        for args in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
                                       "-m", "outer"]):
            subprocess.run(["git", "-C", str(outer), *args], check=True, capture_output=True, env=env)
        (outer / "kit").mkdir()
        self.assertIsNone(codehash.git_commit(root=outer / "kit"))


def _demo_run(tool=None):
    """An oracle run of the demo config (all stages); with ``tool`` it is a tool run of the same corpus."""
    out = Path(tempfile.mkdtemp())
    with contextlib.redirect_stdout(io.StringIO()):
        if tool is None:
            rc = cli_main(["demo", "--out", str(out), "--client", "oracle"])
        else:
            d = json.loads(Path(DEMO_CONFIG).read_text(encoding="utf-8"))
            d.update(out_dir=str(out), base_dir=str(Path(DEMO_CONFIG).parent), tool=tool)
            cfg = out / "config.json"
            cfg.write_text(json.dumps(d), encoding="utf-8")
            rc = cli_main(["run", "--config", str(cfg), "--stages", "all", "--client", "oracle"])
    with open(out / "logs" / "run.jsonl", encoding="utf-8") as fh:
        records = [json.loads(line) for line in fh if line.strip()]
    agg = json.loads((out / "aggregates" / "aggregates.json").read_text(encoding="utf-8"))
    return out, rc, records, agg


class TestRunRecords(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out, cls.rc, cls.records, cls.agg = _demo_run()
        cls.t_out, cls.t_rc, cls.t_records, cls.t_agg = _demo_run({"reference_type": "A"})

    def test_run_start_has_code_fields_and_aggregates_chain(self):
        self.assertEqual((self.rc, self.t_rc), (0, 0))
        for records in (self.records, self.t_records):          # every run, study or tool
            starts = [r["payload"] for r in records if r["record_type"] == "run_start"]
            self.assertEqual(len(starts), 1)
            self.assertEqual(starts[0]["code_sha256"], codehash.code_sha256())
            self.assertIn("code_commit", starts[0])
        self.assertNotIn("chain", self.agg)                       # a study config's aggregates: unchanged
        chain = self.t_agg["chain"]                               # a tool run's: the log as it stood
        self.assertEqual(set(chain), {"head", "records", "verified"})
        self.assertIs(chain["verified"], True)
        self.assertIsInstance(chain["records"], int)
        self.assertGreater(chain["records"], 1)
        self.assertLess(chain["records"], len(self.t_records))         # audit_summary and run_end come later
        self.assertRegex(chain["head"], HEX64)
        self.assertEqual(chain["head"], self.t_records[chain["records"] - 1]["hash"])
        rest = {k: v for k, v in self.t_agg.items() if k not in ("chain", "config_sha256")}
        self.assertEqual(rest, {k: v for k, v in self.agg.items() if k != "config_sha256"})   # chain is the only addition

    def test_chain_keys_pass_the_aggregate_only_guard(self):
        """A real corpus's map may hold a node labelled "Records" or "Head": the chain block's keys are the kit's own
        words (``kit_vocab``), so the guard does not refuse a tool run's aggregates over them."""
        from second_scribe.kit_vocab import KIT_KEYS, KIT_VALUES
        from second_scribe.report import assert_aggregate_only
        labels = {"Chain", "Head", "Records", "Verified"}
        assert_aggregate_only({"chain": self.t_agg["chain"]}, labels, keys=KIT_KEYS, values=KIT_VALUES)
        with self.assertRaises(ValueError):
            assert_aggregate_only({"chain": self.t_agg["chain"]}, labels)

    def test_study_aggregates_unchanged(self):
        rest = json.loads(json.dumps(self.agg))
        decl = rest["ladder"]["report"]["declaration_check"]
        self.assertRegex(decl["declared_on"], r"^\d{4}-\d{2}-\d{2}$")
        decl["declared_on"] = "2026-10-18"          # the declaration's UTC date (the day of the run): the base's day
        text = json.dumps(_round_aggregate_floats(rest), sort_keys=True, ensure_ascii=False, indent=2) + "\n"
        self.assertEqual(hashlib.sha256(text.encode("utf-8")).hexdigest(), BASE_DEMO_AGGREGATES_SHA)

    def test_aggregate_rounding_preserves_material_changes_and_other_types(self):
        original = {"nested": [0.42570287792181305, 7, True, None, "0.42570287792181305"]}
        noisy = {"nested": [0.42570287792181294, 7, True, None, "0.42570287792181305"]}
        normalized = _round_aggregate_floats(original)
        self.assertEqual(normalized, _round_aggregate_floats(noisy))
        noisy["nested"][0] += 1e-6
        self.assertNotEqual(normalized, _round_aggregate_floats(noisy))
        self.assertEqual(original["nested"][0], 0.42570287792181305)  # no mutation
        for actual, expected in zip(normalized["nested"][1:], original["nested"][1:]):
            self.assertIs(type(actual), type(expected))
            self.assertEqual(actual, expected)
        self.assertNotEqual(normalized, _round_aggregate_floats({**original, "extra": None}))

    def test_demo_config_hash_unchanged(self):
        self.assertEqual(RunConfig.load(DEMO_CONFIG).sha256(),
                         "ccc13208b1fe5e66b5f4bef970af228cb92af9aefca6f21d341a0ccaa18c1ba9")

    def test_chain_of_a_broken_log_is_not_verified(self):
        log_path = Path(tempfile.mkdtemp()) / "run.jsonl"
        lines = (self.out / "logs" / "run.jsonl").read_text(encoding="utf-8").splitlines(keepends=True)
        rec = json.loads(lines[3])
        rec["payload"] = {**rec["payload"], "edited": True}           # the stored hash no longer matches
        log_path.write_text("".join(lines[:3]) + json.dumps(rec) + "\n", encoding="utf-8")
        r = types.SimpleNamespace(log=types.SimpleNamespace(path=str(log_path)))
        self.assertEqual(pipeline.chain_state(r)["verified"], False)
        r.log.path = str(log_path.parent / "absent.jsonl")
        self.assertEqual(pipeline.chain_state(r), {"head": None, "records": 0, "verified": False})


if __name__ == "__main__":
    unittest.main()
