"""profile_export (Task 6, MILESTONE 1): assembly of the per-row cell builders into one ``orgsci-profile/v2`` file, the run block,
the privacy scan, the k floor and the ``export-profile`` command. Synthetic runs only (the oracle client): zero model
calls, zero network calls. The demo run is built once per module."""
import contextlib
import copy
import dataclasses
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from unittest import mock

from second_scribe import pipeline, prereg
from second_scribe import profile_export as prof
from second_scribe import profile_base as pb
from second_scribe import profile_floors as pf
from second_scribe.cli import DEMO_CONFIG, main

REPO = Path(__file__).resolve().parent.parent
SKELETON = json.loads((REPO / "tests" / "data" / "profile_v2_skeleton.json").read_text(encoding="utf-8"))
ROW_ORDER = ["lead", "ledger", 1, 2, 3, 4, 5, 6, 7, 8, 9]
BANNED = ("validated", "ground truth", "first", "replicable")
HEX64 = re.compile(r"\b[0-9a-f]{64}\b")


def head_commit():
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True)
    return r.stdout.strip()


def names(cells):
    return [(c["name"], c.get("x")) for c in cells]


def rows_by_id(profile):
    return {r["row"]: r for r in profile["rows"]}


def cell_of(profile, row, name, x=None):
    hits = [c for c in rows_by_id(profile)[row]["cells"] if c["name"] == name and c.get("x") == x]
    assert len(hits) == 1, (row, name, x, len(hits))
    return hits[0]


