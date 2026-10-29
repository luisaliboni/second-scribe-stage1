import json
import tempfile
import unittest
from pathlib import Path

from second_scribe.audit.registry import AuditLog
from second_scribe.llm import MockClient, ModelResponse, derive_seed
from second_scribe.reconstruct import (ARMS, OUTPUT_SCHEMA_TEXT, ReconContext, ReconSpec, build_messages,
                                       load_definitions, load_prompt, load_system, parse_recon, reconstruct,
                                       validate_recon)
from second_scribe.schemas import Transcript, Turn

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
DEFS = load_definitions(PROMPTS)
CTX = ReconContext(domain="getting a food-truck licence", role="food-truck owner",
                   phases=("Planning", "Permits", "Inspections", "Operating"))
T = Transcript("SYN-OW01", [Turn(0, "interviewer", "What happened first?"),
                            Turn(1, "participant", "Then we had to get the business licence from city hall, it took weeks."),
                            Turn(2, "participant", "I wish there had been one office that handled every single permit.")], "synthetic")
GOOD = {"nodes": [{"id": "n1", "label": "Business licence", "type": "service", "phase": "Permits", "status": "experienced",
                   "quote": "we had to get the business licence from city hall, it took weeks"},
                  {"id": "n2", "label": "One-stop office", "type": "service", "phase": "Permits", "status": "wished",
                   "quote": "I wish there had been one office that handled every single permit"}],
        "edges": [{"source": "n1", "target": "n2", "status": "wished",
                   "quote": "I wish there had been one office that handled every single permit"}],
        "emotion_word": None}


def variant():
    """Deep copy of GOOD, for a test to mutate."""
    return json.loads(json.dumps(GOOD))


def check(d, arm="full", **kw):
    return validate_recon(json.dumps(d), arm=arm, transcript_text=T.text("full"), **kw)


