import unittest, tempfile, hashlib, json, re
from pathlib import Path
from collections import Counter
from second_scribe.synth.build import build_corpus
from second_scribe.synth.bank import (CONCEPTS, TRAP_KINDS, TRAP_TEMPLATES, SPOKEN_KINDS, ORDINARY_PROMPTS,
                                      ORDINARY_TURNS, ACKS, FILLERS, OPENING, CLOSING, SYN_PHASES)
from second_scribe.maps.vsdx import extract_vsdx, ExtractConfig
from second_scribe.schemas import MapGraph, NODE_TYPES, MODALITIES, BUCKETS
from second_scribe.transcripts import read_docx_text, parse_paragraphs, flag_mapping_talk, MAPPING_PATTERNS

DOMAIN_WORDS = re.compile(r"\b(map|box|draw|arrow|red|legend|screen)(s|es|ing|n)?\b", re.IGNORECASE)
ANY_MAPPING = [re.compile(p, re.IGNORECASE) for p in MAPPING_PATTERNS]


def tree_hash(d):
    h = hashlib.sha256()
    for p in sorted(Path(d).rglob("*")):
        if p.is_file(): h.update(str(p.relative_to(d)).encode()); h.update(p.read_bytes())
    return h.hexdigest()


def load(d, sub, pid, suffix=".json"):
    return json.loads(Path(d, sub, pid + suffix).read_text(encoding="utf-8"))


def parsed(d, pid):
    return flag_mapping_talk(parse_paragraphs(read_docx_text(Path(d, "transcripts", pid + ".docx")), {"pt"}, pid))


