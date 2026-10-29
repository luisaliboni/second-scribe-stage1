import contextlib
import copy
import hashlib
import inspect
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock
from dataclasses import replace
from pathlib import Path

from second_scribe import prereg
from second_scribe.audit.registry import AuditLog
from second_scribe.cli import DEMO_CONFIG, build_clients
from second_scribe.cli import main as cli_main
from second_scribe.match import PRIMARY_SETTING, SETTINGS_GRID
from second_scribe.pipeline import KIT_ROOT, PROMPTS_DIR, STAGE_ORDER, RunConfig, run

RECON_PARAMS = {"chat_template_kwargs": {"enable_thinking": False}}
MODEL_INFO = {"recon": {"model_id": "recon-model.gguf", "weights": "a" * 64, "params": RECON_PARAMS},
              "judge": {"model_id": "judge-model.gguf", "weights": "rev-1234", "params": {}}}


def frozen_info(info):
    """What build_prereg stores for a model_info: basename plus the sha256 of the full id."""
    return {r: {**i, "model_id": i["model_id"].rsplit("/", 1)[-1],
                "model_id_sha256": hashlib.sha256(i["model_id"].encode()).hexdigest()} for r, i in info.items()}
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def records(log_path):
    return [json.loads(line) for line in Path(log_path).read_text().splitlines()]


def call_request(log, stage, model_id=None, params=None):
    rid = f"{stage}:x:0:{len(open(log.path).read()) if os.path.exists(log.path) else 0}"
    payload = {"request_id": rid, "stage": stage, "item_id": "x", "attempt": 0}
    if model_id is not None:
        payload.update(model_id=model_id, params=params or {})
    log.append("call_request", payload)
    log.append("call_response", {"request_id": rid})
    return rid


def forge_freeze(prereg_obj, path, log):
    """Write the file and append a prereg_frozen record by hand: freeze() itself refuses to run after a model call,
    and a forged freeze is what check must still catch."""
    data = prereg.canonical(prereg_obj).encode()
    Path(path).write_bytes(data)
    log.append("prereg_frozen", {"sha256": hashlib.sha256(data).hexdigest(), "path": str(path)})


def copy_kit(dest: Path) -> Path:
    """A throwaway kit root: second_scribe/, prompts/, docs/ and the root *.py copied without bytecode, ``demo/``
    linked. Freezing and checking against it keeps these tests off the live tree, whose __pycache__ and files change
    while other tests run."""
    dest.mkdir(parents=True)
    skip = shutil.ignore_patterns("__pycache__", "*.pyc", ".*")
    for d in ("second_scribe", "prompts", "docs"):
        shutil.copytree(KIT_ROOT / d, dest / d, ignore=skip)
    for f in KIT_ROOT.glob("*.py"):
        shutil.copyfile(f, dest / f.name)
    (dest / "demo").symlink_to(KIT_ROOT / "demo")
    return dest


_KIT_COPY: list = []


def shared_kit() -> Path:
    """One copy of the kit per test process (nothing writes to it), removed at exit."""
    if not _KIT_COPY:
        import atexit
        root = Path(tempfile.mkdtemp())
        atexit.register(shutil.rmtree, root, True)
        _KIT_COPY.append(copy_kit(root / "kit"))
    return _KIT_COPY[0]


def use_kit(case: unittest.TestCase, dest=None) -> Path:
    """Point prereg's KIT_ROOT, PROMPTS_DIR and DOCS_DIR (what the run guard and the CLI read) at the copy for one test."""
    kit = shared_kit()
    for name, value in (("KIT_ROOT", kit), ("PROMPTS_DIR", kit / "prompts"), ("DOCS_DIR", kit / "docs")):
        patcher = unittest.mock.patch.object(prereg, name, value)
        patcher.start()
        case.addCleanup(patcher.stop)
    return kit


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.kit = use_kit(self)
        self.prompts = self.tmp / "prompts"
        shutil.copytree(PROMPTS_DIR, self.prompts)
        self.docs = self.tmp / "docs"
        self.docs.mkdir()
        shutil.copyfile(KIT_ROOT / "docs" / "prereg-judge-labels.md", self.docs / "prereg-judge-labels.md")
        self.cfg = RunConfig.load(DEMO_CONFIG, out_dir=str(self.tmp / "out"))
        self.path = self.tmp / "prereg.json"
        self.log_path = self.tmp / "run.jsonl"
        self.log = AuditLog(self.log_path, self.cfg.run_id)

    def build(self, cfg=None, **kw):
        kw.setdefault("code_commit", COMMIT)
        kw.setdefault("model_info", MODEL_INFO)
        kw.setdefault("decision_rules", prereg.DECISION_RULES)
        return prereg.build_prereg(cfg or self.cfg, self.prompts, **kw)

    def freeze(self, **kw):
        return prereg.freeze(self.build(**kw), self.path, self.log)

    def check(self, **kw):
        return prereg.check(self.path, self.log_path, self.prompts, **kw)


class TestFreezeAndCheck(Base):
    def test_freeze_logs_the_hash_of_the_file_it_wrote(self):
        digest = self.freeze()
        self.assertEqual(digest, sha(self.path))
        rec = [r for r in records(self.log_path) if r["record_type"] == "prereg_frozen"]
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["payload"]["sha256"], sha(self.path))
        self.assertEqual(rec[0]["payload"]["path"], str(self.path))
        self.assertEqual(self.check(), (True, "ok"))

    def test_the_file_is_canonical_json(self):
        self.freeze()
        text = self.path.read_text()
        obj = json.loads(text)
        self.assertEqual(text, json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False))

    def test_editing_a_prompt_after_the_freeze_fails_the_check(self):
        self.freeze()
        f = self.prompts / "reconstruct.p0.v2.txt"
        f.write_text(f.read_text() + "\nbe creative\n")
        ok, msg = self.check()
        self.assertFalse(ok)
        self.assertIn("prompt changed", msg)
        self.assertIn("reconstruct.p0.v2.txt", msg)

    def test_a_new_or_removed_prompt_file_fails_the_check(self):
        self.freeze()
        (self.prompts / "extra.v9.txt").write_text("x")
        ok, msg = self.check()
        self.assertFalse(ok)
        self.assertIn("prompt changed", msg)
        (self.prompts / "extra.v9.txt").unlink()
        (self.prompts / "align.v2.txt").unlink()
        ok, msg = self.check()
        self.assertFalse(ok)
        self.assertIn("align.v2.txt", msg)

    def test_editing_the_prereg_file_fails_the_check(self):
        self.freeze()
        obj = json.loads(self.path.read_text())
        obj["thresholds"]["min_words"] = 3
        self.path.write_text(json.dumps(obj, sort_keys=True, separators=(",", ":")))
        ok, msg = self.check()
        self.assertFalse(ok)
        self.assertIn("prereg file changed", msg)

    def test_the_judge_label_conventions_are_hashed_too(self):
        self.freeze()
        f = self.docs / "prereg-judge-labels.md"
        f.write_text(f.read_text() + "More.\n")
        ok, msg = self.check(docs_dir=self.docs)
        self.assertFalse(ok)
        self.assertIn("judge label", msg)

    def test_freeze_refuses_after_a_model_call(self):
        call_request(self.log, "recon:removed")
        with self.assertRaises(ValueError) as cm:
            self.freeze()
        self.assertIn("before the first one", str(cm.exception))
        self.assertFalse(self.path.exists())

    def test_a_forged_freeze_after_a_recon_call_fails_the_check(self):
        call_request(self.log, "recon:removed")
        forge_freeze(self.build(), self.path, self.log)
        ok, msg = self.check()
        self.assertFalse(ok)
        self.assertIn("before the freeze", msg)

    def test_any_call_before_the_freeze_fails_not_only_model_stages(self):
        for stage in ("q:mapping_talk", "q:in_transcript", "judge:modality", "judge2:modality", "prior_only",
                      "calibration:matcher", "bench:judge", "audit:canary"):
            with self.subTest(stage=stage):
                tmp = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, tmp, True)
                log_path = tmp / "run.jsonl"
                log = AuditLog(log_path, "r")
                call_request(log, stage)
                path = tmp / "p.json"
                forge_freeze(self.build(), path, log)
                ok, msg = prereg.check(path, log_path, self.prompts)
                self.assertFalse(ok, stage)
                self.assertIn("before the freeze", msg)

    def test_calls_after_the_freeze_are_fine(self):
        self.freeze()
        call_request(self.log, "recon:removed")
        self.assertEqual(self.check(), (True, "ok"))

    def test_no_frozen_record_is_not_frozen(self):
        self.path.write_text("{}")
        self.assertEqual(self.check(), (False, "prereg not frozen"))
        self.assertEqual(prereg.check(self.tmp / "none.json", self.tmp / "none.jsonl", self.prompts),
                         (False, "prereg not frozen"))

    def test_a_second_frozen_record_fails_the_check(self):
        self.freeze()
        self.log.append("prereg_frozen", {"sha256": "0" * 64, "path": str(self.path)})
        ok, msg = self.check()
        self.assertFalse(ok)
        self.assertIn("more than one", msg)

    def test_a_tampered_log_fails_the_check(self):
        self.freeze()
        lines = self.log_path.read_text().splitlines()
        rec = json.loads(lines[-1])
        rec["payload"]["sha256"] = sha(self.path)[::-1]
        lines[-1] = json.dumps(rec)
        self.log_path.write_text("\n".join(lines) + "\n")
        ok, msg = self.check()
        self.assertFalse(ok)
        self.assertIn("log", msg)

    def test_a_changed_config_fails_the_check(self):
        self.freeze()
        self.assertEqual(self.check(cfg=self.cfg), (True, "ok"))
        changed = replace(self.cfg, stats={**self.cfg.stats, "B": 5})
        ok, msg = self.check(cfg=changed)
        self.assertFalse(ok)
        self.assertIn("config changed", msg)

    def test_the_output_folder_is_not_part_of_the_config_hash(self):
        self.freeze()
        moved = replace(self.cfg, out_dir=str(self.tmp / "elsewhere"))
        self.assertEqual(self.check(cfg=moved), (True, "ok"))

    def test_freeze_refuses_a_second_time(self):
        self.freeze()
        with self.assertRaises(RuntimeError):
            self.freeze()
        self.assertEqual(sum(r["record_type"] == "prereg_frozen" for r in records(self.log_path)), 1)

    def test_freeze_refuses_when_the_prompt_manifest_is_stale(self):
        (self.prompts / "align.v2.txt").write_text("changed")
        with self.assertRaises(ValueError):
            self.build()


