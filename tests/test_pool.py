"""Task 7 Part A: the synthetic labs (labA, labB, lab9) and `pool` (profiles side by side, never combined).

Zero model calls and zero network calls: the labs run on the oracle client, in-process, with the socket layer patched
to raise. The pool tests use profile dicts built here, in profile-v2's top-level shape.
"""
import contextlib
import copy
import io
import json
import re
import socket
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from second_scribe import cli
from second_scribe import pool as poolmod
from second_scribe import profile_base as pb
from second_scribe import profile_coverage as pc
from second_scribe.synth import labs

REPO = Path(__file__).resolve().parent.parent
Q_SHA, JM_SHA = "a" * 64, "b" * 64                     # 64 lower-case hex (fix round 2, N1)
FORBIDDEN_KEYS = {"pooled_estimate", "combined", "meta", "mean_across_labs"}


def profile(n_people=12, *, recall=0.5, ref="participant-endorsed", q=Q_SHA, jm=JM_SHA, state="synthetic"):
    return {
        "schema": "orgsci-profile/v2", "state": state,
        "population": {"label": f"{n_people} of {n_people}", "n_people": n_people, "n_total": n_people},
        "reference": {"type": ref, "map_size": {"ai_boxes": 100}},
        "pooling": {"rule": "side_by_side", "within": ["reference_type", "prompt_set_pair", "state"], "meta_analysis": False},
        "rows": [
            {"row": 1, "label_key": "row1.title", "population": f"{n_people} of {n_people}", "n_people": n_people,
             "cells": [
                 {"name": "attested_recall", "x": "primary", "n": 50, "unit": "node", "kind": "share",
                  "value": recall, "ci_lo": recall - 0.1, "ci_hi": recall + 0.1, "ci_level": 0.95},
                 {"name": "r_star", "unit": "node", "kind": "ratio", "state": "not_run", "value": None,
                  "needs": ["sidejudge"]}]},
            {"row": 2, "label_key": "row2.title", "n_people": n_people,
             "cells": [{"name": "kept", "n": 50, "unit": "node", "kind": "share", "value": recall / 2}]},
        ],
        "run": {"prompt_set": {"q_phase_sha256": q, "j_m_phase_sha256": jm}, "settings": {"B": 2000}},
    }