class TestSynth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp(); cls.meta = build_corpus(cls.d, seed=0)
        cls.pids = [p["pid"] for p in cls.meta["participants"]]
        cls.cfg = ExtractConfig(phase_names=SYN_PHASES)

    # ---- the brief's tests -------------------------------------------------------------------
    def test_deterministic(self):
        d2 = tempfile.mkdtemp(); build_corpus(d2, seed=0)
        self.assertEqual(tree_hash(self.d), tree_hash(d2))

    def test_seed_changes_output(self):
        d2 = tempfile.mkdtemp(); build_corpus(d2, seed=1)
        self.assertNotEqual(tree_hash(self.d), tree_hash(d2))

    def test_bank_shape(self):
        self.assertEqual(len(CONCEPTS), 32)
        self.assertEqual(len({c.cid for c in CONCEPTS}), 32)

    def test_every_trap_kind_at_least_three_times(self):
        kinds = Counter()
        for p in Path(self.d, "traps").glob("*.json"):
            kinds.update(t["kind"] for t in json.loads(p.read_text()))
        for k in TRAP_KINDS: self.assertGreaterEqual(kinds[k], 3, k)

    def test_maps_extract_with_phases_and_red(self):
        cfg = ExtractConfig(phase_names=SYN_PHASES)
        reds = 0
        for p in sorted(Path(self.d, "maps").glob("*.vsdx")):
            g = extract_vsdx(p, p.stem, cfg)
            self.assertTrue(all(n.phase in SYN_PHASES for n in g.nodes), p.stem)
            self.assertIsNotNone(g.emotion_word)
            reds += sum(n.status == "wished" for n in g.nodes)
        self.assertGreaterEqual(reds, 3)

    def test_transcripts_parse_both_formats_and_flag_mapping_talk(self):
        fmts = Counter(); flagged = 0
        for p in sorted(Path(self.d, "transcripts").glob("*.docx")):
            t = flag_mapping_talk(parse_paragraphs(read_docx_text(p), {"pt"}, p.stem))
            fmts[t.speaker_format] += 1; flagged += sum(x.mapping_talk for x in t.turns)
        self.assertEqual(fmts, Counter({"bracket": 6, "first_token": 6}))
        self.assertGreaterEqual(flagged, 3)

    # ---- bank ----------------------------------------------------------------------------------
    def test_bank_phases_roles_types(self):
        self.assertEqual(Counter(c.phase for c in CONCEPTS), Counter({p: 8 for p in SYN_PHASES}))
        for c in CONCEPTS:
            self.assertIn(c.node_type, NODE_TYPES); self.assertNotEqual(c.node_type, "other")
            self.assertEqual(len(c.forms), 3); self.assertEqual(len(set(c.forms)), 3)
        for role in ("owner", "officer"):
            self.assertGreaterEqual(sum(role in c.roles for c in CONCEPTS), 20, role)
        self.assertEqual(len({c.forms[0] for c in CONCEPTS}), 32)

    def test_templates_shape(self):
        self.assertEqual(set(TRAP_TEMPLATES), set(TRAP_KINDS))
        for k, ts in TRAP_TEMPLATES.items():
            self.assertGreaterEqual(len(ts), 3, k)
            for t in ts: self.assertIn("{x}", t, k)

    def test_ordinary_speech_avoids_mapping_vocabulary(self):
        ordinary = list(ORDINARY_PROMPTS) + list(FILLERS) + list(ACKS["interviewer_voiced"]) + \
            list(ACKS["mapping_talk"]) + list(OPENING) + list(CLOSING)
        spoken = [t for k in SPOKEN_KINDS if k != "mapping_talk" for t in TRAP_TEMPLATES[k]] + list(ORDINARY_TURNS)
        for c in CONCEPTS:
            spoken += [c.forms[1], c.forms[2], c.forms[0]]
        for s in ordinary + spoken:
            self.assertIsNone(DOMAIN_WORDS.search(s), s)
            self.assertFalse(any(r.search(s) for r in ANY_MAPPING), s)
        for t in TRAP_TEMPLATES["mapping_talk"]:
            self.assertTrue(any(r.search(t.format(x="the business licence")) for r in ANY_MAPPING), t)

    # ---- mapping-talk flags are exactly the planted turns ---------------------------------------
    def test_flag_mapping_talk_flags_exactly_the_planted_turns(self):
        total = 0
        for pid in self.pids:
            t = parsed(self.d, pid)
            planted = sorted(x["sentence"] for x in load(self.d, "traps", pid) if x["kind"] == "mapping_talk")
            flagged = sorted(x.text for x in t.turns if x.mapping_talk)
            self.assertEqual(flagged, planted, pid)
            self.assertTrue(all(x.speaker == "interviewer" for x in t.turns if x.mapping_talk), pid)
            total += len(planted)
        self.assertGreaterEqual(total, 3)

    def test_no_domain_words_outside_mapping_talk(self):
        for pid in self.pids:
            for x in parsed(self.d, pid).turns:
                if not x.mapping_talk:
                    self.assertIsNone(DOMAIN_WORDS.search(x.text), (pid, x.text))

    def test_participant_turns_are_participant(self):
        for pid in self.pids:
            t = parsed(self.d, pid)
            self.assertEqual(t.turns[0].speaker, "interviewer", pid)
            self.assertTrue(all(x.speaker in ("interviewer", "participant") for x in t.turns), pid)

    # ---- traps file ----------------------------------------------------------------------------
    def test_trap_entries(self):
        for pid in self.pids:
            traps = load(self.d, "traps", pid)
            self.assertGreaterEqual(len({t["kind"] for t in traps}), 4, pid)
            text = "\n".join(x.text for x in parsed(self.d, pid).turns)
            for t in traps:
                self.assertIn(t["kind"], TRAP_KINDS)
                self.assertIn(t["expected_modality"], MODALITIES); self.assertIn(t["expected_bucket"], BUCKETS)
                self.assertEqual(set(t["arm_expectation"]), {"full", "removed"})
                for v in t["arm_expectation"].values(): self.assertIn(v, BUCKETS)
                self.assertEqual(t["arm_expectation"]["full"], t["expected_bucket"])
                if t["kind"] in SPOKEN_KINDS:
                    self.assertIn(t["sentence"], text, (pid, t["kind"]))
                else:
                    self.assertEqual(t["sentence"], "")
                if t["kind"] in ("interviewer_voiced", "mapping_talk"):
                    self.assertEqual(t["arm_expectation"], {"full": "matched", "removed": "missed"})
                    self.assertEqual(t["speaker"], "interviewer")

    def test_trap_expectations_by_kind(self):
        want = {"wished": ("wished", "matched"), "negated": ("negated", "unanchored"),
                "other_person": ("other_person", "unanchored"), "hypothetical": ("hypothetical", "unanchored"),
                "off_record": ("unsupported", "off_record")}
        for pid in self.pids:
            for t in load(self.d, "traps", pid):
                if t["kind"] in want:
                    self.assertEqual((t["expected_modality"], t["expected_bucket"]), want[t["kind"]])

    def test_interviewer_voiced_and_mapping_talk_concepts_never_in_participant_speech(self):
        by = {c.cid: c for c in CONCEPTS}
        for pid in self.pids:
            t = parsed(self.d, pid)
            ptext = " ".join(x.text for x in t.turns if x.speaker == "participant")
            for tr in load(self.d, "traps", pid):
                if tr["kind"] in ("interviewer_voiced", "mapping_talk"):
                    self.assertNotIn(by[tr["cid"]].forms[1], ptext, (pid, tr["kind"]))

    # ---- endorsed map vs gold ------------------------------------------------------------------
    def test_endorsed_nodes_match_concepts_and_types_come_from_the_legend(self):
        by = {c.cid: c for c in CONCEPTS}
        for pid in self.pids:
            g = extract_vsdx(Path(self.d, "maps", pid + ".vsdx"), pid, self.cfg)
            cmap = load(self.d, "gold", pid, ".concepts.json")
            self.assertEqual(set(cmap["endorsed"]), {n.node_id for n in g.nodes}, pid)
            wk = {t["cid"] for t in load(self.d, "traps", pid) if t["kind"] == "workaround"}
            used = {"scenario" if cid in wk else by[cid].node_type for cid in cmap["endorsed"].values()}
            self.assertEqual(g.meta["legend_types_found"], len(used), pid)   # one legend shape per type USED
            for n in g.nodes:
                c = by[cmap["endorsed"][n.node_id]]
                self.assertEqual(n.text, c.forms[0])
                want = "scenario" if any(t["kind"] == "workaround" and t["cid"] == c.cid
                                         for t in load(self.d, "traps", pid)) else c.node_type
                self.assertEqual(n.node_type, want, (pid, c.cid))
                self.assertEqual(n.phase, c.phase)
            self.assertEqual(g.unresolved_edges, 0, pid)
            self.assertTrue(all(e.glued for e in g.edges), pid)

    def test_gold_graph(self):
        by = {c.cid: c for c in CONCEPTS}
        for pid in self.pids:
            gold = MapGraph.from_json(Path(self.d, "gold", pid + ".json").read_text(encoding="utf-8"))
            cmap = load(self.d, "gold", pid, ".concepts.json")
            traps = load(self.d, "traps", pid)
            self.assertEqual(gold.origin, "synthetic_gold"); self.assertEqual(gold.map_id, pid)
            self.assertEqual({n.node_id: c for n, c in ((n, cmap["gold"][n.node_id]) for n in gold.nodes)},
                             cmap["gold"])
            for n in gold.nodes:
                c = by[cmap["gold"][n.node_id]]
                self.assertEqual(n.text, c.forms[2]); self.assertEqual(n.phase, c.phase)
            ids = {n.node_id for n in gold.nodes}
            self.assertTrue(all(e.source in ids and e.target in ids for e in gold.edges), pid)
            gold_cids = set(cmap["gold"].values())
            for t in traps:
                k = t["kind"]
                if k in ("negated", "other_person", "hypothetical", "off_record"):
                    self.assertNotIn(t["cid"], gold_cids, (pid, k))
                if k in ("wished", "interviewer_voiced", "mapping_talk", "workaround", "dead_end"):
                    self.assertIn(t["cid"], gold_cids, (pid, k))
            wished = {cmap["gold"][n.node_id] for n in gold.nodes if n.status == "wished"}
            self.assertEqual(wished, {t["cid"] for t in traps if t["kind"] == "wished"}, pid)
            inv = {cmap["gold"][i] for i in gold.meta["interviewer_voiced"]}
            mto = {cmap["gold"][i] for i in gold.meta["mapping_talk_only"]}
            self.assertEqual(inv, {t["cid"] for t in traps if t["kind"] == "interviewer_voiced"}, pid)
            self.assertEqual(mto, {t["cid"] for t in traps if t["kind"] == "mapping_talk"}, pid)

    def test_structure_traps_in_endorsed_and_gold(self):
        inv_e = lambda m: {v: k for k, v in m.items()}
        for pid in self.pids:
            g = extract_vsdx(Path(self.d, "maps", pid + ".vsdx"), pid, self.cfg)
            gold = MapGraph.from_json(Path(self.d, "gold", pid + ".json").read_text(encoding="utf-8"))
            cm = load(self.d, "gold", pid, ".concepts.json")
            e_edges = {(cm["endorsed"][e.source], cm["endorsed"][e.target]) for e in g.edges}
            g_edges = {(cm["gold"][e.source], cm["gold"][e.target]) for e in gold.edges}
            e_cids = set(cm["endorsed"].values())
            for t in load(self.d, "traps", pid):
                k = t["kind"]
                if k == "loop":
                    a, b = t["cids"]
                    for edges in (e_edges, g_edges):
                        self.assertIn((a, b), edges); self.assertIn((b, a), edges)
                elif k == "dead_end":
                    for edges in (e_edges, g_edges):
                        self.assertTrue(any(tgt == t["cid"] for _, tgt in edges), pid)
                        self.assertFalse(any(src == t["cid"] for src, _ in edges), pid)
                elif k == "unstated_edge":
                    a, b = t["cids"]
                    self.assertIn((a, b), e_edges); self.assertNotIn((a, b), g_edges)
                    self.assertNotIn((b, a), g_edges)
                    self.assertIn(a, set(cm["gold"].values())); self.assertIn(b, set(cm["gold"].values()))
                elif k == "off_record":
                    self.assertIn(t["cid"], e_cids)
                    self.assertFalse(any(t["cid"] in pair for pair in e_edges))
            kinds = {t["kind"] for t in load(self.d, "traps", pid)}
            if "unstated_edge" not in kinds:
                self.assertEqual(e_edges, g_edges, pid)

    def test_wished_nodes_are_red_in_endorsed_map(self):
        for pid in self.pids:
            g = extract_vsdx(Path(self.d, "maps", pid + ".vsdx"), pid, self.cfg)
            cm = load(self.d, "gold", pid, ".concepts.json")
            red = {cm["endorsed"][n.node_id] for n in g.nodes if n.status == "wished"}
            self.assertEqual(red, {t["cid"] for t in load(self.d, "traps", pid) if t["kind"] == "wished"}, pid)

    def test_concept_wording_never_nested(self):
        wording = [c.forms[1] for c in CONCEPTS]
        for w in wording:
            self.assertEqual(sum(w in o for o in wording), 1, w)

    def test_map_labels_never_collide_with_legend_phase_or_emotion_text(self):
        from second_scribe.maps.vsdx import LEGEND_TEXTS
        reserved = {t.casefold() for t in LEGEND_TEXTS} | {p.casefold() for p in SYN_PHASES}
        for c in CONCEPTS:
            self.assertNotIn(c.forms[0].casefold(), reserved, c.cid)
            self.assertFalse(c.forms[0].startswith(("Anything in red", "Word:")), c.cid)

    def test_map_label_is_never_a_substring_of_the_transcript_wording(self):
        for c in CONCEPTS:
            self.assertNotIn(c.forms[0].casefold(), c.forms[1].casefold(), c.cid)
            self.assertNotIn(c.forms[0].casefold(), c.forms[2].casefold(), c.cid)

    def test_loop_templates_name_both_concepts(self):
        for t in TRAP_TEMPLATES["loop"]:
            self.assertIn("{x}", t); self.assertIn("{y}", t)

    def test_loop_sentence_names_both_cycle_concepts(self):
        by = {c.cid: c for c in CONCEPTS}; n = 0
        for pid in self.pids:
            t = parsed(self.d, pid)
            for tr in load(self.d, "traps", pid):
                if tr["kind"] == "loop":
                    n += 1
                    a, b = tr["cids"]
                    self.assertNotEqual(a, b)
                    turn = next(x.text for x in t.turns if tr["sentence"] in x.text).casefold()
                    self.assertIn(by[a].forms[1].casefold(), turn, pid)
                    self.assertIn(by[b].forms[1].casefold(), turn, pid)
        self.assertGreaterEqual(n, 3)

    def test_trap_kind_names_never_appear_in_speech(self):
        for pid in self.pids:
            text = " ".join(x.text for x in parsed(self.d, pid).turns).casefold()
            for k in SPOKEN_KINDS:
                self.assertNotIn(k.replace("_", " "), text, (pid, k))

    def test_rebuild_removes_stale_output_but_only_its_own(self):
        d = tempfile.mkdtemp(); build_corpus(d, seed=0)
        Path(d, "maps", "stale.vsdx").write_bytes(b"x"); Path(d, "traps", "stale.json").write_text("[]")
        Path(d, "keep.txt").write_text("keep"); Path(d, "other").mkdir(); Path(d, "other", "a.txt").write_text("a")
        build_corpus(d, seed=0)
        self.assertFalse(Path(d, "maps", "stale.vsdx").exists()); self.assertFalse(Path(d, "traps", "stale.json").exists())
        self.assertEqual(Path(d, "keep.txt").read_text(), "keep"); self.assertEqual(Path(d, "other", "a.txt").read_text(), "a")
        Path(d, "keep.txt").unlink(); Path(d, "other", "a.txt").unlink(); Path(d, "other").rmdir()
        self.assertEqual(tree_hash(d), tree_hash(self.d))

    def test_committed_demo_corpus_equals_a_fresh_seed_0_build(self):
        demo = Path(__file__).resolve().parent.parent / "demo" / "corpus"
        self.assertTrue(demo.is_dir(), "demo/corpus is not committed")
        self.assertEqual(tree_hash(demo), tree_hash(self.d))

    def test_removed_arm_never_holds_the_mapping_talk_concept(self):
        by = {c.cid: c for c in CONCEPTS}
        for pid in self.pids:
            t = parsed(self.d, pid)
            for tr in load(self.d, "traps", pid):
                if tr["kind"] == "mapping_talk":
                    self.assertIn(by[tr["cid"]].forms[1], t.text("full"), pid)
                    self.assertNotIn(by[tr["cid"]].forms[1], t.text("removed"), pid)

    def test_invariants_hold_for_other_seeds(self):
        for seed in (1, 2):
            d = tempfile.mkdtemp(); meta = build_corpus(d, seed=seed)
            kinds = Counter()
            for p in meta["participants"]:
                traps = load(d, "traps", p["pid"])
                kinds.update(t["kind"] for t in traps)
                self.assertGreaterEqual(len({t["kind"] for t in traps}), 4, (seed, p["pid"]))
                t = parsed(d, p["pid"])
                self.assertEqual(sorted(x.text for x in t.turns if x.mapping_talk),
                                 sorted(x["sentence"] for x in traps if x["kind"] == "mapping_talk"), (seed, p["pid"]))
            for k in TRAP_KINDS: self.assertGreaterEqual(kinds[k], 3, (seed, k))

    # ---- files -------------------------------------------------------------------------------------
    def test_json_formatting_and_meta(self):
        for p in Path(self.d).rglob("*.json"):
            raw = p.read_text(encoding="utf-8")
            self.assertTrue(raw.endswith("}\n") or raw.endswith("]\n"), p)
            self.assertEqual(raw, json.dumps(json.loads(raw), sort_keys=True, ensure_ascii=False, indent=2) + "\n", p)
        self.assertEqual(self.meta["seed"], 0)
        self.assertEqual([p["pid"] for p in self.meta["participants"]],
                         [f"SYN-OW{i:02d}" for i in range(1, 8)] + [f"SYN-OF{i:02d}" for i in range(1, 6)])
        self.assertEqual(Counter(p["interviewer"] for p in self.meta["participants"]), Counter(I1=4, I2=4, I3=4))
        self.assertEqual(json.loads(Path(self.d, "meta.json").read_text(encoding="utf-8"))["seed"], 0)

    def test_every_map_draws_a_legend(self):
        import zipfile
        for pid in self.pids:
            g = extract_vsdx(Path(self.d, "maps", pid + ".vsdx"), pid, self.cfg)
            self.assertGreaterEqual(g.meta["n_legend"], 2, pid)   # >=1 type + the red note
            self.assertEqual(g.meta["n_phase_headers"], 4, pid)
            with zipfile.ZipFile(Path(self.d, "maps", pid + ".vsdx")) as z:
                self.assertIn("Anything in red is the ideal", z.read("visio/pages/page1.xml").decode())

    def test_docx_zip_dates_fixed(self):
        import zipfile
        for p in Path(self.d, "transcripts").glob("*.docx"):
            with zipfile.ZipFile(p) as z:
                self.assertTrue(all(i.date_time == (2026, 1, 1, 0, 0, 0) for i in z.infolist()))
                self.assertEqual([i.filename for i in z.infolist()], sorted(i.filename for i in z.infolist()))


if __name__ == "__main__":
    unittest.main()