class TestContent(Base):
    def test_required_fields(self):
        p = self.build()
        self.assertEqual(p["run_id"], "demo")
        self.assertEqual(p["code_commit"], COMMIT)
        self.assertEqual(p["config_sha256"], self.cfg.sha256())
        manifest = json.loads((self.prompts / "MANIFEST.json").read_text())
        self.assertEqual(p["prompt_set_sha256"], manifest["set_sha256"])
        self.assertEqual(p["prompts"], manifest["files"])
        self.assertEqual(p["primary_spec"], "removed__p0__t0__r0")
        self.assertEqual(p["primary_setting"], PRIMARY_SETTING.key())
        self.assertEqual(p["settings"], [s.key() for s in self.cfg.match_settings()])
        self.assertEqual(set(p["settings"]), {s.key() for s in SETTINGS_GRID})
        self.assertEqual(p["specs"], [s.key() for s in self.cfg.recon_specs()])
        self.assertEqual(p["thresholds"]["min_words"], 8)
        self.assertEqual(p["thresholds"]["min_words_grid"], [5, 8, 12])
        self.assertEqual(p["thresholds"]["max_unfound_share"], 0.5)      # L16, was 0.1
        self.assertEqual(p["thresholds"]["script_support"], 2)
        self.assertEqual(p["thresholds"]["not_judged_limit"], 0.10)
        self.assertEqual(p["headline_order"], ["absences", "pull_toward_script", "homogenization", "linearization"])
        self.assertEqual(p["model_info"], frozen_info(MODEL_INFO))
        self.assertEqual(p["decision_rules"], prereg.DECISION_RULES)
        self.assertEqual(p["judge_labels"]["sha256"], sha(KIT_ROOT / "docs" / "prereg-judge-labels.md"))
        self.assertIn("precedence", p["judge_labels"]["text"])
        self.assertEqual(p["corpus_kind"], "synthetic")
        self.assertEqual(p["prior_only"], self.cfg.prior_only)
        self.assertIn("created", p)

    def test_rules_constants(self):
        p = self.build()
        self.assertEqual(p["coverage_rule"]["min_cell_n_floor"], 3)
        self.assertEqual(p["coverage_rule"]["min_cell_n_share_of_primary"], 0.5)
        self.assertEqual(p["coverage_rule"]["n_nan"], 0)
        self.assertTrue(p["coverage_rule"]["all_cells_same_sign"])
        self.assertTrue(p["canonical_gate"]["canonical_used"])
        self.assertTrue(p["canonical_gate"]["judge_channel_canonical"])
        lr = p["leakage_rule"]
        self.assertNotIn("status_permutation_null", lr)                         # the old claim/mechanism keys are gone
        self.assertEqual(lr["claim"], "script-conditional leak difference (leak rate of scripted minus unscripted "
                                      "endorsed ideals), other-family script source, permutation p < 0.05 and "
                                      "bootstrap CI lower bound above 0")
        self.assertEqual(lr["own_script"], "sensitivity only, never sets the flag")
        self.assertEqual(lr["base_rate"], "reported, never a headline, never a gate")
        self.assertEqual(lr["worse_than_chance_check"], "reported, never a headline, never a gate")
        self.assertEqual(p["not_judged_rule"], {"limit": 0.10, "above_limit": "reported_not_interpreted"})
        self.assertEqual(p["canonicalization"]["batch_size"], 50)
        self.assertEqual(p["judge_pass_rule"]["accuracy_min"], 0.90)
        self.assertEqual(p["judge_pass_rule"]["recall_min"], 0.80)
        self.assertEqual(p["judge_pass_rule"]["not_judged_max"], 0.02)

    def test_defaults_track_the_code(self):
        from second_scribe.metrics import MIN_CELL_N, MIN_WORDS_GRID, build_script
        from second_scribe.pipeline import CANON_BATCH, NOT_JUDGED_LIMIT
        from second_scribe.reconstruct import validate_recon
        self.assertEqual(inspect.signature(validate_recon).parameters["max_unfound_share"].default,
                         prereg.MAX_UNFOUND_SHARE)
        self.assertEqual(inspect.signature(build_script).parameters["support"].default, prereg.SCRIPT_SUPPORT)
        self.assertEqual(list(MIN_WORDS_GRID), prereg.MIN_WORDS_GRID)
        self.assertEqual(MIN_CELL_N, prereg.MIN_CELL_N_FLOOR)
        self.assertEqual(CANON_BATCH, prereg.CANON_BATCH_SIZE)
        self.assertEqual(NOT_JUDGED_LIMIT, prereg.NOT_JUDGED_LIMIT)

    def test_the_frozen_judge_pass_rule_is_the_benches_rule(self):
        """One source of truth: judge_bench's PASS_* constants. The frozen rule and the canary floor derive from them."""
        from second_scribe import audits, judge_bench as jb
        rule = prereg.JUDGE_PASS_RULE
        self.assertEqual(rule, jb.pass_rule_thresholds())                  # one source of truth, every threshold
        for key in ("actual_recall_min", "wished_precision_min", "off_record_mention_recall_min", "off_record_min_n",
                    "letter_mass_min"):
            self.assertIn(key, rule)                                       # the Wave-1 floors are frozen too
        self.assertEqual(audits.CANARY_MIN_PASS, jb.PASS_CLASS_RECALL)
        self.assertEqual(self.build()["judge_pass_rule"], rule)

    def test_no_endpoint_or_key_reaches_the_prereg(self):
        host = ".".join(("10", "1", "2", "3"))       # built, so this file holds no address for the leak scan to find
        os.environ["SS_RECON_URL"] = f"http://{host}:8000/v1"
        self.addCleanup(os.environ.pop, "SS_RECON_URL", None)
        text = json.dumps(self.build())
        self.assertNotIn(host, text)
        self.assertNotIn("http", text)

    def test_model_info_must_cover_every_configured_model(self):
        with self.assertRaises(ValueError):
            self.build(model_info={"recon": MODEL_INFO["recon"]})
        with self.assertRaises(ValueError):
            self.build(model_info={**MODEL_INFO, "judge": {"model_id": "x"}})

    def test_a_model_path_is_reduced_to_its_basename(self):
        p = self.build(model_info={"recon": {"model_id": "/Users/someone/models/m.gguf", "weights": "w"},
                                   "judge": {"model_id": "org/name", "weights": "w"}})
        self.assertEqual(p["model_info"]["recon"]["model_id"], "m.gguf")
        self.assertEqual(p["model_info"]["judge"]["model_id"], "name")

    def test_judge2_is_included_when_configured(self):
        cfg = replace(self.cfg, models={**self.cfg.models, "judge2": "JUDGE2"})
        with self.assertRaises(ValueError):
            self.build(cfg)
        info = {**MODEL_INFO, "judge2": {"model_id": "j2", "weights": "w"}}
        self.assertIn("judge2", self.build(cfg, model_info=info)["model_info"])

    def test_the_primary_spec_must_be_the_registered_one(self):
        specs = list(self.cfg.specs)
        specs[0], specs[1] = specs[1], specs[0]
        with self.assertRaises(ValueError):
            self.build(replace(self.cfg, specs=specs))

    def real(self):
        return replace(self.cfg, corpus={**self.cfg.corpus, "kind": "real"}, prereg_required=True)

    CAL = {"matcher": {"lex0.6_same": {"f1": 0.9}}, "in_transcript": {"precision": 1.0, "recall": 0.9}}

    def refused(self, **kw):
        with self.assertRaises(ValueError) as cm:
            prereg.freeze(self.build(self.real(), **kw), self.path, self.log)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.log_path.exists())          # nothing written, nothing logged
        return str(cm.exception)

    def test_synthetic_can_freeze_with_pending_weights_and_no_calibration(self):
        info = {**MODEL_INFO, "judge": {"model_id": "j", "weights": "pending"}}
        self.assertEqual(self.build(model_info=info)["model_info"]["judge"]["weights"], "pending")
        prereg.freeze(self.build(model_info=info), self.path, self.log)
        self.assertEqual(self.check(), (True, "ok"))

    def test_real_refuses_pending_weights(self):
        info = {**MODEL_INFO, "judge": {"model_id": "j", "weights": "pending"}}
        self.assertIn("judge", self.refused(model_info=info, calibration=self.CAL))
        self.assertIn("pending", self.refused(model_info=info, calibration=self.CAL))

    def test_real_refuses_missing_calibration(self):
        self.assertIn("matcher", self.refused())
        self.assertIn("off-record", self.refused())
        self.assertIn("off-record", self.refused(calibration={"matcher": self.CAL["matcher"]}))
        only_off = self.refused(calibration={"in_transcript": self.CAL["in_transcript"]})
        self.assertIn("matcher", only_off)
        self.assertNotIn("off-record", only_off)
        self.assertIn("matcher", self.refused(calibration={"matcher": {}, "in_transcript": self.CAL["in_transcript"]}))

    def test_real_names_every_missing_item_at_once(self):
        info = {**MODEL_INFO, "recon": {"model_id": "r", "weights": "pending"}}
        msg = self.refused(model_info=info)
        for word in ("recon", "matcher", "off-record"):
            self.assertIn(word, msg)

    def test_real_freezes_with_everything_present(self):
        digest = prereg.freeze(self.build(self.real(), calibration=self.CAL), self.path, self.log)
        self.assertEqual(digest, sha(self.path))

    def test_calibration_is_stored_as_given(self):
        cal = {"matcher": {"lex0.6_same": {"precision": 0.9, "recall": 0.8, "f1": 0.85}},
               "in_transcript": {"precision": 1.0, "recall": 0.9}}
        self.assertEqual(self.build(calibration=cal)["calibration"], cal)
        self.assertEqual(self.build()["calibration"], {})

    def test_the_template_doc_holds_the_decision_rules_verbatim(self):
        text = (KIT_ROOT / "docs" / "prereg-template.md").read_text()
        self.assertIn(prereg.DECISION_RULES, text)
        self.assertIn("removed__p0__t0__r0", prereg.DECISION_RULES)
        self.assertNotIn("ground truth", text.lower())
        self.assertNotIn("validated", text.lower())


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


class RepoBase(Base):
    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "repo"
        (self.repo / "second_scribe").mkdir(parents=True)
        (self.repo / "prompts").mkdir()
        (self.repo / "docs").mkdir()
        (self.repo / "second_scribe" / "a.py").write_text("x = 1\n")
        (self.repo / "prompts" / "p.txt").write_text("p\n")
        (self.repo / "docs" / "n.md").write_text("n\n")
        (self.repo / ".gitignore").write_text("__pycache__/\n*.pyc\n.DS_Store\n")
        git(self.repo, "init", "-q")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "one")
        self.head = git(self.repo, "rev-parse", "HEAD")

    def code(self, commit=None):
        return prereg.check_code(commit or self.head, self.repo)


class TestCodeCheck(RepoBase):
    def test_same_commit_clean_tree_is_fine(self):
        self.assertIsNone(self.code())

    def test_a_later_commit_touching_only_docs_is_fine(self):
        (self.repo / "docs" / "n.md").write_text("changed\n")
        git(self.repo, "commit", "-q", "-am", "docs")
        self.assertIsNone(self.code())

    def test_a_later_commit_touching_code_or_prompts_fails(self):
        (self.repo / "second_scribe" / "a.py").write_text("x = 2\n")
        git(self.repo, "commit", "-q", "-am", "code")
        self.assertIn("code changed", self.code())

    def test_a_later_commit_touching_prompts_fails(self):
        (self.repo / "prompts" / "p.txt").write_text("q\n")
        git(self.repo, "commit", "-q", "-am", "prompt")
        self.assertIn("code changed", self.code())

    def test_uncommitted_changes_under_code_fail(self):
        (self.repo / "second_scribe" / "a.py").write_text("x = 3\n")
        self.assertIn("uncommitted", self.code())

    def test_an_untracked_file_under_code_fails(self):
        (self.repo / "second_scribe" / "new.py").write_text("y = 1\n")
        self.assertIn("uncommitted", self.code())

    def test_a_commit_that_is_not_an_ancestor_fails(self):
        git(self.repo, "checkout", "-q", "-b", "other")
        (self.repo / "docs" / "n.md").write_text("other\n")
        git(self.repo, "commit", "-q", "-am", "other")
        git(self.repo, "checkout", "-q", "-")
        git(self.repo, "checkout", "-q", "-b", "third")
        (self.repo / "docs" / "n.md").write_text("third\n")
        git(self.repo, "commit", "-q", "-am", "third")
        other = git(self.repo, "rev-parse", "other")
        self.assertIn("ancestor", self.code(other))

    def test_an_unknown_commit_or_a_non_git_folder_fails(self):
        self.assertIsNotNone(self.code("f" * 40))
        self.assertIsNotNone(prereg.check_code(self.head, self.tmp / "docs"))

    def test_check_wires_the_code_check(self):
        self.freeze(code_commit=self.head, code_root=self.repo)
        self.assertEqual(self.check(kit_root=self.repo), (True, "ok"))
        (self.repo / "second_scribe" / "a.py").write_text("x = 9\n")
        ok, msg = self.check(kit_root=self.repo)
        self.assertFalse(ok)
        self.assertIn("code changed", msg)


class TestIndexFlagsCannotHideChanges(RepoBase):
    """I1: the code is hashed from disk at the freeze and re-hashed at the check, so git index flags and
    info/exclude, which fool ``git status``, do not hide an edit."""

    def setUp(self):
        super().setUp()
        self.freeze(code_commit=self.head, code_root=self.repo)

    def flagged(self, *flags):
        git(self.repo, "update-index", *flags, "second_scribe/a.py")
        (self.repo / "second_scribe" / "a.py").write_text("x = 'changed after the freeze'\n")

    def assert_files_catch_it(self, what):
        self.assertIsNone(self.code(self.head), "git alone is fooled")          # the premise of the finding
        ok, msg = self.check(kit_root=self.repo)
        self.assertFalse(ok)
        self.assertIn("code changed", msg)
        self.assertIn(what, msg)

    def test_skip_worktree(self):
        self.flagged("--skip-worktree")
        self.assert_files_catch_it("second_scribe/a.py (edited)")

    def test_assume_unchanged(self):
        self.flagged("--assume-unchanged")
        self.assert_files_catch_it("second_scribe/a.py (edited)")

    def test_info_exclude_hides_a_new_file_from_git(self):
        (self.repo / ".git" / "info" / "exclude").write_text("second_scribe/sneaky.py\n")
        (self.repo / "second_scribe" / "sneaky.py").write_text("import os\n")
        self.assert_files_catch_it("second_scribe/sneaky.py (new)")

    def test_a_removed_file(self):
        git(self.repo, "update-index", "--skip-worktree", "prompts/p.txt")
        (self.repo / "prompts" / "p.txt").unlink()
        self.assert_files_catch_it("prompts/p.txt (removed)")

    def test_pycache_and_dotfiles_are_not_code(self):
        (self.repo / "second_scribe" / "__pycache__").mkdir()
        (self.repo / "second_scribe" / "__pycache__" / "a.cpython-314.pyc").write_bytes(b"\0")
        (self.repo / "second_scribe" / ".DS_Store").write_bytes(b"\0")
        self.assertEqual(set(prereg.code_hashes(self.repo)), {"second_scribe/a.py", "prompts/p.txt"})   # not code

    def test_the_frozen_map_lists_every_code_file(self):
        files = json.loads(self.path.read_text())["code_files"]
        self.assertEqual(set(files), {"second_scribe/a.py", "prompts/p.txt"})
        self.assertEqual(files["prompts/p.txt"], hashlib.sha256(b"p\n").hexdigest())


