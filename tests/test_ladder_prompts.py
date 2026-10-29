"""Ladder item 2/3: the remedy and placebo prompt files differ from reconstruct.p0.v2 in instruction text only."""
import hashlib
import json
import re
import unittest
from collections import Counter
from pathlib import Path

from second_scribe.prompt_manifest import file_hashes, verify_manifest
from second_scribe.reconstruct import OUTPUT_SCHEMA_TEXT, ReconContext, build_messages
from second_scribe.synth.demos import render_demos
from tests.test_reconstruct import CTX, DEFS

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
V2 = "reconstruct.p0.v2.txt"
V1 = "reconstruct.p0.v1.txt"
REMEDY = "reconstruct.p0.remedy.v1.txt"
PLACEBO = "reconstruct.p0.placebo.v1.txt"
RUNG1_SHA = "4b17dd7827b2de459229e931a4195efc5cb2f256d1cd8120235a46035c43584a"   # reconstruct.p0.v1.txt at a92a922
_PLACEHOLDER = re.compile(r"\{(role|domain|phases|schema|transcript|definitions)\}")
SYSTEM = (PROMPTS / "system.reconstruct.v2.txt").read_text(encoding="utf-8").strip()

# the v2 lines the remedy / placebo are allowed to reword; every other v2 line must survive byte for byte
# run 2 added the node-type line after {schema}, which moved the "TRANSCRIPT:" line from index 14 to 15
REMEDY_EDITED = {2, 5, 6, 8, 15}
PLACEBO_EDITED = {2, 15}


def text(name):
    return (PROMPTS / name).read_text(encoding="utf-8")


def _lines_without_demos(name, arm):
    t = text(name)
    block = render_demos(arm)
    assert t.count(block) == 1, name
    return t.replace(block + "\n", "", 1).split("\n")


class TestLadderPromptFiles(unittest.TestCase):
    def test_placeholder_set_is_identical_to_v2(self):
        base = Counter(_PLACEHOLDER.findall(text(V2)))
        for name in (REMEDY, PLACEBO):
            self.assertEqual(Counter(_PLACEHOLDER.findall(text(name))), base, name)

    def test_schema_and_reminder_segments_are_byte_identical(self):
        v2 = text(V2).split("\n")
        schema = [l for l in v2 if l == "{schema}"]
        reminder = [l for l in v2 if l.startswith("REMINDER:")]
        ret = [l for l in v2 if l.startswith("Return only JSON")]
        self.assertEqual((len(schema), len(reminder), len(ret)), (1, 1, 1))
        for name in (REMEDY, PLACEBO):
            t = text(name).split("\n")
            self.assertEqual([l for l in t if l == "{schema}"], schema, name)
            self.assertEqual([l for l in t if l.startswith("REMINDER:")], reminder, name)
            self.assertEqual([l for l in t if l.startswith("Return only JSON")], ret, name)
            self.assertTrue(text(name).endswith(v2[-1]), name)       # the file ends with the v2 reminder, same bytes

    def test_only_the_named_instruction_lines_differ_from_v2(self):
        v2 = text(V2).split("\n")
        for name, arm, edited in ((REMEDY, "remedy", REMEDY_EDITED), (PLACEBO, "placebo", PLACEBO_EDITED)):
            rest = _lines_without_demos(name, arm)
            self.assertEqual(len(rest), len(v2), name)
            for i, (a, b) in enumerate(zip(v2, rest)):
                if i in edited:
                    self.assertNotEqual(a, b, (name, i))
                else:
                    self.assertEqual(a, b, (name, i))

    def test_demos_are_embedded_literally_at_the_same_position(self):
        for name, arm, other in ((REMEDY, "remedy", "placebo"), (PLACEBO, "placebo", "remedy")):
            t = text(name)
            self.assertEqual(t.count(render_demos(arm)), 1, name)
            self.assertNotIn(render_demos(other), t)
        r_pre, r_post = text(REMEDY).split(render_demos("remedy"))
        p_pre, p_post = text(PLACEBO).split(render_demos("placebo"))
        # the line that precedes the block (a rule that is not reworded) and the line that follows it are shared
        self.assertEqual(r_pre.split("\n")[-2], p_pre.split("\n")[-2])
        self.assertEqual(r_post.split("\n")[1], p_post.split("\n")[1])
        self.assertTrue(r_post.split("\n")[1].startswith("Return only JSON"))
        self.assertEqual(len(r_pre.split("\n")), len(p_pre.split("\n")))

    def test_remedy_has_the_narrator_frame_and_placebo_does_not(self):
        r, p = text(REMEDY), text(PLACEBO)
        self.assertIn("\nThe participant says:\n{transcript}\n", r)
        self.assertNotIn("TRANSCRIPT:", r)
        self.assertGreaterEqual(r.lower().count("according to the participant"), 3)
        self.assertNotIn("The participant says:", p)
        self.assertNotIn("according to", p.lower())
        self.assertIn("\nThe transcript follows:\n{transcript}\n", p)
        self.assertNotIn("TRANSCRIPT:", p)
        # the demos' third-person notes are the remedy's only other narrator wording
        self.assertNotIn("The participant says", p)

    def test_placebo_frame_claims_nothing_about_the_transcript(self):
        # review M3: no claim about fidelity ("edited", "cleaned"), language ("English") or provenance ("recording")
        added = [l for l in text(PLACEBO).split("\n")
                 if l not in set(text(V2).split("\n")) and "Example" not in l and not l.startswith(("[", "{", " ", "Note:"))]
        self.assertTrue(added)
        for line in added:
            for word in ("edited", "cleaned", "english", "recording", "recorded", "lightly", "verbatim", "accurate",
                         "transcrib", "language"):
                self.assertNotIn(word, line.lower(), line)
        self.assertIn("from the first turn to the last", text(PLACEBO))

    def test_remedy_vs_placebo_length_within_two_percent(self):
        a, b = len(text(REMEDY)), len(text(PLACEBO))
        self.assertLessEqual(abs(a - b) / a, 0.02, (a, b))

    def test_built_messages_are_complete_and_leave_demo_braces_alone(self):
        for name, arm in ((REMEDY, "remedy"), (PLACEBO, "placebo")):
            m = build_messages(text(name), CTX, "[I] hi\n[P] I went to the office and waited a long time.",
                               system=SYSTEM, definitions=DEFS)
            self.assertEqual([x["role"] for x in m], ["system", "user"])
            user = m[1]["content"]
            self.assertIsNone(_PLACEHOLDER.search(user), name)
            self.assertIn(render_demos(arm), user)
            schema = OUTPUT_SCHEMA_TEXT.replace("{phases}", ", ".join(CTX.phases))
            self.assertIn(schema, user)
            self.assertIn(DEFS, user)
            self.assertIn("[P] I went to the office and waited a long time.", user)

    def test_system_message_is_the_v2_one(self):
        m = build_messages(text(REMEDY), CTX, "x", system=SYSTEM, definitions=DEFS)
        self.assertEqual(m[0]["content"], SYSTEM)


class TestRungOnePinned(unittest.TestCase):
    def test_rung1_prompt_is_the_unchanged_v1_file(self):
        digest = hashlib.sha256((PROMPTS / V1).read_bytes()).hexdigest()
        self.assertEqual(digest, RUNG1_SHA)
        manifest = json.loads((PROMPTS / "MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["files"][V1], RUNG1_SHA)
        self.assertEqual(file_hashes(PROMPTS)[V1], RUNG1_SHA)


if __name__ == "__main__":
    unittest.main()
