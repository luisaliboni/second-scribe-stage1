"""K9 (app T16): a tool run may replace rung 1's instruction text with the user's own first-draft prompt.

The user's text goes first, verbatim (never slot-filled), then the app's output-format wrapper
(``second_scribe/app/prompts/rung1_wrapper.txt``) filled by the kit's own slot filler. Rung 1 keeps no system message
and no definitions. The wrapper lives outside ``prompts/`` so the study's prompt set and its hash are untouched, and a
config without a ``tool`` block sends byte-identical rung-1 messages. Synthetic corpus, oracle client, temp dirs only.
"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from second_scribe import ladder, pipeline, prompt_manifest, toolcfg
from second_scribe.audit.registry import AuditLog
from second_scribe.cli import DEMO_CONFIG, build_clients
from second_scribe.pipeline import PROMPTS_DIR, RunConfig, run
from second_scribe.prompts import load_definitions
from second_scribe.reconstruct import ReconContext, build_messages

PROMPT_SET = "fcb27f0efc56ff87d1fb6a45db0f9d1e3999dbc16de0ff51d88d741bc7aa5f20"
DEMO_HASH = "ccc13208b1fe5e66b5f4bef970af228cb92af9aefca6f21d341a0ccaa18c1ba9"
READING3_PROMPT_SHA = "9b48f8639447dfb74da3fd24876cb5c69d3adff8878fd4b50dcdceffd84302c0"
# measured on 7974b00 (before T16): rung 1's identity from its files, and the sha256 of every rung-1 call's messages
# in a Q run of the demo with ladder rungs ["rung1"], draws 1 (json of [[item_id, messages]] sorted by item_id)
RUNG1_FILE_IDENTITY = "8ee25086ca3fb283e128efd4bd03018e152f205566ba0b3c44f103a8e43a7966"
RUNG1_MESSAGES_DIGEST = "84cf702d4e354e2b12c17f954068c2d3ca92ebc564e478f64b29e0899f5b01f2"

USER_TEXT = "Draw the journey of this {role} as a map, step by step. Keep it short."
CTX = ReconContext(domain="food-truck licensing in a mid-sized city", role="food-truck owner",
                   phases=("Planning", "Permits", "Inspections", "Operating"))
TRANSCRIPT = "[pt] I filed the permit in March and waited six weeks for the {schema} letter to come back."
RUNG1 = ladder.get_rung("rung1")


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def records(path):
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines()]


def rung1_calls(recs):
    return [r for r in recs if r["record_type"] == "call_request" and r["payload"]["stage"].endswith(":rung1")]


def messages_digest(calls) -> str:
    blob = json.dumps([[q["payload"]["item_id"], q["payload"]["messages"]]
                       for q in sorted(calls, key=lambda q: q["payload"]["item_id"])], sort_keys=True)
    return sha(blob)


class TestMessages(unittest.TestCase):
    def setUp(self):
        self.block = toolcfg.rung1_user_block(USER_TEXT)

    def test_wrapper_file_is_app_owned_and_carried_in_the_block(self):
        self.assertEqual(toolcfg.RUNG1_WRAPPER_PATH.parent.name, "prompts")
        self.assertEqual(toolcfg.RUNG1_WRAPPER_PATH.parent.parent.name, "app")
        wrapper = toolcfg.RUNG1_WRAPPER_PATH.read_text(encoding="utf-8")
        self.assertEqual(self.block, {"text": USER_TEXT, "text_sha256": sha(USER_TEXT),
                                      "wrapper": wrapper, "wrapper_sha256": sha(wrapper)})
        toolcfg.check_tool({"rung1_user": self.block}, {})       # T4's check accepts it

    def test_user_text_then_wrapper_one_message(self):
        msgs = ladder.rung_messages(PROMPTS_DIR, RUNG1, CTX, TRANSCRIPT, template=self.block)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "user")
        filled = build_messages(self.block["wrapper"], CTX, TRANSCRIPT)[0]["content"]
        self.assertEqual(msgs[0]["content"], USER_TEXT + "\n\n" + filled)
        self.assertTrue(msgs[0]["content"].startswith("Draw the journey of this {role}"))   # user text never filled
        self.assertTrue(msgs[0]["content"].endswith("TRANSCRIPT:\n" + TRANSCRIPT + "\n"))
        self.assertIn("{schema} letter", msgs[0]["content"])                                # transcript not re-expanded
        for slot in ("{domain}", "{phases}", "{transcript}"):
            self.assertNotIn(slot, filled)
        self.assertIn(CTX.domain, filled)
        self.assertIn("Planning, Permits, Inspections, Operating", filled)
        self.assertIn('"nodes"', filled)                                                    # the kit's JSON schema

    def test_no_system_no_definitions(self):
        msgs = ladder.rung_messages(PROMPTS_DIR, RUNG1, CTX, TRANSCRIPT, template=self.block)
        self.assertEqual([m["role"] for m in msgs], ["user"])
        defs = load_definitions(PROMPTS_DIR, "v2")
        self.assertNotIn(defs.strip().splitlines()[0], msgs[0]["content"])
        self.assertNotIn("{definitions}", self.block["wrapper"])

    def test_without_template_rung1_is_the_kit_prompt(self):
        kit = build_messages(ladder.load_rung_prompt(PROMPTS_DIR, RUNG1), CTX, TRANSCRIPT)
        self.assertEqual(ladder.rung_messages(PROMPTS_DIR, RUNG1, CTX, TRANSCRIPT), kit)
        self.assertEqual(ladder.rung_messages(PROMPTS_DIR, RUNG1, CTX, TRANSCRIPT, template=None), kit)

    def test_template_only_for_rung1(self):
        for k in ("rung2", "rung3", "placebo"):
            with self.subTest(rung=k), self.assertRaises(ValueError):
                ladder.rung_messages(PROMPTS_DIR, ladder.get_rung(k), CTX, TRANSCRIPT, template=self.block)
            with self.subTest(rung=k), self.assertRaises(ValueError):
                ladder.rung_prompt_sha256(PROMPTS_DIR, ladder.get_rung(k), template=self.block)

    def test_rung_prompt_sha256_hashes_the_pair(self):
        self.assertEqual(ladder.rung_prompt_sha256(PROMPTS_DIR, RUNG1), RUNG1_FILE_IDENTITY)
        want = sha(f"user:{sha(USER_TEXT)}\nwrapper:{sha(self.block['wrapper'])}")
        self.assertEqual(ladder.rung_prompt_sha256(PROMPTS_DIR, RUNG1, template=self.block), want)
        other = toolcfg.rung1_user_block(USER_TEXT + " Use plain words.")
        self.assertNotEqual(ladder.rung_prompt_sha256(PROMPTS_DIR, RUNG1, template=other), want)


class TestValidate(unittest.TestCase):
    def test_wrapper_missing_slot_fails_validate(self):
        good = toolcfg.rung1_user_block(USER_TEXT)
        self.assertEqual(ladder.validate_rungs(PROMPTS_DIR), [])
        self.assertEqual(ladder.validate_rungs(PROMPTS_DIR, rung1_template=good), [])
        for slot in ("{schema}", "{domain}", "{role}", "{phases}", "{transcript}"):
            with self.subTest(slot=slot):
                bad = toolcfg.rung1_user_block(USER_TEXT, good["wrapper"].replace(slot, "x"))
                problems = ladder.validate_rungs(PROMPTS_DIR, rung1_template=bad)
                self.assertTrue(any("rung1_user" in p for p in problems), problems)
        for bad_wrapper in (good["wrapper"].replace("TRANSCRIPT:\n", "TEXT:\n"),          # no marker line
                            good["wrapper"].replace("TRANSCRIPT:\n{transcript}\n", "{transcript}\nTRANSCRIPT:\n"),
                            good["wrapper"] + "{definitions}\n",                           # rung 1 has no definitions
                            good["wrapper"] + "More text after the transcript.\n"):        # must end with it
            with self.subTest(wrapper=bad_wrapper[-60:]):
                problems = ladder.validate_rungs(PROMPTS_DIR, rung1_template=toolcfg.rung1_user_block(USER_TEXT, bad_wrapper))
                self.assertTrue(any("rung1_user" in p for p in problems), problems)
        empty = dict(good, text="")
        self.assertTrue(ladder.validate_rungs(PROMPTS_DIR, rung1_template=empty))

    def test_bad_wrapper_stops_the_arm_before_any_call(self):
        bad = toolcfg.rung1_user_block(USER_TEXT, "no slots at all\n")

        class NoCall:
            def complete(self, *a, **k):
                raise AssertionError("a model call was made")

        with self.assertRaises(RuntimeError):
            ladder.run_ladder_arm(RUNG1, [], ladder.ReconSpec("removed", "p0", 0.0), 1, log=None, client=NoCall(),
                                  ctx=CTX, prompts_dir=PROMPTS_DIR, real_data=False, template=bad)

    def test_pinned_prompts_still_checked(self):
        self.assertIn("reconstruct.p0.v1.txt", ladder.PINNED_SHA256)      # untouched pin table
        self.assertEqual(ladder.PINNED_SHA256["reconstruct.p0.v1.txt"], ladder.RUNG1_SHA256)


class TestPromptSet(unittest.TestCase):
    def test_prompt_set_hash_unchanged(self):
        self.assertEqual(pipeline.prompt_set_sha256(), PROMPT_SET)
        self.assertIsNone(pipeline.prompt_manifest_problem())
        files = prompt_manifest.build_manifest(PROMPTS_DIR)["files"]
        self.assertNotIn("rung1_wrapper.txt", files)
        self.assertFalse((PROMPTS_DIR / "rung1_wrapper.txt").exists())
        self.assertNotEqual(toolcfg.RUNG1_WRAPPER_PATH.resolve().parent, PROMPTS_DIR.resolve())
        self.assertEqual(hashlib.sha256((PROMPTS_DIR / "prior_only.p0.v2.txt").read_bytes()).hexdigest(),
                         READING3_PROMPT_SHA)

    def test_demo_config_hash_unchanged(self):
        self.assertEqual(RunConfig.load(DEMO_CONFIG).sha256(), DEMO_HASH)

    def test_changed_user_prompt_new_config_hash(self):
        def cfg(tool):
            d = json.loads(DEMO_CONFIG.read_text(encoding="utf-8"))
            d["base_dir"] = str(DEMO_CONFIG.parent)
            if tool:
                d["tool"] = tool
            return RunConfig(**d).sha256()

        a = cfg({"rung1_user": toolcfg.rung1_user_block(USER_TEXT)})
        b = cfg({"rung1_user": toolcfg.rung1_user_block(USER_TEXT + " ")})
        w = cfg({"rung1_user": toolcfg.rung1_user_block(USER_TEXT, "x" + toolcfg.RUNG1_WRAPPER_PATH.read_text())})
        self.assertEqual(cfg({}), DEMO_HASH)
        self.assertEqual(len({a, b, w, DEMO_HASH}), 4)


class RunBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ss-r1u-")
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name) / "out"

    def cfg(self, tool=None) -> RunConfig:
        d = json.loads(DEMO_CONFIG.read_text(encoding="utf-8"))
        d["base_dir"] = str(DEMO_CONFIG.parent)
        d["out_dir"] = str(self.out)
        d["ladder"] = {"rungs": ["rung1"], "draws": 1, "n_perm": 50, "B": 50}
        if tool:
            d["tool"] = tool
        return RunConfig(**d)

    def go(self, cfg):
        (self.out / "logs").mkdir(parents=True, exist_ok=True)
        self.log_path = self.out / "logs" / "run.jsonl"
        log = AuditLog(self.log_path, cfg.run_id)
        try:
            run(cfg, ["Q"], build_clients("oracle", cfg), log=log)
        finally:
            log.close()
        return records(self.log_path)


class TestRun(RunBase):
    def test_no_tool_rung1_messages_byte_identical(self):
        recs = self.go(self.cfg())
        calls = rung1_calls(recs)
        self.assertEqual(len(calls), 12)
        self.assertEqual(messages_digest(calls), RUNG1_MESSAGES_DIGEST)
        start = next(r for r in recs if r["record_type"] == "run_start")["payload"]
        self.assertNotIn("rung1_user_prompt_sha256", start)
        self.assertNotIn("reading3_prompt_sha256", start)

    def test_sha_in_run_start_before_first_call(self):
        block = toolcfg.rung1_user_block(USER_TEXT)
        recs = self.go(self.cfg({"rung1_user": block}))
        i_start = next(i for i, r in enumerate(recs) if r["record_type"] == "run_start")
        start = recs[i_start]["payload"]
        self.assertEqual(start["rung1_user_prompt_sha256"], block["text_sha256"])
        self.assertEqual(start["rung1_user_prompt_sha256"], sha(USER_TEXT))
        self.assertEqual(start["reading3_prompt_sha256"], READING3_PROMPT_SHA)        # V7
        self.assertEqual(start["prompt_set_sha256"], PROMPT_SET)
        i_calls = [i for i, r in enumerate(recs) if r["record_type"] == "call_request"]
        self.assertTrue(i_calls)
        self.assertLess(i_start, min(i_calls))
        calls = rung1_calls(recs)
        self.assertEqual(len(calls), 12)
        for c in calls:
            msgs = c["payload"]["messages"]
            self.assertEqual([m["role"] for m in msgs], ["user"])
            self.assertTrue(msgs[0]["content"].startswith(USER_TEXT + "\n\nThe interview is with a "))
        # the other rungs' (here: the main run's) messages do not carry the user's text
        others = [r for r in recs if r["record_type"] == "call_request" and not r["payload"]["stage"].endswith(":rung1")]
        self.assertTrue(others)
        self.assertFalse(any(USER_TEXT in m["content"] for r in others for m in r["payload"]["messages"]))
        # the stored items carry the template identity, so the ladder sees no gap and a resume makes no new call
        ident = ladder.rung_prompt_sha256(PROMPTS_DIR, RUNG1, template=block)
        items = [json.loads(p.read_text()) for p in (self.out / "work" / "recon:rung1").glob("*.json")]
        self.assertEqual(len(items), 12)
        self.assertEqual({it["prompt_sha256"] for it in items}, {ident})
        self.assertEqual(pipeline.ladder_missing(self.cfg({"rung1_user": block})), {})
        self.assertEqual(pipeline.ladder_missing(self.cfg()), {"rung1": 12})     # file identity: these are not its items

    def test_tool_without_rung1_user_keeps_kit_rung1(self):
        recs = self.go(self.cfg({"reference_type": "A"}))
        self.assertEqual(messages_digest(rung1_calls(recs)), RUNG1_MESSAGES_DIGEST)
        start = next(r for r in recs if r["record_type"] == "run_start")["payload"]
        self.assertNotIn("rung1_user_prompt_sha256", start)
        self.assertEqual(start["reading3_prompt_sha256"], READING3_PROMPT_SHA)

    def test_reading3_sha_in_config_must_match_live_file(self):
        """Audit K10: a config's reading3_prompt_sha256 equal to the live file runs; a different one is refused
        before run_start (no run_start record, no model call)."""
        recs = self.go(self.cfg({"reference_type": "A", "reading3_prompt_sha256": READING3_PROMPT_SHA}))
        start = next(r for r in recs if r["record_type"] == "run_start")["payload"]
        self.assertEqual(start["reading3_prompt_sha256"], READING3_PROMPT_SHA)
        self._tmp2 = tempfile.TemporaryDirectory(prefix="ss-r1u-")
        self.addCleanup(self._tmp2.cleanup)
        self.out = Path(self._tmp2.name) / "out"
        with self.assertRaises(ValueError) as cm:
            self.go(self.cfg({"reference_type": "A", "reading3_prompt_sha256": "ab" * 32}))
        self.assertIn("reading3_prompt_sha256", str(cm.exception))
        types = [r["record_type"] for r in records(self.log_path)] if self.log_path.exists() else []
        self.assertNotIn("run_start", types)
        self.assertNotIn("call_request", types)


class TestRestart(unittest.TestCase):
    def test_restart_with_changed_prompt_refused(self):
        from tests.test_real_run_validity import Base as RealBase

        class Real(RealBase):
            def runTest(self):
                pass

        t = Real()
        t.setUp()
        try:
            d = json.loads(t.cfg.read_text())
            d["ladder"] = {"rungs": ["rung1"], "draws": 1, "n_perm": 50, "B": 50}
            d["tool"] = {"rung1_user": toolcfg.rung1_user_block(USER_TEXT)}
            t.cfg.write_text(json.dumps(d))
            rc, err = t.run_all("Q")
            self.assertEqual(rc, 0, err)
            n_before = len(records(t.log))
            d["tool"] = {"rung1_user": toolcfg.rung1_user_block(USER_TEXT + " Use plain words.")}
            t.cfg.write_text(json.dumps(d))
            rc, err = t.run_all("Q")
            self.assertEqual(rc, 2, err)
            self.assertIn("refusing to resume", err)
            self.assertIn("config", err)
            self.assertFalse([r for r in records(t.log)[n_before:] if r["record_type"] == "call_request"])
        finally:
            t.doCleanups()


if __name__ == "__main__":
    unittest.main()