class TestRootCodeAndShadows(RepoBase):
    """Round 3: the hashed code also covers *.py at the kit root and every top-level package, and a root file that
    shadows a standard-library module is refused."""

    def setUp(self):
        super().setUp()
        (self.repo / "runner.py").write_text("print('hi')\n")
        (self.repo / "extras").mkdir()
        (self.repo / "extras" / "__init__.py").write_text("")
        (self.repo / "extras" / "tool.py").write_text("y = 1\n")
        (self.repo / "notes").mkdir()
        (self.repo / "notes" / "n.txt").write_text("not a package\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "more")
        self.head = git(self.repo, "rev-parse", "HEAD")
        self.freeze(code_commit=self.head, code_root=self.repo)

    def test_the_frozen_map_covers_root_py_and_top_level_packages(self):
        files = set(json.loads(self.path.read_text())["code_files"])
        self.assertEqual(files, {"second_scribe/a.py", "prompts/p.txt", "runner.py", "extras/__init__.py", "extras/tool.py"})

    def test_an_edit_or_a_new_file_at_the_root_or_in_a_package_fails(self):
        self.assertEqual(self.check(kit_root=self.repo), (True, "ok"))
        for what, edit in (("runner.py (edited)", lambda: (self.repo / "runner.py").write_text("print('bye')\n")),
                           ("extras/tool.py (edited)", lambda: (self.repo / "extras" / "tool.py").write_text("y = 2\n")),
                           ("extras/more.py (new)", lambda: (self.repo / "extras" / "more.py").write_text("z = 1\n")),
                           ("helper.py (new)", lambda: (self.repo / "helper.py").write_text("x = 1\n"))):
            with self.subTest(what=what):
                edit()
                ok, msg = self.check(kit_root=self.repo)
                self.assertFalse(ok)
                self.assertIn(what, msg)
                git(self.repo, "checkout", "-q", "--", ".")
                git(self.repo, "clean", "-fdq")

    def test_a_folder_that_is_not_a_package_is_not_code(self):
        (self.repo / "notes" / "n.txt").write_text("changed\n")
        self.assertIsNone(prereg.code_file_problem(json.loads(self.path.read_text())["code_files"], self.repo))

    def test_a_stdlib_shadowing_file_or_package_at_the_root_is_refused(self):
        self.assertIsNone(prereg.shadow_problem(self.repo))
        for name, make in (("random.py", lambda: (self.repo / "random.py").write_text("x = 1\n")),
                           ("json", lambda: ((self.repo / "json").mkdir(), (self.repo / "json" / "__init__.py").write_text(""))),
                           ("os.py", lambda: (self.repo / "os.py").write_text(""))):
            with self.subTest(name=name):
                make()
                self.assertIn("shadows", prereg.shadow_problem(self.repo))
                self.assertIn(name, prereg.shadow_problem(self.repo))
                ok, msg = self.check(kit_root=self.repo)
                self.assertFalse(ok)
                self.assertIn("shadows", msg)
                git(self.repo, "clean", "-fdq")

    def test_every_importable_suffix_at_the_root_or_in_a_top_level_folder_is_refused_and_hashed(self):
        for rel in ("random.pyc", "random.so", "random.pyd", "random.cpython-314-darwin.so", "random.abi3.so",
                    "extras/json.so", "notes/types.pyc", "second_scribe/copy.pyd"):
            with self.subTest(file=rel):
                f = self.repo / rel
                f.write_bytes(b"x")
                self.assertIn("shadows", prereg.shadow_problem(self.repo))
                self.assertIn(rel.split("/")[-1].split(".")[0], prereg.shadow_problem(self.repo))
                self.assertEqual(prereg.code_hashes(self.repo)[rel], hashlib.sha256(b"x").hexdigest())
                ok, msg = self.check(kit_root=self.repo)
                self.assertFalse(ok)
                f.unlink()
                self.assertIsNone(prereg.shadow_problem(self.repo))

    def test_a_non_importable_file_with_a_stdlib_stem_is_fine(self):
        (self.repo / "notes" / "random.md").write_text("notes\n")
        (self.repo / "types.txt").write_text("notes\n")
        self.assertIsNone(prereg.shadow_problem(self.repo))
        self.assertNotIn("types.txt", prereg.code_hashes(self.repo))

    def compiled(self, src_rel="second_scribe/a.py"):
        import py_compile
        src = self.repo / src_rel
        return Path(py_compile.compile(str(src), doraise=True))

    def test_an_honest_pyc_passes_and_a_planted_one_is_refused(self):
        pyc = self.compiled()
        self.assertIsNone(prereg.shadow_problem(self.repo))
        good = pyc.read_bytes()
        # same header (so Python would load it), other code
        evil = compile("x = 'evil'\n", "evil", "exec")
        import marshal
        pyc.write_bytes(good[:16] + marshal.dumps(evil))
        self.assertIn("does not match its source", prereg.shadow_problem(self.repo))
        ok, msg = self.check(kit_root=self.repo)
        self.assertFalse(ok)
        self.assertIn("__pycache__", msg)
        pyc.write_bytes(good)
        self.assertIsNone(prereg.shadow_problem(self.repo))

    def test_an_unchecked_hash_pyc_with_other_code_is_refused(self):
        import importlib.util
        import marshal
        pyc = self.compiled()
        src = (self.repo / "second_scribe" / "a.py").read_bytes()
        header = importlib.util.MAGIC_NUMBER + (1).to_bytes(4, "little") + importlib.util.source_hash(src)
        pyc.write_bytes(header + marshal.dumps(compile("x = 'evil'\n", "evil", "exec")))
        self.assertIn("does not match its source", prereg.shadow_problem(self.repo))
        pyc.write_bytes(header + marshal.dumps(compile(src, "a.py", "exec", dont_inherit=True)))
        self.assertIsNone(prereg.shadow_problem(self.repo))

    def test_a_current_tag_pyc_without_source_or_garbage_is_refused(self):
        cache = self.repo / "second_scribe" / "__pycache__"
        cache.mkdir(exist_ok=True)
        tag = sys.implementation.cache_tag
        for name, data, why in ((f"gone.{tag}.pyc", b"\0" * 20, "no source"),
                                (f"a.{tag}.pyc", b"junk", "not a valid bytecode")):
            with self.subTest(pyc=name):
                (cache / name).write_bytes(data)
                self.assertIn(why, prereg.shadow_problem(self.repo))
                (cache / name).unlink()
        self.assertIsNone(prereg.shadow_problem(self.repo))

    def test_a_pyc_of_another_interpreter_beside_the_source_passes(self):
        """The honest false positive: a python3.13 pyc next to a.py (the repo is also used by another Python)."""
        cache = self.repo / "second_scribe" / "__pycache__"
        cache.mkdir(exist_ok=True)
        other = "cpython-313" if sys.implementation.cache_tag != "cpython-313" else "cpython-312"
        (cache / f"a.{other}.pyc").write_bytes(b"\0" * 40)                      # foreign bytes, never loaded here
        (cache / f"a.{other}.opt-1.pyc").write_bytes(b"\0" * 40)
        (cache / "a.cpython-27.pyc").write_bytes(b"\0")
        self.assertIsNone(prereg.shadow_problem(self.repo))
        self.assertEqual(self.check(kit_root=self.repo), (True, "ok"))

    def test_an_optimised_pyc_is_looked_at_only_under_that_optimize_level(self):
        cache = self.repo / "second_scribe" / "__pycache__"
        cache.mkdir(exist_ok=True)
        tag = sys.implementation.cache_tag
        other_level = 2 if sys.flags.optimize != 2 else 1
        (cache / f"a.{tag}.opt-{other_level}.pyc").write_bytes(b"junk")
        self.assertIsNone(prereg.shadow_problem(self.repo))                     # not this interpreter's level: ignored
        (cache / f"a.{tag}.opt-{sys.flags.optimize or 1}.pyc").write_bytes(b"junk")
        if sys.flags.optimize:
            self.assertIn("not a valid bytecode", prereg.shadow_problem(self.repo))
        else:
            self.assertIsNone(prereg.shadow_problem(self.repo))

    def test_foreign_folders_are_not_walked(self):
        """A .venv, venv, out/ or .superpowers/ at the root, or a dot-folder in a package, may hold any pycs."""
        for rel in (".venv/lib/site-packages/pkg", "venv/lib/pkg", "out/work", ".superpowers/x",
                    "second_scribe/.cache", "second_scribe/venv/lib"):
            cache = self.repo / rel / "__pycache__"
            cache.mkdir(parents=True)
            (self.repo / rel / "m.py").write_text("x = 1\n")
            (cache / f"m.{sys.implementation.cache_tag}.pyc").write_bytes(b"junk")   # would be refused if looked at
            (cache / "m.cpython-27.pyc").write_bytes(b"junk")
        self.assertIsNone(prereg.shadow_problem(self.repo))

    def test_a_mismatching_current_tag_pyc_still_fails_beside_foreign_ones(self):
        import marshal
        (self.repo / ".venv" / "__pycache__").mkdir(parents=True)
        (self.repo / ".venv" / "__pycache__" / f"m.{sys.implementation.cache_tag}.pyc").write_bytes(b"junk")
        pyc = self.compiled()
        self.assertIsNone(prereg.shadow_problem(self.repo))
        pyc.write_bytes(pyc.read_bytes()[:16] + marshal.dumps(compile("x = 'evil'\n", "e", "exec")))
        self.assertIn("does not match its source", prereg.shadow_problem(self.repo))

    def test_a_plain_pyc_is_compared_to_a_compile_at_optimize_zero(self):
        """Source with an assert: a plain pyc keeps it, so the comparison must not strip asserts."""
        (self.repo / "second_scribe" / "a.py").write_text("assert True, 'kept'\nx = 1\n")
        pyc = self.compiled()                              # py_compile at the interpreter's own level
        if sys.flags.optimize == 0:
            self.assertIsNone(prereg.shadow_problem(self.repo))

    def test_a_package_at_the_root_named_like_a_stdlib_module_is_refused_by_any_init_suffix(self):
        for init in ("__init__.py", "__init__.pyc", "__init__.so", "__init__.pyd", "__init__.cpython-314-darwin.so"):
            with self.subTest(init=init):
                pkg = self.repo / "json"
                pkg.mkdir()
                (pkg / init).write_bytes(b"")
                msg = prereg.shadow_problem(self.repo)
                self.assertIn("json", msg)
                self.assertIn("shadows", msg)
                shutil.rmtree(pkg)
        (self.repo / "json").mkdir()                        # a plain folder with no __init__ (a namespace dir) is fine
        (self.repo / "json" / "data.txt").write_text("x")
        self.assertIsNone(prereg.shadow_problem(self.repo))

    def test_a_stale_pyc_that_python_would_recompile_is_not_refused(self):
        pyc = self.compiled()
        (self.repo / "second_scribe" / "a.py").write_text("y = 2  # edited, so the recorded size no longer matches\n")
        self.assertIsNone(prereg.shadow_problem(self.repo))

    def test_a_pyc_at_the_kit_root_cache_is_checked_too(self):
        pyc = self.compiled("runner.py")
        self.assertEqual(pyc.parent, self.repo / "__pycache__")
        self.assertIsNone(prereg.shadow_problem(self.repo))
        import marshal
        pyc.write_bytes(pyc.read_bytes()[:16] + marshal.dumps(compile("x = 1\n", "e", "exec")))
        self.assertIn("runner", prereg.shadow_problem(self.repo))

    def test_the_kit_itself_shadows_nothing(self):
        self.assertIsNone(prereg.shadow_problem(KIT_ROOT))

    def test_a_non_stdlib_name_is_fine(self):
        self.assertIsNone(prereg.shadow_problem(self.repo))          # runner.py, extras/, notes/


class TestFrozenModels(Base):
    """I2: the model ids and inference params frozen per role are compared with every call after the freeze."""

    def freeze_and_call(self, stage, model_id, params, **build):
        self.freeze(**build)
        call_request(self.log, stage, model_id, params)
        return self.check()

    def test_the_frozen_model_and_params_pass(self):
        self.assertEqual(self.freeze_and_call("recon:full", "recon-model.gguf", RECON_PARAMS), (True, "ok"))

    def test_the_full_id_is_compared_not_only_the_basename(self):
        info = {**MODEL_INFO, "recon": {**MODEL_INFO["recon"], "model_id": "/models/a/recon-model.gguf"}}
        self.assertEqual(self.freeze_and_call("recon:full", "/models/a/recon-model.gguf", RECON_PARAMS, model_info=info),
                         (True, "ok"))
        call_request(self.log, "recon:full", "/models/b/recon-model.gguf", RECON_PARAMS)     # same basename, other file
        ok, msg = self.check()
        self.assertFalse(ok)
        self.assertIn("differ from the frozen", msg)

    def test_the_frozen_file_holds_the_hash_of_the_full_id_not_the_path(self):
        info = {**MODEL_INFO, "recon": {**MODEL_INFO["recon"], "model_id": "/Users/someone/models/recon-model.gguf"}}
        text = json.dumps(self.build(model_info=info)["model_info"])
        self.assertNotIn("someone", text)
        self.assertIn(hashlib.sha256(b"/Users/someone/models/recon-model.gguf").hexdigest(), text)

    def test_another_model_id_fails(self):
        ok, msg = self.freeze_and_call("recon:full", "other-model.gguf", RECON_PARAMS)
        self.assertFalse(ok)
        self.assertIn("differ from the frozen", msg)

    def test_other_params_fail(self):
        ok, msg = self.freeze_and_call("recon:full", "recon-model.gguf", {"chat_template_kwargs": {"enable_thinking": True}})
        self.assertFalse(ok)
        self.assertIn("params", msg)

    def test_the_judge_stage_is_held_to_the_judge_model(self):
        self.assertEqual(self.freeze_and_call("judge:modality", "judge-model.gguf", {}), (True, "ok"))

    def test_the_recon_model_on_a_judge_stage_fails(self):
        self.freeze()
        call_request(self.log, "judge:modality", "recon-model.gguf", RECON_PARAMS)
        ok, msg = self.check()
        self.assertFalse(ok)
        self.assertIn("judge:modality", msg)

    def test_q_stages_use_the_recon_model(self):
        self.freeze()
        call_request(self.log, "q:mapping_talk", "judge-model.gguf", {})
        self.assertFalse(self.check()[0])

    def test_a_stage_the_table_does_not_know_may_use_any_frozen_model_but_no_other(self):
        self.freeze()
        call_request(self.log, "audit:canary", "judge-model.gguf", {})
        self.assertEqual(self.check(), (True, "ok"))
        call_request(self.log, "audit:canary", "stranger.gguf", {})
        self.assertFalse(self.check()[0])

    def test_a_call_without_a_model_id_is_allowed_for_synthetic_and_refused_for_real(self):
        self.freeze()
        call_request(self.log, "recon:full")
        self.assertEqual(self.check(), (True, "ok"))
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        log_path = tmp / "run.jsonl"
        log = AuditLog(log_path, "r")
        real = replace(self.cfg, corpus={**self.cfg.corpus, "kind": "real"}, prereg_required=True)
        prereg.freeze(self.build(real, calibration=TestContent.CAL), tmp / "p.json", log)
        call_request(log, "recon:full")
        ok, msg = prereg.check(tmp / "p.json", log_path, self.prompts)
        self.assertFalse(ok)
        self.assertIn("no model id", msg)

    def test_secret_params_are_redacted_in_the_frozen_file(self):
        info = {**MODEL_INFO, "recon": {**MODEL_INFO["recon"], "params": {"api_key": "sk-123", "top_p": 0.9}}}
        p = self.build(model_info=info)
        self.assertEqual(p["model_info"]["recon"]["params"], {"api_key": "[REDACTED]", "top_p": 0.9})
        self.assertNotIn("sk-123", json.dumps(p))

    def test_weights_must_be_a_sha256_or_a_short_revision_never_a_path(self):
        for bad in ("/Users/x/models/w.gguf", "a" * 65, "x" * 41, "has space", "A" * 64 + "!"):
            with self.subTest(weights=bad):
                with self.assertRaises(ValueError):
                    self.build(model_info={**MODEL_INFO, "judge": {"model_id": "j", "weights": bad}})
        for good in ("a" * 64, "rev-1234", "main", "v1.2_3", "pending"):
            self.build(model_info={**MODEL_INFO, "judge": {"model_id": "j", "weights": good}})


class LetterClient:
    """A judge server that answers the letter prompt with logprobs; ``cfg`` names the model like ``HTTPClient`` does."""

    def __init__(self, model_id="judge-model.gguf", params=None):
        self.cfg = types.SimpleNamespace(model_id=model_id, params=params or {}, api_key_env=None)

    def complete(self, messages, *, temperature=0.0, seed=None, max_tokens=None, params=None):
        from second_scribe.llm import ModelResponse
        return ModelResponse("B", "length", {}, 1, "0" * 64, [["B", -0.1], ["A", -2.5]])


class TestLetterModeIsFrozen(Base):
    """A judge call in letter mode carries per-call params (logprobs, top_logprobs). The freeze records them, and the
    check holds a letter-mode call to base params + frozen letter params, every other call to the base params."""

    ITEM = {"element_id": "n1", "element": "a short element label", "quote": "I wanted to leave the house on time",
            "speaker": "participant", "reply": None}

    def letter_info(self, role="judge"):
        from second_scribe import judge
        return {**MODEL_INFO, role: {**MODEL_INFO[role], "letter_params": dict(judge.LETTER_PARAMS)}}

    def letter_call(self, client=None, item_id="x"):
        from second_scribe.judge import judge_modality_logprob
        labels, _d, not_judged = judge_modality_logprob(self.log, client or LetterClient(), [dict(self.ITEM)],
                                                        self.prompts, item_id=item_id)
        self.assertEqual(not_judged, set())              # the call was made and its logprobs were read
        return labels

    def test_the_letter_params_are_frozen_with_the_judge_model(self):
        from second_scribe import judge
        p = self.build(model_info=self.letter_info())
        self.assertEqual(p["model_info"]["judge"]["letter_params"], judge.LETTER_PARAMS)
        self.assertNotIn("letter_params", p["model_info"]["recon"])
        self.assertNotIn("letter_params", self.build()["model_info"]["judge"])       # off unless asked for

    def test_a_frozen_run_in_letter_mode_passes_the_check(self):
        self.freeze(model_info=self.letter_info())
        self.letter_call()
        self.assertEqual(self.check(), (True, "ok"))

    def test_generative_and_letter_calls_of_the_same_frozen_run_both_pass(self):
        self.freeze(model_info=self.letter_info())
        call_request(self.log, "judge:modality", "judge-model.gguf", {})
        self.letter_call()
        self.assertEqual(self.check(), (True, "ok"))

    def test_the_judge_base_params_still_count_in_letter_mode(self):
        info = self.letter_info()
        info["judge"]["params"] = {"temperature_cap": 1}
        self.freeze(model_info=info)
        self.letter_call(LetterClient(params={"temperature_cap": 1}))
        self.assertEqual(self.check(), (True, "ok"))
        self.letter_call(LetterClient(params={"temperature_cap": 2}), item_id="y")
        ok, msg = self.check()
        self.assertFalse(ok)
        self.assertIn("differ from the frozen", msg)

    def test_a_letter_param_changed_after_the_freeze_is_refused(self):
        from second_scribe import judge
        self.freeze(model_info=self.letter_info())
        with unittest.mock.patch.object(judge, "LETTER_PARAMS", {"logprobs": True, "top_logprobs": 5}):
            self.letter_call()
        ok, msg = self.check()
        self.assertFalse(ok)
        self.assertIn("differ from the frozen", msg)

    def test_a_letter_call_without_letter_mode_in_the_freeze_is_refused(self):
        self.freeze()                                                  # letter mode not frozen
        self.letter_call()
        ok, msg = self.check()
        self.assertFalse(ok)
        self.assertIn("judge:modality_letter", msg)

    def test_letter_params_on_a_call_that_is_not_in_letter_mode_are_refused(self):
        from second_scribe import judge
        self.freeze(model_info=self.letter_info())
        call_request(self.log, "judge:modality", "judge-model.gguf", dict(judge.LETTER_PARAMS))
        self.assertFalse(self.check()[0])

    def test_the_second_judge_may_freeze_letter_mode_on_its_own(self):
        from second_scribe import judge
        from second_scribe.judge import judge_modality_logprob
        cfg = replace(self.cfg, models={**self.cfg.models, "judge2": "judge2"})
        info = {**MODEL_INFO, "judge2": {"model_id": "other.gguf", "weights": "rev-1", "params": {},
                                         "letter_params": dict(judge.LETTER_PARAMS)}}
        prereg.freeze(self.build(cfg, model_info=info), self.path, self.log)
        judge_modality_logprob(self.log, LetterClient("other.gguf"), [dict(self.ITEM)], self.prompts, item_id="x",
                               stage="judge2:modality_letter")
        self.assertEqual(self.check(), (True, "ok"))
        judge_modality_logprob(self.log, LetterClient("judge-model.gguf"), [dict(self.ITEM)], self.prompts, item_id="y",
                               stage="judge2:modality_letter")                 # the first judge's model on the second's stage
        self.assertFalse(self.check()[0])

    def test_letter_params_cannot_be_frozen_for_the_reconstructor(self):
        from second_scribe import judge
        info = {**MODEL_INFO, "recon": {**MODEL_INFO["recon"], "letter_params": dict(judge.LETTER_PARAMS)}}
        with self.assertRaises(ValueError):
            self.build(model_info=info)

    def test_the_letter_stage_name_is_the_judges_default(self):
        from second_scribe.judge import judge_modality_logprob
        self.assertEqual(inspect.signature(judge_modality_logprob).parameters["stage"].default,
                         f"judge:{prereg.LETTER_STAGE}")

    def test_the_cli_freezes_letter_mode_on_request(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        d = json.loads(Path(DEMO_CONFIG).read_text())
        d.update(out_dir=str(tmp / "out"), base_dir=str(Path(DEMO_CONFIG).parent), prereg_required=True)
        (tmp / "c.json").write_text(json.dumps(d))
        env = {"SS_RECON_MODEL": "recon.gguf", "SS_RECON_WEIGHTS": "b" * 64, "SS_JUDGE_MODEL": "judge.gguf",
               "SS_JUDGE_WEIGHTS": "rev-7"}
        with unittest.mock.patch.dict(os.environ, env), \
             unittest.mock.patch.object(prereg, "git_head", return_value=COMMIT), \
             unittest.mock.patch.object(prereg, "check_code", return_value=None), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli_main(["prereg", "freeze", "--config", str(tmp / "c.json"), "--letter-mode"]), 0)
        from second_scribe import judge
        frozen = json.loads((tmp / "out" / "prereg.json").read_text())["model_info"]
        self.assertEqual(frozen["judge"]["letter_params"], judge.LETTER_PARAMS)
        self.assertNotIn("letter_params", frozen["recon"])


class TestFreezeAfterTheRun(Base):
    """C1: a freeze after the run is refused, and work that was not produced after the freeze fails the check."""

    def real_cfg(self, out):
        d = json.loads(Path(DEMO_CONFIG).read_text())
        d.update(out_dir=str(out), base_dir=str(Path(DEMO_CONFIG).parent))
        return d

    def test_the_reviewers_probe_run_synthetic_delete_the_log_flip_to_real_freeze(self):
        out = self.tmp / "probe"
        d = self.real_cfg(out)
        cfg = RunConfig(**d)
        log_path = out / "logs" / "run.jsonl"
        log_path.parent.mkdir(parents=True)
        log = AuditLog(log_path, cfg.run_id)
        run(cfg, ["mapping_talk_confirm"], build_clients("oracle", cfg), log=log)          # 1. run under synthetic
        self.assertTrue(list((out / "work" / "mapping_talk_confirm").glob("*.json")))
        log_path.unlink()                                                                  # 2. delete the log
        (out / "logs" / "run.manifest.json").unlink(missing_ok=True)
        d["corpus"]["kind"] = "real"                                                       # 3. flip to real
        d["prereg_required"] = True
        real = RunConfig(**d)
        log = AuditLog(log_path, real.run_id)
        p = prereg.build_prereg(real, self.kit / 'prompts', code_commit=COMMIT, model_info=MODEL_INFO,
                                decision_rules=prereg.DECISION_RULES, calibration=TestContent.CAL)
        with self.assertRaises(ValueError) as cm:                                          # 4. freeze: refused
            prereg.freeze(p, prereg.prereg_path(real), log)
        self.assertIn("already under work/", str(cm.exception))
        self.assertFalse(prereg.prereg_path(real).exists())

    def test_synthetic_may_waive_the_work_check_a_real_corpus_never(self):
        (self.tmp / "work" / "recon").mkdir(parents=True)
        (self.tmp / "work" / "recon" / "x.json").write_text("{}")
        with self.assertRaises(ValueError):
            self.freeze()
        prereg.freeze(self.build(), self.path, self.log, allow_existing_work=True)
        real = replace(self.cfg, corpus={**self.cfg.corpus, "kind": "real"}, prereg_required=True)
        tmp2 = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp2, True)
        (tmp2 / "aggregates").mkdir()
        (tmp2 / "aggregates" / "aggregates.json").write_text("{}")
        with self.assertRaises(ValueError):
            prereg.freeze(self.build(real, calibration=TestContent.CAL), tmp2 / "p.json", AuditLog(tmp2 / "l.jsonl", "r"),
                          allow_existing_work=True)

    def test_an_empty_work_folder_does_not_block(self):
        (self.tmp / "work").mkdir()
        self.freeze()

    def frozen_cfg(self):
        out = self.tmp / "run"
        cfg = replace(RunConfig(**{**json.loads(Path(DEMO_CONFIG).read_text()), "out_dir": str(out),
                                   "base_dir": str(Path(DEMO_CONFIG).parent)}), prereg_required=True)
        log_path = out / "logs" / "run.jsonl"
        log_path.parent.mkdir(parents=True)
        log = AuditLog(log_path, cfg.run_id)
        prereg.freeze(prereg.build_prereg(cfg, self.kit / 'prompts', code_commit=COMMIT, model_info=MODEL_INFO,
                                          decision_rules=prereg.DECISION_RULES, code_root=self.kit),
                      prereg.prereg_path(cfg), log)
        return cfg, log, log_path

    def check(self, cfg, log_path):
        with unittest.mock.patch.object(prereg, "check_code", return_value=None):
            return prereg.check(prereg.prereg_path(cfg), log_path, self.kit / 'prompts', cfg=cfg, kit_root=self.kit,
                                work_dir=Path(cfg.out_dir) / "work")

    def test_items_written_after_the_freeze_carry_their_request_ids_and_pass(self):
        cfg, log, log_path = self.frozen_cfg()
        with unittest.mock.patch.object(prereg, "check_code", return_value=None):
            run(cfg, ["mapping_talk_confirm", "in_transcript"], build_clients("oracle", cfg), log=log)
        items = list((Path(cfg.out_dir) / "work").glob("*/*.json"))
        self.assertTrue(items)
        stamped = [json.loads(f.read_text())["request_ids"] for f in items]
        self.assertTrue(any(stamped), "some item must name the calls that produced it")
        post = {r["payload"]["request_id"] for r in records(log_path) if r["record_type"] == "call_request"}
        for ids in stamped:
            self.assertLessEqual(set(ids), post)
        self.assertEqual(self.check(cfg, log_path), (True, "ok"))

    def plant(self, cfg, stage, name, item):
        d = Path(cfg.out_dir) / "work" / stage
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.json").write_text(json.dumps(item))

    def test_work_planted_after_the_freeze_fails_the_check_and_the_run(self):
        cfg, log, log_path = self.frozen_cfg()
        for ids in (None, ["recon:full:SYN-OW01:1:deadbeef"]):
            item = {"status": "ok", "map": None}
            if ids is not None:
                item["request_ids"] = ids
            with self.subTest(ids=ids):
                self.plant(cfg, "recon", "x", item)
                ok, msg = self.check(cfg, log_path)
                self.assertFalse(ok)
                self.assertIn("recon/x", msg)
                with unittest.mock.patch.object(prereg, "check_code", return_value=None), \
                        self.assertRaises(RuntimeError) as cm:
                    run(cfg, ["metrics"], {}, log=log)
                self.assertIn("not produced after the freeze", str(cm.exception))

    def test_an_item_planted_with_empty_request_ids_fails_for_every_model_stage(self):
        cfg, log, log_path = self.frozen_cfg()
        plain = {"recon": {"map": None}, "prior_only": {"map": None}, "canonicalize": {"labels": ["a"], "concepts": ["a"]},
                 "mapping_talk_confirm": {"speaker_format": "pt_iv", "stats": {"rule_flagged": 3}, "mapping_talk_turns": []},
                 "in_transcript": {"in_transcript": {"n1": True}},
                 "judge_modality": {"labels": {"n1": "actual"}, "n_items": 1, "stats": {"batches": 1}},
                 "judge2_modality": {"labels": {"n1": "actual"}, "n_items": 1, "stats": {"batches": 1}},
                 "judge_shuffled": {"labels": {"n1": "actual"}, "n_items": 1, "stats": {"batches": 1}},
                 "align": {"left": ["a"], "right": ["b"], "pairs": []}}
        for stage, body in plain.items():
            for status in ("ok", "fallback"):
                with self.subTest(stage=stage, status=status):
                    self.plant(cfg, stage, "x", {"status": status, "request_ids": [], **body})
                    ok, msg = self.check(cfg, log_path)
                    self.assertFalse(ok)
                    self.assertIn(f"{stage}/x", msg)
                    self.assertIn("names no model call", msg)
            (Path(cfg.out_dir) / "work" / stage / "x.json").unlink()
        self.assertEqual(self.check(cfg, log_path), (True, "ok"))

    def test_items_that_make_no_call_by_construction_may_have_empty_request_ids(self):
        cfg, log, log_path = self.frozen_cfg()
        exempt = [("recon", {"status": "too_long_excluded"}), ("recon", {"status": "no_speakers_excluded"}),
                  ("prior_only", {"status": "too_long_excluded"}),
                  ("mapping_talk_confirm", {"status": "ok", "speaker_format": "none", "stats": None}),
                  ("mapping_talk_confirm", {"status": "ok", "speaker_format": "pt_iv", "stats": {"rule_flagged": 0}}),
                  ("in_transcript", {"status": "ok", "in_transcript": {}}),
                  ("judge_modality", {"status": "ok", "labels": {"n1": "unsupported"}, "n_items": 1,
                                      "stats": {"batches": 0, "cache_hits": 0}}),
                  ("judge2_modality", {"status": "ok", "labels": {}, "n_items": 0, "stats": {"batches": 0}}),
                  ("judge_shuffled", {"status": "ok", "labels": None, "n_items": 0}),
                  ("align", {"status": "ok", "left": [], "right": ["b"], "pairs": []}),
                  ("align", {"status": "ok", "left": ["a"], "right": [], "pairs": []}),
                  ("audit_canary", {"status": "ok"})]            # a stage this check does not know
        for k, (stage, item) in enumerate(exempt):
            with self.subTest(stage=stage, item=item):
                self.plant(cfg, stage, f"x{k}", {**item, "request_ids": []})
        self.assertEqual(self.check(cfg, log_path), (True, "ok"))

    def test_a_full_honest_run_passes_the_check(self):
        """Every stage of the demo, including the items that make no call (no speaker labels, cache hits)."""
        from second_scribe.pipeline import STAGE_ORDER
        cfg, log, log_path = self.frozen_cfg()
        with unittest.mock.patch.object(prereg, "check_code", return_value=None):
            run(cfg, list(STAGE_ORDER), build_clients("oracle", cfg), log=log)
            ok, msg = self.check(cfg, log_path)
            self.assertEqual((ok, msg), (True, "ok"), msg)
            run(cfg, list(STAGE_ORDER), build_clients("oracle", cfg), log=log)       # and a resumed run
        self.assertEqual(self.check(cfg, log_path), (True, "ok"))

    def test_request_ids_from_before_the_freeze_do_not_count(self):
        cfg, log, log_path = self.frozen_cfg()
        old = self.tmp / "oldlog.jsonl"
        oldlog = AuditLog(old, cfg.run_id)
        rid = call_request(oldlog, "recon:full")
        planted = Path(cfg.out_dir) / "work" / "recon"
        planted.mkdir(parents=True)
        (planted / "x.json").write_text(json.dumps({"status": "ok", "map": None, "request_ids": [rid]}))
        self.assertFalse(self.check(cfg, log_path)[0])

    def test_runs_without_the_requirement_write_plain_items(self):
        cfg = RunConfig(**{**json.loads(Path(DEMO_CONFIG).read_text()), "out_dir": str(self.tmp / "plain"),
                           "base_dir": str(Path(DEMO_CONFIG).parent)})
        (self.tmp / "plain" / "logs").mkdir(parents=True)
        run(cfg, ["mapping_talk_confirm"], build_clients("oracle", cfg),
            log=AuditLog(self.tmp / "plain" / "logs" / "run.jsonl", cfg.run_id))
        for f in (self.tmp / "plain" / "work").glob("*/*.json"):
            self.assertNotIn("request_ids", json.loads(f.read_text()))


