"""Task 4: the ``tool`` block in ``RunConfig`` (tool-only switches; study hashes unchanged)."""
import copy
import hashlib
import unittest
from pathlib import Path

from second_scribe import toolcfg
from second_scribe.pipeline import RunConfig

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "configs" / "demo.json"
# RunConfig.load("configs/demo.json").sha256() at the branch point fb26351 (T1 ledger, DEMO_HASH)
DEMO_HASH = "ccc13208b1fe5e66b5f4bef970af228cb92af9aefca6f21d341a0ccaa18c1ba9"
HEX = "ab" * 32


def _sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _full_tool():
    text, wrapper = "Draw the pathway the participant describes.", "{text}"
    return {
        "app_log_head": {"records": 3, "head": HEX},
        "judge_rule": "two_family",
        "model_families": {"recon": "qwen", "judge": "gpt-oss"},
        "model_digests": {"recon": HEX},
        "model_check": {"path": "checks/model_check.json", "sha256": HEX},
        "rung1_user": {"text": text, "wrapper": wrapper, "text_sha256": _sha(text), "wrapper_sha256": _sha(wrapper)},
        "remote_endpoints": [{"role": "judge2", "host": "api.anthropic.com", "provider": "anthropic",
                              "declaration_sha256": HEX}],
        "endpoint_optins": [{"role": "judge2", "host": "api.anthropic.com"}],
        "reference_type": "B",
        "reading3_prompt_sha256": "9b48f8639447dfb74da3fd24876cb5c69d3adff8878fd4b50dcdceffd84302c0",
    }


class TestStudyHash(unittest.TestCase):
    def test_demo_hash_unchanged(self):
        self.assertEqual(RunConfig.load(DEMO).sha256(), DEMO_HASH)

    def test_empty_tool_block_keeps_hash(self):
        self.assertEqual(RunConfig.load(DEMO, tool={}).sha256(), DEMO_HASH)

    def test_tool_block_changes_hash(self):
        self.assertNotEqual(RunConfig.load(DEMO, tool={"reference_type": "A"}).sha256(), DEMO_HASH)

    def test_full_tool_block_loads(self):
        cfg = RunConfig.load(DEMO, tool=_full_tool())
        self.assertEqual(cfg.tool["reference_type"], "B")