class TestRecon(unittest.TestCase):
    def setUp(self): self.log = AuditLog(Path(tempfile.mkdtemp()) / "r.jsonl", "r", clock=lambda: "t")

    def test_valid_json_in_fence_parses(self):
        c = MockClient(fn=lambda m: "```json\n" + json.dumps(GOOD) + "\n```")
        g, st = reconstruct(self.log, c, T, ReconSpec("removed", "p0", 0.0), CTX, PROMPTS, item_id="SYN-OW01")
        self.assertEqual(st, "ok"); self.assertEqual(g.node("n2").status, "wished"); self.assertEqual(g.origin, "ai")

    def test_paraphrased_quotes_trigger_retry(self):
        bad = json.loads(json.dumps(GOOD)); bad["nodes"][0]["quote"] = "we got a licence"; bad["nodes"][1]["quote"] = "one office would help"
        replies = iter([json.dumps(bad), json.dumps(GOOD)])
        g, st = reconstruct(self.log, MockClient(fn=lambda m: next(replies)), T, ReconSpec("full", "p0", 0.0), CTX, PROMPTS, item_id="x")
        self.assertEqual(st, "ok")

    def test_three_failures_mark_failed(self):
        g, st = reconstruct(self.log, MockClient(fn=lambda m: "Sure! Here is the map:"), T,
                            ReconSpec("full", "p0", 0.0), CTX, PROMPTS, item_id="x")
        self.assertIsNone(g); self.assertEqual(st, "failed")

    def test_too_long_is_excluded_without_call(self):
        def boom(m): raise AssertionError("must not call")
        ctx = ReconContext(CTX.domain, CTX.role, CTX.phases, max_input_words=5)
        g, st = reconstruct(self.log, MockClient(fn=boom), T, ReconSpec("full", "p0", 0.0), ctx, PROMPTS, item_id="x")
        self.assertEqual(st, "too_long_excluded")

    def test_prior_only_rejects_quotes_and_sends_no_transcript(self):
        seen = []
        def fn(m):
            seen.append(m[-1]["content"]); out = json.loads(json.dumps(GOOD))
            for n in out["nodes"]: n["quote"] = None; n["status"] = "experienced"
            for e in out["edges"]: e["quote"] = None; e["status"] = "experienced"
            return json.dumps(out)
        g, st = reconstruct(self.log, MockClient(fn=fn), None, ReconSpec("prior_only", "p0", 0.7), CTX, PROMPTS, item_id="owner")
        self.assertEqual(st, "ok"); self.assertEqual(g.origin, "prior_only")
        self.assertNotIn("city hall", seen[0])

    def test_validate_rejects_unknown_edge_endpoint(self):
        bad = json.loads(json.dumps(GOOD)); bad["edges"][0]["target"] = "n9"
        self.assertIn("n9", validate_recon(json.dumps(bad), arm="full", transcript_text=T.text("full")))

    # ---- additions ----

    def test_spec_key_and_arms(self):
        self.assertEqual(ReconSpec("full", "p1", 0.7, 2).key(), "full__p1__t0.7__r2")
        self.assertEqual(ReconSpec("removed", "p0", 0.0).key(), "removed__p0__t0__r0")
        self.assertEqual(ARMS, ("full", "removed", "prior_only"))

    def test_prompt_files_exist_and_cover_required_content(self):
        for kind in ("reconstruct", "prior_only"):
            for v in ("p0", "p1", "p2"):
                p = load_prompt(PROMPTS, kind, v)
                for ph in ("{role}", "{domain}", "{phases}", "{schema}"):
                    self.assertIn(ph, p, f"{kind}.{v} lacks {ph}")
                self.assertEqual("{definitions}" in p, kind == "reconstruct", f"{kind}.{v}")
                self.assertEqual("{transcript}" in p, kind == "reconstruct", f"{kind}.{v}")
                low = p.lower()
                self.assertIn("json", low); self.assertIn("one node", low)
                self.assertIn("experienced", low)
                if kind == "reconstruct":
                    self.assertIn("wished", low); self.assertIn("transcript", low)
                else:   # the official-script map has no wished items
                    self.assertNotIn("wished", low, f"{kind}.{v}")
        with self.assertRaises(ValueError):
            load_prompt(PROMPTS, "reconstruct", "../p0")

    def test_build_messages_fills_placeholders_and_keeps_braces_in_transcript(self):
        raw = "I said {role} and {schema} and {phases} {x}."
        msgs = build_messages(load_prompt(PROMPTS, "reconstruct", "p0"), CTX, raw, definitions=DEFS)
        body = msgs[-1]["content"]
        self.assertIn(raw, body)
        self.assertIn("food-truck owner", body); self.assertIn("getting a food-truck licence", body)
        self.assertIn("Planning, Permits, Inspections, Operating", body)
        self.assertIn('"nodes"', body); self.assertIn("quote", body)
        # the schema block lists the phases inline and has no unfilled placeholder before the transcript
        head = body.split("TRANSCRIPT:")[0]
        for ph in ("{role}", "{domain}", "{phases}", "{schema}", "{transcript}"):
            self.assertNotIn(ph, head)

    def test_schema_text_is_the_contract(self):
        for key in ('"nodes"', '"edges"', '"emotion_word"', "experienced|wished",
                    "service|information_source|actor|actor_information_source|scenario|other"):
            self.assertIn(key, OUTPUT_SCHEMA_TEXT)

    def test_prior_only_messages_have_null_quotes_and_no_transcript(self):
        body = build_messages(load_prompt(PROMPTS, "prior_only", "p1"), CTX, None)[-1]["content"]
        self.assertIn('"quote": null', body)
        self.assertIn("food-truck owner", body)

    def test_prior_only_schema_has_no_wished(self):
        for v in ("p0", "p1", "p2"):
            body = build_messages(load_prompt(PROMPTS, "prior_only", v), CTX, None)[-1]["content"]
            self.assertNotIn("wished", body.lower(), v)

    def test_prior_only_prompts_never_carry_transcript_text(self):
        # privacy regression: a transcript passed by mistake must not reach a prior-only prompt
        for v in ("p0", "p1", "p2"):
            msgs = build_messages(load_prompt(PROMPTS, "prior_only", v), CTX, "I went to city hall and waited.")
            self.assertFalse(any("city hall" in m["content"] for m in msgs), v)

    def test_reconstruct_prior_only_ignores_a_passed_transcript(self):
        sent = []
        def fn(m):
            sent.extend(x["content"] for x in m)
            out = variant()
            for n in out["nodes"]: n["quote"] = None; n["status"] = "experienced"
            for e in out["edges"]: e["quote"] = None; e["status"] = "experienced"
            return json.dumps(out)
        for v in ("p0", "p1", "p2"):
            g, st = reconstruct(self.log, MockClient(fn=fn), T, ReconSpec("prior_only", v, 0.7), CTX, PROMPTS, item_id="owner")
            self.assertEqual(st, "ok")
        self.assertEqual(len(sent), 6)      # system + user message per call
        self.assertFalse(any("city hall" in s for s in sent))
        self.assertNotIn("city hall", Path(self.log.path).read_text())

    def test_prior_only_rejects_wished(self):
        d = variant()
        for n in d["nodes"]: n["quote"] = None; n["status"] = "experienced"
        for e in d["edges"]: e["quote"] = None
        err = validate_recon(json.dumps(d), arm="prior_only", transcript_text=None)
        self.assertIn("n1->n2", err); self.assertIn("wished", err); self.assertLessEqual(len(err), 300)
        d["nodes"][1]["status"] = "wished"; d["edges"][0]["status"] = "experienced"
        self.assertIn("n2", validate_recon(json.dumps(d), arm="prior_only", transcript_text=None))
        # full/removed arms still allow wished
        self.assertIsNone(check(GOOD))

    def test_full_arm_without_transcript_is_an_error(self):
        with self.assertRaises(ValueError):
            build_messages(load_prompt(PROMPTS, "reconstruct", "p0"), CTX, None, definitions=DEFS)

    def test_validate_good_passes_and_fence_variants(self):
        self.assertIsNone(check(GOOD))
        j = json.dumps(GOOD)
        for wrapped in ("```json\n" + j + "\n```", "```\n" + j + "\n```", "  \n" + j + "\n  "):
            self.assertIsNone(validate_recon(wrapped, arm="full", transcript_text=T.text("full")))

    def test_one_line_fence_and_non_string_endpoints(self):
        self.assertIsNone(validate_recon("```json" + json.dumps(GOOD) + "```", arm="full", transcript_text=T.text("full")))
        d = variant(); d["edges"][0]["source"] = ["n1"]
        self.assertIn("edge", check(d))

    def test_validate_structure_errors(self):
        def mutate(f):
            d = variant(); f(d); return check(d)
        self.assertIn("JSON", validate_recon("Sure! Here is the map:", arm="full", transcript_text=T.text("full")))
        self.assertIn("nodes", validate_recon("[1]", arm="full", transcript_text=T.text("full")))
        self.assertIn("edges", validate_recon('{"nodes": []}', arm="full", transcript_text=T.text("full")))
        self.assertIn("at least 1", mutate(lambda d: d.update(nodes=[], edges=[])))
        self.assertIn("120", mutate(lambda d: d.update(nodes=[dict(GOOD["nodes"][0], id=f"n{i}") for i in range(121)], edges=[])))
        self.assertIn("duplicate", mutate(lambda d: d["nodes"][1].update(id="n1")))
        self.assertIsNone(mutate(lambda d: d["nodes"][0].update(type="bogus")))   # run 2: coerced to "other", not an error
        self.assertIn("status", mutate(lambda d: d["nodes"][0].update(status="maybe")))
        self.assertIn("status", mutate(lambda d: d["edges"][0].update(status="maybe")))
        self.assertIn("label", mutate(lambda d: d["nodes"][0].update(label="")))
        self.assertIn("label", mutate(lambda d: d["nodes"][0].update(label="x" * 121)))
        self.assertIn("self-loop", mutate(lambda d: d["edges"][0].update(target="n1")))
        self.assertIn("unknown node n9", mutate(lambda d: d["edges"][0].update(target="n9")))
        self.assertIn("edge n1->n9", mutate(lambda d: d["edges"][0].update(target="n9")))

    def test_phase_checked_only_when_phases_given(self):
        d = variant(); d["nodes"][0]["phase"] = "Retirement"
        self.assertIsNone(check(d))
        err = check(d, phases=CTX.phases)
        self.assertIn("n1", err); self.assertIn("phase", err)
        d["nodes"][0]["phase"] = None
        self.assertIsNone(check(d, phases=CTX.phases))

    def test_quote_rules_for_full_and_removed(self):
        d = variant(); d["nodes"][0]["quote"] = ""
        self.assertIn("n1", check(d)); self.assertIn("quote", check(d))
        d = variant(); del d["edges"][0]["quote"]
        self.assertIn("n1->n2", check(d))
        d = variant(); d["nodes"][0]["quote"] = 7
        self.assertIn("n1", check(d))

    def test_unfound_quote_share_threshold(self):
        # 1 unfound of 3 = 33% <= 50% (L16): tolerated; an explicit lower limit still rejects it, naming it
        d = variant(); d["nodes"][0]["quote"] = "completely invented words here"
        self.assertIsNone(check(d))
        err = check(d, max_unfound_share=0.10)
        self.assertIn("not verbatim", err); self.assertIn("n1", err); self.assertNotIn("n2", err)
        # exactly at the share is tolerated: 1 of 2 items unfound, share 0.50; 2 of 3 is over
        half = variant()
        half["nodes"] = [dict(GOOD["nodes"][0], id="a0", quote="completely invented words here"),
                         dict(GOOD["nodes"][1], id="n2")]
        half["edges"] = []
        self.assertIsNone(check(half))
        over = variant()
        over["nodes"] = [dict(GOOD["nodes"][0], id="a0", quote="completely invented words here"),
                         dict(GOOD["nodes"][0], id="a1", quote="more invented words here"),
                         dict(GOOD["nodes"][1], id="n2")]
        over["edges"] = []
        self.assertIn("not verbatim", check(over))

    def test_error_messages_are_short_and_name_at_most_five_ids(self):
        d = variant()
        d["nodes"] = [dict(GOOD["nodes"][0], id=f"node_with_long_id_{i:03d}", quote="made up words here") for i in range(40)]
        d["edges"] = []
        err = check(d)
        self.assertLessEqual(len(err), 300)
        self.assertEqual(sum(f"node_with_long_id_{i:03d}" in err for i in range(40)), 5)

    def test_prior_only_validation(self):
        d = variant()
        for n in d["nodes"]: n["quote"] = None; n["status"] = "experienced"
        for e in d["edges"]: del e["quote"]; e["status"] = "experienced"
        self.assertIsNone(validate_recon(json.dumps(d), arm="prior_only", transcript_text=None))
        d["nodes"][0]["quote"] = "some words"
        err = validate_recon(json.dumps(d), arm="prior_only", transcript_text=None)
        self.assertIn("n1", err); self.assertIn("null", err)

    def test_parse_recon_maps_fields(self):
        d = variant(); d["emotion_word"] = "Stuck"
        g = parse_recon("```json\n" + json.dumps(d) + "\n```", "SYN-OW01", "ai", spec_key="full__p0__t0__r0")
        self.assertEqual((g.map_id, g.origin, g.emotion_word), ("SYN-OW01", "ai", "Stuck"))
        n = g.node("n1")
        self.assertEqual((n.text, n.node_type, n.phase, n.status), ("Business licence", "service", "Permits", "experienced"))
        self.assertEqual(n.quote, GOOD["nodes"][0]["quote"])
        e = g.edges[0]
        self.assertEqual((e.source, e.target, e.status, e.glued, e.label), ("n1", "n2", "wished", True, None))
        self.assertEqual(e.quote, GOOD["edges"][0]["quote"])
        self.assertEqual(g.meta, {"spec": "full__p0__t0__r0", "type_coercions": 0})

    def test_reconstruct_sets_meta_seed_and_stage(self):
        calls = []
        class Spy:
            def complete(self, messages, *, temperature, seed, max_tokens):
                calls.append((temperature, seed, max_tokens, messages[-1]["content"]))
                return ModelResponse(json.dumps(GOOD), "stop", {}, 0, "h")
        spec = ReconSpec("removed", "p2", 0.7, 3)
        g, st = reconstruct(self.log, Spy(), T, spec, CTX, PROMPTS, item_id="SYN-OW01", max_tokens=1234)
        self.assertEqual(st, "ok"); self.assertEqual(g.meta["spec"], "removed__p2__t0.7__r3")
        self.assertEqual(g.map_id, "SYN-OW01")
        temp, seed, mt, body = calls[0]
        self.assertEqual((temp, mt, seed), (0.7, 1234, derive_seed("SYN-OW01", spec.key())))
        self.assertIn("recon:removed", Path(self.log.path).read_text())

    def test_removed_arm_sends_text_without_mapping_talk(self):
        t = Transcript("S", [Turn(0, "participant", "Alpha beta gamma delta epsilon zeta eta theta."),
                             Turn(1, "interviewer", "Let me draw a box for that.", mapping_talk=True)], "synthetic")
        seen = []
        def fn(m):
            seen.append(m[-1]["content"])
            return json.dumps({"nodes": [{"id": "n1", "label": "A", "type": "other", "phase": None, "status": "experienced",
                                          "quote": "alpha beta gamma delta epsilon zeta eta theta"}], "edges": []})
        g, st = reconstruct(self.log, MockClient(fn=fn), t, ReconSpec("removed", "p0", 0.0), CTX, PROMPTS, item_id="S")
        self.assertEqual(st, "ok"); self.assertNotIn("draw a box", seen[0])

    def test_retry_message_names_the_problem(self):
        bad = variant(); bad["edges"][0]["target"] = "n9"
        sent = []
        replies = iter([json.dumps(bad), json.dumps(GOOD)])
        def fn(m):
            sent.append(m[-1]["content"]); return next(replies)
        g, st = reconstruct(self.log, MockClient(fn=fn), T, ReconSpec("full", "p0", 0.0), CTX, PROMPTS, item_id="x")
        self.assertEqual(st, "ok"); self.assertIn("edge n1->n9: unknown node n9", sent[1])

    def test_phase_violation_triggers_retry_via_reconstruct(self):
        bad = variant(); bad["nodes"][0]["phase"] = "Retirement"
        replies = iter([json.dumps(bad), json.dumps(GOOD)])
        g, st = reconstruct(self.log, MockClient(fn=lambda m: next(replies)), T, ReconSpec("full", "p0", 0.0), CTX, PROMPTS, item_id="x")
        self.assertEqual(st, "ok"); self.assertEqual(g.node("n1").phase, "Permits")


if __name__ == "__main__":
    unittest.main()