class TestRealFreezeRules(Base):
    def real(self, **over):
        return replace(self.cfg, corpus={**self.cfg.corpus, "kind": "real"}, prereg_required=True, **over)

    def test_a_primary_setting_that_is_not_the_calibrated_one_is_refused(self):
        settings = list(self.cfg.settings)
        settings[0], settings[1] = settings[1], settings[0]
        p = self.build(self.real(settings=settings), calibration=TestContent.CAL)
        with self.assertRaises(ValueError) as cm:
            prereg.freeze(p, self.path, self.log)
        self.assertIn("PRIMARY_SETTING", str(cm.exception))

    def test_settings_that_miss_the_grid_are_refused(self):
        p = self.build(self.real(settings=list(self.cfg.settings)[:6]), calibration=TestContent.CAL)
        with self.assertRaises(ValueError) as cm:
            prereg.freeze(p, self.path, self.log)
        self.assertIn("grid", str(cm.exception))

    def test_synthetic_may_use_other_settings(self):
        prereg.freeze(self.build(replace(self.cfg, settings=list(self.cfg.settings)[:6])), self.path, self.log)

    def test_the_aggregate_string_guard_runs_on_real_calibration(self):
        for bad in ({"matcher": {"note": "x" * 41}, "in_transcript": {"n": 1}},
                    {"matcher": {"n": 1}, "in_transcript": {"k" * 41: 1}}):
            with self.subTest(bad=list(bad)):
                with self.assertRaises(ValueError) as cm:
                    prereg.freeze(self.build(self.real(), calibration=bad), self.path, self.log)
                self.assertIn("aggregate-only", str(cm.exception))
        self.assertFalse(self.path.exists())
        digest = "f" * 64                    # a whole sha256 is allowed, as in the aggregates
        prereg.freeze(self.build(self.real(), calibration={"matcher": {"sha": digest}, "in_transcript": {"n": 1}}),
                      self.path, self.log)


