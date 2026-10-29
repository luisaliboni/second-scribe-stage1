"""Task 40: the app's Results routes and the Instructions routes (spec 6.3, 6.7 gate, 6.8; plan V2, V13, V14).

- ``GET /api/view?run=`` and ``GET /api/results/profile?run=`` register with ``coding.results_route``: refused while a
  B coding is open, the reveal logged once after the handler answers, a refusal from the handler reveals nothing.
- Only ``<project>/views/<run_id>.view.json`` is served; any other run id or a link out of ``views/`` is refused.
- The profile is the kit exporter's (``profile_view.run_exporter``) on the run; a refusal comes back verbatim.
- ``GET /api/results/runs`` lists runs (id, group, ready) without anything AI-derived.
- ``GET /api/prompts`` reads the kit's ``prompts/`` and never writes there.
- ``GET/POST /api/instructions`` keeps the rung choice and the user's rung-1 prompt (``tool.rung1_user`` shape).

Runs are the shipped demo with the oracle client, placed as Task 38 will lay them out (``tests/results_fixture.py``).
"""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from second_scribe import ladder, profile_export, toolcfg
from second_scribe.app import api, coding, profile_view, results
from tests import results_fixture as rf
from tests.test_coding import SENTINEL, make_project, plant_ai_content, span

REPO = Path(__file__).resolve().parent.parent
PROMPTS = REPO / "prompts"
EN = json.loads((REPO / "second_scribe" / "app" / "strings" / "en.json").read_text(encoding="utf-8"))
RUN = rf.RUN_ID


def setUpModule():
    rf.demo_run()          # build the demo once, before any test's clock starts


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.p = make_project(self.tmp / "proj")
        self.state = api.AppState(project=self.p)
        api.load_route_modules()

    def call(self, method, path, body=None, query=None):
        try:
            return 200, api.dispatch(self.state, method, path, body or {}, query or {})
        except api.ApiError as e:
            return e.status, e.to_json()

    def ok(self, method, path, body=None, query=None):
        status, doc = self.call(method, path, body, query)
        self.assertEqual(status, 200, doc)
        return doc

    def reveals(self):
        return len(self.p.log.records("reveal"))

    def open_b_coding(self):
        self.assertEqual(self.call("POST", "/api/references", {"pid": "CASE-B01", "type": "B"})[0], 200)
        body = {"pid": "CASE-B01", **span("CASE-B01", 1, 10), "status": "happened", "step": "Ask the clerk",
                "phase": "Applying"}
        self.assertEqual(self.call("POST", "/api/coding/mark", body)[0], 200)


