"""Ladder item 5: the rung registry (pure data) and its validator."""
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from second_scribe.ladder import (PINNED_SHA256, REAL_DATA_RULE, RUNGS, Rung, check_rung_spec, get_rung,
                                  load_rung_prompt, rung_messages, validate_rungs)
from second_scribe.prompt_manifest import write_manifest
from second_scribe.prompts import load_definitions, load_system
from second_scribe.reconstruct import ReconSpec, build_messages, load_prompt
from second_scribe.metrics import PRIMARY_SPEC
from second_scribe.synth.demos import render_demos
from tests.test_reconstruct import CTX

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"


class TestRegistry(unittest.TestCase):
    def test_four_rungs_in_ladder_order(self):
        self.assertEqual([r.key for r in RUNGS], ["rung1", "rung2", "rung3", "placebo"])
        self.assertEqual([r.name for r in RUNGS], ["first_draft", "good_practice", "remedy", "placebo"])
        self.assertEqual(len({r.label for r in RUNGS}), 4)

    def test_prompt_files(self):
        files = {r.key: r.prompt_file for r in RUNGS}
        self.assertEqual(files, {"rung1": "reconstruct.p0.v1.txt", "rung2": "reconstruct.p0.v2.txt",
                                 "rung3": "reconstruct.p0.remedy.v1.txt", "placebo": "reconstruct.p0.placebo.v1.txt"})

    def test_every_rung_runs_the_primary_setting_only_on_real_data(self):
        self.assertEqual(REAL_DATA_RULE, "primary setting only")
        for r in RUNGS:
            self.assertEqual(r.real_data, "primary setting only", r.key)
            self.assertEqual(r.real_data_specs, (PRIMARY_SPEC,), r.key)

    def test_rung1_is_the_bare_v1_prompt_and_the_rest_share_the_v2_system_and_definitions(self):
        r1 = get_rung("rung1")
        self.assertIsNone(r1.system_file)
        self.assertIsNone(r1.definitions_file)
        for key in ("rung2", "rung3", "placebo"):
            r = get_rung(key)
            self.assertEqual((r.system_file, r.definitions_file), ("system.reconstruct.v2.txt", "definitions.v2.txt"))

    def test_demo_arms_and_the_placebo_requirement(self):
        self.assertEqual({r.key: r.demo_arm for r in RUNGS},
                         {"rung1": None, "rung2": None, "rung3": "remedy", "placebo": "placebo"})
        self.assertEqual(get_rung("rung3").requires, ("placebo",))
        self.assertEqual(get_rung("rung2").requires, ())

    def test_get_rung_unknown(self):
        with self.assertRaises(KeyError):
            get_rung("rung9")

    def test_registry_is_immutable_data(self):
        self.assertIsInstance(RUNGS, tuple)
        with self.assertRaises(Exception):
            RUNGS[0].key = "x"