class TestSyntheticLabelIsChecked(unittest.TestCase):
    """I3: ``kind`` is a label the user types; a "synthetic" corpus must really be one."""

    def cfg_dict(self, out, **corpus):
        d = json.loads(Path(DEMO_CONFIG).read_text())
        d.update(out_dir=str(out), base_dir=str(Path(DEMO_CONFIG).parent))
        d["corpus"].update(corpus)
        return d

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_the_demo_corpus_is_synthetic(self):
        RunConfig(**self.cfg_dict(self.tmp))

    def test_real_looking_paths_with_a_synthetic_label_are_refused(self):
        data = self.tmp / "data"
        (data / "maps").mkdir(parents=True)
        for corpus in ({"maps_glob": str(data / "maps" / "*.vsdx")},
                       {"transcripts_glob": str(data / "transcripts" / "*.docx")},
                       {"synthetic_dir": str(data)},
                       {"pairs": {"X01": [str(data / "maps" / "a.vsdx"), str(data / "t" / "a.docx")]}}):
            with self.subTest(corpus=list(corpus)):
                with self.assertRaises(ValueError) as cm:
                    RunConfig(**self.cfg_dict(self.tmp, **corpus))
                self.assertIn("synthetic", str(cm.exception))

    def test_a_folder_built_by_build_corpus_is_accepted(self):
        from second_scribe.synth.build import build_corpus
        corpus = self.tmp / "corpus"
        build_corpus(corpus)
        self.assertTrue((corpus / "meta.json").exists())
        RunConfig(**self.cfg_dict(self.tmp, maps_glob=str(corpus / "maps" / "*.vsdx"),
                                  transcripts_glob=str(corpus / "transcripts" / "*.docx"), synthetic_dir=str(corpus)))

    def test_a_meta_json_without_a_domain_does_not_count(self):
        corpus = self.tmp / "corpus"
        (corpus / "maps").mkdir(parents=True)
        for meta in ({"phases": []}, {"domain": ""}, {"domain": 3}):
            (corpus / "meta.json").write_text(json.dumps(meta))
            with self.subTest(meta=meta), self.assertRaises(ValueError):
                RunConfig(**self.cfg_dict(self.tmp, maps_glob=str(corpus / "maps" / "*.vsdx"),
                                          transcripts_glob=str(corpus / "maps" / "*.docx"),
                                          synthetic_dir=str(corpus)))

    def test_a_relative_path_resolves_against_the_config_folder(self):
        corpus = self.tmp / "corpus"
        (corpus / "maps").mkdir(parents=True)
        (corpus / "meta.json").write_text(json.dumps({"domain": "food trucks"}))
        d = self.cfg_dict(self.tmp, maps_glob="corpus/maps/*.vsdx", transcripts_glob="corpus/maps/*.docx",
                          synthetic_dir="corpus")
        d["base_dir"] = str(self.tmp)
        RunConfig(**d)

    def test_a_real_corpus_is_not_subject_to_the_check(self):
        d = self.cfg_dict(self.tmp, kind="real", maps_glob=str(self.tmp / "data" / "*.vsdx"))
        d["prereg_required"] = True
        RunConfig(**d)


class TestAnchor(Base):
    def test_write_and_check(self):
        digest = self.freeze()
        anchor = prereg.write_anchor(digest, "demo", self.tmp / "private")
        self.assertEqual(anchor.name, "prereg.sha256")
        self.assertEqual(anchor.read_text(), f"{digest} demo\n")
        self.assertEqual(self.check(anchor=anchor), (True, "ok"))

    def test_another_freeze_does_not_match_the_anchor(self):
        digest = self.freeze()
        anchor = prereg.write_anchor("0" * 64, "demo", self.tmp / "private")
        ok, msg = self.check(anchor=anchor)
        self.assertFalse(ok)
        self.assertIn("anchor", msg)
        anchor = prereg.write_anchor(digest, "other-run", self.tmp / "private")
        self.assertFalse(self.check(anchor=anchor)[0])

    def test_a_missing_or_malformed_anchor_fails(self):
        self.freeze()
        self.assertFalse(self.check(anchor=self.tmp / "none.sha256")[0])
        bad = self.tmp / "bad.sha256"
        bad.write_text("justonetoken\n")
        self.assertFalse(self.check(anchor=bad)[0])


def with_model_ids(clients, judge_id="judge.gguf"):
    """Copies of the clients that name their model like an HTTP client does (a recon id and the given judge id)."""
    out = {}
    for role, c in clients.items():
        c = copy.copy(c)
        c.cfg = types.SimpleNamespace(model_id={"recon": "recon.gguf", "judge": judge_id, "judge2": "judge2.gguf"}[role],
                                      params={}, api_key_env=None)
        out[role] = c
    return out


class FrozenRunBase(Base):
    """A frozen demo-config run in its own folder, with clients that name their models."""

    def frozen(self, name="run", judge_id="judge.gguf"):
        out = self.tmp / name
        cfg = replace(RunConfig(**{**json.loads(Path(DEMO_CONFIG).read_text()), "out_dir": str(out),
                                   "base_dir": str(Path(DEMO_CONFIG).parent)}), prereg_required=True)
        log_path = out / "logs" / "run.jsonl"
        log_path.parent.mkdir(parents=True)
        log = AuditLog(log_path, cfg.run_id)
        info = {"recon": {"model_id": "recon.gguf", "weights": "a" * 64, "params": {}},
                "judge": {"model_id": judge_id, "weights": "b" * 64, "params": {}}}
        prereg.freeze(prereg.build_prereg(cfg, self.kit / 'prompts', code_commit=COMMIT, model_info=info,
                                          decision_rules=prereg.DECISION_RULES, code_root=self.kit),
                      prereg.prereg_path(cfg), log)
        patch = unittest.mock.patch.object(prereg, "check_code", return_value=None)
        patch.start()
        self.addCleanup(patch.stop)
        return cfg, log, log_path

    def run_stages(self, cfg, log, stages, judge_id="judge.gguf"):
        return run(cfg, list(stages), with_model_ids(build_clients("oracle", cfg), judge_id), log=log)

    def check(self, cfg, log_path):
        return prereg.check(prereg.prereg_path(cfg), log_path, self.kit / 'prompts', cfg=cfg, kit_root=self.kit,
                            work_dir=Path(cfg.out_dir) / "work")

    def items(self, cfg, stage):
        return sorted((Path(cfg.out_dir) / "work" / stage).glob("*.json"))

    def edit(self, f, **fields):
        d = json.loads(f.read_text())
        d.update(fields)
        f.write_text(json.dumps(d))
        return d


class TestJudgeCacheProbes(FrozenRunBase):
    """Round 3: a judge cache copied into work/ after the freeze must not pass for this log's judge calls."""

    def test_the_reviewers_probe_another_judge_model(self):
        b_cfg, b_log, _ = self.frozen("b", judge_id="judge-b.gguf")
        self.run_stages(b_cfg, b_log, STAGE_ORDER, "judge-b.gguf")
        b_cache = Path(b_cfg.out_dir) / "work" / "judge_cache.json"
        self.assertTrue(b_cache.exists())
        c_cfg, c_log, c_log_path = self.frozen("c", judge_id="judge-c.gguf")          # C freezes with another judge
        (Path(c_cfg.out_dir) / "work").mkdir()
        shutil.copyfile(b_cache, Path(c_cfg.out_dir) / "work" / "judge_cache.json")   # B's cache copied into C
        ok, msg = self.check(c_cfg, c_log_path)
        self.assertFalse(ok)
        self.assertIn("judge cache", msg)
        with self.assertRaises(RuntimeError) as cm:                                    # then C runs
            self.run_stages(c_cfg, c_log, STAGE_ORDER, "judge-c.gguf")
        self.assertIn("judge cache", str(cm.exception))

    def test_the_same_judge_model_cache_of_another_run_is_not_this_logs_calls(self):
        b_cfg, b_log, _ = self.frozen("b")
        self.run_stages(b_cfg, b_log, STAGE_ORDER)
        c_cfg, c_log, c_log_path = self.frozen("c")                                    # the same models
        (Path(c_cfg.out_dir) / "work").mkdir()
        shutil.copyfile(Path(b_cfg.out_dir) / "work" / "judge_cache.json", Path(c_cfg.out_dir) / "work" / "judge_cache.json")
        ok, msg = self.check(c_cfg, c_log_path)
        self.assertFalse(ok)
        self.assertIn("not produced after the freeze", msg)
        with self.assertRaises(RuntimeError):
            self.run_stages(c_cfg, c_log, STAGE_ORDER)

    def test_a_cache_with_a_legacy_entry_or_a_foreign_id_fails_and_an_honest_one_passes(self):
        cfg, log, log_path = self.frozen()
        self.run_stages(cfg, log, STAGE_ORDER)
        self.assertEqual(self.check(cfg, log_path), (True, "ok"))
        cache = Path(cfg.out_dir) / "work" / "judge_cache.json"
        data = json.loads(cache.read_text())
        first = next(iter(data))
        for bad in ("wished", {"modality": "wished"}, {"modality": "wished", "request_id": "judge:modality:x:1:deadbeef"}):
            with self.subTest(entry=bad):
                cache.write_text(json.dumps({**data, first: bad}))
                self.assertFalse(self.check(cfg, log_path)[0])
        cache.write_text(json.dumps(data))
        self.assertEqual(self.check(cfg, log_path), (True, "ok"))

    def test_an_item_served_from_the_cache_names_the_calls_behind_its_labels(self):
        cfg, log, log_path = self.frozen()
        self.run_stages(cfg, log, STAGE_ORDER)
        for f in self.items(cfg, "judge_modality"):        # redo them: now every label comes from the cache
            f.unlink()
        self.run_stages(cfg, log, ["judge_modality"])
        redone = self.items(cfg, "judge_modality")
        self.assertTrue(redone)
        served = [json.loads(f.read_text()) for f in redone]
        self.assertTrue(any(d["stats"]["cache_hits"] > 0 and d["stats"]["batches"] == 0 for d in served))
        for d in served:
            if d["stats"]["cache_hits"]:
                self.assertTrue(d["request_ids"])
                self.assertEqual(sorted(d["stats"]["cache_request_ids"]), sorted(d["request_ids"]))
        self.assertEqual(self.check(cfg, log_path), (True, "ok"))

    def test_a_cache_served_item_that_names_no_call_or_hides_its_hits_fails(self):
        cfg, log, log_path = self.frozen()
        self.run_stages(cfg, log, STAGE_ORDER)
        for f in self.items(cfg, "judge_modality"):
            f.unlink()
        self.run_stages(cfg, log, ["judge_modality"])
        f = next(f for f in self.items(cfg, "judge_modality") if json.loads(f.read_text())["stats"]["cache_hits"])
        original = json.loads(f.read_text())
        self.edit(f, request_ids=[])
        ok, msg = self.check(cfg, log_path)
        self.assertFalse(ok)
        self.assertIn("names no model call", msg)
        self.edit(f, request_ids=[], stats={**original["stats"], "cache_hits": 0})         # claims "decided by rule"
        self.assertIn("names no model call", self.check(cfg, log_path)[1])
        f.write_text(json.dumps(original))
        self.assertEqual(self.check(cfg, log_path), (True, "ok"))

    def test_a_judge_item_claiming_by_rule_while_holding_real_labels_fails(self):
        cfg, log, log_path = self.frozen()
        self.plant(cfg, "judge_modality", "x", {"status": "ok", "labels": {"n1": "wished"}, "n_items": 1,
                                                 "stats": {"batches": 0, "cache_hits": 0}, "request_ids": []})
        self.assertIn("names no model call", self.check(cfg, log_path)[1])

    def plant(self, cfg, stage, name, item):
        d = Path(cfg.out_dir) / "work" / stage
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.json").write_text(json.dumps(item))