class Base(unittest.TestCase):
    """The oracle demo, run once; ``cls.ctx`` is its profile context, ``cls.profile`` the assembled profile."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ss-tests-e2-t6-"))
        cls.out = cls.tmp / "demo"
        r = subprocess.run([sys.executable, "-m", "second_scribe.cli", "demo", "--out", str(cls.out), "--client", "oracle"],
                           cwd=REPO, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stdout + r.stderr)
        cls.ctx = pb.open_context(Path(DEMO_CONFIG), cls.out)
        cls.profile = prof.build_profile(cls.ctx)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def nine(self):
        d = json.loads(Path(DEMO_CONFIG).read_text(encoding="utf-8"))
        d["base_dir"] = str(Path(DEMO_CONFIG).parent)
        pids = sorted(d["corpus"]["roles"])[:9]
        d["corpus"]["roles"] = {p: d["corpus"]["roles"][p] for p in pids}
        path = self.tmp / "config9.json"
        path.write_text(json.dumps(d), encoding="utf-8")
        return path


class TestAssembly(Base):
    def test_demo_profile_structure_matches_skeleton(self):
        p = self.profile
        self.assertEqual(p["schema"], "orgsci-profile/v2")
        self.assertEqual([r["row"] for r in p["rows"]], ROW_ORDER)
        sk = {r["row"]: r for r in SKELETON["rows"]}
        got = rows_by_id(p)
        for rid in ROW_ORDER:
            self.assertEqual(names(got[rid]["cells"]), [(c["name"], c.get("x")) for c in sk[rid]["cells"]], rid)
            self.assertEqual(got[rid]["label_key"], sk[rid]["label_key"], rid)

    def test_top_level_fields(self):
        p = self.profile
        self.assertEqual(sorted(p), ["pooling", "population", "reference", "rows", "run", "schema", "state"])
        n = len(self.ctx.analysed)
        self.assertEqual(p["population"], {"label": f"{n} of {len(self.ctx.run.people)}", "n_people": n,
                                           "n_total": len(self.ctx.run.people)})
        self.assertEqual(p["pooling"]["meta_analysis"], False)
        self.assertEqual(p["pooling"]["rule"], "side_by_side")
        self.assertEqual(p["reference"]["type"], "participant-endorsed")
        self.assertEqual(set(p["reference"]["map_size"]),
                         {"ai_boxes", "endorsed_boxes", "ai_per_map", "endorsed_per_map", "ai_per_endorsed",
                          "reading3_map_size", "source"})
        m3 = p["reference"]["map_size"]["reading3_map_size"]
        self.assertEqual(sorted(m3), ["edges_per_map", "nodes_per_map", "note", "over_ai_nodes", "source"])
        lead = {(c["name"]): c for c in rows_by_id(p)["lead"]["cells"] if c.get("x") is None}
        self.assertEqual(m3["nodes_per_map"], lead["no_interview_nodes_per_map"]["value"])
        self.assertEqual(m3["edges_per_map"], lead["no_interview_edges_per_map"]["value"])
        self.assertEqual(m3["over_ai_nodes"], lead["no_interview_over_ai_nodes"]["value"])
        self.assertEqual(m3["note"], "pooled_only")
        ms = p["reference"]["map_size"]
        self.assertEqual(ms["ai_boxes"], cell_of(p, 2, "ai_boxes")["value"])
        self.assertEqual(ms["endorsed_boxes"], cell_of(p, 2, "endorsed_boxes")["value"])
        self.assertEqual(ms["ai_per_map"], cell_of(p, 2, "ai_per_map")["value"])

    def test_row_level_fields(self):
        sk_codes = re.compile(r"^[A-Z0-9]{1,3}$")
        for r in self.profile["rows"]:
            n = len(self.ctx.analysed)
            self.assertEqual(r["n_people"], n)
            self.assertEqual(r["population"], self.profile["population"]["label"])
            self.assertEqual(r["source"], sorted(set(r["source"])))
            self.assertTrue(r["source"] and all(s.startswith("kit:") and "+" not in s for s in r["source"]), r["row"])
            if r["row"] != 4:         # only computed or partial cells: a not_run cell reads nothing
                ran = {s for c in r["cells"] if c.get("state") != "not_run" for s in c["source"].split("+")}
                self.assertEqual(set(r["source"]), ran, r["row"])
            self.assertTrue(r["rests_on"] and all(sk_codes.match(c) for c in r["rests_on"]), r["row"])
            self.assertTrue(r["qualifiers"], r["row"])
            for c in r["cells"]:
                for code in c.get("rests_on", []):
                    self.assertIn(code, r["rests_on"], (r["row"], c["name"]))
        rows = rows_by_id(self.profile)
        # row 4 takes its row-level fields from row4_meta
        from second_scribe import profile_links as pl
        meta = pl.row4_meta(rows[4]["cells"])
        for k in ("rests_on", "qualifiers", "source"):
            self.assertEqual(rows[4][k], meta[k], k)
        self.assertEqual(rows[4]["band"], meta["band"])

    def test_row_source_leaves_out_not_run_only_sources(self):
        rows = rows_by_id(self.profile)
        self.assertNotIn("kit:sidejudge", rows[9]["source"])                  # its 7 side-study cells are all not_run
        self.assertIn("kit:audits", rows[9]["source"])                        # the determinism count ran
        self.assertNotIn("kit:a7_additions_source", rows[6]["source"])        # row 6's a7 cells are not_run on the demo
        self.assertTrue(any(c.get("state") == "not_run" and c["source"] == "kit:a7_additions_source" for c in rows[6]["cells"]))
        # all cells of a row not_run: no source at all
        cells = [pb.not_run("ai_per_map", ["ladder"], x="rung1", unit="ai_box", kind="count", source="kit:a12_ladder")]
        self.assertEqual(prof._row(8, cells, 12, "12 of 12")["source"], [])

    def test_row_bands(self):
        rows = rows_by_id(self.profile)
        # copied from the headline cell's band (the analysis build's rule)
        for rid, name, x in (("ledger", "attested_absent_from_map", "primary"), (1, "kept", None), (3, "precision", "primary"),
                             (5, "matcher_free_rate", "judge"), (6, "pull", "canonical"), (9, "attested_recall", "judge")):
            c = cell_of(self.profile, rid, name, x)
            if "band" in c:
                self.assertEqual(rows[rid]["band"], c["band"], rid)
            else:
                self.assertNotIn("band", rows[rid], rid)
        self.assertEqual(rows[1]["band"]["over"], "8_settings")
        self.assertEqual(rows[9]["band"]["over"], "labellers")
        self.assertNotIn("band", rows[2])
        # lead and row 7: primary against lenient listening_added
        for rid in ("lead", 7):
            vals = [cell_of(self.profile, rid, "listening_added", x)["value"] for x in ("primary", "lenient")]
            self.assertEqual(rows[rid]["band"], pb.band(min(vals), max(vals), "lenient"))

    def test_row8_band_is_min_max_of_the_four_ai_per_map_cells(self):
        vals = [cell_of(self.profile, 8, "ai_per_map", x)["value"] for x in ("rung1", "rung2", "remedy", "placebo")]
        b = rows_by_id(self.profile)[8]["band"]
        self.assertEqual((b["lo"], b["hi"], b["over"]), (min(vals), max(vals), "variants"))
        self.assertEqual([m["x"] for m in b["members"]], ["rung1", "rung2", "remedy", "placebo"])
        self.assertEqual([m["value"] for m in b["members"]], vals)

    def test_row8_band_absent_when_one_of_the_four_is_not_run(self):
        cells = copy.deepcopy(rows_by_id(self.profile)[8]["cells"])
        i = next(i for i, c in enumerate(cells) if c["name"] == "ai_per_map" and c["x"] == "remedy")
        cells[i] = pb.not_run("ai_per_map", ["ladder"], x="remedy", unit="ai_box", kind="count", source="kit:a12_ladder")
        self.assertIsNone(prof.row8_band(cells))
        self.assertIsNotNone(prof.row8_band(rows_by_id(self.profile)[8]["cells"]))

    def test_unjudged_nodes_note_on_tampered_copy(self):
        cells = copy.deepcopy(rows_by_id(self.profile)[2]["cells"])
        self.assertEqual(prof.mark_unjudged_nodes(copy.deepcopy(cells)), cells)          # judged = all nodes: untouched
        self.assertNotIn("note", next(c for c in cells if c["name"] == "ai_per_map"))
        nodes = next(c for c in cells if c["name"] == "ai_nodes_per_map")
        nodes["value"] = round(nodes["value"] + 1.0, 3)                                   # a node the judge never labelled
        marked = prof.mark_unjudged_nodes(cells)
        self.assertEqual(next(c for c in marked if c["name"] == "ai_per_map")["note"], "unjudged_nodes")
        self.assertNotIn("note", next(c for c in marked if c["name"] == "ai_nodes_per_map"))

    def test_no_banned_words(self):
        text = json.dumps(self.profile).lower()
        for w in BANNED:
            self.assertIsNone(re.search(r"\b" + re.escape(w) + r"\b", text), w)

    def test_state_synthetic_on_demo(self):
        self.assertEqual(self.profile["state"], "synthetic")
        self.assertEqual(prof.file_state(self.ctx.cfg), "synthetic")

    def test_state_pending_when_not_synthetic_and_never_reported(self):
        for kind in ("real", "anything"):
            cfg = type("Cfg", (), {"corpus": {"kind": kind}})()
            self.assertEqual(prof.file_state(cfg), "pending")

    def test_no_pid_or_role_in_the_file_outside_nothing(self):
        text = json.dumps(self.profile)
        for pid in self.ctx.cfg.corpus["roles"]:
            self.assertNotIn(pid, text)
        self.assertEqual(prof.privacy_scan(self.profile, forbidden=prof.forbidden_tokens(self.ctx)), [])

    def test_only_run_holds_hex(self):
        body = json.dumps({k: v for k, v in self.profile.items() if k != "run"})
        self.assertIsNone(HEX64.search(body))
        self.assertIsNotNone(HEX64.search(json.dumps(self.profile["run"])))

    def test_no_ladder_run_has_no_row8_band_and_ladder_cells_not_run(self):
        cfg_d = json.loads(Path(DEMO_CONFIG).read_text(encoding="utf-8"))
        cfg_d["base_dir"] = str(Path(DEMO_CONFIG).parent)
        cfg_d.pop("ladder")
        p = self.tmp / "noladder.json"
        p.write_text(json.dumps(cfg_d), encoding="utf-8")
        ctx = pb.open_context(p, self.out)
        profile = prof.build_profile(ctx)
        r8 = rows_by_id(profile)[8]
        self.assertNotIn("band", r8)
        for c in r8["cells"]:
            self.assertEqual(c["state"], "not_run", c["name"])
            self.assertIn("ladder", c["needs"])
        self.assertEqual(cell_of(profile, 2, "ai_per_endorsed", "rung1")["state"], "not_run")


class TestRow9(Base):
    def test_row9_order_and_sidejudge_cells(self):
        cells = prof.row9_cells(self.ctx)
        sk = [(c["name"], c.get("x")) for c in next(r for r in SKELETON["rows"] if r["row"] == 9)["cells"]]
        self.assertEqual(names(cells), sk)
        for c in cells:
            if c["name"] in ("flip_rate", "canonical_ari", "label_order_changed_rate") or (
                    c["name"] == "identical_requests" and c["x"] in ("modality", "alignment", "canonical_names")):
                self.assertEqual((c["state"], c["needs"], c["value"]), ("not_run", ["sidejudge"], None), c["name"])

    def test_identical_requests_from_audits_json(self):
        c = next(c for c in prof.row9_cells(self.ctx) if c.get("x") == "run2_determinism_audit")
        audits = json.loads((self.out / "aggregates" / "audits.json").read_text(encoding="utf-8"))
        counts = next(r["counts"] for r in audits["stages"]["J"] if r["name"] == "determinism_judge")
        self.assertEqual((c["name"], c["unit"], c["kind"]), ("identical_requests", "request", "count"))
        self.assertEqual((c["value"], c["n"]), (counts["n_identical"], counts["n_checked"]))
        self.assertNotIn("state", c)

    def test_identical_requests_not_run_without_audits(self):
        tmp = Path(tempfile.mkdtemp(prefix="ss-tests-e2-t6-noaud-"))
        try:
            out = tmp / "o"
            shutil.copytree(self.out, out)
            (out / "aggregates" / "audits.json").unlink()
            ctx = pb.open_context(Path(DEMO_CONFIG), out)
            c = next(c for c in prof.row9_cells(ctx) if c.get("x") == "run2_determinism_audit")
            self.assertEqual((c["state"], c["value"], c["reason_code"]), ("not_run", None, "no_determinism_audit"))
            # the key absent from a present file
            (out / "aggregates" / "audits.json").write_text(json.dumps({"stages": {"J": []}}), encoding="utf-8")
            c = next(c for c in prof.row9_cells(ctx) if c.get("x") == "run2_determinism_audit")
            self.assertEqual((c["state"], c["reason_code"]), ("not_run", "no_determinism_audit"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_labeller_cells_equal_labeller_numbers_and_carry_members(self):
        nums = pf.labeller_numbers(self.ctx)
        cells = prof.row9_cells(self.ctx)
        for name, metric in (("attested_recall", "node_recall"), ("attested_absent_from_map", "attested_absent_from_map"),
                             ("unanchored", "unanchored"), ("matcher_free_rate", "matcher_free_rate"),
                             ("quote_actual_drawn_as_wished", "contradiction_actual_as_wished"),
                             ("contaminated_share", "contaminated_share")):
            c = next(c for c in cells if c["name"] == name and c.get("x") == "judge")
            expect = nums["judge"][metric]
            if expect is None:
                self.assertEqual(c.get("state"), "not_run", name)
                continue
            self.assertAlmostEqual(c["value"], expect, places=3, msg=name)
            if "band" in c:
                self.assertEqual(c["band"]["over"], "labellers")
                self.assertEqual([m["x"] for m in c["band"]["members"]], [k for k in ("judge", "judge2", "keyword")
                                                                        if k in nums and nums[k][metric] is not None])
            else:
                self.assertEqual(c["state"], "partial_not_run")


class TestRunBlock(Base):
    def test_run_block_keys_and_nulls(self):
        run = self.profile["run"]
        self.assertEqual(sorted(run), ["declaration_log", "gate_chain", "kit", "models", "prompt_set", "reading3_prompt",
                                       "run_chain_heads", "settings"])
        self.assertIsNone(run["gate_chain"])
        self.assertIsNone(run["declaration_log"])
        self.assertIsNone(run["reading3_prompt"]["agree"])

    def test_kit_commits(self):
        kit = self.profile["run"]["kit"]
        self.assertEqual(kit["release_commit"], head_commit())
        starts = [json.loads(l)["payload"] for l in (self.out / "logs" / "run.jsonl").read_text().splitlines()
                  if '"run_start"' in l and json.loads(l)["record_type"] == "run_start"]
        self.assertEqual(kit["run_commits"], [s["code_commit"] for s in starts])
        self.assertTrue(kit["run_commits"])
        # village E6: the code hash every run_start logged (one value here: the demo ran one kit)
        self.assertEqual({s["code_sha256"] for s in starts}, {kit["code_sha256"]})
        self.assertRegex(kit["code_sha256"], r"^[0-9a-f]{64}$")

    def test_prompt_set_and_reading3_prompt(self):
        run = self.profile["run"]
        start = next(json.loads(l)["payload"] for l in (self.out / "logs" / "run.jsonl").read_text().splitlines()
                     if json.loads(l)["record_type"] == "run_start")
        self.assertEqual(run["prompt_set"], {"q_phase_sha256": start["prompt_set_sha256"],
                                             "j_m_phase_sha256": start["prompt_set_sha256"]})
        man = json.loads((REPO / "prompts" / "MANIFEST.json").read_text(encoding="utf-8"))
        r3 = run["reading3_prompt"]
        self.assertEqual(r3["sha256"], man["files"]["prior_only.p0.v2.txt"])
        self.assertEqual(r3["path"], "prompts/prior_only.p0.v2.txt")

    def test_run_chain_heads_from_manifest(self):
        man = json.loads((self.out / "logs" / "run.manifest.json").read_text(encoding="utf-8"))
        h = self.profile["run"]["run_chain_heads"]
        self.assertEqual(h["head"], man["head_hash"])
        self.assertEqual(h["records"], man["record_count"])

    def test_settings(self):
        s = self.profile["run"]["settings"]
        self.assertEqual(s["bootstrap_resamples"], pb.B)
        self.assertEqual(s["bootstrap_seed"], pb.SEED)
        self.assertEqual(s["interval_method"], "cluster_bootstrap")
        self.assertEqual(s["matcher_primary"], {"lexical_threshold": 0.6, "judge_label": "same"})
        self.assertEqual(s["matcher_lenient"], {"lexical_threshold": 0.4, "judge_label": "same_or_close"})
        self.assertEqual(s["gate_k_people"], 10)
        self.assertNotIn("k_floor_people", s)
        self.assertEqual(s["reconstructor_temperature"], self.ctx.cfg.specs[0]["temperature"])
        self.assertEqual(s["reading3_temperature"], (self.ctx.cfg.reading3 or self.ctx.cfg.prior_only)["temperature"])
        self.assertEqual(sorted(s["match_settings"]), sorted(pb.SETTINGS))

    def test_b_and_seed_are_recorded(self):
        ctx = dataclasses.replace(self.ctx, B=200, seed=7)
        run = prof.build_run_block(ctx, None)
        self.assertEqual((run["settings"]["bootstrap_resamples"], run["settings"]["bootstrap_seed"]), (200, 7))

    def test_models_default_source_and_model_info(self):
        models = {m["role"]: m for m in self.profile["run"]["models"]}
        self.assertEqual(sorted(models), ["judge", "reconstructor"])
        for m in models.values():
            self.assertEqual((m["quantisation"], m["weights"]), ("[SOURCE]", "[SOURCE]"))
            self.assertEqual(m["model_id"], "oracle")
        run = prof.build_run_block(self.ctx, {"reconstructor": {"quantisation": "AWQ", "weights": "org.model-x"}})
        models = {m["role"]: m for m in run["models"]}
        self.assertEqual((models["reconstructor"]["quantisation"], models["reconstructor"]["weights"]), ("AWQ", "org.model-x"))
        self.assertEqual(models["judge"]["quantisation"], "[SOURCE]")
        with self.assertRaises(prof.ProfileRefused):
            prof.build_run_block(self.ctx, {"reconstructor": {"quantisation": 4}})

    def test_second_judge_role_name_is_the_v2_one(self):
        recs = [{"record_type": "run_start", "payload": {"models": {"recon": "m-a", "judge": "m-b", "judge2": "gemma-x"}}}]
        models = {m["role"]: m for m in prof._models(recs, {"judge2": {"quantisation": "Q4"}})}
        self.assertEqual(sorted(models), ["judge", "reconstructor", "second_judge_family"])
        self.assertEqual((models["second_judge_family"]["model_id"], models["second_judge_family"]["quantisation"],
                          models["second_judge_family"]["family"]), ("gemma-x", "Q4", "gemma"))
        models = {m["role"]: m for m in prof._models(recs, {"second_judge_family": {"weights": "w"}})}
        self.assertEqual(models["second_judge_family"]["weights"], "w")
        prof.check_model_info({"second_judge_family": {"weights": "w"}})

    def test_refuses_when_the_run_log_does_not_verify(self):
        tmp = Path(tempfile.mkdtemp(prefix="ss-tests-e2-t6-bad-"))
        try:
            out = tmp / "o"
            shutil.copytree(self.out, out)
            with open(out / "logs" / "run.jsonl", "a", encoding="utf-8") as fh:
                fh.write('{"x": 1}\n')
            ctx = pb.open_context(Path(DEMO_CONFIG), out)
            with self.assertRaises(prof.ProfileRefused) as cm:
                prof.build_run_block(ctx, None)
            self.assertIn("verify", str(cm.exception))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestPrivacyAndFloor(Base):
    def test_privacy_scan_clean_on_demo(self):
        self.assertEqual(prof.privacy_scan(self.profile), [])

    def test_privacy_scan_rejects_planted_pid_and_hex(self):
        p = copy.deepcopy(self.profile)
        p["rows"][2]["cells"][0]["map_id"] = "m1"
        self.assertTrue(any("forbidden key" in s for s in prof.privacy_scan(p)))
        p = copy.deepcopy(self.profile)
        p["rows"][2]["cells"][0]["note"] = "ab" * 32
        self.assertTrue(any("64-hex" in s for s in prof.privacy_scan(p)))
        # hex inside the run block is allowed
        p = copy.deepcopy(self.profile)
        p["run"]["extra"] = "cd" * 32
        self.assertEqual(prof.privacy_scan(p), [])
        # a pid planted in a free field is refused
        p = copy.deepcopy(self.profile)
        pid = sorted(self.ctx.cfg.corpus["roles"])[0]
        p["population"]["label"] = pid
        tokens = prof.forbidden_tokens(self.ctx)
        self.assertTrue(any("forbidden value" in s for s in prof.privacy_scan(p, forbidden=tokens)))
        # ... also as a dict key, and inside a longer string
        p = copy.deepcopy(self.profile)
        p["rows"][2]["cells"][0][pid] = 1
        self.assertTrue(any("forbidden value" in s for s in prof.privacy_scan(p, forbidden=tokens)))
        # an interviewer code and a role name too
        for tok in ("I1", "owner"):
            p = copy.deepcopy(self.profile)
            p["population"]["label"] = f"12 of 12 ({tok})"
            self.assertTrue(any("forbidden value" in s for s in prof.privacy_scan(p, forbidden=tokens)), tok)

    def test_value_check_matches_underscore_joined_tokens(self):
        tokens = prof.forbidden_tokens(self.ctx)
        pid = sorted(self.ctx.cfg.corpus["roles"])[0]
        for text in (f"{pid}_primary", f"map_{pid}", "owner_map", "map_owner", "I1_x", "x_I1"):
            p = copy.deepcopy(self.profile)
            p["population"]["label"] = text
            self.assertTrue(any("forbidden value" in s for s in prof.privacy_scan(p, forbidden=tokens)), text)
        for text in ("ownership", "I10", "Isolated", "officers"):               # short codes need [A-Za-z0-9] boundaries
            p = copy.deepcopy(self.profile)
            p["population"]["label"] = text
            self.assertEqual(prof.privacy_scan(p, forbidden=tokens), [], text)

    def test_uppercase_initials_match_case_sensitively(self):
        """Ruling R31 (final review A1): a forbidden token of at most 3 characters, all upper case (interviewer initials),
        matches case-sensitively on token boundaries, so the kit's own text ("12 of 12", "side_by_side") does not refuse
        an honest export; the same initials planted in a scanned field are still refused."""
        tokens = prof.forbidden_tokens(self.ctx)
        initials = {"pids": tokens["pids"], "codes": tokens["codes"] | {"OF", "BY", "IS", "IN"}}
        self.assertEqual(prof.privacy_scan(self.profile, forbidden=initials), [])
        prof.write_profile(copy.deepcopy(self.profile), self.tmp / "r31-ok.json", forbidden=initials)
        for code in ("OF", "BY", "IS", "IN"):
            for text in (f"12 of 12 {code}", f"{code}_map", f"map ({code})"):
                p = copy.deepcopy(self.profile)
                p["population"]["label"] = text
                self.assertTrue(any("forbidden value" in s for s in prof.privacy_scan(p, forbidden=initials)), text)
            p = copy.deepcopy(self.profile)
            p["rows"][2]["population"] = code
            with self.assertRaises(prof.ProfileRefused):
                prof.write_profile(p, self.tmp / "r31-bad.json", forbidden=initials)
            self.assertFalse((self.tmp / "r31-bad.json").exists())
        # longer tokens keep R24's case-insensitive match; lower-case short codes too
        for codes, text in (({"ABCD"}, "abcd"), ({"of"}, "OF"), ({"Ab"}, "ab")):
            p = copy.deepcopy(self.profile)
            p["population"]["label"] = text
            self.assertTrue(prof.privacy_scan(p, forbidden={"pids": set(), "codes": codes}), (codes, text))
        # a short upper-case pid (initials) follows the same rule
        self.assertEqual(prof.privacy_scan(self.profile, forbidden={"pids": {"OF"}, "codes": set()}), [])

    def test_refusal_messages_never_echo_a_key_or_value(self):
        """Final review A2: messages name fields and positions; a planted key or value never appears in them."""
        tokens = prof.forbidden_tokens(self.ctx)
        pid = "SYN-OW07"
        self.assertIn(pid, tokens["pids"])
        plants = {
            "pid key": lambda p: p["rows"][2]["cells"][0].__setitem__(pid, 1),
            "fragment key": lambda p: p["rows"][3]["cells"][0].__setitem__("participant_note", 1),
            # name and row are closed vocabulary (R23, not refused by themselves): a second plant in the same cell makes
            # a message that locates the cell, and the location must not carry the name or row id
            "pid as cell name": lambda p: p["rows"][2]["cells"][0].update(name=pid, value="a b"),
            "sentence as cell name": lambda p: p["rows"][2]["cells"][1].update(name="maria_said_it", value="a b"),
            "pid as row id": lambda p: (p["rows"][2].__setitem__("row", pid),
                                        p["rows"][2]["cells"][0].__setitem__("value", "a b")),
            "lowercase pid key": lambda p: p["rows"][2].__setitem__("syn-ow07_map", 1),
            "pid as unit, map n": lambda p: p["rows"][0]["cells"][0].update(unit="map", n=3, name=pid),
            "long string": lambda p: p["rows"][2]["cells"][0].__setitem__("kind", pid + "x" * 50),
        }
        for tag, edit in plants.items():
            with self.subTest(tag):
                p = copy.deepcopy(self.profile)
                edit(p)
                with self.assertRaises(prof.ProfileRefused) as cm:
                    prof.write_profile(p, self.tmp / "a2.json", forbidden=tokens)
                text = " ".join(cm.exception.reasons)
                for planted in (pid, pid.lower(), "participant_note", "maria"):
                    self.assertNotIn(planted, text, tag)
                # without tokens (the pooler's case) the structural messages still hold no planted string
                for planted in (pid, "participant_note"):
                    self.assertNotIn(planted, " ".join(prof.privacy_scan(p)), tag)
        self.assertFalse((self.tmp / "a2.json").exists())
        # the k-floor reason and model-info refusals echo nothing either
        self.assertNotIn("Maria", prof.k_floor_reason("Maria said"))
        with self.assertRaises(prof.ProfileRefused) as cm:
            prof.check_model_info({"judge": {f"{pid} notes": "x"}})
        self.assertNotIn(pid, str(cm.exception))
        with self.assertRaises(prof.ProfileRefused) as cm:
            prof.check_model_info({f"{pid}": {"weights": "x"}})
        self.assertNotIn(pid, str(cm.exception))

    def test_value_check_scans_the_run_block_and_model_info(self):
        """Final review A3: free text in the run block (the --model-info values, any extra field) is value-scanned; the
        kit-written run fields (hashes, commits, model ids, roles) are not, so the honest block passes."""
        tokens = prof.forbidden_tokens(self.ctx)
        self.assertEqual(prof.value_scan(self.profile, tokens), [])
        for where in ("weights", "quantisation", "thinking"):
            p = copy.deepcopy(self.profile)
            p["run"]["models"][0][where] = "tuned on SYN-OW07 interviews"
            hits = prof.value_scan(p, tokens)
            self.assertTrue(hits, where)
            self.assertTrue(all("/run/models[0]" in h for h in hits), hits)
            with self.assertRaises(prof.ProfileRefused):
                prof.write_profile(p, self.tmp / "a3.json", forbidden=tokens)        # the export and calibrate path
        p = copy.deepcopy(self.profile)
        p["run"]["note_from_lab"] = "owner of the plant"
        self.assertTrue(prof.value_scan(p, tokens))
        self.assertFalse((self.tmp / "a3.json").exists())
        # build_profile scans the model-info text given on the command line
        info = {"judge": {"weights": "SYN-OW07"}}
        with self.assertRaises(prof.ProfileRefused):
            prof.build_profile(self.ctx, info)

    def test_reference_type_is_a_short_code(self):
        """Final review A4: --reference-type is at most 40 characters of [a-z0-9_-]; anything else exits 2, writes
        nothing and is not echoed."""
        self.assertEqual(prof.check_reference_type("participant-endorsed"), "participant-endorsed")
        self.assertEqual(prof.check_reference_type("lab_drawn-2"), "lab_drawn-2")
        for bad in ("Maria said the plant will close", "x" * 41, "", "Participant-Endorsed", None, 3, "a b"):
            with self.assertRaises(prof.ProfileRefused, msg=repr(bad)) as cm:
                prof.check_reference_type(bad)
            if isinstance(bad, str) and bad:
                self.assertNotIn(bad, str(cm.exception))
            with self.assertRaises(prof.ProfileRefused):
                prof.build_profile(self.ctx, reference_type=bad)
        out = self.tmp / "a4.json"
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = main(["export-profile", "--config", str(DEMO_CONFIG), "--out-dir", str(self.out), "--profile",
                       str(out), "--reference-type", "Maria said the plant will close"])
        self.assertEqual(rc, 2)
        self.assertIn("refused", err.getvalue())
        self.assertNotIn("Maria", err.getvalue())
        self.assertFalse(out.exists())

    def test_export_input_errors_exit_2_without_traceback(self):
        """Final review A8: a missing or unreadable config, or bad model info, is a refusal (exit 2), never a traceback."""
        bad = self.tmp / "bad-config.json"
        bad.write_text("{not json", encoding="utf-8")
        listed = self.tmp / "list-config.json"
        listed.write_text("[1, 2]", encoding="utf-8")
        info = self.tmp / "info.json"
        info.write_text("[1]", encoding="utf-8")
        cases = {"missing config": ["--config", str(self.tmp / "nope.json")],
                 "bad json config": ["--config", str(bad)],
                 "config not an object": ["--config", str(listed)],
                 "model info not an object": ["--config", str(DEMO_CONFIG), "--out-dir", str(self.out),
                                              "--model-info", str(info)],
                 "missing out-dir": ["--config", str(DEMO_CONFIG), "--out-dir", str(self.tmp / "no-out")]}
        for tag, argv in cases.items():
            with self.subTest(tag):
                out = self.tmp / "a8.json"
                err = io.StringIO()
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    rc = main(["export-profile", *argv, "--profile", str(out)])
                self.assertEqual(rc, 2, err.getvalue())
                self.assertIn("refused", err.getvalue())
                self.assertNotIn("Traceback", err.getvalue())
                self.assertFalse(out.exists())

    def test_make_export_profile_target_and_command_list(self):
        """Final review A10: `make export-profile CONFIG= PROFILE= [OUTDIR=] [WITHHOLD=1]` (PROFILE-SCHEMA section 14) and
        the CLI docstring's command list name export-profile and pool."""
        from second_scribe import cli as climod
        doc_cmds = {ln.split()[0] for ln in climod.__doc__.splitlines() if ln.startswith("  ") and ln.split()}
        self.assertTrue({"export-profile", "pool", "phantom", "calibrate"} <= doc_cmds, doc_cmds)
        r = subprocess.run(["make", "export-profile"], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage: make export-profile", r.stdout + r.stderr)
        out = self.tmp / "make-export.json"
        r = subprocess.run(["make", "export-profile", f"CONFIG={DEMO_CONFIG}", f"PROFILE={out}", f"OUTDIR={self.out}",
                            "WITHHOLD=1"], cwd=REPO, capture_output=True, text=True, timeout=1800)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        d = json.loads(out.read_text(encoding="utf-8"))
        self.assertIsNone(d["run"]["run_chain_heads"])                      # WITHHOLD=1 passed the flag
        self.assertEqual(d["run"]["run_chain_heads_source"], "available_from_authors")

    def test_lab_entered_fields_are_codes_scanned_ignoring_case(self):
        """Fix round 2, N2 + N3 (Ruling R32): --model-info values (family included) are codes of at most 40 characters of
        [A-Za-z0-9._:+-]; they and reference.type are scanned ignoring case for every token, while R31's case-sensitive
        match stays for the kit's own text."""
        for good in ("Qwen3-235B-A22B", "AWQ", "Q4_K_M", "sha256:0a1b", "rev+2", "bf16", "org/model-x", "Qwen/Qwen3-32B"):
            prof.check_model_info({"judge": {"family": good, "weights": good}})
        for bad in ("tuned on SYN-OW07 transcripts", "/Users/x", "a/b/c", "org/", "x" * 41, "a b", "naïve"):
            with self.assertRaises(prof.ProfileRefused, msg=bad) as cm:
                prof.check_model_info({"judge": {"family": bad}})
            self.assertNotIn(bad, str(cm.exception))
        tokens = prof.forbidden_tokens(self.ctx)
        lab = {"pids": tokens["pids"] | {"P17"}, "codes": tokens["codes"] | {"JD", "OF", "IN"}}
        self.assertEqual(prof.privacy_scan(self.profile, forbidden=lab), [])     # the kit's own text: R31 holds
        for field in ("family", "weights", "quantisation", "thinking"):
            for planted in ("SYN-OW07", "jd", "JD", "p17", "x_jd"):
                with self.subTest(field=field, planted=planted):
                    p = copy.deepcopy(self.profile)
                    p["run"]["models"][0][field] = planted
                    self.assertTrue(prof.value_scan(p, lab))
            p = copy.deepcopy(self.profile)
            p["run"]["models"][0][field] = "Qwen3-235B-A22B"                         # an honest family passes
            self.assertEqual(prof.value_scan(p, lab), [], field)
        for planted in ("jd", "p17", "jd-maps"):
            p = copy.deepcopy(self.profile)
            p["reference"]["type"] = planted
            self.assertTrue(prof.value_scan(p, lab), planted)
        # end to end: a family typed with --model-info holding a pid is refused by build_profile
        with self.assertRaises(prof.ProfileRefused):
            prof.build_profile(self.ctx, {"judge": {"family": "SYN-OW07"}})
        with mock.patch.object(prof, "forbidden_tokens", return_value=lab):
            with self.assertRaises(prof.ProfileRefused):
                prof.build_profile(self.ctx, {"judge": {"family": "jd"}})

    def test_value_check_skips_the_closed_vocabulary(self):
        # an interviewer coded JS appears in rests_on (row 6 lists JS) and passes; roles lead / judge / kind / R collide with
        # row ids, x labels, names and kinds and pass
        self.assertIn("JS", rows_by_id(self.profile)[6]["rests_on"])
        for codes in ({"JS"}, {"lead", "judge", "keyword", "primary", "share", "count", "node"}):
            self.assertEqual(prof.privacy_scan(self.profile, forbidden={"pids": set(), "codes": codes}), [], codes)
        # a plain iterable counts as codes
        self.assertEqual(prof.privacy_scan(self.profile, forbidden={"JS", "lead"}), [])
        # the same strings in a FREE field are refused
        p = copy.deepcopy(self.profile)
        p["population"]["label"] = "JS"
        self.assertTrue(prof.privacy_scan(p, forbidden={"pids": set(), "codes": {"JS"}}))
        # a closed-vocabulary field is not scanned even for a pid
        p = copy.deepcopy(self.profile)
        p["rows"][2]["cells"][0]["x"] = "SYN-OW01"
        self.assertEqual(prof.privacy_scan(p, forbidden={"pids": {"SYN-OW01"}, "codes": set()}), [])

    def test_write_profile_runs_the_value_check_when_given_tokens(self):
        p = copy.deepcopy(self.profile)
        p["population"]["label"] = sorted(self.ctx.cfg.corpus["roles"])[0]
        path = self.tmp / "val.json"
        prof.write_profile(copy.deepcopy(p), self.tmp / "val-nocheck.json")           # backward compatible: no tokens, no check
        with self.assertRaises(prof.ProfileRefused) as cm:
            prof.write_profile(p, path, forbidden=prof.forbidden_tokens(self.ctx))
        self.assertTrue(any("forbidden value" in r for r in cm.exception.reasons))
        self.assertFalse(path.exists())
        prof.write_profile(self.profile, self.tmp / "val-ok.json", forbidden=prof.forbidden_tokens(self.ctx))

    def test_privacy_scan_other_rules(self):
        p = copy.deepcopy(self.profile)
        p["rows"][2]["cells"][0]["x"] = "interviewer_3"
        self.assertTrue(any("suspicious x" in s for s in prof.privacy_scan(p)))
        p = copy.deepcopy(self.profile)
        unit_map = next(c for c in p["rows"][0]["cells"] if c.get("unit") == "map")
        unit_map["n"] = 3
        self.assertTrue(any("map-level" in s for s in prof.privacy_scan(p)))
        for n in (len(self.ctx.analysed) - 1, len(self.ctx.analysed) + 1):       # the port's rule: n must equal the population
            p = copy.deepcopy(self.profile)
            unit_map = next(c for c in p["rows"][0]["cells"] if c.get("unit") == "map")
            unit_map["n"] = n
            self.assertTrue(any("map-level" in s for s in prof.privacy_scan(p)), n)
        p = copy.deepcopy(self.profile)
        next(c for c in p["rows"][0]["cells"] if c.get("unit") == "map").pop("n")        # n absent is allowed (the port)
        self.assertEqual(prof.privacy_scan(p), [])
        p = copy.deepcopy(self.profile)
        p["rows"][0]["cells"][0]["unit"] = "map"                                          # every map cell, not only the lead's
        p["rows"][0]["cells"][0]["n"] = len(self.ctx.analysed) - 1
        self.assertTrue(any("map-level" in s for s in prof.privacy_scan(p)))
        p = copy.deepcopy(self.profile)
        p["rows"][4]["n_people"] = 9
        self.assertTrue(any("below K" in s for s in prof.privacy_scan(p)))
        p = copy.deepcopy(self.profile)
        p["rows"][4]["cells"][0]["value"] = float("nan")
        self.assertTrue(any("non-finite" in s for s in prof.privacy_scan(p)))
        # forbidden fragments in keys of the run block are not scanned (the scan is outside run)
        p = copy.deepcopy(self.profile)
        p["run"]["models"][0]["interviewer"] = "x"
        self.assertEqual(prof.privacy_scan(p), [])

    def test_demo_export_passes_lab_limits(self):
        """The oracle demo, with and without the ladder, breaks no limit of PROFILE-SCHEMA section 13 and passes the privacy
        scan (which now includes the string rules)."""
        from tests.test_profile_strings import _no_ladder_ctx
        tmp = tempfile.mkdtemp(prefix="ss-tests-e2-t6i-")
        self.addCleanup(shutil.rmtree, tmp, True)
        noladder = prof.build_profile(_no_ladder_ctx(tmp))
        for label, p in (("ladder", self.profile), ("no ladder", noladder)):
            with self.subTest(label):
                self.assertEqual(pb.lab_string_problems(p), [])
                self.assertEqual(prof.privacy_scan(p), [])
                for r in p["rows"]:                                    # the row-level strings, the rule 5S left out
                    for k, v in r.items():
                        if k == "cells":
                            continue
                        lim = 200 if k == "source" else 40
                        for s in (v if isinstance(v, list) else [v]):
                            if isinstance(s, str):
                                self.assertLessEqual(len(s), lim, (r["row"], k))
        self.assertTrue(any(c.get("needs") == ["ladder"] for r in noladder["rows"] for c in r["cells"]))

    def test_privacy_scan_includes_the_string_rules(self):
        p = copy.deepcopy(self.profile)
        p["rows"][2]["rests_on"].append("z" * 41)
        self.assertTrue(any("row 1" in m or "row" in m for m in prof.privacy_scan(p)))
        p = copy.deepcopy(self.profile)
        p["rows"][2]["cells"][0]["reason_code"] = "no_such_code_xyz"
        self.assertTrue(prof.privacy_scan(p))

    def test_write_profile_refuses_reason_key(self):
        """A free sentence under ``reason`` (or ``formula``) in any cell is refused by write_profile and nothing is written."""
        for key in ("reason", "formula"):
            p = copy.deepcopy(self.profile)
            p["rows"][2]["cells"][0][key] = "A planted sentence."
            path = self.tmp / f"reason-{key}.json"
            with self.assertRaises(prof.ProfileRefused) as cm:
                prof.write_profile(p, path)
            self.assertTrue(any(f"'{key}'" in r for r in cm.exception.reasons), cm.exception.reasons)
            self.assertFalse(path.exists())

    def test_short_pid_is_matched_as_a_token_not_a_substring(self):
        """Ruling R24: a pid under 6 characters (initials) uses [A-Za-z0-9] boundaries; 6 or more stays a substring."""
        def scan(label, pid):
            p = copy.deepcopy(self.profile)
            p["population"]["label"] = label
            return prof.privacy_scan(p, forbidden={"pids": {pid}, "codes": set()})
        self.assertEqual(scan("absent", "AB"), [])                       # inside an ordinary word
        self.assertEqual(scan("12 of 12 absent labels", "ab"), [])
        self.assertTrue(scan("AB", "AB"))                                # standing alone
        self.assertEqual(scan("ab", "AB"), [])                           # Ruling R31: upper-case initials of 3 or fewer
        self.assertTrue(scan("ab", "Ab"))                                # characters are case-sensitive; others are not
        self.assertTrue(scan("abcd", "ABCD"))
        self.assertTrue(scan("AB_map", "AB"))                            # joined with an underscore
        self.assertTrue(scan("the AB map", "AB"))
        self.assertTrue(scan("x-AB", "AB"))
        self.assertEqual(scan("ABC", "AB"), [])                          # an alphanumeric neighbour is not a boundary
        self.assertEqual(scan("1AB", "AB"), [])
        # 6 characters or more: still a substring
        self.assertTrue(scan("xxSYN-01xx", "SYN-01"))
        self.assertTrue(scan("SYN-OW01_map", "SYN-OW01"))
        self.assertEqual(scan("SYN-0", "SYN-01"), [])
        # a key is scanned the same way
        p = copy.deepcopy(self.profile)
        p["rows"][2]["AB_map"] = 1
        self.assertTrue(prof.privacy_scan(p, forbidden={"pids": {"AB"}, "codes": set()}))
        p = copy.deepcopy(self.profile)
        p["rows"][2]["absent_cells"] = 1
        self.assertEqual(prof.privacy_scan(p, forbidden={"pids": {"AB"}, "codes": set()}), [])

    def test_value_scan_no_longer_exempts_reason_and_formula(self):
        self.assertFalse({"reason", "formula"} & prof.CLOSED_VOCABULARY)
        self.assertIn("label_key", prof.CLOSED_VOCABULARY)

    def test_write_profile_refuses_a_dirty_profile_and_writes_nothing(self):
        p = copy.deepcopy(self.profile)
        p["rows"][2]["cells"][0]["map_id"] = "m1"
        path = self.tmp / "dirty.json"
        with self.assertRaises(prof.ProfileRefused) as cm:
            prof.write_profile(p, path)
        self.assertTrue(cm.exception.reasons)
        self.assertFalse(path.exists())

    def test_two_exports_same_bytes(self):
        a, b = self.tmp / "a" / "p.json", self.tmp / "b" / "p.json"
        sa = prof.write_profile(self.profile, a)
        sb = prof.write_profile(prof.build_profile(pb.open_context(Path(DEMO_CONFIG), self.out)), b)
        self.assertEqual(a.read_bytes(), b.read_bytes())
        self.assertEqual(sa, sb)
        import hashlib
        self.assertEqual(sa, hashlib.sha256(a.read_bytes()).hexdigest())
        json.loads(a.read_text(encoding="utf-8"))

    def test_refuses_nine_analysed(self):
        ctx = pb.open_context(self.nine(), self.out)
        self.assertEqual(len(ctx.analysed), 9)
        profile = prof.build_profile(ctx)
        self.assertEqual(profile["population"]["n_people"], 9)
        path = self.tmp / "nine.json"
        with self.assertRaises(prof.ProfileRefused) as cm:
            prof.write_profile(profile, path)
        self.assertTrue(any(s.startswith("this run analysed 9 people; a profile needs at least 10 (the k floor)")
                            for s in cm.exception.reasons))                               # village E4 wording
        self.assertFalse(path.exists())

    def test_refuses_on_n_analysed_not_on_file_count(self):
        # 12 people in the corpus, exclusions leave 9: the count that matters is the analysed one
        ctx = dataclasses.replace(self.ctx, analysed=self.ctx.analysed[:9])
        self.assertEqual(len(ctx.p_list), 12)
        profile = prof.build_profile(ctx)
        self.assertEqual((profile["population"]["n_people"], profile["population"]["n_total"]), (9, 12))
        with self.assertRaises(prof.ProfileRefused):
            prof.write_profile(profile, self.tmp / "nine-b.json")

    def test_ten_analysed_is_accepted(self):
        ctx = dataclasses.replace(self.ctx, analysed=self.ctx.analysed[:10])
        profile = prof.build_profile(ctx)
        self.assertEqual(profile["population"]["n_people"], 10)
        prof.write_profile(profile, self.tmp / "ten.json")


class TestCli(Base):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(["export-profile", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_cli_export_profile_exit_codes(self):
        path = self.tmp / "cli" / "profile.json"
        rc, out, err = self.run_cli("--config", str(DEMO_CONFIG), "--out-dir", str(self.out), "--profile", str(path))
        self.assertEqual(rc, 0, err)
        import hashlib
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertIn(digest, out)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schema"], "orgsci-profile/v2")
        # the same bytes as the API
        prof.write_profile(self.profile, self.tmp / "api.json")
        self.assertEqual(path.read_bytes(), (self.tmp / "api.json").read_bytes())
        # nine analysed: exit 2, the reason on stderr, no file
        nine = self.tmp / "cli9" / "profile.json"
        rc, out, err = self.run_cli("--config", str(self.nine()), "--out-dir", str(self.out),
                                    "--profile", str(nine))
        self.assertEqual(rc, 2)
        self.assertIn("refused: this run analysed 9 people; a profile needs at least 10 (the k floor) so no number "
                      "can point to one person. Nothing was written.", err)                # village E4 wording
        self.assertFalse(nine.exists())
        self.assertNotRegex(out, r"\b[0-9a-f]{64}\b")

    def test_cli_options_reach_the_profile(self):
        path = self.tmp / "cli2" / "profile.json"
        info = self.tmp / "info.json"
        # Ruling R32: model-info values are codes (no "/")
        info.write_text(json.dumps({"reconstructor": {"quantisation": "Q8", "weights": "org.w"}}), encoding="utf-8")
        rc, out, err = self.run_cli("--config", str(DEMO_CONFIG), "--out-dir", str(self.out), "--profile", str(path),
                                    "--B", "50", "--seed", "11", "--model-info", str(info), "--reference-type", "interviewer-drawn")
        self.assertEqual(rc, 0, err)
        p = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((p["run"]["settings"]["bootstrap_resamples"], p["run"]["settings"]["bootstrap_seed"]), (50, 11))
        self.assertEqual(p["reference"]["type"], "interviewer-drawn")
        self.assertEqual({m["role"]: m["quantisation"] for m in p["run"]["models"]}["reconstructor"], "Q8")

    def test_cli_bad_model_info_is_exit_2(self):
        info = self.tmp / "bad.json"
        info.write_text("[1, 2", encoding="utf-8")
        rc, out, err = self.run_cli("--config", str(DEMO_CONFIG), "--out-dir", str(self.out),
                                    "--profile", str(self.tmp / "bad-out.json"), "--model-info", str(info))
        self.assertEqual(rc, 2)
        self.assertFalse((self.tmp / "bad-out.json").exists())


class TestWithholdChainHeads(Base):
    """Ruling R25 (analysis C19): ``--withhold-chain-heads`` nulls the run's chain heads and names who holds them."""
    SENTENCE = "The run's hash-chain heads are held by the authors and are available from them."

    def test_default_form_keeps_the_heads_and_adds_no_source_key(self):
        run = self.profile["run"]
        self.assertIsInstance(run["run_chain_heads"], dict)
        self.assertNotIn("run_chain_heads_source", run)

    def test_withheld_form(self):
        kept = prof.build_run_block(self.ctx)
        run = prof.build_run_block(self.ctx, withhold_chain_heads=True)
        self.assertIsNone(run["run_chain_heads"])
        self.assertEqual(run["run_chain_heads_source"], "available_from_authors")
        for k in kept:
            if k != "run_chain_heads":
                self.assertEqual(run[k], kept[k], k)                      # nothing else changes
        self.assertEqual(sorted(set(run) - set(kept)), ["run_chain_heads_source"])
        p = prof.build_profile(self.ctx, withhold_chain_heads=True)       # the scan and the writer still pass
        path = self.tmp / "withheld.json"
        prof.write_profile(p, path, forbidden=prof.forbidden_tokens(self.ctx))
        text = path.read_text(encoding="utf-8")
        self.assertNotIn(kept["run_chain_heads"]["head"], text)
        self.assertNotIn(str(kept["run_chain_heads"]["manifest_hash"]), text)
        self.assertEqual(json.loads(text)["run"]["run_chain_heads"], None)

    def test_withheld_log_that_does_not_verify_is_still_refused(self):
        bad = dataclasses.replace(self.ctx, out_dir=self.tmp / "no-such-run")
        with self.assertRaises(prof.ProfileRefused):
            prof.build_run_block(bad, withhold_chain_heads=True)

    def test_the_sentence_is_in_the_strings_file(self):
        self.assertEqual(pb.load_profile_strings()["profile.reason.available_from_authors"], self.SENTENCE)

    def test_cli_flag_and_default(self):
        def export(*extra):
            path = self.tmp / f"cli-{len(extra)}" / "profile.json"
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main(["export-profile", "--config", str(DEMO_CONFIG), "--out-dir", str(self.out), "--profile",
                           str(path), "--B", "50", *extra])
            self.assertEqual(rc, 0, err.getvalue())
            return json.loads(path.read_text(encoding="utf-8"))["run"]
        plain, withheld = export(), export("--withhold-chain-heads")
        self.assertIsInstance(plain["run_chain_heads"], dict)
        self.assertNotIn("run_chain_heads_source", plain)
        self.assertIsNone(withheld["run_chain_heads"])
        self.assertEqual(withheld["run_chain_heads_source"], "available_from_authors")


class TestRemoteEndpoints(Base):
    """PROFILE-SCHEMA section 14, item 8: ``run.remote_endpoints`` = [{role, provider, declaration_sha256}], from the
    config's ``tool.remote_endpoints``. Never a host, never free text; absent (not empty) when there is none."""
    HOST_A, HOST_B = "api.cloud-host-aaa.example", "inference.cloud-host-bbb.example"
    DECL_A, DECL_B = hashlib.sha256(b"declaration a").hexdigest(), hashlib.sha256(b"declaration b").hexdigest()

    def config_with(self, name, endpoints=..., tool_extra=None):
        d = json.loads(Path(DEMO_CONFIG).read_text(encoding="utf-8"))
        d["base_dir"] = str(Path(DEMO_CONFIG).parent)
        tool = dict(tool_extra or {})
        if endpoints is not ...:
            tool["remote_endpoints"] = endpoints
        if tool:
            d["tool"] = tool
        path = self.tmp / f"{name}.json"
        path.write_text(json.dumps(d), encoding="utf-8")
        return path

    def two(self):
        return [{"role": "recon", "host": self.HOST_A, "provider": "openai_compat", "declaration_sha256": self.DECL_A},
                {"role": "judge", "host": self.HOST_B, "provider": "anthropic", "declaration_sha256": self.DECL_B}]

    def export(self, config, name, *extra):
        path = self.tmp / name / "profile.json"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(["export-profile", "--config", str(config), "--out-dir", str(self.out), "--profile", str(path),
                       "--B", "50", *extra])
        return rc, path, err.getvalue()

    def test_no_remote_endpoints_no_field(self):
        for tag, cfg in (("study", self.config_with("study")),
                         ("tool-without", self.config_with("toolno", tool_extra={"reference_type": "A"})),
                         ("empty-list", self.config_with("empty", endpoints=[], tool_extra={"reference_type": "A"}))):
            rc, path, err = self.export(cfg, tag)
            self.assertEqual(rc, 0, err)
            self.assertNotIn("remote_endpoints", path.read_text(encoding="utf-8"), tag)
            self.assertNotIn("remote_endpoints", json.loads(path.read_text(encoding="utf-8"))["run"], tag)

    def test_two_endpoints_exact_sorted_list_and_no_host_in_the_file(self):
        rc, path, err = self.export(self.config_with("two", self.two()), "two")
        self.assertEqual(rc, 0, err)
        raw = path.read_bytes()
        run = json.loads(raw)["run"]
        self.assertEqual(run["remote_endpoints"], [        # sorted by role: judge before recon
            {"role": "judge", "provider": "anthropic", "declaration_sha256": self.DECL_B},
            {"role": "recon", "provider": "openai_compat", "declaration_sha256": self.DECL_A}])
        for needle in (self.HOST_A, self.HOST_B, "cloud-host", ".example", '"host"'):
            self.assertNotIn(needle.encode(), raw, needle)

    def test_sorted_by_role_then_provider_whatever_the_config_order(self):
        eps = [{"role": "judge", "host": self.HOST_B, "provider": "openai_compat", "declaration_sha256": self.DECL_B},
               {"role": "judge2", "host": self.HOST_A, "provider": "anthropic", "declaration_sha256": self.DECL_A},
               {"role": "judge", "host": self.HOST_A, "provider": "anthropic", "declaration_sha256": self.DECL_A}]
        ctx = pb.open_context(self.config_with("order", eps), self.out)
        got = prof.build_run_block(ctx)["remote_endpoints"]
        self.assertEqual([(e["role"], e["provider"]) for e in got],
                         [("judge", "anthropic"), ("judge", "openai_compat"), ("judge2", "anthropic")])
        for e in got:
            self.assertEqual(set(e), {"role", "provider", "declaration_sha256"})

    def test_deterministic_same_bytes_on_rerun(self):
        cfg = self.config_with("det", self.two())
        rc1, p1, e1 = self.export(cfg, "det1")
        rc2, p2, e2 = self.export(cfg, "det2")
        self.assertEqual((rc1, rc2), (0, 0), e1 + e2)
        self.assertEqual(p1.read_bytes(), p2.read_bytes())

    def test_every_emitted_role_and_provider_has_a_display_string(self):
        from second_scribe import toolcfg
        eps = [{"role": r, "host": self.HOST_A, "provider": p, "declaration_sha256": self.DECL_A}
               for r in prof.REMOTE_ROLES for p in toolcfg.PROVIDERS]
        rc, path, err = self.export(self.config_with("allcodes", eps), "allcodes")
        self.assertEqual(rc, 0, err)
        got = json.loads(path.read_text(encoding="utf-8"))["run"]["remote_endpoints"]
        self.assertEqual({e["role"] for e in got}, {"recon", "judge", "judge2"})
        self.assertEqual({e["provider"] for e in got}, set(toolcfg.PROVIDERS))
        strings = pb.load_profile_strings()
        for e in got:
            self.assertTrue(strings.get(f"profile.role.{e['role']}", "").strip(), e["role"])
            self.assertTrue(strings.get(f"profile.provider.{e['provider']}", "").strip(), e["provider"])

    def test_withhold_chain_heads_leaves_remote_endpoints_alone(self):
        cfg = self.config_with("wh", self.two())
        _, kept, _ = self.export(cfg, "wh-kept")
        rc, held, err = self.export(cfg, "wh-held", "--withhold-chain-heads")
        self.assertEqual(rc, 0, err)
        a, b = json.loads(kept.read_text())["run"], json.loads(held.read_text())["run"]
        self.assertIsNone(b["run_chain_heads"])
        self.assertEqual(a["remote_endpoints"], b["remote_endpoints"])
        self.assertEqual(len(b["remote_endpoints"]), 2)

    def bypass(self, endpoints, name):
        """A config that skips toolcfg's check at load (``check_tool`` patched out), as a hand-built config could."""
        cfg = self.config_with(name, endpoints)
        with mock.patch.object(pipeline.toolcfg, "check_tool"):
            ctx = pb.open_context(cfg, self.out)
            with mock.patch.object(prof.pb, "open_context", return_value=ctx):
                return self.export(cfg, name)

    def test_bad_entries_refuse_exit_2_no_file_and_name_the_field_not_the_value(self):
        good = self.two()[0]
        secret = "SECRETVALUE" + "z" * 20
        cases = {
            "provider": ({**good, "provider": secret}, "provider"),
            "provider_long": ({**good, "provider": "p" * 41}, "provider"),
            "role": ({**good, "role": secret}, "role"),
            "role_reconstructor": ({**good, "role": "reconstructor"}, "role"),     # the old vocabulary is gone
            "role_reading3": ({**good, "role": "reading3"}, "role"),
            "role_case": ({**good, "role": "Judge"}, "role"),
            "hash_short": ({**good, "declaration_sha256": "ab" * 10}, "declaration_sha256"),
            "hash_upper": ({**good, "declaration_sha256": self.DECL_A.upper()}, "declaration_sha256"),
            "hash_text": ({**good, "declaration_sha256": secret}, "declaration_sha256"),
            "hash_missing": ({k: v for k, v in good.items() if k != "declaration_sha256"}, "declaration_sha256"),
        }
        for tag, (entry, field) in cases.items():
            with self.subTest(tag):
                rc, path, err = self.bypass([entry], f"bad-{tag}")
                self.assertEqual(rc, 2, err)
                self.assertFalse(path.exists(), "a file was written")
                self.assertIn(field, err)
                for leaked in (secret, self.DECL_A.upper(), "p" * 41, "ab" * 10, self.HOST_A):
                    self.assertNotIn(leaked, err)

    def test_build_run_block_raises_profile_refused_on_a_bad_entry(self):
        eps = self.two()
        eps[1]["provider"] = "someone_else"
        cfg = self.config_with("badblock", eps)
        with mock.patch.object(pipeline.toolcfg, "check_tool"):
            ctx = pb.open_context(cfg, self.out)
        with self.assertRaises(prof.ProfileRefused):
            prof.build_run_block(ctx)

    def test_unknown_role_in_a_loadable_config_exits_2(self):
        # toolcfg does not check the role, so this config loads; the exporter refuses it
        eps = self.two()
        eps[0]["role"] = "reconstructor"
        rc, path, err = self.export(self.config_with("badrole", eps), "badrole")
        self.assertEqual(rc, 2, err)
        self.assertFalse(path.exists())

    # ---- the 64-hex rule: allowed at one key only
    def test_declaration_hash_allowed_at_its_key_refused_elsewhere(self):
        from second_scribe import publish
        rc, path, err = self.export(self.config_with("hex", self.two()), "hex")
        self.assertEqual(rc, 0, err)
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(publish.violations(doc, 10, "orgsci-profile/v2"), [])          # at its key: passes the gate
        h = self.DECL_A
        plants = {
            "row_population": lambda d: d["rows"][0].__setitem__("population", h),
            "row_cell_note": lambda d: d["rows"][0]["cells"][0].__setitem__("note", h),
            "population_label": lambda d: d["population"].__setitem__("label", h),
            "run_kit_source": lambda d: d["run"]["kit"].__setitem__("source", h),
            "run_other_key": lambda d: d["run"]["kit"].__setitem__("declaration_sha256", h),
            "run_top_key": lambda d: d["run"].__setitem__("declaration_sha256", h),
            "endpoint_provider": lambda d: d["run"]["remote_endpoints"][0].__setitem__("provider", h),
            "endpoint_role": lambda d: d["run"]["remote_endpoints"][0].__setitem__("role", h),
            "settings_key": lambda d: d["run"]["settings"].__setitem__("declaration_sha256", h),
        }
        for tag, plant in plants.items():
            with self.subTest(tag):
                bad = copy.deepcopy(doc)
                plant(bad)
                self.assertTrue(publish.violations(bad, 10, "orgsci-profile/v2"), tag)
        for tag in ("row_population", "row_cell_note", "population_label"):            # the exporter's own scan too
            bad = copy.deepcopy(doc)
            plants[tag](bad)
            self.assertTrue(prof.privacy_scan(bad), tag)
            with self.assertRaises(prof.ProfileRefused):
                prof.write_profile(bad, self.tmp / "planted" / f"{tag}.json")
            self.assertFalse((self.tmp / "planted" / f"{tag}.json").exists())

    def test_hash_only_valid_inside_a_list_entry(self):
        from second_scribe import publish
        _, path, _ = self.export(self.config_with("hex2", self.two()), "hex2")
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["run"]["remote_endpoints"] = {"declaration_sha256": self.DECL_A, "role": "judge", "provider": "anthropic"}
        self.assertTrue(publish.violations(doc, 10, "orgsci-profile/v2"))


if __name__ == "__main__":
    unittest.main()
