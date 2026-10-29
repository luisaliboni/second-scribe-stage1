"""Task 5B: reconstruction prompt v2 (sandwich layout, shared definitions, system message, frozen wordings)."""
import json
import re
import tempfile
import unittest
from pathlib import Path

from second_scribe.audit.registry import AuditLog
from second_scribe.llm import MockClient, ModelResponse
from second_scribe.reconstruct import (ReconContext, ReconSpec, build_messages, load_definitions, load_prompt,
                                       load_system, reconstruct)
from second_scribe import prompts as prompts_mod
from tests.test_reconstruct import CTX, GOOD, PROMPTS, T, variant

DEFS = load_definitions(PROMPTS)


class TestPromptV2(unittest.TestCase):
    def setUp(self): self.log = AuditLog(Path(tempfile.mkdtemp()) / "r.jsonl", "r", clock=lambda: "t")

    def msgs(self, kind, v, transcript="[P] I went to the office.\n[I] Did you wait?"):
        return build_messages(load_prompt(PROMPTS, kind, v), CTX, transcript if kind == "reconstruct" else None,
                              system=load_system(PROMPTS, "reconstruct"), definitions=DEFS)

    def test_default_version_is_v2_and_v1_is_still_loadable(self):
        self.assertEqual(load_prompt(PROMPTS, "reconstruct", "p0"),
                         (PROMPTS / "reconstruct.p0.v2.txt").read_text(encoding="utf-8"))
        self.assertEqual(load_prompt(PROMPTS, "reconstruct", "p0", version="v1"),
                         (PROMPTS / "reconstruct.p0.v1.txt").read_text(encoding="utf-8"))
        for bad in ("../v2", "2", "v", "v2/../x"):
            with self.assertRaises(ValueError):
                load_prompt(PROMPTS, "reconstruct", "p0", version=bad)

    def test_loaders_live_in_prompts_module_and_reconstruct_reexports_them(self):
        from second_scribe import reconstruct
        self.assertIs(reconstruct.load_system, prompts_mod.load_system)
        self.assertIs(reconstruct.load_definitions, prompts_mod.load_definitions)
        self.assertEqual(prompts_mod.check_version("v12"), "v12")
        for bad in ("../v2", "v", "2", "v2\n"):
            with self.assertRaises(ValueError):
                prompts_mod.check_version(bad)
        with self.assertRaises(ValueError):
            prompts_mod.load_system(PROMPTS, "other")

    def test_system_message_is_first_and_never_empty(self):
        for kind in ("reconstruct", "prior_only"):
            for v in ("p0", "p1", "p2"):
                m = self.msgs(kind, v)
                self.assertEqual([x["role"] for x in m], ["system", "user"], (kind, v))
                self.assertEqual(m[0]["content"], load_system(PROMPTS, "reconstruct"))
        sysmsg = load_system(PROMPTS, "reconstruct").strip()
        self.assertTrue(sysmsg)
        self.assertLess(len(sysmsg.split()), 60)       # one short paragraph
        self.assertNotIn("\n\n", sysmsg)

    def test_build_messages_without_system_has_only_the_user_turn(self):
        m = build_messages(load_prompt(PROMPTS, "prior_only", "p0"), CTX, None)
        self.assertEqual([x["role"] for x in m], ["user"])

    def test_sandwich_rules_then_transcript_then_reminder(self):
        t = "[P] I went to the office.\n[I] Did you wait?"
        for v in ("p0", "p1", "p2"):
            body = self.msgs("reconstruct", v, t)[-1]["content"]
            i_rules, i_schema = body.index("one node"), body.index('"nodes"')
            i_tr, i_text, i_rem = body.index("TRANSCRIPT:"), body.index(t), body.rindex("REMINDER:")
            self.assertTrue(i_rules < i_schema < i_tr < i_text < i_rem, v)
            self.assertEqual(body.count("TRANSCRIPT:"), 1, v)
            self.assertTrue(body.rstrip().endswith("Begin your answer with {"), v)
            self.assertIn("Begin your answer with {", body[:i_tr], f"{v}: format rule also before the transcript")

    def test_reminder_is_short_and_restates_the_critical_rules(self):
        for v in ("p0", "p1", "p2"):
            rem = load_prompt(PROMPTS, "reconstruct", v).split("REMINDER:")[1]
            self.assertLessEqual(len(rem.split()), 80, v)
            low = rem.lower()
            for phrase in ("one turn", "one speaker", "someone else", "might have happened", "wished", "json"):
                self.assertIn(phrase, low, (v, phrase))
            self.assertTrue(rem.rstrip().endswith("Begin your answer with {"), v)
        for v in ("p0", "p1", "p2"):
            rem = load_prompt(PROMPTS, "prior_only", v).split("REMINDER:")[1]
            self.assertLessEqual(len(rem.split()), 80, v)
            self.assertTrue(rem.rstrip().endswith("Begin your answer with {"), v)

    def test_prior_only_has_reminder_and_no_transcript_block(self):
        for v in ("p0", "p1", "p2"):
            body = self.msgs("prior_only", v)[-1]["content"]
            self.assertNotIn("TRANSCRIPT:", body)
            self.assertIn("REMINDER:", body)
            self.assertTrue(body.rstrip().endswith("Begin your answer with {"), v)
            self.assertNotIn("wished", body.lower())

    def test_definitions_are_the_shared_text_in_every_reconstruct_prompt(self):
        d = (PROMPTS / "definitions.v2.txt").read_text(encoding="utf-8").strip()
        self.assertEqual(DEFS.strip(), d)
        for v in ("p0", "p1", "p2"):
            self.assertIn("{definitions}", load_prompt(PROMPTS, "reconstruct", v))
            self.assertIn(d, self.msgs("reconstruct", v)[-1]["content"], v)
        low = d.lower()
        for phrase in ("happened to them", "existed for them", "wanted or needed this",
                       "should have existed for them", "did not happen to them", "happened only to someone else",
                       "might have happened", "everything experienced or wished is part of this person's journey",
                       "unless they wanted or needed it themselves (then it is wished)"):
            self.assertIn(phrase, low)
        self.assertNotIn("create a node", low)       # the instruction lives in each prompt, not the shared file
        for ph in ("{role}", "{domain}", "{phases}", "{schema}", "{transcript}"):
            self.assertNotIn(ph, d)

    def test_build_messages_requires_definitions_when_the_prompt_has_the_slot(self):
        with self.assertRaises(ValueError):
            build_messages(load_prompt(PROMPTS, "reconstruct", "p0"), CTX, "x")

    def test_transcript_text_is_never_expanded(self):
        raw = "[P] I said {definitions} and {schema} here."
        self.assertIn(raw, self.msgs("reconstruct", "p1", raw)[-1]["content"])

    def test_quote_rule_is_stated_generically(self):
        for v in ("p0", "p1", "p2"):
            low = load_prompt(PROMPTS, "reconstruct", v).lower()
            for phrase in ("never join", "interviewer", "participant's own words", "[i]", "exactly"):
                self.assertIn(phrase, low, (v, phrase))
            self.assertTrue("word for word" in low or "verbatim" in low, v)

    def test_no_node_instruction_follows_the_definitions_in_every_reconstruct_prompt(self):
        rule = "- Make no node or edge for something that is not part of this person's journey."
        for v in ("p0", "p1", "p2"):
            p = load_prompt(PROMPTS, "reconstruct", v)
            self.assertIn("{definitions}\n" + (rule.replace("- ", "6. ", 1) if v == "p1" else rule), p, v)

    def test_scope_phrase_and_reminder_push_inclusion_as_well_as_exclusion(self):
        for v in ("p0", "p1", "p2"):
            p = load_prompt(PROMPTS, "reconstruct", v)
            self.assertIn("the person says was part of their journey", p, v)
            rem = p.split("REMINDER:")[1].lower()
            self.assertIn("was part of their journey", rem, v)
            self.assertIn("general knowledge", rem, v)
            self.assertIn("happened only to someone else", rem, v)
            self.assertIn("wanted or needed it themselves", rem, v)
            self.assertNotIn("for themselves", rem, v)

    def test_wordings_agree_on_edges_granularity_and_reading(self):
        for v in ("p0", "p1", "p2"):
            low = load_prompt(PROMPTS, "reconstruct", v).lower()
            self.assertIn("one node", low, v)
            self.assertIn("read the whole", low, v)
        self.assertIn("keep different things in different nodes", load_prompt(PROMPTS, "reconstruct", "p0"))
        self.assertIn("Do not merge different things", load_prompt(PROMPTS, "reconstruct", "p1"))
        self.assertIn("Connect two nodes with a directed edge, pointing to the node that the other led to, whenever "
                      "the person says one led to the other.", load_prompt(PROMPTS, "reconstruct", "p1"))
        self.assertIn("- Whenever the person says one node led to another, add a directed edge from that node to the "
                      "one it led to.", load_prompt(PROMPTS, "reconstruct", "p0"))
        self.assertIn("Whenever the person says one node led to another, draw an edge pointing from the first to "
                      "the second.", load_prompt(PROMPTS, "reconstruct", "p2"))
        self.assertIn("keep different things in different nodes", load_prompt(PROMPTS, "prior_only", "p0"))
        self.assertIn("Connect two nodes with a directed edge when the official pathway leads from one to the other.",
                      load_prompt(PROMPTS, "prior_only", "p1"))
        self.assertIn("An edge points from a node to another node it leads to in the pathway.",
                      load_prompt(PROMPTS, "prior_only", "p2"))
        self.assertIn("; never merge different things into one node.", load_prompt(PROMPTS, "prior_only", "p2"))
        for v in ("p0", "p1", "p2"):
            self.assertIn("Every node and edge is part of the official pathway", load_prompt(PROMPTS, "prior_only", v), v)

    def test_domain_is_a_noun_phrase_in_every_wording(self):
        ctx = ReconContext(domain="food-truck licensing in a mid-sized city", role="food-truck owner",
                           phases=("A", "B"))
        for kind in ("reconstruct", "prior_only"):
            for v in ("p0", "p1", "p2"):
                raw = load_prompt(PROMPTS, kind, v)
                for bad in ("when {domain}", "who is {domain}", "goes through {domain}"):
                    self.assertNotIn(bad, raw, (kind, v))
                body = build_messages(raw, ctx, "[P] x" if kind == "reconstruct" else None,
                                      definitions=DEFS)[-1]["content"]
                self.assertRegex(body, r"\b(about|in|on|dealing with) food-truck licensing in a mid-sized city",
                                 (kind, v))
        raw = {v: load_prompt(PROMPTS, "prior_only", v) for v in ("p0", "p1", "p2")}
        self.assertIn("pathway of a {role} in {domain}, as official guidance", raw["p0"])
        self.assertIn("official guidance document on {domain} for a {role}.", raw["p1"])
        self.assertIn("official pathway of a {role} in {domain}, as official guidance", raw["p2"])

    def test_round3_exact_phrases(self):
        r = {v: load_prompt(PROMPTS, "reconstruct", v) for v in ("p0", "p1", "p2")}
        self.assertIn("Stay with what the participant says, and add nothing from general knowledge.", r["p1"])
        self.assertNotIn("do not add steps", r["p1"])
        self.assertIn("REMINDER: Everything the participant says was part of their journey", r["p1"])
        self.assertIn("Each quote is copied exactly from one turn of one speaker;", r["p1"])
        self.assertNotIn("One quote comes", r["p1"])
        self.assertIn("- If the person says or clearly implies how the journey felt overall, record that as one word; "
                      "otherwise use null.", r["p2"])
        self.assertIn("- Record one word for how the journey typically feels, or null.",
                      load_prompt(PROMPTS, "prior_only", "p2"))

    def test_wished_is_judged_only_from_what_the_participant_says(self):
        d = (PROMPTS / "definitions.v2.txt").read_text(encoding="utf-8")
        self.assertIn("- wished: the participant says they wanted or needed this, or that it should have existed "
                      "for them, and does not say that it happened or existed for them.", d)
        self.assertNotIn("and it did not happen", d)

    def test_no_prompt_cue_that_the_kit_checks_speakers(self):
        for f in sorted(PROMPTS.glob("*.v2.txt")):
            low = f.read_text(encoding="utf-8").lower()
            for cue in (r"\bthe kit\b", "tracks speakers", "tracks who spoke", "records the speaker"):
                self.assertIsNone(re.search(cue, low), f"{f.name}: {cue}")

    def test_quote_rule_says_do_not_reword_or_shorten_in_every_wording(self):
        self.assertIn("do not reword it, leave out words in the middle, or invent it",
                      load_prompt(PROMPTS, "reconstruct", "p0"))
        self.assertIn("never leave out words in the middle of a quote, and never invent one.",
                      load_prompt(PROMPTS, "reconstruct", "p1"))
        self.assertIn("Do not reword, leave out words in the middle of a quote, or invent quotes.",
                      load_prompt(PROMPTS, "reconstruct", "p2"))
        self.assertIn("Prefer the participant's own words", load_prompt(PROMPTS, "reconstruct", "p2"))
        for v in ("p0", "p1", "p2"):
            self.assertNotIn("shorten", load_prompt(PROMPTS, "reconstruct", v), v)

    def test_p1_steps_are_numbered_without_a_stray_bullet(self):
        lines = load_prompt(PROMPTS, "reconstruct", "p1").splitlines()
        steps = [l for l in lines if re.match(r"\d+\. ", l)]
        self.assertEqual([int(l.split(".")[0]) for l in steps], list(range(1, 9)))
        self.assertFalse([l for l in lines if l.startswith("- Make no node")])

    def test_prompts_contain_no_trap_specific_vocabulary(self):
        banned = ("cousin", "food", "truck", "neighbour", "neighbor", "propane", "licen", "sales tax",
                  "renewal", "parking")
        for f in sorted(PROMPTS.glob("*.v2.txt")):
            low = f.read_text(encoding="utf-8").lower()
            for w in banned:
                self.assertNotIn(w, low, f"{f.name} mentions {w!r}")

    def test_reconstruct_sends_system_then_user_and_defaults_to_v2(self):
        seen = []
        c = MockClient(fn=lambda m: seen.append([dict(x) for x in m]) or json.dumps(GOOD))
        g, st = reconstruct(self.log, c, T, ReconSpec("full", "p2", 0.0), CTX, PROMPTS, item_id="x")
        self.assertEqual(st, "ok")
        self.assertEqual([m["role"] for m in seen[0]], ["system", "user"])
        self.assertEqual(seen[0][0]["content"], load_system(PROMPTS, "reconstruct"))
        self.assertIn("REMINDER:", seen[0][1]["content"])
        self.assertIn(DEFS.strip(), seen[0][1]["content"])

    def test_prior_only_reconstruct_sends_system_message(self):
        seen = []

        def fn(m):
            seen.append([dict(x) for x in m])
            out = variant()
            for n in out["nodes"]: n["quote"] = None; n["status"] = "experienced"
            for e in out["edges"]: e["quote"] = None; e["status"] = "experienced"
            return json.dumps(out)
        reconstruct(self.log, MockClient(fn=fn), None, ReconSpec("prior_only", "p1", 0.7), CTX, PROMPTS, item_id="o")
        self.assertEqual([m["role"] for m in seen[0]], ["system", "user"])

    def test_v1_run_for_the_record_sends_the_v1_prompt_and_no_system_message(self):
        seen = []
        c = MockClient(fn=lambda m: seen.append([dict(x) for x in m]) or json.dumps(GOOD))
        reconstruct(self.log, c, T, ReconSpec("full", "p0", 0.0), CTX, PROMPTS, item_id="x", prompt_version="v1")
        self.assertEqual([m["role"] for m in seen[0]], ["user"])
        self.assertNotIn("REMINDER:", seen[0][0]["content"])

    def test_retry_after_bad_reply_keeps_system_and_adds_assistant_then_correction(self):
        seen = []
        replies = iter(["Sure! Here is the map", json.dumps(GOOD)])
        c = MockClient(fn=lambda m: seen.append([x["role"] for x in m]) or next(replies))
        g, st = reconstruct(self.log, c, T, ReconSpec("full", "p0", 0.0), CTX, PROMPTS, item_id="x")
        self.assertEqual(st, "ok")
        self.assertEqual(seen[1], ["system", "user", "assistant", "user"])

    def test_truncated_reply_is_retried_by_the_harness(self):
        calls = []

        class Cut(MockClient):
            def complete(self, messages, *, temperature, seed, max_tokens):
                calls.append(1)
                return ModelResponse(json.dumps(GOOD), "length" if len(calls) == 1 else "stop", {}, 0, "h")
        g, st = reconstruct(self.log, Cut(fn=lambda m: ""), T, ReconSpec("full", "p0", 0.0), CTX, PROMPTS, item_id="x")
        self.assertEqual((st, len(calls)), ("ok", 2))


if __name__ == "__main__":
    unittest.main()