class TestStatusesAndBinding(FrozenRunBase):
    def setUp(self):
        super().setUp()
        self.cfg, self.log, self.log_path = self.frozen()
        self.run_stages(self.cfg, self.log, ["mapping_talk_confirm", "in_transcript", "recon"])
        self.assertEqual(self.check(self.cfg, self.log_path), (True, "ok"))

    def test_a_failed_item_must_carry_ids(self):
        d = Path(self.cfg.out_dir) / "work" / "recon"
        (d / "planted.json").write_text(json.dumps({"status": "failed", "request_ids": []}))
        ok, msg = self.check(self.cfg, self.log_path)
        self.assertFalse(ok)
        self.assertIn("recon/planted", msg)
        self.assertIn("names no model call", msg)
        for status in ("timeout_excluded", "fallback", "some_new_status"):
            (d / "planted.json").write_text(json.dumps({"status": status, "request_ids": []}))
            self.assertFalse(self.check(self.cfg, self.log_path)[0], status)

    def test_a_quarantined_item_is_held_to_the_status_it_had(self):
        """``audits.quarantine_session`` re-marks a session's items ``quarantined`` (old status in ``original_status``); the
        next session's check must neither refuse an item that made no call by construction nor excuse one that should have."""
        d = Path(self.cfg.out_dir) / "work" / "mapping_talk_confirm"
        planted = d / "planted.json"
        base = {"status": "quarantined", "quarantined_session": "abc", "request_ids": [],
                "stats": {"rule_flagged": 0}, "speaker_format": "first_token"}
        planted.write_text(json.dumps({**base, "original_status": "ok"}))               # an ok item with no call to make
        self.assertEqual(self.check(self.cfg, self.log_path), (True, "ok"))
        planted.write_text(json.dumps({**base, "original_status": "ok", "stats": {"rule_flagged": 2}}))
        self.assertIn("names no model call", self.check(self.cfg, self.log_path)[1])    # it should have called
        planted.write_text(json.dumps({**base, "original_status": "failed"}))
        self.assertIn("names no model call", self.check(self.cfg, self.log_path)[1])
        planted.write_text(json.dumps(base))                                            # no original status at all
        self.assertIn("names no model call", self.check(self.cfg, self.log_path)[1])

    def test_a_quarantined_item_still_names_only_calls_logged_after_the_freeze(self):
        d = Path(self.cfg.out_dir) / "work" / "mapping_talk_confirm"
        (d / "planted.json").write_text(json.dumps({"status": "quarantined", "original_status": "ok",
                                                    "request_ids": ["not-a-logged-call"]}))
        ok, msg = self.check(self.cfg, self.log_path)
        self.assertFalse(ok)
        self.assertIn("not produced after the freeze", msg)

    def test_the_excluded_statuses_need_no_ids(self):
        d = Path(self.cfg.out_dir) / "work" / "recon"
        for status in ("too_long_excluded", "no_speakers_excluded"):
            (d / "planted.json").write_text(json.dumps({"status": status, "request_ids": []}))
            self.assertEqual(self.check(self.cfg, self.log_path), (True, "ok"), status)

    def test_an_item_cannot_claim_the_calls_of_another_item(self):
        a, b = self.items(self.cfg, "recon")[:2]
        self.edit(b, request_ids=json.loads(a.read_text())["request_ids"])
        ok, msg = self.check(self.cfg, self.log_path)
        self.assertFalse(ok)
        self.assertIn("belongs to another item", msg)

    def test_an_item_cannot_add_the_calls_of_another_item_to_its_own(self):
        a, b = self.items(self.cfg, "recon")[:2]
        self.edit(b, request_ids=json.loads(b.read_text())["request_ids"] + json.loads(a.read_text())["request_ids"][:1])
        self.assertIn("belongs to another item", self.check(self.cfg, self.log_path)[1])

    def test_an_item_cannot_claim_a_call_of_another_stage(self):
        recon = json.loads(self.items(self.cfg, "recon")[0].read_text())["request_ids"]
        item = self.items(self.cfg, "in_transcript")[0]
        self.edit(item, request_ids=recon)
        ok, msg = self.check(self.cfg, self.log_path)
        self.assertFalse(ok)
        self.assertIn("another stage", msg)

    def test_a_judge_item_copied_to_the_other_judge_stage_does_not_claim_its_calls(self):
        self.run_stages(self.cfg, self.log, ["judge_modality"])
        a = next(f for f in self.items(self.cfg, "judge_modality") if json.loads(f.read_text())["stats"]["batches"])
        d = Path(self.cfg.out_dir) / "work" / "judge_shuffled"
        d.mkdir(parents=True, exist_ok=True)
        (d / a.name).write_text(a.read_text())             # same item name, so the same calls look like its own
        ok, msg = self.check(self.cfg, self.log_path)
        self.assertFalse(ok)
        self.assertIn("another stage", msg)

    def test_no_call_is_claimed_as_its_own_by_two_items(self):
        """The backstop: item names that both own one call (a name and its ':' extension) never both claim it."""
        calls = {"r1": ("recon:full", "a:b")}
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        (work / "recon").mkdir()
        for name in ("a", "a:b"):
            (work / "recon" / f"{name}.json").write_text(json.dumps({"status": "ok", "request_ids": ["r1"]}))
        self.assertIn("already claimed", prereg._work_problem(work, calls))

    def test_judge_items_may_name_cached_calls_only_when_they_list_them(self):
        d = Path(self.cfg.out_dir) / "work" / "judge_modality"
        d.mkdir(parents=True)
        recon_id = json.loads(self.items(self.cfg, "recon")[0].read_text())["request_ids"][0]
        self.assertIn("another stage", self.check_with(d, {"request_ids": [recon_id]}))
        # a real judge call: run J, then lend one item's call to another item
        self.run_stages(self.cfg, self.log, ["judge_modality"])
        files = self.items(self.cfg, "judge_modality")
        docs = {f: json.loads(f.read_text()) for f in files}
        a, b = next((x, y) for x in files for y in files if x != y and docs[x]["request_ids"] and not
                    set(docs[x]["request_ids"]) & set(docs[y]["request_ids"]))
        stolen = docs[a]["request_ids"][0]
        original = docs[b]
        self.edit(b, request_ids=original["request_ids"] + [stolen])
        self.assertIn("belongs to another item", self.check(self.cfg, self.log_path)[1])
        self.edit(b, stats={**original["stats"], "cache_request_ids": original["stats"]["cache_request_ids"] + [stolen]})
        # listed as a cache hit: allowed
        self.assertEqual(self.check(self.cfg, self.log_path), (True, "ok"))

    def sibling_probe(self, item_stage, own_call_stage, other_call_stage, listed):
        """An item of ``item_stage`` named X holding its own call and a same-named call of the sibling judge stage."""
        calls = {"r_own": (own_call_stage, "X"), "r_sib": (other_call_stage, "X")}
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        (work / item_stage).mkdir()
        stats = {"cache_request_ids": ["r_sib"] if listed else []}
        (work / item_stage / "X.json").write_text(json.dumps(
            {"status": "ok", "labels": {"n": "actual"}, "n_items": 1, "stats": stats, "request_ids": ["r_own", "r_sib"]}))
        return prereg._work_problem(work, calls)

    def test_a_cache_hit_from_the_sibling_judge_stage_with_the_same_item_name_passes_when_listed(self):
        """The reviewer's honest probe: two nodes with one quote, so the shuffled item takes a label from the modality
        item's call (and the reverse). Listed in stats.cache_request_ids it passes; unlisted it is refused."""
        for item_stage, own, other in (("judge_shuffled", "judge:shuffled", "judge:modality"),
                                       ("judge_modality", "judge:modality", "judge:shuffled")):
            with self.subTest(item=item_stage):
                self.assertIsNone(self.sibling_probe(item_stage, own, other, listed=True))
                self.assertIn("another stage", self.sibling_probe(item_stage, own, other, listed=False))

    def test_a_sibling_stage_call_cannot_be_the_items_own_claim(self):
        calls = {"r_sib": ("judge:modality", "X")}
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        for stage in ("judge_modality", "judge_shuffled"):
            (work / stage).mkdir()
            (work / stage / "X.json").write_text(json.dumps(
                {"status": "ok", "labels": {"n": "actual"}, "n_items": 1, "stats": {"batches": 0, "cache_hits": 0},
                 "request_ids": ["r_sib"]}))
        self.assertIn("another stage", prereg._work_problem(work, calls))     # judge_shuffled/X, unlisted

    def test_a_listed_cache_call_of_the_other_family_is_refused(self):
        calls = {"r_own": ("judge2:modality", "X"), "r_x": ("judge:modality", "X")}
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        (work / "judge2_modality").mkdir()
        (work / "judge2_modality" / "X.json").write_text(json.dumps(
            {"status": "ok", "labels": {}, "n_items": 1, "stats": {"cache_request_ids": ["r_x"]}, "request_ids": ["r_own", "r_x"]}))
        self.assertIn("another stage", prereg._work_problem(work, calls))

    def check_with(self, d, fields):
        (d / "x.json").write_text(json.dumps({"status": "ok", "labels": {"n": "actual"}, "n_items": 1,
                                              "stats": {"batches": 1, "cache_hits": 0}, **fields}))
        try:
            return self.check(self.cfg, self.log_path)[1]
        finally:
            (d / "x.json").unlink()