class TestRunLookup(Case):
    def test_lists_runs_in_task38_layout(self):
        rf.place_run(self.p)
        rf.place_run(self.p, "A-1b2c3d4e", view=False)
        plant_ai_content(self.p)                  # runs/B-0a1b2c3d without a config: not a run
        runs = results.list_runs(self.p)
        self.assertEqual([(r.run_id, r.group) for r in runs], [(RUN, "A"), ("A-1b2c3d4e", "A")])
        ref = results.find_run(self.p, RUN)
        self.assertEqual(ref.config_path, self.p.path(f"runs/{RUN}/config.json"))
        self.assertEqual(ref.out_dir, self.p.path(f"runs/{RUN}"))
        self.assertTrue(ref.finished)
        self.assertTrue(results.has_view(self.p, RUN))
        self.assertFalse(results.has_view(self.p, "A-1b2c3d4e"))

    def test_runs_route_has_no_ai_content(self):
        rf.place_run(self.p)
        plant_ai_content(self.p)
        self.open_b_coding()
        doc = self.ok("GET", "/api/results/runs")
        base = ("run_id", "group", "finished", "ready", "state")
        self.assertEqual([{k: r[k] for k in base} for r in doc["runs"]],
                         [{"run_id": RUN, "group": "A", "finished": True, "ready": True, "state": "finished"}])
        # Part 5a (scout F10): the row's facts come from the config and the run_start records only
        self.assertEqual(set(doc["runs"][0]) - set(base), {"started", "rungs", "rung1_own", "n_people", "models"})
        self.assertIsInstance(doc["runs"][0]["started"], str)
        self.assertIsInstance(doc["runs"][0]["n_people"], int)
        self.assertTrue(set(doc["runs"][0]["models"]) <= {"recon", "judge"})
        self.assertNotIn("ZEBRAQUARTZ", json.dumps(doc))
        self.assertEqual(self.reveals(), 0)

    def _unfinished(self, run_id):
        """A run that never finished: the demo copy without its manifest and without a view (as a failed run is)."""
        folder = rf.place_run(self.p, run_id, view=False)
        (folder / "logs" / "run.manifest.json").unlink()
        return folder

    def test_r14_runs_route_sends_the_job_state(self):
        """R14: a finished run is "finished", a run nobody is running is "stopped", a run being run is "running"."""
        from second_scribe.app import jobs
        from second_scribe.app.routes import runs as runs_routes
        rf.place_run(self.p)
        self._unfinished("A-1b2c3d4e")
        folder = self._unfinished("A-2b2c3d4e")
        by_id = lambda: {r["run_id"]: r for r in self.ok("GET", "/api/results/runs")["runs"]}   # noqa: E731
        got = by_id()
        self.assertEqual(got[RUN]["state"], "finished")
        self.assertEqual(got["A-1b2c3d4e"]["state"], "stopped", "no live job: it stopped (or the app was restarted)")
        self.assertEqual(got["A-1b2c3d4e"]["ready"], False)
        # a live job that is running this run: "running"; the same job after it failed: "stopped"
        job = jobs.RunJob(self.p, folder / "config.json", env={})
        self.state.jobs[runs_routes.LIVE] = job
        job.state = "running"
        self.assertEqual(by_id()["A-2b2c3d4e"]["state"], "running")
        self.assertEqual(by_id()["A-1b2c3d4e"]["state"], "stopped", "only the live run is running")
        job.state = "failed"
        self.assertEqual(by_id()["A-2b2c3d4e"]["state"], "stopped")
        job.state = "paused"
        self.assertEqual(by_id()["A-2b2c3d4e"]["state"], "stopped")
        self.assertEqual(self.reveals(), 0, "the runs list never reveals")

    def test_out_dir_outside_runs_is_not_a_run(self):
        folder = rf.place_run(self.p, view=False)
        cfg = json.loads((folder / "config.json").read_text(encoding="utf-8"))
        cfg["out_dir"] = str(self.tmp)
        (folder / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.assertEqual(results.list_runs(self.p), [])
        status, doc = self.call("GET", "/api/view", query={"run": RUN})
        self.assertEqual(status, 404, doc)

    def test_group_must_match_tool_reference_type(self):
        folder = rf.place_run(self.p, view=False)
        cfg = json.loads((folder / "config.json").read_text(encoding="utf-8"))
        cfg["tool"] = {"reference_type": "C"}
        (folder / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.assertEqual(results.list_runs(self.p), [])


class TestViewGate(Case):
    def test_view_requires_gate(self):
        rf.place_run(self.p)
        self.open_b_coding()
        status, doc = self.call("GET", "/api/view", query={"run": RUN})
        self.assertEqual((status, doc["error"]), (409, "coding_unlocked"))
        status, doc = self.call("GET", "/api/results/profile", query={"run": RUN})
        self.assertEqual((status, doc["error"]), (409, "coding_unlocked"))
        self.assertEqual(self.reveals(), 0)
        self.ok("POST", "/api/coding/lock", {"pid": "CASE-B01"})
        self.assertEqual(self.reveals(), 0)
        doc = self.ok("GET", "/api/view", query={"run": RUN})
        on_disk = json.loads(self.p.path(f"views/{RUN}.view.json").read_text(encoding="utf-8"))
        self.assertEqual(doc, {"run_id": RUN, "group": "A", "view": on_disk})
        self.assertEqual(self.reveals(), 1)
        self.ok("GET", "/api/view", query={"run": RUN})
        self.assertEqual(self.reveals(), 1)

    def test_results_routes_are_gated(self):
        """Every results route answers 409 while a B coding is open, and is registered through results_route."""
        for path in results.RESULT_ROUTES:
            self.assertIn(("GET", path), api.ROUTES)
            self.assertIn("results_route.<locals>", api.ROUTES[("GET", path)].__qualname__)
        self.open_b_coding()
        for path in results.RESULT_ROUTES:
            with self.subTest(path=path):
                status, doc = self.call("GET", path, query={"run": RUN})
                self.assertEqual((status, doc["error"]), (409, "coding_unlocked"))
        self.assertEqual(self.reveals(), 0)

    def test_unknown_run_reveals_nothing(self):
        status, doc = self.call("GET", "/api/view", query={"run": "A-ffffffff"})
        self.assertEqual((status, doc["error"]), (404, "unknown_run"))
        rf.place_run(self.p, view=False)
        status, doc = self.call("GET", "/api/view", query={"run": RUN})
        self.assertEqual((status, doc["error"]), (404, "view_missing"))
        self.assertEqual(self.reveals(), 0)

    def test_view_path_traversal_refused(self):
        rf.place_run(self.p)
        secret = self.tmp / "outside.view.json"
        secret.write_text(json.dumps({"ai": SENTINEL}), encoding="utf-8")
        for bad in ("../outside", f"{RUN}/../../outside", "/etc/passwd", f"..%2F{RUN}", "A-0A1B2C3D", "",
                    f"{RUN}.view", "D-0a1b2c3d", "A-0a1b2c3", "A-0a1b2c3d\x00", ["A-0a1b2c3d"]):
            with self.subTest(run=bad):
                status, doc = self.call("GET", "/api/view", query={"run": bad})
                self.assertEqual((status, doc["error"]), (422, "bad_run"))
                self.assertNotIn("ZEBRAQUARTZ", json.dumps(doc))
        # a link inside views/ that points out of the project is refused too
        link_id = "A-2c3d4e5f"
        rf.place_run(self.p, link_id, view=False)
        os.symlink(secret, self.p.root / "views" / f"{link_id}.view.json")
        status, doc = self.call("GET", "/api/view", query={"run": link_id})
        self.assertEqual((status, doc["error"]), (404, "view_missing"))
        self.assertNotIn("ZEBRAQUARTZ", json.dumps(doc))
        self.assertEqual(self.reveals(), 0)

    def test_links_are_never_followed(self):
        rf.place_run(self.p)
        # a link inside views/ to another run's view (same folder) is not served either
        twin = "A-3d4e5f60"
        rf.place_run(self.p, twin, view=False)
        os.symlink(self.p.root / "views" / f"{RUN}.view.json", self.p.root / "views" / f"{twin}.view.json")
        self.assertFalse(results.has_view(self.p, twin))
        status, doc = self.call("GET", "/api/view", query={"run": twin})
        self.assertEqual((status, doc["error"]), (404, "view_missing"))
        # a run folder that is a link to a run kept outside the project is not a run
        far_id = "A-4e5f6071"
        far = self.tmp / "elsewhere" / far_id
        far.parent.mkdir()
        rf.place_run(self.p, far_id, view=False).rename(far)
        cfg = json.loads((far / "config.json").read_text(encoding="utf-8"))
        cfg["out_dir"] = str(far)
        (far / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        os.symlink(far, self.p.root / "runs" / far_id)
        self.assertNotIn(far_id, [r.run_id for r in results.list_runs(self.p)])
        status, doc = self.call("GET", "/api/results/profile", query={"run": far_id})
        self.assertEqual((status, doc["error"]), (404, "unknown_run"))
        self.assertEqual(self.reveals(), 0)


class TestProfile(Case):
    def test_profile_from_the_exporter_then_cached(self):
        rf.place_run(self.p)
        real = profile_view.run_exporter
        with mock.patch.object(profile_view, "run_exporter", side_effect=real) as spy:
            doc = self.ok("GET", "/api/results/profile", query={"run": RUN})
            self.assertEqual(spy.call_count, 1)
            args, kwargs = spy.call_args
            self.assertEqual(Path(args[0]), self.p.path(f"runs/{RUN}/config.json"))
            self.assertEqual(Path(args[1]), self.p.path(f"runs/{RUN}"))
            self.assertEqual(Path(args[2]), self.p.path(f"views/{RUN}.profile.json"))
            self.assertEqual(kwargs.get("reference_type"), "participant-endorsed")
            again = self.ok("GET", "/api/results/profile", query={"run": RUN})
            self.assertEqual(spy.call_count, 1)                 # the second answer is the file already written
        self.assertEqual(doc, again)
        self.assertEqual(doc["run_id"], RUN)
        self.assertEqual(doc["group"], "A")
        self.assertEqual(doc["profile"]["schema"].split("/")[0], "orgsci-profile")
        self.assertEqual(doc["profile"]["state"], "synthetic")
        profile_view.validate_profile(doc["profile"])
        self.assertNotIn("refused", doc)
        self.assertEqual(self.reveals(), 1)

    def test_refusal_comes_with_plain_sentences_and_the_verbatim_reasons(self):
        """R12: ``refused`` stays the exporter's own words (for the terminal log); ``refusals`` are what the screen shows."""
        rf.place_run(self.p)
        reasons = [profile_export.k_floor_reason(4), "privacy scan: a string longer than 200 characters"]
        with mock.patch.object(profile_view, "run_exporter", side_effect=profile_view.ProfileRefused(reasons)), \
                mock.patch("builtins.print") as printed:
            doc = self.ok("GET", "/api/results/profile", query={"run": RUN})
        self.assertEqual(doc["refused"], reasons)
        self.assertEqual([r["code"] for r in doc["refusals"]], ["k_floor", "privacy_check"])
        self.assertEqual(doc["refusals"][0]["message"],
                         "Too few participants were analysed to share a profile: 4, and at least 10 are needed.")
        shown = json.dumps(doc["refusals"])
        for dev_word in ("n_analysed", "k floor", "privacy scan:"):
            self.assertNotIn(dev_word, shown)
        logged = " ".join(str(a) for c in printed.call_args_list for a in c.args)
        for reason in reasons:
            self.assertIn(reason, logged, "each verbatim exporter reason goes to the terminal log")
        self.assertFalse(self.p.path(f"views/{RUN}.profile.json").exists())


    def test_exporter_failure_is_an_error_not_a_profile(self):
        rf.place_run(self.p)
        with mock.patch.object(profile_view, "run_exporter", side_effect=RuntimeError("exit 1: boom")):
            status, doc = self.call("GET", "/api/results/profile", query={"run": RUN})
        self.assertEqual((status, doc["error"]), (500, "profile_failed"))
        self.assertEqual(self.reveals(), 0)

    def test_stale_or_broken_cache_is_rebuilt(self):
        rf.place_run(self.p)
        self.p.path(f"views/{RUN}.profile.json").write_text("{not json", encoding="utf-8")
        doc = self.ok("GET", "/api/results/profile", query={"run": RUN})
        self.assertEqual(doc["profile"]["state"], "synthetic")


class TestPrompts(Case):
    def snapshot(self):
        return {p.name: (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
                for p in sorted(PROMPTS.iterdir())}

    def test_prompts_read_only(self):
        before = self.snapshot()
        doc = self.ok("GET", "/api/prompts")
        self.assertEqual(self.snapshot(), before)
        rungs = {r["rung"]: r for r in doc["rungs"]}
        self.assertEqual(list(rungs), ["rung1", "rung2", "rung3", "placebo"])
        for rung in ladder.RUNGS:
            r = rungs[rung.key]
            self.assertEqual(r["file"], rung.prompt_file)
            self.assertEqual(r["text"], (PROMPTS / rung.prompt_file).read_text(encoding="utf-8"))
            self.assertEqual(r["sha256"], hashlib.sha256((PROMPTS / rung.prompt_file).read_bytes()).hexdigest())
            for part, name in (("system", rung.system_file), ("definitions", rung.definitions_file)):
                if name is None:
                    self.assertIsNone(r[part])
                else:
                    self.assertEqual(r[part]["text"], (PROMPTS / name).read_text(encoding="utf-8"))
        self.assertEqual(doc["wrapper"]["text"], toolcfg.RUNG1_WRAPPER_PATH.read_text(encoding="utf-8"))
        self.assertTrue(doc["reading3"]["sha256"].startswith("9b48f863"))
        self.assertEqual(doc["reading3"]["file"], "prior_only.p0.v2.txt")

    def test_reading3_string_is_the_as_run_file(self):
        quoted = EN["study.reading3.prompt"].replace("{{", "{").replace("}}", "}")
        self.assertEqual(quoted.rstrip("\n"), (PROMPTS / "prior_only.p0.v2.txt").read_text(encoding="utf-8").rstrip("\n"))

    def test_prompts_need_no_project_and_no_gate(self):
        self.open_b_coding()
        self.ok("GET", "/api/prompts")
        self.state.project = None
        self.ok("GET", "/api/prompts")
        self.assertEqual(self.reveals(), 0)


class TestInstructions(Case):
    def test_defaults(self):
        doc = self.ok("GET", "/api/instructions")
        self.assertEqual(doc["instructions"], {"rung1": "basic", "rung1_user": None, "rungs": ["rung2"],
                                               "preset": "standard"})
        self.assertEqual(doc["run_rungs"], ["rung2"])

    def test_own_prompt_stored_as_rung1_user_block(self):
        text = "Draw the journey this person describes as a map of steps."
        doc = self.ok("POST", "/api/instructions", {"rung1": "own", "rung1_text": text, "rungs": ["rung1", "rung3"],
                                                    "preset": "full"})
        ins = doc["instructions"]
        self.assertEqual(ins["rungs"], ["rung1", "rung2", "rung3"])        # rung 2 always runs
        self.assertEqual(doc["run_rungs"], ["rung1", "rung2", "placebo", "rung3"])   # rung 3 brings its placebo
        self.assertEqual(ins["preset"], "full")
        block = ins["rung1_user"]
        self.assertEqual(block, toolcfg.rung1_user_block(text))
        self.assertEqual(ladder.rung1_template_problems(block), [])
        toolcfg.check_tool({"rung1_user": block}, {})
        self.assertEqual(self.p.data["instructions"], ins)
        self.assertEqual(self.ok("GET", "/api/instructions")["instructions"], ins)
        # back to the basic prompt: the user's text is kept, unused
        ins2 = self.ok("POST", "/api/instructions", {"rung1": "basic", "rungs": ["rung1"]})["instructions"]
        self.assertEqual((ins2["rung1"], ins2["rung1_user"]), ("basic", block))

    def test_refusals(self):
        for body, code in (({"rung1": "own", "rung1_text": "  ", "rungs": ["rung1"]}, "rung1_text_empty"),
                           ({"rung1": "own", "rung1_text": "x" * 20001, "rungs": []}, "rung1_text_long"),
                           ({"rung1": "mine", "rungs": []}, "bad_rung1"),
                           ({"rungs": ["placebo"]}, "bad_rungs"),
                           ({"rungs": "rung3"}, "bad_rungs"),
                           ({"rungs": [], "preset": "huge"}, "bad_preset")):
            with self.subTest(code=code):
                status, doc = self.call("POST", "/api/instructions", body)
                self.assertEqual((status, doc["error"]), (422, code))
        self.assertNotIn("instructions", self.p.data)

    def test_needs_a_project(self):
        self.state.project = None
        status, doc = self.call("GET", "/api/instructions")
        self.assertEqual((status, doc["error"]), (409, "no_project"))



class TestRefusalWording(unittest.TestCase):
    """R12: every reason the exporter gives maps to one plain sentence under ``results.refused.reason.<code>``."""

    # (reason as the kit writes it, code)
    CASES = [
        (profile_export.k_floor_reason(4), "k_floor"),
        ("n_people below K at $.population.n_people", "k_floor"),
        ("row [3] n_people below K (absent)", "k_floor"),
        (profile_export.k_floor_reason(None), "k_floor_unknown"),
        # village E4's plain kit wording (vil-pool ab4e3ad), read as well as the older form
        ("this run analysed 4 people; a profile needs at least 10 (the k floor) so no number can point to one person. Nothing was written.", "k_floor"),
        ("the number of people this run analysed is missing or not a whole number; a profile needs at least 10 (the k "
         "floor) so no number can point to one person. Nothing was written.", "k_floor_unknown"),
        ("no finished run found at /x/run; pass --out-dir <the run's folder>", "no_finished_run"),   # village A12
        ("the run log cannot be read or does not verify (OSError)", "log_unreadable"),
        ("the run log does not verify against its manifest (chain_break): the run is not exported", "log_unverified"),
        ("the run config cannot be opened (KeyError)", "config_unreadable"),
        ("the profile holds a non-finite number", "non_finite"),
        ("non-finite number at $.rows[2]", "non_finite"),
        ("forbidden key $.rows[0].pid", "privacy_check"),
        ("64-hex string outside the run block", "privacy_check"),
        ("suspicious x label in row 3 cell[1]", "privacy_check"),
        ("map-level cell with n != population: row 2 cell[0]", "privacy_check"),
        ("row 1: key 'note' is not allowed (lab exports carry codes, not sentences)", "privacy_check"),
        ("a row is not an object", "privacy_check"),
        ("model info: unknown role or not an object", "other"),
        ("", "other"),
    ]

    def test_every_reason_has_a_code_and_a_plain_string(self):
        for reason, code in self.CASES:
            with self.subTest(reason=reason):
                got = results.refusal_messages([reason])
                self.assertEqual([r["code"] for r in got], [code])
                self.assertIn(f"results.refused.reason.{code}", EN)
                msg = got[0]["message"]
                self.assertTrue(msg.endswith("."), msg)
                for dev in ("n_analysed", "k floor", "manifest", "non-finite", "forbidden key", "64-hex", "n_people"):
                    self.assertNotIn(dev, msg)
                self.assertNotIn("{", msg)

    def test_k_floor_sentence_carries_the_counts(self):
        self.assertEqual(results.refusal_messages([profile_export.k_floor_reason(4)])[0]["message"],
                         "Too few participants were analysed to share a profile: 4, and at least 10 are needed.")
        self.assertEqual(results.refusal_messages([profile_export.k_floor_reason(0)])[0]["message"],
                         "Too few participants were analysed to share a profile: 0, and at least 10 are needed.")

    def test_e4_k_floor_wording_carries_the_counts(self):
        self.assertEqual(results.refusal_messages(["this run analysed 4 people; a profile needs at least 10 (the k floor) so no number can point to one person. Nothing was written."])[0]["message"],
                         "Too few participants were analysed to share a profile: 4, and at least 10 are needed.")
        one = "this run analysed 1 person; a profile needs at least 10 (the k floor) so no number can point to one person."
        self.assertIn(": 1,", results.refusal_messages([one])[0]["message"])
        no_run = results.refusal_messages(["no finished run found at /x/run; pass --out-dir <the run's folder>"])
        self.assertNotIn("/x/run", no_run[0]["message"])

    def test_one_sentence_for_each_code_even_when_the_exporter_repeats_itself(self):
        reasons = [profile_export.k_floor_reason(4), "n_people below K at $.population.n_people",
                   "row [1] n_people below K (absent)", "forbidden key $.a", "forbidden key $.b"]
        got = results.refusal_messages(reasons)
        self.assertEqual([r["code"] for r in got], ["k_floor", "privacy_check"])
        self.assertIn(": 4,", got[0]["message"], "the sentence with the count wins over the bare ones")

    def test_a_bare_k_floor_reason_still_reads_plainly(self):
        got = results.refusal_messages(["n_people below K at $.population.n_people"])
        self.assertEqual(got[0]["message"], "Too few participants were analysed to share a profile: at least 10 are needed.")

    def test_every_code_string_exists_and_nothing_else_is_left_unmapped(self):
        codes = {c for _r, c in self.CASES}
        self.assertEqual(codes, set(results.REFUSAL_CODES))
        for code in results.REFUSAL_CODES:
            self.assertIn(f"results.refused.reason.{code}", EN)


if __name__ == "__main__":
    unittest.main()