def keys_of(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from keys_of(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from keys_of(v)


class PoolTests(unittest.TestCase):
    def test_pool_side_by_side_no_combined_value(self):
        a, b = profile(recall=0.9), profile(recall=0.6)
        before = copy.deepcopy([a, b])
        out = poolmod.pool([a, b], labels=["labA", "labB"])
        self.assertEqual([a, b], before, "pool must not alter its inputs")
        self.assertFalse(FORBIDDEN_KEYS & set(keys_of(out)))
        self.assertIs(out["meta_analysis"], False)
        self.assertEqual(out["separate"], [])
        self.assertEqual(len(out["groups"]), 1)
        g = out["groups"][0]
        self.assertEqual(g["labs"], ["labA", "labB"])
        self.assertEqual(g["key"], {"reference_type": "participant-endorsed", "q_phase_sha256": Q_SHA,
                                    "j_m_phase_sha256": JM_SHA, "state": "synthetic"})
        line = {(t["row"], t["name"], t.get("x")): t for t in g["table"]}[(1, "attested_recall", "primary")]
        # each lab's cell is copied as it came, in its own column
        self.assertEqual(line["columns"]["labA"], a["rows"][0]["cells"][0])
        self.assertEqual(line["columns"]["labB"], b["rows"][0]["cells"][0])
        rstar = {(t["row"], t["name"], t.get("x")): t for t in g["table"]}[(1, "r_star", None)]
        self.assertEqual(rstar["columns"]["labA"]["needs"], ["sidejudge"])      # not_run copied with its needs
        self.assertEqual(g["n_analysed"], {"labA": 12, "labB": 12})
        md = poolmod.pool_markdown(out)
        for word in ("first", "validated", "ground truth", "replicable"):
            self.assertNotIn(word, md.lower())
        self.assertIn("labA", md)
        self.assertIn("0.9", md)
        self.assertIn("not run", md)

    def test_pool_separates_mismatched_prompt_pair(self):
        a, b, c = profile(), profile(q="c" * 64), profile()
        out = poolmod.pool([a, b, c], labels=["labA", "labB", "labC"])
        self.assertEqual(out["separate"], ["labB"])
        self.assertEqual(len(out["groups"]), 1)
        self.assertEqual(out["groups"][0]["labs"], ["labA", "labC"])
        self.assertNotIn("labB", json.dumps(out["groups"]))          # never placed in a table with the others
        # reference type and state split groups the same way; J/M hash too
        for odd in (profile(ref="other"), profile(state="pending"), profile(jm="d" * 64)):
            o = poolmod.pool([a, odd], labels=["labA", "labX"])
            self.assertEqual((o["groups"], o["separate"]), ([], ["labA", "labX"]))

    def test_pool_refuses_profile_under_10(self):
        with self.assertRaisesRegex(ValueError, "labN.*9.*10|10"):
            poolmod.pool([profile(), profile(9)], labels=["labA", "labN"])
        poolmod.pool([profile(10), profile(10)], labels=["labA", "labB"])      # 10 is enough

    def test_pool_runs_the_privacy_scan_on_every_input(self):
        """A5: a hand-edited profile (a row under K, a participant_id key, a quote in a cell) is refused by pool() and
        by the command (exit 2, nothing written), and no planted string appears in any message or output."""
        quote, pid = "Maria said the plant will close", "SYN-OW07"

        def edited(kind):
            p = profile()
            if kind in ("row_n", "all"):
                p["rows"][0]["n_people"] = 3
            if kind in ("key", "all"):
                p["rows"][0]["cells"][0]["participant_id"] = pid
            if kind in ("quote", "all"):
                p["rows"][1]["cells"][0]["value"] = quote
            if kind == "cell_n_people":
                p["rows"][1]["cells"][0]["n_people"] = 3
            return p
        for kind in ("row_n", "key", "quote", "cell_n_people", "all"):
            with self.assertRaises(ValueError) as cm:
                poolmod.pool([profile(), edited(kind)], labels=["labA", "labX"])
            msg = str(cm.exception)
            self.assertIn("labX", msg, kind)
            for planted in (quote, pid, "participant_id", "Maria"):
                self.assertNotIn(planted, msg, kind)
        with tempfile.TemporaryDirectory(prefix="ss-tests-fixA-") as t:
            t = Path(t)
            (t / "labA.json").write_text(json.dumps(profile()), encoding="utf-8")
            (t / "labX.json").write_text(json.dumps(edited("all")), encoding="utf-8")
            err, out = io.StringIO(), io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                rc = cli.main(["pool", "--profiles", str(t / "labA.json"), str(t / "labX.json"),
                               "--out", str(t / "pool.json")])
            self.assertEqual(rc, 2)
            self.assertIn("refused", err.getvalue())
            for planted in (quote, pid, "participant_id", "Maria"):
                self.assertNotIn(planted, err.getvalue() + out.getvalue())
            self.assertEqual(sorted(p.name for p in t.iterdir()), ["labA.json", "labX.json"])

    def test_pool_refuses_a_null_or_empty_key_field(self):
        """A6: a null prompt hash (or state, or reference type) is malformed: pool() refuses it, the command exits 2 and
        writes neither file."""
        def null_q(p):
            p["run"]["prompt_set"]["q_phase_sha256"] = None
        def empty_jm(p):
            p["run"]["prompt_set"]["j_m_phase_sha256"] = ""
        def null_state(p):
            p["state"] = None
        def num_ref(p):
            p["reference"]["type"] = 3
        def sentence_ref(p):                                       # A4: a sentence is not a reference-type code
            p["reference"]["type"] = "drawn by Maria at home"
        for edit in (null_q, empty_jm, null_state, num_ref, sentence_ref):
            bad = profile()
            edit(bad)
            with self.assertRaisesRegex(ValueError, "labX"):
                poolmod.pool([profile(), bad], labels=["labA", "labX"])
            with tempfile.TemporaryDirectory(prefix="ss-tests-fixA-") as t:
                t = Path(t)
                (t / "labA.json").write_text(json.dumps(profile()), encoding="utf-8")
                (t / "labX.json").write_text(json.dumps(bad), encoding="utf-8")
                err = io.StringIO()
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    rc = cli.main(["pool", "--profiles", str(t / "labA.json"), str(t / "labX.json"),
                                   "--out", str(t / "pool.json")])
                self.assertEqual(rc, 2, edit.__name__)
                self.assertIn("refused", err.getvalue())
                self.assertEqual(sorted(p.name for p in t.iterdir()), ["labA.json", "labX.json"], edit.__name__)

    def test_pool_cli_builds_both_texts_then_writes_each_atomically(self):
        """A6: the markdown is built before anything is written (a failure there leaves no pool.json), and both files go
        through report.write_text_atomic."""
        from second_scribe import report
        with tempfile.TemporaryDirectory(prefix="ss-tests-fixA-") as t:
            t = Path(t)
            for name in ("labA", "labB"):
                (t / f"{name}.json").write_text(json.dumps(profile()), encoding="utf-8")
            argv = ["pool", "--profiles", str(t / "labA.json"), str(t / "labB.json"), "--out", str(t / "pool.json")]
            with mock.patch.object(poolmod, "pool_markdown", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    cli.main(argv)
            self.assertEqual(sorted(p.name for p in t.iterdir()), ["labA.json", "labB.json"])
            with mock.patch.object(report, "write_text_atomic", wraps=report.write_text_atomic) as spy, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(argv), 0)
            self.assertEqual(sorted(Path(c.args[0]).name for c in spy.call_args_list), ["pool.json", "pool.md"])
            self.assertEqual(sorted(p.name for p in t.iterdir()), ["labA.json", "labB.json", "pool.json", "pool.md"])

    def test_repeated_cell_message_names_a_position_not_a_value(self):
        """A2: a repeated (row, name, x) is refused by its position; the name is not echoed."""
        a = profile()
        a["rows"][0]["cells"].append(dict(a["rows"][0]["cells"][0]))
        for c in a["rows"][0]["cells"]:
            if c.get("x") == "primary":
                c["x"] = "maria_x"
        with self.assertRaises(ValueError) as cm:
            poolmod.pool([a, profile()], labels=["labA", "labB"])
        self.assertIn("rows[0].cells[2]", str(cm.exception))
        self.assertNotIn("maria", str(cm.exception))
        self.assertNotIn("attested_recall", str(cm.exception))

    def test_pool_malformed_inputs_exit_2(self):
        """Final review A8: a malformed profile (a cell without a name, rows not a list, a list instead of an object, a
        non-object row) is refused with exit 2 and no traceback, and nothing is written."""
        def no_name(p):
            del p["rows"][0]["cells"][0]["name"]
        def rows_text(p):
            p["rows"] = "rows"
        def cells_number(p):
            p["rows"][0]["cells"] = 3
        def population_text(p):
            p["population"] = "12 of 12"
        cases = [no_name, rows_text, cells_number, population_text]
        for edit in cases + ["list"]:
            bad = [1, 2] if edit == "list" else profile()
            if edit != "list":
                edit(bad)
            tag = getattr(edit, "__name__", edit)
            with tempfile.TemporaryDirectory(prefix="ss-tests-fixA-") as t:
                t = Path(t)
                (t / "labA.json").write_text(json.dumps(profile()), encoding="utf-8")
                (t / "labX.json").write_text(json.dumps(bad), encoding="utf-8")
                err = io.StringIO()
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    rc = cli.main(["pool", "--profiles", str(t / "labA.json"), str(t / "labX.json"),
                                   "--out", str(t / "pool.json")])
                self.assertEqual(rc, 2, tag)
                self.assertIn("refused", err.getvalue(), tag)
                self.assertEqual(sorted(p.name for p in t.iterdir()), ["labA.json", "labX.json"], tag)

    def test_pool_within_says_prompt_set_pair(self):
        """A9: the pool's grouping rule is spelled as the profile's (PROFILE-SCHEMA section 14.3)."""
        out = poolmod.pool([profile(), profile(q="c" * 64)], labels=["labA", "labB"])
        self.assertEqual(out["pooling"]["within"], ["reference_type", "prompt_set_pair", "state"])
        md = poolmod.pool_markdown(out)
        self.assertIn("prompt-set pair", md)
        self.assertNotIn("prompt pair", md)

    def test_pool_markdown_shows_display_names_without_script_words(self):
        """A11: pool.md shows a display name; the three row-6 names that begin with "scripted" read "no-interview"
        there, while pool.json keeps the schema names."""
        a, b = profile(), profile()
        for p in (a, b):
            p["rows"].append({"row": 6, "label_key": "profile.row6.slant", "n_people": 12, "cells": [
                {"name": n, "x": "own_script", "unit": "node", "kind": "share", "value": 0.1}
                for n in ("scripted_share_unanchored", "scripted_share_attested_absent", "scripted_share_difference")]})
        out = poolmod.pool([a, b], labels=["labA", "labB"])
        md = poolmod.pool_markdown(out)
        self.assertIsNone(re.search(r"(?i)\bscript\w*", md), md)
        self.assertIn("no-interview_share_unanchored", md)
        self.assertIn("scripted_share_unanchored", json.dumps(out))            # the data keeps the v2 names

    def test_pool_refuses_free_text_in_key_and_row_fields(self):
        """Fix round 2, N1 (the re-review's probe): free text in state, either prompt hash, a row's label_key or row id,
        and code-shaped text in a cell field outside its closed form, are refused (exit 2, nothing written); nothing
        from the input reaches pool.json or pool.md."""
        quote = "Maria said the plant will close"

        def setter(*path):
            def edit(p, value):
                o = p
                for k in path[:-1]:
                    o = o[k]
                o[path[-1]] = value
            return edit
        probes = {
            "state": (setter("state"), quote),
            "q hash": (setter("run", "prompt_set", "q_phase_sha256"), quote),
            "j/m hash": (setter("run", "prompt_set", "j_m_phase_sha256"), quote),
            "label_key": (setter("rows", 0, "label_key"), quote),
            "row id": (setter("rows", 0, "row"), quote),
            "q hash upper hex": (setter("run", "prompt_set", "q_phase_sha256"), "A" * 64),
            "q hash [SOURCE]": (setter("run", "prompt_set", "q_phase_sha256"), "[SOURCE]"),
            "state code": (setter("state"), "Maria_said"),
            "row id code": (setter("rows", 0, "row"), "SYN-OW07"),
            "row id 10": (setter("rows", 0, "row"), 10),
            "label_key code": (setter("rows", 0, "label_key"), "Maria_said"),
            "cell x": (setter("rows", 0, "cells", 0, "x"), "Maria_said"),
            "cell name": (setter("rows", 0, "cells", 0, "name"), "SYN-OW07"),
            "cell unit": (setter("rows", 0, "cells", 0, "unit"), "Maria_said"),
            "cell state": (setter("rows", 0, "cells", 1, "state"), "maria_said"),
            "cell source": (setter("rows", 0, "cells", 0, "source"), "Maria/said"),
            "extra cell key": (setter("rows", 0, "cells", 0, "remark"), "Maria_said"),
            "extra row key": (setter("rows", 0, "remark"), "Maria_said"),
            "needs item": (setter("rows", 0, "cells", 1, "needs"), ["Maria_said"]),
            "rests_on item": (setter("rows", 0, "cells", 0, "rests_on"), ["SYN-OW07"]),
            "qualifier item": (setter("rows", 0, "cells", 0, "qualifiers"), ["maria_said"]),
            "band member x": (setter("rows", 0, "cells", 0, "band"),
                              {"lo": 0.1, "hi": 0.2, "over": "labellers", "members": [{"x": "Maria_said", "value": 0.1}]}),
            # N5: one-word text in the values pool.json copies whole
            "cell derived": (setter("rows", 0, "cells", 0, "derived"), "Maria_said"),
            "band lo": (setter("rows", 0, "cells", 0, "band"), {"lo": "Maria_said", "hi": 0.2, "over": "labellers"}),
            "band hi": (setter("rows", 0, "cells", 0, "band"), {"lo": 0.1, "hi": "Maria_said", "over": "labellers"}),
            "band narrow": (setter("rows", 0, "cells", 0, "band"),
                            {"lo": 0.1, "hi": 0.2, "over": "labellers", "narrow": "Maria_said"}),
            "band member value": (setter("rows", 0, "cells", 0, "band"),
                                  {"lo": 0.1, "hi": 0.2, "over": "labellers",
                                   "members": [{"x": "judge", "value": "Maria_said"}]}),
            "subband lo": (setter("rows", 0, "cells", 0, "band"),
                           {"lo": 0.1, "hi": 0.2, "over": "labellers",
                            "subbands": [{"name": "pair", "lo": "Maria_said", "hi": 0.2}]}),
        }
        planted_strings = (quote, "Maria", "maria", "SYN-OW07")
        for tag, (edit, value) in probes.items():
            with self.subTest(tag):
                bad = profile()
                edit(bad, value)
                with self.assertRaises(ValueError) as cm:
                    poolmod.pool([profile(), bad], labels=["labA", "labX"])
                for planted in planted_strings:
                    self.assertNotIn(planted, str(cm.exception))
                with tempfile.TemporaryDirectory(prefix="ss-tests-fixA2-") as t:
                    t = Path(t)
                    (t / "labA.json").write_text(json.dumps(profile()), encoding="utf-8")
                    (t / "labX.json").write_text(json.dumps(bad), encoding="utf-8")
                    err, out = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                        rc = cli.main(["pool", "--profiles", str(t / "labA.json"), str(t / "labX.json"),
                                       "--out", str(t / "pool.json")])
                    self.assertEqual(rc, 2)
                    self.assertEqual(sorted(p.name for p in t.iterdir()), ["labA.json", "labX.json"])
                    for planted in planted_strings:
                        self.assertNotIn(planted, err.getvalue() + out.getvalue())
        # the closed forms an honest export uses all pass
        ok = profile(state="reported")
        ok["rows"][0].update(row="lead", label_key="profile.lead")
        ok["rows"][0]["cells"][0].update(source="kit:a1_recall_matchers+kit:a10_band", rests_on=["R", "AL"],
                                         qualifiers=["one_draw"], unit="ai_box", x="remedy-placebo",
                                         band={"lo": 0.1, "hi": 0.2, "over": "8_settings", "narrow": True,
                                               "members": [{"x": "judge", "value": 0.1}],
                                               "subbands": [{"name": "lexical_only", "lo": 0.1, "hi": 0.2}]})
        ok["rows"][0]["cells"][1].update(needs=["J2", "judge_script"])
        poolmod.pool([ok, profile(state="reported")], labels=["labA", "labB"])
        poolmod.pool([profile(state="pending"), profile(state="pending")], labels=["labA", "labB"])

    def test_pool_refuses_duplicate_labels(self):
        # two files with the same stem (a/labA.json, b/labA.json) would share a column
        with self.assertRaisesRegex(ValueError, "two files are called labA; rename one"):      # village E3
            poolmod.pool([profile(), profile()], labels=["labA", "labA"])

    def test_fmt_falls_back_to_reason_code_after_needs(self):
        a = profile()
        a["rows"][0]["cells"].append({"name": "x1", "state": "not_run", "value": None, "reason_code": "ladder_unfinished"})  # a code the strings file has (A5 scan)
        out = poolmod.pool([a, profile()], labels=["labA", "labB"])
        md = poolmod.pool_markdown(out)
        self.assertIn("not run (sidejudge)", md)
        self.assertIn("not run (ladder_unfinished)", md)

    def test_settings_differ_is_recorded_not_merged(self):
        a, b = profile(), profile()
        a["run"]["settings"] = {"bootstrap_resamples": 2000, "bootstrap_seed": 1}
        b["run"]["settings"] = {"bootstrap_resamples": 2000, "bootstrap_seed": 1}
        same = poolmod.pool([a, b], labels=["labA", "labB"])
        self.assertIs(same["groups"][0]["settings_differ"], False)
        self.assertNotIn("settings", poolmod.pool_markdown(same).lower())
        b["run"]["settings"]["bootstrap_seed"] = 2
        diff = poolmod.pool([a, b], labels=["labA", "labB"])
        self.assertEqual(len(diff["groups"]), 1)                       # still side by side
        self.assertEqual(diff["groups"][0]["labs"], ["labA", "labB"])
        self.assertIs(diff["groups"][0]["settings_differ"], True)
        self.assertIn("bootstrap settings (B or seed) differ", poolmod.pool_markdown(diff))
        self.assertFalse(FORBIDDEN_KEYS & set(keys_of(diff)))

    def test_pool_cli_refusals(self):
        with tempfile.TemporaryDirectory(prefix="ss-tests-t7-") as t:
            t = Path(t)
            (t / "labA.json").write_text(json.dumps(profile()), encoding="utf-8")
            (t / "labB.json").write_text(json.dumps(profile()), encoding="utf-8")
            (t / "junk.json").write_text("{not json", encoding="utf-8")
            cases = {"missing file": ["--profiles", str(t / "labA.json"), str(t / "nope.json"), "--out", str(t / "o.json")],
                     "bad json": ["--profiles", str(t / "labA.json"), str(t / "junk.json"), "--out", str(t / "o.json")],
                     "md out": ["--profiles", str(t / "labA.json"), str(t / "labB.json"), "--out", str(t / "o.md")]}
            for what, argv in cases.items():
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(cli.main(["pool"] + argv), 2, what)
                self.assertIn("refused:", err.getvalue(), what)
            self.assertEqual(sorted(p.name for p in t.iterdir()), ["junk.json", "labA.json", "labB.json"])

    def test_pool_cli_writes_json_and_markdown(self):
        with tempfile.TemporaryDirectory(prefix="ss-tests-t7-") as t:
            t = Path(t)
            for name, rec in (("labA", 0.9), ("labB", 0.6)):
                (t / f"{name}.json").write_text(json.dumps(profile(recall=rec)), encoding="utf-8")
            (t / "bad.json").write_text(json.dumps(profile(9)), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["pool", "--profiles", str(t / "labA.json"), str(t / "labB.json"),
                                           "--out", str(t / "pool.json")]), 0)
            data = json.loads((t / "pool.json").read_text(encoding="utf-8"))
            self.assertEqual(data["groups"][0]["labs"], ["labA", "labB"])
            self.assertIn("labB", (t / "pool.md").read_text(encoding="utf-8"))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(["pool", "--profiles", str(t / "labA.json"), str(t / "bad.json"),
                                           "--out", str(t / "pool2.json")]), 2)
            self.assertIn("refused", err.getvalue())
            self.assertFalse((t / "pool2.json").exists())