class TestConfigHashTracksTheCorpus(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = KIT_ROOT / "demo" / "corpus"

    def cfg_for(self, corpus_dir, out):
        d = json.loads(Path(DEMO_CONFIG).read_text())
        d.update(out_dir=str(out), base_dir=str(self.tmp))
        d["corpus"].update(maps_glob=str(corpus_dir / "maps" / "*.vsdx"),
                           transcripts_glob=str(corpus_dir / "transcripts" / "*.docx"), synthetic_dir=str(corpus_dir))
        return RunConfig(**d)

    def test_moving_the_folder_keeps_the_hash_changing_a_file_does_not(self):
        one = self.tmp / "one"
        shutil.copytree(self.src, one)
        h1 = self.cfg_for(one, self.tmp / "o1").sha256()
        two = self.tmp / "elsewhere" / "two"
        shutil.copytree(self.src, two)
        self.assertEqual(self.cfg_for(two, self.tmp / "o2").sha256(), h1)
        f = sorted((two / "transcripts").glob("*.docx"))[0]
        f.write_bytes(f.read_bytes() + b"x")
        self.assertNotEqual(self.cfg_for(two, self.tmp / "o2").sha256(), h1)

    def test_adding_an_input_changes_the_hash_and_removing_a_listed_pid_s_file_fails(self):
        one = self.tmp / "one"
        shutil.copytree(self.src, one)
        h1 = self.cfg_for(one, self.tmp / "o1").sha256()
        extra = one / "maps" / "EXTRA.vsdx"
        shutil.copyfile(sorted((one / "maps").glob("*.vsdx"))[0], extra)
        self.assertNotEqual(self.cfg_for(one, self.tmp / "o1").sha256(), h1)
        extra.unlink()
        self.assertEqual(self.cfg_for(one, self.tmp / "o1").sha256(), h1)
        sorted((one / "maps").glob("*.vsdx"))[0].unlink()
        with self.assertRaises(ValueError) as cm:                  # a pid in roles with no map file
            self.cfg_for(one, self.tmp / "o1").sha256()
        self.assertIn("has no map file", str(cm.exception))

    def test_swapping_the_names_of_two_files_changes_the_hash(self):
        one = self.tmp / "one"
        shutil.copytree(self.src, one)
        h1 = self.cfg_for(one, self.tmp / "o1").sha256()
        a, b = sorted((one / "maps").glob("*.vsdx"))[:2]
        tmp = one / "maps" / "swap.tmp"
        a.rename(tmp)
        b.rename(a)
        tmp.rename(b)
        self.assertNotEqual(self.cfg_for(one, self.tmp / "o1").sha256(), h1)

    def test_a_symlinked_duplicate_pid_changes_the_hash(self):
        one = self.tmp / "one"
        shutil.copytree(self.src, one)
        h1 = self.cfg_for(one, self.tmp / "o1").sha256()
        a, b = sorted((one / "maps").glob("*.vsdx"))[:2]
        b.unlink()
        b.symlink_to(a)                                            # pid b now reads pid a's map
        h2 = self.cfg_for(one, self.tmp / "o1").sha256()
        self.assertNotEqual(h2, h1)
        # a further name that only points at an existing file is still a new input (reads are deduped, names are not)
        (one / "maps" / "ZZ-DUP.vsdx").symlink_to(a)
        self.assertNotEqual(self.cfg_for(one, self.tmp / "o1").sha256(), h2)

    def test_a_pid_without_a_transcript_fails(self):
        one = self.tmp / "one"
        shutil.copytree(self.src, one)
        sorted((one / "transcripts").glob("*.docx"))[0].unlink()
        with self.assertRaises(ValueError) as cm:
            self.cfg_for(one, self.tmp / "o1").sha256()
        self.assertIn("has no transcript file", str(cm.exception))

    def test_the_synthetic_dir_content_is_in_the_hash(self):
        one = self.tmp / "one"
        shutil.copytree(self.src, one)
        h1 = self.cfg_for(one, self.tmp / "o1").sha256()
        gold = sorted((one / "gold").rglob("*"))
        f = next(x for x in gold if x.is_file())
        f.write_bytes(f.read_bytes() + b"x")                       # not a map or transcript: only synthetic_dir sees it
        self.assertNotEqual(self.cfg_for(one, self.tmp / "o1").sha256(), h1)

    def test_symlinked_subfolders_of_synthetic_dir_are_followed_and_a_loop_ends(self):
        one = self.tmp / "one"
        shutil.copytree(self.src, one)
        h1 = self.cfg_for(one, self.tmp / "o1").sha256()
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "extra.txt").write_text("a")
        (one / "linked").symlink_to(elsewhere, target_is_directory=True)
        h2 = self.cfg_for(one, self.tmp / "o1").sha256()
        self.assertNotEqual(h2, h1)                                            # the linked folder's file counts
        (elsewhere / "extra.txt").write_text("b")
        self.assertNotEqual(self.cfg_for(one, self.tmp / "o1").sha256(), h2)   # and its content is hashed
        (elsewhere / "back").symlink_to(one, target_is_directory=True)         # a loop: one -> linked -> back -> one
        self.cfg_for(one, self.tmp / "o1").sha256()                           # terminates

    def test_a_missing_pid_file_stops_every_command_with_a_message_and_no_traceback(self):
        use_kit(self)
        one = self.tmp / "one"
        shutil.copytree(self.src, one)
        d = json.loads(Path(DEMO_CONFIG).read_text())
        d.update(out_dir=str(self.tmp / "out"), base_dir=str(self.tmp), prereg_required=True)
        d["corpus"].update(maps_glob=str(one / "maps" / "*.vsdx"), transcripts_glob=str(one / "transcripts" / "*.docx"),
                           synthetic_dir=str(one))
        config = self.tmp / "config.json"
        config.write_text(json.dumps(d))
        d["prereg_required"] = False
        loose = self.tmp / "loose.json"
        loose.write_text(json.dumps(d))
        env = {"SS_RECON_MODEL": "recon.gguf", "SS_RECON_WEIGHTS": "b" * 64, "SS_JUDGE_MODEL": "judge.gguf",
               "SS_JUDGE_WEIGHTS": "rev-7"}
        for k, v in env.items():
            os.environ[k] = v
            self.addCleanup(os.environ.pop, k, None)

        def cli(*argv):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                    unittest.mock.patch.object(prereg, "git_head", return_value=COMMIT), \
                    unittest.mock.patch.object(prereg, "check_code", return_value=None):
                rc = cli_main(list(argv))
            return rc, out.getvalue() + err.getvalue()

        self.assertEqual(cli("prereg", "freeze", "--config", str(config))[0], 0)       # a complete corpus freezes
        sorted((one / "maps").glob("*.vsdx"))[0].unlink()
        for argv in (("prereg", "check", "--config", str(config)),
                     ("run", "--config", str(loose), "--stages", "Q", "--client", "oracle"),
                     ("prereg", "freeze", "--config", str(loose), "--allow-existing-work")):
            with self.subTest(argv=argv[:2]):
                rc, text = cli(*argv)
                self.assertNotEqual(rc, 0)
                self.assertNotIn("Traceback", text)
                self.assertIn("has no map file", text)

    def test_pairs_are_hashed_by_pid_and_role_as_literal_paths(self):
        one = self.tmp / "one"
        shutil.copytree(self.src, one)
        a, b = sorted((one / "maps").glob("*.vsdx"))[:2]
        t = sorted((one / "transcripts").glob("*.docx"))[0]
        d = json.loads(Path(DEMO_CONFIG).read_text())
        d.update(out_dir=str(self.tmp / "o"), base_dir=str(self.tmp))
        d["corpus"].update(pairs={"X1": [str(a), str(t)]}, roles={"X1": "owner"}, synthetic_dir=None,
                           maps_glob=str(one / "maps" / "*.vsdx"), transcripts_glob=str(one / "transcripts" / "*.docx"))
        h1 = RunConfig(**d).sha256()
        d["corpus"]["pairs"] = {"X1": [str(b), str(t)]}
        self.assertNotEqual(RunConfig(**d).sha256(), h1)
        d["corpus"]["pairs"] = {"X1": [str(a), str(t)], "X2": [str(a), str(t)]}
        d["corpus"]["roles"] = {"X1": "owner", "X2": "owner"}
        self.assertNotEqual(RunConfig(**d).sha256(), h1)
        d["corpus"]["pairs"] = {"X1": [str(a), str(one / "transcripts" / "missing.docx")]}
        d["corpus"]["roles"] = {"X1": "owner"}
        with self.assertRaises(ValueError):
            RunConfig(**d).sha256()

    def test_a_changed_input_after_the_freeze_fails_the_check(self):
        one = self.tmp / "one"
        shutil.copytree(self.src, one)
        cfg = replace(self.cfg_for(one, self.tmp / "out"), prereg_required=True)
        prompts = self.tmp / "prompts"
        shutil.copytree(PROMPTS_DIR, prompts)
        (self.tmp / "docs").mkdir()
        shutil.copyfile(KIT_ROOT / "docs" / "prereg-judge-labels.md", self.tmp / "docs" / "prereg-judge-labels.md")
        log = AuditLog(self.tmp / "run.jsonl", cfg.run_id)
        info = {r: {"model_id": f"{r}.gguf", "weights": "a" * 64, "params": {}} for r in ("recon", "judge")}
        prereg.freeze(prereg.build_prereg(cfg, prompts, code_commit=COMMIT, model_info=info,
                                          decision_rules=prereg.DECISION_RULES), self.tmp / "p.json", log)
        self.assertEqual(prereg.check(self.tmp / "p.json", self.tmp / "run.jsonl", prompts, cfg=cfg), (True, "ok"))
        f = sorted((one / "maps").glob("*.vsdx"))[0]
        f.write_bytes(f.read_bytes() + b"x")
        ok, msg = prereg.check(self.tmp / "p.json", self.tmp / "run.jsonl", prompts, cfg=cfg)
        self.assertFalse(ok)
        self.assertIn("config changed", msg)


class TestSyntheticInputsAreRealpathChecked(unittest.TestCase):
    def setUp(self):
        from second_scribe.synth.build import build_corpus
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.corpus = self.tmp / "corpus"
        build_corpus(self.corpus)
        self.outside = self.tmp / "outside"
        (self.outside / "maps").mkdir(parents=True)
        for f in sorted((self.corpus / "maps").glob("*.vsdx"))[:2]:
            shutil.copyfile(f, self.outside / "maps" / f.name)

    def cfg(self, **corpus):
        d = json.loads(Path(DEMO_CONFIG).read_text())
        d.update(out_dir=str(self.tmp / "out"), base_dir=str(self.tmp))
        d["corpus"].update(maps_glob=str(self.corpus / "maps" / "*.vsdx"),
                           transcripts_glob=str(self.corpus / "transcripts" / "*.docx"),
                           synthetic_dir=str(self.corpus))
        d["corpus"].update(corpus)
        return RunConfig(**d)

    def test_a_clean_corpus_passes(self):
        self.cfg()

    def test_a_symlinked_file_that_leaves_the_folder_is_refused(self):
        victim = sorted((self.corpus / "maps").glob("*.vsdx"))[0]
        victim.unlink()
        victim.symlink_to(self.outside / "maps" / victim.name)
        with self.assertRaises(ValueError) as cm:
            self.cfg()
        self.assertIn("synthetic", str(cm.exception))

    def test_a_symlinked_folder_that_leaves_the_folder_is_refused(self):
        shutil.rmtree(self.corpus / "maps")
        (self.corpus / "maps").symlink_to(self.outside / "maps")
        with self.assertRaises(ValueError):
            self.cfg()

    def test_a_glob_that_climbs_out_after_its_wildcard_is_refused(self):
        for pat in (str(self.corpus / "maps" / "*" / ".." / ".." / ".." / "outside" / "maps" / "*.vsdx"),
                    str(self.corpus / "m*" / ".." / ".." / "outside" / "maps" / "*.vsdx")):
            with self.subTest(pattern=pat), self.assertRaises(ValueError) as cm:
                self.cfg(maps_glob=pat)
            self.assertIn("..", str(cm.exception))

    def test_a_dot_dot_before_the_wildcard_is_resolved_and_judged_by_where_it_lands(self):
        inside = str(self.corpus / "maps" / ".." / "maps" / "*.vsdx")
        self.cfg(maps_glob=inside)                                           # lands inside the synthetic folder
        out = str(self.corpus / ".." / "outside" / "maps" / "*.vsdx")
        with self.assertRaises(ValueError):
            self.cfg(maps_glob=out)                                          # lands outside

    def test_a_pairs_path_is_a_literal_file_never_a_glob(self):
        """The reviewer's probe: ``X[1].vsdx`` is a symlink to a file outside; as a glob it would match ``X1.vsdx`` only."""
        t = sorted((self.corpus / "transcripts").glob("*.docx"))[0]
        link = self.corpus / "maps" / "X[1].vsdx"
        link.symlink_to(sorted((self.outside / "maps").glob("*.vsdx"))[0])
        with self.assertRaises(ValueError) as cm:
            self.cfg(pairs={"X1": [str(link), str(t)]}, roles={"X1": "owner"})
        self.assertIn("synthetic", str(cm.exception))
        inside = self.corpus / "maps" / "X[2].vsdx"                           # the same name, pointing inside: allowed
        shutil.copyfile(sorted((self.corpus / "maps").glob("SYN-*.vsdx"))[0], inside)
        self.cfg(pairs={"X2": [str(inside), str(t)]}, roles={"X2": "owner"})

    def test_a_dangling_literal_pairs_path_that_points_outside_is_refused(self):
        t = sorted((self.corpus / "transcripts").glob("*.docx"))[0]
        link = self.corpus / "maps" / "gone.vsdx"
        link.symlink_to(self.outside / "maps" / "does-not-exist.vsdx")
        with self.assertRaises(ValueError):
            self.cfg(pairs={"X1": [str(link), str(t)]}, roles={"X1": "owner"})

    def test_pairs_files_are_checked_by_their_real_path(self):
        a = sorted((self.corpus / "maps").glob("*.vsdx"))[0]
        t = sorted((self.corpus / "transcripts").glob("*.docx"))[0]
        self.cfg(pairs={"X1": [str(a), str(t)]})
        with self.assertRaises(ValueError):
            self.cfg(pairs={"X1": [str(self.outside / "maps" / a.name), str(t)]})