class TestValidator(unittest.TestCase):
    def copy_prompts(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        for p in PROMPTS.iterdir():
            if p.is_file():
                shutil.copyfile(p, Path(d.name) / p.name)
        return Path(d.name)

    def test_the_real_registry_validates_against_the_real_prompts(self):
        self.assertEqual(validate_rungs(PROMPTS), [])

    def test_missing_file_is_reported(self):
        d = self.copy_prompts()
        (d / "reconstruct.p0.placebo.v1.txt").unlink()
        self.assertTrue(any("reconstruct.p0.placebo.v1.txt" in p for p in validate_rungs(d)))

    def test_edited_file_breaks_the_manifest_check(self):
        d = self.copy_prompts()
        f = d / "reconstruct.p0.v1.txt"
        f.write_text(f.read_text(encoding="utf-8") + " ", encoding="utf-8")
        self.assertTrue(any("reconstruct.p0.v1.txt" in p for p in validate_rungs(d)))

    def test_rung3_without_its_placebo_is_reported(self):
        probs = validate_rungs(PROMPTS, [r for r in RUNGS if r.key != "placebo"])
        self.assertTrue(any("placebo" in p for p in probs))

    def test_duplicate_key_or_label_and_wrong_real_data_rule_are_reported(self):
        r2 = get_rung("rung2")
        self.assertTrue(any("duplicate" in p for p in validate_rungs(PROMPTS, list(RUNGS) + [r2])))
        probs = validate_rungs(PROMPTS, [replace(r, label="same") if r.key in ("rung1", "rung2") else r for r in RUNGS])
        self.assertTrue(any("label" in p for p in probs))
        probs = validate_rungs(PROMPTS, [replace(r, real_data="full grid") if r.key == "rung2" else r for r in RUNGS])
        self.assertTrue(any("real_data" in p for p in probs))

    def test_demo_arm_must_match_the_literal_block_in_the_file(self):
        swapped = [replace(r, demo_arm="placebo") if r.key == "rung3" else r for r in RUNGS]
        self.assertTrue(any("rung3" in p for p in validate_rungs(PROMPTS, swapped)))
        wrongly_none = [replace(r, demo_arm=None) if r.key == "placebo" else r for r in RUNGS]
        self.assertTrue(any("placebo" in p for p in validate_rungs(PROMPTS, wrongly_none)))

    def test_placeholder_drift_is_reported(self):
        d = self.copy_prompts()
        f = d / "reconstruct.p0.remedy.v1.txt"
        f.write_text(f.read_text(encoding="utf-8").replace("{definitions}", ""), encoding="utf-8")
        probs = validate_rungs(d)
        self.assertTrue(any("placeholder" in p for p in probs))

    def test_a_remedy_rung_must_require_a_placebo_specifically(self):
        # requires an existing rung, but not a placebo one: only the "remedy needs a placebo" rule can object
        bad = [replace(r, requires=("rung2",)) if r.key == "rung3" else r for r in RUNGS]
        probs = validate_rungs(PROMPTS, bad)
        self.assertEqual(len(probs), 1, probs)
        self.assertIn("placebo", probs[0])
        none = [replace(r, requires=()) if r.key == "rung3" else r for r in RUNGS]
        self.assertTrue(any("placebo" in p for p in validate_rungs(PROMPTS, none)))

    def test_placeholder_set_must_equal_v2_even_when_required_ones_are_present(self):
        d = self.copy_prompts()
        f = d / "reconstruct.p0.remedy.v1.txt"
        # one extra {role}: every required placeholder is still there, only the Counter differs from v2
        f.write_text(f.read_text(encoding="utf-8").replace("The interview is with a {role}.",
                                                           "The interview is with a {role} ({role}).", 1),
                     encoding="utf-8")
        write_manifest(d)
        probs = validate_rungs(d)
        self.assertEqual([p for p in probs if "placeholder set differs" in p],
                         ["rung3: placeholder set differs from reconstruct.p0.v2.txt"])

    def test_remedy_placebo_length_gap_is_reported(self):
        d = self.copy_prompts()
        f = d / "reconstruct.p0.placebo.v1.txt"
        f.write_text(f.read_text(encoding="utf-8") + "x" * 400, encoding="utf-8")
        probs = validate_rungs(d)
        self.assertTrue(any("2%" in p for p in probs))


class TestLoading(unittest.TestCase):
    def test_load_rung_prompt_reads_the_registered_file(self):
        for r in RUNGS:
            self.assertEqual(load_rung_prompt(PROMPTS, r), (PROMPTS / r.prompt_file).read_text(encoding="utf-8"))

    def test_rung_messages_shape_per_rung(self):
        t = "[I] hi\n[P] I went to the office and waited a long time."
        m1 = rung_messages(PROMPTS, get_rung("rung1"), CTX, t)
        self.assertEqual([x["role"] for x in m1], ["user"])
        for key in ("rung2", "rung3", "placebo"):
            m = rung_messages(PROMPTS, get_rung(key), CTX, t)
            self.assertEqual([x["role"] for x in m], ["system", "user"], key)
            self.assertIn(t, m[1]["content"])
        self.assertIn(render_demos("remedy"), rung_messages(PROMPTS, get_rung("rung3"), CTX, t)[1]["content"])
        self.assertNotIn("Example 1", rung_messages(PROMPTS, get_rung("rung2"), CTX, t)[1]["content"])
        self.assertIsInstance(get_rung("rung1"), Rung)


class TestMessagesEqualThePipeline(unittest.TestCase):
    """Review I3: only the instruction text changes, so rung 1 and rung 2 must send exactly what the pipeline
    sends today for p0 (v1 and v2)."""
    T = "[I] hi\n[P] I went to the office and waited a long time."

    def test_rung2_equals_the_pipeline_p0_v2_messages(self):
        expected = build_messages(load_prompt(PROMPTS, "reconstruct", "p0", version="v2"), CTX, self.T,
                                  system=load_system(PROMPTS, "reconstruct", "v2"),
                                  definitions=load_definitions(PROMPTS, "v2"))
        self.assertEqual(rung_messages(PROMPTS, get_rung("rung2"), CTX, self.T), expected)

    def test_rung1_equals_the_pipeline_p0_v1_messages(self):
        expected = build_messages(load_prompt(PROMPTS, "reconstruct", "p0", version="v1"), CTX, self.T)
        self.assertEqual(rung_messages(PROMPTS, get_rung("rung1"), CTX, self.T), expected)

    def test_rung3_and_placebo_share_the_system_message_and_definitions_with_rung2(self):
        base = rung_messages(PROMPTS, get_rung("rung2"), CTX, self.T)
        for key in ("rung3", "placebo"):
            m = rung_messages(PROMPTS, get_rung(key), CTX, self.T)
            self.assertEqual(m[0], base[0], key)
            self.assertIn(load_definitions(PROMPTS, "v2"), m[1]["content"])


class TestVariantAndRealDataGuard(unittest.TestCase):
    """Review I4: the registry is p0-only and says so; a p1/p2 spec cannot run under it silently."""

    def test_every_rung_carries_variant_p0(self):
        self.assertEqual({r.key: r.variant for r in RUNGS}, {k: "p0" for k in ("rung1", "rung2", "rung3", "placebo")})

    def test_check_rung_spec_rejects_other_variants(self):
        for r in RUNGS:
            check_rung_spec(r, ReconSpec("removed", "p0", 0.0))
            for v in ("p1", "p2"):
                with self.assertRaises(ValueError):
                    check_rung_spec(r, ReconSpec("removed", v, 0.0))

    def test_on_real_data_only_the_primary_spec_is_allowed(self):
        r = get_rung("rung3")
        check_rung_spec(r, ReconSpec("removed", "p0", 0.0), real_data=True)
        for bad in (ReconSpec("full", "p0", 0.0), ReconSpec("removed", "p0", 0.7), ReconSpec("removed", "p0", 0.7, 1)):
            check_rung_spec(r, bad)                                    # synthetic: any p0 spec is fine
            with self.assertRaises(ValueError):
                check_rung_spec(r, bad, real_data=True)

    def test_on_real_data_only_draw_0_of_the_primary_setting_is_allowed(self):
        # the primary setting is T=0 (greedy): extra draws would be copies (DECISIONS C7), so only r0 passes
        r = get_rung("rung3")
        check_rung_spec(r, ReconSpec("removed", "p0", 0.0, 0), real_data=True)
        with self.assertRaises(ValueError):
            check_rung_spec(r, ReconSpec("full", "p0", 0.0, 0), real_data=True)

    def test_a_replicate_of_a_temperature_0_spec_is_refused_on_synthetic_and_real_data(self):
        for r in RUNGS:
            for real in (False, True):
                for k in (1, 2):
                    with self.assertRaises(ValueError) as cm:
                        check_rung_spec(r, ReconSpec("removed", "p0", 0.0, k), real_data=real)
                    msg = str(cm.exception)
                    self.assertIn("temperature 0", msg)
                    self.assertIn("greedy", msg)
        # also with a raised max_draws: the greedy refusal does not depend on the declared draw count
        with self.assertRaises(ValueError):
            check_rung_spec(get_rung("rung2"), ReconSpec("removed", "p0", 0.0, 1), real_data=True, max_draws=3)

    def test_replicates_at_a_sampling_temperature_pass_on_synthetic_data(self):
        for r in RUNGS:
            for k in (0, 1, 2):
                check_rung_spec(r, ReconSpec("removed", "p0", 0.7, k))
                check_rung_spec(r, ReconSpec("full", "p0", 0.7, k))
        # real data still takes the T=0 primary setting only, whatever the replicate
        with self.assertRaises(ValueError):
            check_rung_spec(get_rung("rung3"), ReconSpec("removed", "p0", 0.7, 1), real_data=True)

    def test_rung_messages_checks_the_spec_when_given(self):
        with self.assertRaises(ValueError):
            rung_messages(PROMPTS, get_rung("rung2"), CTX, "x", spec=ReconSpec("removed", "p1", 0.0))
        rung_messages(PROMPTS, get_rung("rung2"), CTX, "x", spec=ReconSpec("removed", "p0", 0.0))

    def test_validator_reports_variant_drift(self):
        bad = [replace(r, variant="p1") if r.key == "rung2" else r for r in RUNGS]
        self.assertTrue(any("variant" in p for p in validate_rungs(PROMPTS, bad)))
        bad = [replace(r, real_data_specs=("removed__p1__t0__r0",)) if r.key == "rung3" else r for r in RUNGS]
        self.assertTrue(any("real_data_specs" in p for p in validate_rungs(PROMPTS, bad)))


class TestPinnedHashes(unittest.TestCase):
    """Review M6: the rung 1 / rung 2 wordings are pinned in the registry, so regenerating the manifest after an
    edit cannot pass the pre-run gate."""

    def test_pins_cover_v1_v2_system_and_definitions(self):
        self.assertEqual(set(PINNED_SHA256), {"reconstruct.p0.v1.txt", "reconstruct.p0.v2.txt",
                                              "system.reconstruct.v2.txt", "definitions.v2.txt"})

    def test_edit_plus_manifest_regeneration_is_still_caught(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        for p in PROMPTS.iterdir():
            if p.is_file():
                shutil.copyfile(p, Path(d.name) / p.name)
        f = Path(d.name) / "reconstruct.p0.v1.txt"
        f.write_text(f.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        write_manifest(d.name)
        self.assertTrue(any("pinned" in p and "reconstruct.p0.v1.txt" in p for p in validate_rungs(d.name)))


if __name__ == "__main__":
    unittest.main()