class LabsTests(unittest.TestCase):
    """Builds the three labs and runs labA and labB on the oracle client with the network layer patched to raise."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="ss-tests-t7-labs-")
        cls.dest = Path(cls.tmp.name)
        cls.net_calls = []

        def boom(*a, **k):
            cls.net_calls.append(a)
            raise AssertionError("network used on the oracle path")

        cls.codes = {}
        with mock.patch.object(socket, "socket", boom), mock.patch.object(socket, "create_connection", boom), \
                mock.patch.object(urllib.request, "urlopen", boom):
            cls.cfgs = labs.build_labs(cls.dest, seed=0)
            for lab in ("labA", "labB"):
                with contextlib.redirect_stdout(io.StringIO()):
                    cls.codes[lab] = cli.main(["demo", "--out", str(cls.dest / lab / "out"), "--config",
                                               str(cls.cfgs[lab]), "--client", "oracle"])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def primary_recall(self, lab):
        ctx = pb.open_context(self.cfgs[lab], self.dest / lab / "out")
        cells = pc.row1_cells(ctx)
        hit = [c for c in cells if c["name"] == "attested_recall" and c.get("x") == "primary"]
        self.assertEqual(len(hit), 1)
        return hit[0]["value"]

    def test_labs_configs(self):
        self.assertEqual(set(self.cfgs), {"labA", "labB", "lab9"})
        for lab, path in self.cfgs.items():
            self.assertTrue(Path(path).is_absolute())
            cfg = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(cfg["corpus"]["kind"], "synthetic")
            self.assertTrue(Path(cfg["out_dir"]).is_absolute())
            self.assertTrue(Path(cfg["out_dir"]).is_relative_to(self.dest.resolve()), lab)
            for k in ("maps_glob", "transcripts_glob", "synthetic_dir"):
                self.assertTrue(Path(cfg["corpus"][k]).is_absolute(), (lab, k))
            self.assertEqual([s for s in cfg["script_sources"]],
                             [{"name": "judgescript", "role": "judge", "variants": ["p0", "p1", "p2"], "samples": 2,
                               "temperature": 0.7}])
            # Ruling R15: the grid spec is not added
            self.assertEqual(cfg["specs"], json.loads(Path(cli.DEMO_CONFIG).read_text(encoding="utf-8"))["specs"])
        roles = lambda lab: json.loads(Path(self.cfgs[lab]).read_text(encoding="utf-8"))["corpus"]["roles"]
        self.assertEqual(len(roles("labA")), 12)
        self.assertEqual(sorted(roles("lab9")), sorted(roles("labA"))[:9])
        self.assertEqual(len(roles("lab9")), 9)

    def test_labB_gold_loses_30_percent_of_nodes_and_their_edges(self):
        for gold_b in sorted((self.dest / "labB" / "corpus" / "gold").glob("SYN-*.json")):
            if gold_b.name.endswith(".concepts.json"):
                continue
            gold_a = json.loads((self.dest / "labA" / "corpus" / "gold" / gold_b.name).read_text(encoding="utf-8"))
            b = json.loads(gold_b.read_text(encoding="utf-8"))
            kept = {n["node_id"] for n in b["nodes"]}
            self.assertEqual(len(kept), len(gold_a["nodes"]) - round(0.3 * len(gold_a["nodes"])))
            self.assertTrue(all(e["source"] in kept and e["target"] in kept for e in b["edges"]))
            self.assertEqual(len(b["edges"]), sum(1 for e in gold_a["edges"]
                                                  if e["source"] in kept and e["target"] in kept))

    def test_labs_are_deterministic(self):
        with tempfile.TemporaryDirectory(prefix="ss-tests-t7-det-") as t:
            labs.build_labs(Path(t), seed=0)
            for rel in ("labB/corpus/gold/SYN-OW01.json", "labA/corpus/gold/SYN-OW01.json"):
                self.assertEqual((Path(t) / rel).read_bytes(), (self.dest / rel).read_bytes())

    def test_oracle_labs_no_network(self):
        self.assertEqual(self.net_calls, [])
        self.assertEqual(self.codes, {"labA": 0, "labB": 0})

    def test_labB_recall_lower(self):
        a, b = self.primary_recall("labA"), self.primary_recall("labB")
        print(f"\nlabA attested_recall primary = {a}; labB = {b}")
        self.assertLess(b, a)


class PoolEndToEnd(unittest.TestCase):
    """The steps of `make pool` in a temp dir: labs, oracle demo, export-profile, pool. Zero model and network calls."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="ss-tests-t7-e2e-")
        cls.dest = Path(cls.tmp.name)
        cls.cfgs = labs.build_labs(cls.dest, seed=0)
        cls.exit, cls.err = {}, {}
        for lab in ("labA", "labB", "lab9"):
            with contextlib.redirect_stdout(io.StringIO()):
                assert cli.main(["demo", "--out", str(cls.dest / lab / "out"), "--config", str(cls.cfgs[lab]),
                                 "--client", "oracle"]) == 0, lab
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                cls.exit[lab] = cli.main(["export-profile", "--config", str(cls.cfgs[lab]), "--out-dir",
                                          str(cls.dest / lab / "out"), "--profile", str(cls.dest / f"{lab}.json")])
            cls.err[lab] = err.getvalue()
        cls.pool_exit = cli.main(["pool", "--profiles", str(cls.dest / "labA.json"), str(cls.dest / "labB.json"),
                                  "--out", str(cls.dest / "pool.json")])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_lab9_is_refused(self):
        self.assertEqual(self.exit["lab9"], 2)
        self.assertIn("refused:", self.err["lab9"])
        self.assertFalse((self.dest / "lab9.json").exists())

    def test_pool_has_labA_and_labB_side_by_side(self):
        self.assertEqual((self.exit["labA"], self.exit["labB"], self.pool_exit), (0, 0, 0))
        data = json.loads((self.dest / "pool.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["groups"]), 1)
        self.assertEqual(data["separate"], [])
        g = data["groups"][0]
        self.assertEqual(g["labs"], ["labA", "labB"])
        self.assertEqual(g["key"]["state"], "synthetic")
        lines = {(t["row"], t["name"], t.get("x")): t for t in g["table"]}
        both = lines[(1, "attested_recall", "primary")]["columns"]
        self.assertGreater(both["labA"]["value"], both["labB"]["value"])
        md = (self.dest / "pool.md").read_text(encoding="utf-8")
        self.assertTrue(md.startswith("# Lab profiles, side by side"))
        self.assertIsNone(re.search(r"(?i)\bscript\w*", md))                   # A11, on the real pool.md

    def test_no_combined_value(self):
        data = json.loads((self.dest / "pool.json").read_text(encoding="utf-8"))
        self.assertFalse(FORBIDDEN_KEYS & set(keys_of(data)))
        self.assertIs(data["meta_analysis"], False)
        # every cell in a column is byte-equal to that lab's own cell: nothing was computed across labs
        for lab in ("labA", "labB"):
            prof = json.loads((self.dest / f"{lab}.json").read_text(encoding="utf-8"))
            own = {(r["row"], c["name"], c.get("x")): c for r in prof["rows"] for c in r["cells"]}
            for t in data["groups"][0]["table"]:
                if lab in t["columns"]:
                    self.assertEqual(t["columns"][lab], own[(t["row"], t["name"], t.get("x"))])

    def test_lab_exports_pass_string_check(self):
        for lab in ("labA", "labB"):
            prof = json.loads((self.dest / f"{lab}.json").read_text(encoding="utf-8"))
            self.assertEqual(pb.lab_string_problems(prof), [], lab)


if __name__ == "__main__":
    unittest.main()