class TestAnchorFolder(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        d = json.loads(Path(DEMO_CONFIG).read_text())
        d.update(out_dir=str(self.tmp / "out"), base_dir=str(Path(DEMO_CONFIG).parent), prereg_required=True)
        self.config = self.tmp / "config.json"
        self.config.write_text(json.dumps(d))
        for k, v in {"SS_RECON_MODEL": "recon.gguf", "SS_RECON_WEIGHTS": "b" * 64,
                     "SS_JUDGE_MODEL": "judge.gguf", "SS_JUDGE_WEIGHTS": "rev-7"}.items():
            os.environ[k] = v
            self.addCleanup(os.environ.pop, k, None)
        use_kit(self)
        for target, value in (("git_head", COMMIT), ("check_code", None)):
            p = unittest.mock.patch.object(prereg, target, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli_main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_an_unwritable_anchor_folder_stops_the_freeze_before_it_happens(self):
        blocker = self.tmp / "afile"
        blocker.write_text("x")
        rc, out, err = self.cli("prereg", "freeze", "--config", str(self.config), "--anchor-dir", str(blocker / "sub"))
        self.assertEqual(rc, 2)
        self.assertIn("not writable", err)
        self.assertIn("nothing was frozen", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse((self.tmp / "out" / "prereg.json").exists())
        self.assertFalse((self.tmp / "out" / "logs" / "run.jsonl").exists())

    def test_a_failure_to_write_the_anchor_after_the_freeze_prints_what_to_anchor_by_hand(self):
        with unittest.mock.patch.object(prereg, "write_anchor", side_effect=OSError(28, "No space left on device")):
            rc, out, err = self.cli("prereg", "freeze", "--config", str(self.config))
        self.assertEqual(rc, 3)
        digest = sha(self.tmp / "out" / "prereg.json")
        self.assertIn(digest, err)
        self.assertIn("demo", err)
        self.assertIn("by hand", err)
        self.assertNotIn("Traceback", err)
        self.assertIn(digest, out)                      # the freeze itself succeeded and is reported

    def test_ensure_writable_creates_the_folder_and_leaves_nothing_behind(self):
        prereg.ensure_writable(self.tmp / "a" / "b")
        self.assertEqual(list((self.tmp / "a" / "b").iterdir()), [])


class TestGuard(Base):
    def guarded(self):
        return replace(self.cfg, prereg_required=True)

    def run_guarded(self, cfg=None, stages=("mapping_talk_confirm",)):
        return run(cfg or self.guarded(), list(stages), {}, log=self.log)

    def test_run_refuses_without_a_frozen_record(self):
        for stages in (["mapping_talk_confirm"], ["recon"], ["judge_modality"], ["metrics"]):
            with self.subTest(stages=stages):
                with self.assertRaises(RuntimeError) as cm:
                    run(self.guarded(), stages, {}, log=self.log)
                self.assertEqual(str(cm.exception), "prereg not frozen")
        self.assertEqual(records(self.log_path) if self.log_path.exists() else [], [])   # nothing was logged

    def test_run_refuses_when_the_prereg_file_changed(self):
        cfg = self.guarded()
        path = prereg.prereg_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        prereg.freeze(prereg.build_prereg(cfg, self.kit / 'prompts', code_commit=COMMIT, model_info=MODEL_INFO,
                                          decision_rules=prereg.DECISION_RULES), path, self.log)
        path.write_text(path.read_text().replace('"min_words":8', '"min_words":9'))
        with self.assertRaises(RuntimeError) as cm:
            run(cfg, ["metrics"], {}, log=self.log)
        self.assertIn("prereg file changed", str(cm.exception))

    def test_run_goes_ahead_when_frozen(self):
        cfg = self.guarded()
        path = prereg.prereg_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        with unittest.mock.patch.object(prereg, "check_code", return_value=None):
            prereg.freeze(prereg.build_prereg(cfg, self.kit / 'prompts', code_commit=COMMIT, model_info=MODEL_INFO,
                                              decision_rules=prereg.DECISION_RULES), path, self.log)
            out = run(cfg, ["mapping_talk_confirm"], build_clients("oracle", cfg), log=self.log)
        self.assertIn("mapping_talk_confirm", out["new_items"])

    def test_code_drift_blocks_the_run(self):
        cfg = self.guarded()
        path = prereg.prereg_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        prereg.freeze(prereg.build_prereg(cfg, self.kit / 'prompts', code_commit=COMMIT, model_info=MODEL_INFO,
                                          decision_rules=prereg.DECISION_RULES), path, self.log)
        with self.assertRaises(RuntimeError) as cm:     # COMMIT is not a commit of this repo
            run(cfg, ["mapping_talk_confirm"], {}, log=self.log)
        self.assertIn("prereg check failed", str(cm.exception))

    def test_without_prereg_required_nothing_changes(self):
        out = run(self.cfg, ["mapping_talk_confirm"], build_clients("oracle", self.cfg), log=self.log)
        self.assertIn("mapping_talk_confirm", out["new_items"])

    def test_the_demo_config_does_not_require_it(self):
        self.assertFalse(RunConfig.load(DEMO_CONFIG).prereg_required)


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        d = json.loads(Path(DEMO_CONFIG).read_text())
        d.update(out_dir=str(self.tmp / "out"), base_dir=str(Path(DEMO_CONFIG).parent), prereg_required=True)
        self.config = self.tmp / "config.json"
        self.config.write_text(json.dumps(d))
        use_kit(self)
        self.env = {"SS_RECON_MODEL": "/models/recon.gguf", "SS_RECON_WEIGHTS": "b" * 64,
                    "SS_JUDGE_MODEL": "judge.gguf", "SS_JUDGE_WEIGHTS": "rev-7"}
        for k, v in self.env.items():
            os.environ[k] = v
            self.addCleanup(os.environ.pop, k, None)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli_main(list(argv))
        return rc, out.getvalue() + err.getvalue()

    def test_run_before_the_freeze_is_refused_without_a_traceback(self):
        rc, text = self.cli("run", "--config", str(self.config), "--stages", "Q", "--client", "oracle")
        self.assertEqual(rc, 2)
        self.assertIn("refused: prereg not frozen", text)
        self.assertFalse((self.tmp / "out" / "work").exists())

    def test_check_before_freeze_fails(self):
        rc, text = self.cli("prereg", "check", "--config", str(self.config))
        self.assertNotEqual(rc, 0)
        self.assertIn("prereg not frozen", text)

    def test_freeze_refuses_a_dirty_or_unknown_code_state(self):
        with unittest.mock.patch.object(prereg, "git_head", return_value=COMMIT), \
             unittest.mock.patch.object(prereg, "check_code", return_value="uncommitted changes under second_scribe/"):
            rc, text = self.cli("prereg", "freeze", "--config", str(self.config))
        self.assertNotEqual(rc, 0)
        self.assertIn("uncommitted", text)
        self.assertFalse((self.tmp / "out" / "prereg.json").exists())

    def test_freeze_then_check(self):
        with unittest.mock.patch.object(prereg, "git_head", return_value=COMMIT), \
             unittest.mock.patch.object(prereg, "check_code", return_value=None):
            rc, text = self.cli("prereg", "freeze", "--config", str(self.config))
            self.assertEqual(rc, 0, text)
            path = self.tmp / "out" / "prereg.json"
            self.assertTrue(path.exists())
            self.assertIn(sha(path), text)
            obj = json.loads(path.read_text())
            self.assertEqual(obj["model_info"]["recon"], {"model_id": "recon.gguf", "weights": "b" * 64, "params": {},
                                                          "model_id_sha256": hashlib.sha256(b"/models/recon.gguf").hexdigest()})
            self.assertEqual(obj["code_commit"], COMMIT)
            rc, text = self.cli("prereg", "check", "--config", str(self.config))
            self.assertEqual(rc, 0, text)
            self.assertIn("ok", text)
        rc, _ = self.cli("verify", str(self.tmp / "out" / "logs" / "run.jsonl"))
        self.assertEqual(rc, 0)

    def test_weights_default_to_pending(self):
        os.environ.pop("SS_JUDGE_WEIGHTS")
        with unittest.mock.patch.object(prereg, "git_head", return_value=COMMIT), \
             unittest.mock.patch.object(prereg, "check_code", return_value=None):
            rc, _ = self.cli("prereg", "freeze", "--config", str(self.config))
        self.assertEqual(rc, 0)       # synthetic corpus: "pending" is allowed
        obj = json.loads((self.tmp / "out" / "prereg.json").read_text())
        self.assertEqual(obj["model_info"]["judge"]["weights"], "pending")

    def test_a_real_corpus_freeze_needs_weights_and_calibration(self):
        d = json.loads(self.config.read_text())
        d["corpus"]["kind"] = "real"
        self.config.write_text(json.dumps(d))
        os.environ.pop("SS_JUDGE_WEIGHTS")
        with unittest.mock.patch.object(prereg, "git_head", return_value=COMMIT), \
             unittest.mock.patch.object(prereg, "check_code", return_value=None):
            rc, text = self.cli("prereg", "freeze", "--config", str(self.config))
            self.assertEqual(rc, 2)
            self.assertIn("pending", text)
            self.assertIn("matcher", text)
            self.assertFalse((self.tmp / "out" / "prereg.json").exists())
            os.environ["SS_JUDGE_WEIGHTS"] = "rev-7"
            cal = self.tmp / "cal.json"
            cal.write_text(json.dumps({"matcher": {"n": 3}, "in_transcript": {"n": 3}}))
            rc, text = self.cli("prereg", "freeze", "--config", str(self.config), "--calibration", str(cal))
            self.assertEqual(rc, 0, text)

    def test_freeze_writes_the_anchor_and_check_can_require_it(self):
        with unittest.mock.patch.object(prereg, "git_head", return_value=COMMIT), \
             unittest.mock.patch.object(prereg, "check_code", return_value=None):
            private = self.tmp / "private-runner"
            rc, text = self.cli("prereg", "freeze", "--config", str(self.config), "--anchor-dir", str(private))
            self.assertEqual(rc, 0, text)
            anchor = private / "prereg.sha256"
            digest = sha(self.tmp / "out" / "prereg.json")
            self.assertEqual(anchor.read_text(), f"{digest} demo\n")
            self.assertIn("commit it", text)
            rc, text = self.cli("prereg", "check", "--config", str(self.config), "--require-anchor", str(anchor))
            self.assertEqual(rc, 0, text)
            anchor.write_text(f"{'0' * 64} demo\n")
            rc, text = self.cli("prereg", "check", "--config", str(self.config), "--require-anchor", str(anchor))
            self.assertEqual(rc, 1)
            self.assertIn("anchor", text)

    def test_the_anchor_goes_next_to_prereg_json_by_default(self):
        with unittest.mock.patch.object(prereg, "git_head", return_value=COMMIT), \
             unittest.mock.patch.object(prereg, "check_code", return_value=None):
            self.assertEqual(self.cli("prereg", "freeze", "--config", str(self.config))[0], 0)
        self.assertTrue((self.tmp / "out" / "prereg.sha256").exists())

    def test_freeze_refuses_when_work_exists_unless_synthetic_and_allowed(self):
        (self.tmp / "out" / "work" / "recon").mkdir(parents=True)
        (self.tmp / "out" / "work" / "recon" / "a.json").write_text("{}")
        with unittest.mock.patch.object(prereg, "git_head", return_value=COMMIT), \
             unittest.mock.patch.object(prereg, "check_code", return_value=None):
            rc, text = self.cli("prereg", "freeze", "--config", str(self.config))
            self.assertEqual(rc, 2)
            self.assertIn("already under work/", text)
            rc, text = self.cli("prereg", "freeze", "--config", str(self.config), "--allow-existing-work")
            self.assertEqual(rc, 0, text)

    def test_the_params_of_the_environment_are_frozen_redacted(self):
        os.environ["SS_RECON_PARAMS"] = json.dumps({"top_p": 0.9, "api_key": "sk-xyz"})
        self.addCleanup(os.environ.pop, "SS_RECON_PARAMS", None)
        with unittest.mock.patch.object(prereg, "git_head", return_value=COMMIT), \
             unittest.mock.patch.object(prereg, "check_code", return_value=None):
            self.assertEqual(self.cli("prereg", "freeze", "--config", str(self.config))[0], 0)
        text = (self.tmp / "out" / "prereg.json").read_text()
        self.assertNotIn("sk-xyz", text)
        self.assertEqual(json.loads(text)["model_info"]["recon"]["params"], {"api_key": "[REDACTED]", "top_p": 0.9})

    def test_calibration_file_is_read(self):
        cal = self.tmp / "cal.json"
        cal.write_text(json.dumps({"matcher": {"n": 3}}))
        with unittest.mock.patch.object(prereg, "git_head", return_value=COMMIT), \
             unittest.mock.patch.object(prereg, "check_code", return_value=None):
            rc, _ = self.cli("prereg", "freeze", "--config", str(self.config), "--calibration", str(cal))
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads((self.tmp / "out" / "prereg.json").read_text())["calibration"], {"matcher": {"n": 3}})


MODE_B_ITEM = {"status": "ok", "in_transcript": {"n1": True, "n2": True}, "request_ids": [],
               "by_construction": "mode_b", "reference_file_type": "B"}


class TestModeBInTranscriptNoCall(unittest.TestCase):
    """L33 (fix H): a mode-B in_transcript item (every node marked present, no call) makes no call by construction
    ONLY when (i) it carries the marker "by_construction": "mode_b", (ii) the run's reference type, as every run_start
    of the log records it, is "B", (iii) the item records that the reference file's own type was "B" (the K9 check the
    skip passed), and (iv) every value of its in_transcript dict is true. A study log (no reference type) is unchanged:
    only the empty dict passes."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ss-tests-modeb-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.n = 0

    def problem(self, item, *starts):
        """run_validity_problem of a real-corpus log whose run_starts carry ``starts`` (one per session) and a work
        store holding the one in_transcript item ``item``."""
        self.n += 1
        out = self.root / f"r{self.n}"
        (out / "logs").mkdir(parents=True)
        (out / "work" / "in_transcript").mkdir(parents=True)
        log = AuditLog(out / "logs" / "run.jsonl", "r")
        for extra in starts or ({},):
            log.append("run_start", {"corpus_kind": "real", "config_sha256": "c" * 64, **extra})
        log.close()
        (out / "work" / "in_transcript" / "CASE-X01.json").write_text(json.dumps(item))
        return prereg.run_validity_problem(out / "logs" / "run.jsonl", out / "work")

    def refused(self, item, *starts):
        return self.problem(item, *starts) or ""

    def test_a_mode_b_item_in_a_mode_b_run_makes_no_call(self):
        self.assertIsNone(self.problem(MODE_B_ITEM, {"reference_type": "B"}))
        self.assertIsNone(self.problem(MODE_B_ITEM, {"reference_type": "B"}, {"reference_type": "B"}))   # resumed

    def test_a_study_log_with_a_forged_marker_fails(self):                      # (a)
        self.assertIn("names no model call", self.refused(MODE_B_ITEM))

    def test_a_mode_a_run_with_the_marker_fails(self):                          # (b)
        self.assertIn("names no model call", self.refused(MODE_B_ITEM, {"reference_type": "A"}))
        self.assertIn("names no model call", self.refused(MODE_B_ITEM, {"reference_type": "C"}))

    def test_a_mode_b_item_without_the_marker_fails(self):                      # (c), condition (i)
        item = {k: v for k, v in MODE_B_ITEM.items() if k != "by_construction"}
        self.assertIn("names no model call", self.refused(item, {"reference_type": "B"}))
        self.assertIn("names no model call", self.refused({**MODE_B_ITEM, "by_construction": "mode_c"},
                                                          {"reference_type": "B"}))

    def test_a_mode_b_item_with_one_value_false_fails(self):                    # (c2), condition (iv)
        item = {**MODE_B_ITEM, "in_transcript": {"n1": True, "n2": False}}
        self.assertIn("names no model call", self.refused(item, {"reference_type": "B"}))
        item = {**MODE_B_ITEM, "in_transcript": {"n1": True, "n2": 1}}            # true, not truthy
        self.assertIn("names no model call", self.refused(item, {"reference_type": "B"}))

    def test_a_mode_b_item_without_the_file_type_fails(self):                   # condition (iii)
        item = {k: v for k, v in MODE_B_ITEM.items() if k != "reference_file_type"}
        self.assertIn("names no model call", self.refused(item, {"reference_type": "B"}))
        self.assertIn("names no model call", self.refused({**MODE_B_ITEM, "reference_file_type": "A"},
                                                          {"reference_type": "B"}))

    def test_every_session_must_be_a_mode_b_session(self):                      # condition (ii), every run_start
        self.assertIn("names no model call", self.refused(MODE_B_ITEM, {"reference_type": "B"}, {}))
        self.assertIn("names no model call", self.refused(MODE_B_ITEM, {}, {"reference_type": "B"}))

    def test_the_empty_dict_still_passes_in_any_run(self):                      # study behaviour unchanged
        empty = {"status": "ok", "in_transcript": {}, "request_ids": []}
        self.assertIsNone(self.problem(empty))
        self.assertIsNone(self.problem(empty, {"reference_type": "A"}))

    def test_a_forged_marker_does_not_pass_the_prereg_check_of_a_study_run(self):   # (a), on the prereg path
        t = TestFreezeAfterTheRun("test_an_empty_work_folder_does_not_block")
        t.setUp()
        self.addCleanup(t.doCleanups)
        cfg, log, log_path = t.frozen_cfg()
        t.plant(cfg, "in_transcript", "CASE-X01", MODE_B_ITEM)
        ok, msg = t.check(cfg, log_path)
        self.assertFalse(ok)
        self.assertIn("names no model call", msg)
        # a log that claims a mode-B session is not enough: the config the check is given must say "B" as well
        log.append("run_start", {"corpus_kind": "synthetic", "config_sha256": cfg.sha256(), "reference_type": "B"})
        ok, msg = t.check(cfg, log_path)
        self.assertFalse(ok)
        self.assertIn("names no model call", msg)


if __name__ == "__main__":
    unittest.main()