class TestCheckTool(unittest.TestCase):
    def _refused(self, tool, key):
        with self.assertRaises(ValueError) as cm:
            toolcfg.check_tool(tool, {"kind": "synthetic"})
        self.assertIn(key, str(cm.exception))

    def test_full_tool_passes(self):
        toolcfg.check_tool(_full_tool(), {"kind": "synthetic"})

    def test_tool_keys(self):
        self.assertEqual(set(toolcfg.TOOL_KEYS), set(_full_tool()))
        self.assertIn("reading3_prompt_sha256", toolcfg.TOOL_KEYS)

    def test_unknown_tool_key_refused(self):
        with self.assertRaises(ValueError) as cm:
            toolcfg.check_tool({"colour": "red"}, {"kind": "synthetic"})
        self.assertEqual(str(cm.exception), "unknown tool key: colour")

    def test_post_init_checks_tool(self):
        with self.assertRaises(ValueError) as cm:
            RunConfig.load(DEMO, tool={"colour": "red"})
        self.assertIn("unknown tool key: colour", str(cm.exception))

    def test_bad_reference_type_refused(self):
        self._refused({"reference_type": "D"}, "reference_type")

    def test_bad_judge_rule_refused(self):
        self._refused({"judge_rule": "majority"}, "judge_rule")

    def test_rung1_sha_mismatch_refused(self):
        t = _full_tool()["rung1_user"]
        bad = dict(t, text_sha256=_sha("something else"))
        self._refused({"rung1_user": bad}, "rung1_user")
        bad = dict(t, wrapper_sha256=_sha("other wrapper"))
        self._refused({"rung1_user": bad}, "rung1_user")

    def test_rung1_text_length(self):
        for text in ("", "x" * 20001):
            self._refused({"rung1_user": {"text": text, "wrapper": "", "text_sha256": _sha(text),
                                          "wrapper_sha256": _sha("")}}, "rung1_user")
        text = "x" * 20000
        toolcfg.check_tool({"rung1_user": {"text": text, "wrapper": "", "text_sha256": _sha(text),
                                           "wrapper_sha256": _sha("")}}, {})

    def test_remote_endpoint_bad_provider_refused(self):
        ep = dict(_full_tool()["remote_endpoints"][0], provider="openai")
        self._refused({"remote_endpoints": [ep]}, "remote_endpoints")

    def test_remote_endpoint_missing_key_refused(self):
        ep = dict(_full_tool()["remote_endpoints"][0])
        del ep["declaration_sha256"]
        self._refused({"remote_endpoints": [ep]}, "remote_endpoints")

    def test_endpoint_optins_shape(self):
        self._refused({"endpoint_optins": [{"role": "judge2"}]}, "endpoint_optins")
        self._refused({"endpoint_optins": {"role": "judge2", "host": "h"}}, "endpoint_optins")

    def test_model_check_extra_subkey_refused(self):
        self._refused({"model_check": {"path": "p", "sha256": HEX, "passed": True}}, "model_check")

    def test_app_log_head_types(self):
        self._refused({"app_log_head": {"records": -1, "head": HEX}}, "app_log_head")
        self._refused({"app_log_head": {"records": True, "head": HEX}}, "app_log_head")
        self._refused({"app_log_head": {"records": 1, "head": "abc"}}, "app_log_head")

    def test_model_digests_need_bare_hex(self):
        self._refused({"model_digests": {"recon": "sha256:" + HEX}}, "model_digests")

    def test_model_families_need_strings(self):
        self._refused({"model_families": {"recon": 3}}, "model_families")

    def test_reading3_prompt_sha_must_be_hex(self):
        self._refused({"reading3_prompt_sha256": "9b48f863"}, "reading3_prompt_sha256")
        self._refused({"reading3_prompt_sha256": HEX.upper()}, "reading3_prompt_sha256")

    def test_tool_must_be_dict(self):
        with self.assertRaises(ValueError):
            toolcfg.check_tool([("reference_type", "A")], {})
        for bad in (None, []):
            with self.assertRaises(ValueError):
                RunConfig.load(DEMO, tool=bad)

    def test_full_tool_not_mutated(self):
        t = _full_tool()
        before = copy.deepcopy(t)
        toolcfg.check_tool(t, {})
        self.assertEqual(t, before)


class TestIsTool(unittest.TestCase):
    def test_is_tool(self):
        self.assertFalse(toolcfg.is_tool(RunConfig.load(DEMO)))
        self.assertTrue(toolcfg.is_tool(RunConfig.load(DEMO, tool={"reference_type": "C"})))


class TestRunStartFieldsReading3(unittest.TestCase):
    """Audit K10: ``tool.reading3_prompt_sha256``, when present, must equal the live no-interview prompt's hash."""
    PROMPTS = ROOT / "prompts"

    def live(self):
        return hashlib.sha256((self.PROMPTS / toolcfg.READING3_PROMPT_FILE).read_bytes()).hexdigest()

    def test_matching_value_runs(self):
        out = toolcfg.run_start_fields({"reading3_prompt_sha256": self.live()}, self.PROMPTS)
        self.assertEqual(out["reading3_prompt_sha256"], self.live())
        self.assertTrue(self.live().startswith("9b48f863"))

    def test_mismatching_value_refused(self):
        with self.assertRaises(ValueError) as cm:
            toolcfg.run_start_fields({"reading3_prompt_sha256": HEX, "reference_type": "A"}, self.PROMPTS)
        self.assertIn("reading3_prompt_sha256", str(cm.exception))
        self.assertIn(self.live(), str(cm.exception))

    def test_absent_value_logs_live_hash(self):
        out = toolcfg.run_start_fields({"reference_type": "A"}, self.PROMPTS)
        self.assertEqual(out, {"reading3_prompt_sha256": self.live()})

    def test_study_config_unchanged(self):
        self.assertEqual(toolcfg.run_start_fields({}, self.PROMPTS), {})


if __name__ == "__main__":
    unittest.main()
