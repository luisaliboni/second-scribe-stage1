import unittest

from second_scribe.families import known_family, model_family, script_family


class TestFamilies(unittest.TestCase):
    def test_families(self):
        self.assertEqual(model_family("org/Qwen3-32B"), "qwen")
        self.assertEqual(model_family("mtplx-qwen38-flashnext"), "qwen")
        self.assertEqual(model_family("deepseek-v4-flash-0731"), "deepseek")
        self.assertEqual(model_family("gemma-4-31b"), "gemma")
        self.assertEqual(model_family("some-new-model-1"), "some-new-model-1")      # unknown: a family of its own
        self.assertIsNone(model_family(None))
        self.assertIsNone(model_family("  "))

    def test_other_only_when_the_log_shows_a_different_non_qwen_family(self):
        self.assertEqual(script_family(["deepseek-v4"], ["mtplx-qwen38"])[0], "other")
        self.assertEqual(script_family(["gemma-4-31b"], ["Qwen3-8B", "qwen3-8b-b"])[0], "other")

    def test_everything_else_is_own(self):
        for script, recon, code in ((["qwen3-32b"], ["qwen3-8b"], "script_is_qwen_family"),
                                    (["deepseek-a"], ["deepseek-b"], "same_model_or_family"),
                                    (["m"], ["m"], "same_model_or_family"),
                                    ([], ["qwen3"], "script_model_not_on_record"),
                                    ([None], ["qwen3"], "script_model_not_on_record"),
                                    (["deepseek-a", "gemma-b"], ["qwen3"], "several_script_models"),
                                    (["deepseek-a"], [], "recon_model_not_on_record"),
                                    (["deepseek-a"], [None], "recon_model_not_on_record")):
            self.assertEqual(script_family(script, recon), ("own", code), (script, recon))

    def test_an_alias_or_unknown_id_never_counts_as_another_family(self):
        """Review M-3 (families): a server that ignores the model field can be served under any alias; an id the
        table does not know says nothing about the family, so the label fails closed."""
        for script, recon in ((["judge"], ["qwen3.8-flash"]),                  # the judge alias of the reviewer's probe
                              (["some-new-model-1"], ["qwen3"]),
                              (["deepseek-v4"], ["local-recon"]),              # the reconstructor served under an alias
                              (["local-judge"], ["local-recon"]),
                              (["gemma-4-31b"], ["qwen3", "mystery"])):        # one unknown among the recon ids
            self.assertEqual(script_family(script, recon), ("own", "family_unknown"), (script, recon))
        self.assertEqual(known_family("deepseek-v4"), "deepseek")
        self.assertIsNone(known_family("judge"))
        self.assertIsNone(known_family(None))

    def test_a_qwen_script_is_still_reported_as_qwen_even_with_an_unknown_recon(self):
        self.assertEqual(script_family(["qwen3-32b"], ["alias"]), ("own", "script_is_qwen_family"))

    def test_a_path_in_the_id_is_ignored(self):
        self.assertEqual(script_family(["/models/deepseek-x.gguf"], ["/m/qwen3.gguf"])[0], "other")


if __name__ == "__main__":
    unittest.main()
