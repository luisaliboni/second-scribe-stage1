"""Project changes must not reuse another project's jobs or credentials."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from second_scribe.app.api import ApiError, AppState
from second_scribe.app.jobs import ModelCheck, RunJob
from second_scribe.app.project import Project
from second_scribe.app.routes.project import create_project, open_project


class ProjectIsolationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ss-tests-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.old = Project.create(self.root / "old", "Old")
        self.state = AppState(project=self.old)
        self.state.secrets.set("recon", "synthetic-test-key")
        self.state.secrets.set_env_name("judge", "SS_TEST_UNUSED_KEY")

    def test_open_discards_old_jobs_and_keys(self):
        other = Project.create(self.root / "other", "Other")
        job = Mock(spec=RunJob)
        job.active = False
        self.state.jobs["run"] = job
        open_project(self.state, {"path": str(other.root)}, {})
        self.assertEqual(self.state.project.project_id, other.project_id)
        self.assertEqual(self.state.jobs, {})
        self.assertFalse(self.state.secrets.has("recon"))
        self.assertFalse(self.state.secrets.has("judge"))

    def test_create_discards_old_jobs_and_keys(self):
        job = Mock(spec=ModelCheck)
        job.state = "passed"
        self.state.jobs["model_check"] = job
        create_project(self.state, {"path": str(self.root / "new"), "name": "New"}, {})
        self.assertEqual(self.state.project.name, "New")
        self.assertEqual(self.state.jobs, {})
        self.assertFalse(self.state.secrets.has("recon"))

    def test_failed_open_keeps_session(self):
        with self.assertRaises(ApiError):
            open_project(self.state, {"path": str(self.root / "absent")}, {})
        self.assertIs(self.state.project, self.old)
        self.assertTrue(self.state.secrets.has("recon"))

    def test_active_run_or_model_check_blocks_switch_before_writes(self):
        for kind, states in ((RunJob, ("running", "swapping", "needs_swap")),
                             (ModelCheck, ("q_running", "j_running", "needs_swap"))):
            for state in states:
                with self.subTest(kind=kind.__name__, state=state):
                    job = Mock(spec=kind)
                    job.state = state
                    if kind is RunJob:
                        job.active = True
                    self.state.jobs = {"job": job}
                    target = self.root / "new"
                    for handler, body in (
                        (create_project, {"path": str(target), "name": "New"}),
                        (open_project, {"path": str(self.old.root)}),
                    ):
                        with self.assertRaises(ApiError) as caught:
                            handler(self.state, body, {})
                        self.assertEqual(caught.exception.code, "project_busy")
                        self.assertIs(self.state.project, self.old)
                        self.assertTrue(self.state.secrets.has("recon"))
                        self.assertIs(self.state.jobs["job"], job)
                    self.assertFalse(target.exists())

    def test_reopening_same_project_also_discards_keys(self):
        # Deliberate: open re-reads the project file, so its endpoints may differ from the ones the keys were
        # entered for; keys are re-entered after any open, even of the folder already open.
        open_project(self.state, {"path": str(self.old.root)}, {})
        self.assertIsNot(self.state.project, self.old)
        self.assertEqual(self.state.project.project_id, self.old.project_id)
        self.assertFalse(self.state.secrets.has("recon"))
        self.assertFalse(self.state.secrets.has("judge"))

    def test_job_that_ended_but_was_not_polled_does_not_block_switch(self):
        # The run or check exited after the last status poll: the switch polls it first (as runs._busy and
        # model_check._require_run_idle do) instead of answering "busy" from the stale state.
        def ended_run():
            job = Mock(spec=RunJob)
            job.state, job.active = "running", True
            def poll():
                job.state, job.active = "done", False
                return {}
            job.poll.side_effect = poll
            return job

        def ended_check():
            job = Mock(spec=ModelCheck)
            job.state = "j_running"
            def poll():
                job.state = "passed"
                return {}
            job.poll.side_effect = poll
            return job

        for make in (ended_run, ended_check):
            for handler, body in (
                (create_project, {"path": str(self.root / f"new-{make.__name__}"), "name": "New"}),
                (open_project, {"path": str(self.old.root)}),
            ):
                with self.subTest(job=make.__name__, route=handler.__name__):
                    job = make()
                    self.state.jobs = {"job": job}
                    handler(self.state, body, {})
                    job.poll.assert_called()
                    self.assertEqual(self.state.jobs, {})

    def test_job_still_running_after_poll_blocks_switch(self):
        for kind, attrs in ((RunJob, {"state": "running", "active": True}), (ModelCheck, {"state": "q_running"})):
            with self.subTest(kind=kind.__name__):
                job = Mock(spec=kind)
                for k, v in attrs.items():
                    setattr(job, k, v)
                job.poll.return_value = {}
                self.state.jobs = {"job": job}
                with self.assertRaises(ApiError) as caught:
                    open_project(self.state, {"path": str(self.old.root)}, {})
                self.assertEqual(caught.exception.code, "project_busy")
                job.poll.assert_called()
                self.assertIs(self.state.project, self.old)


class ModelCheckExclusionTests(unittest.TestCase):
    def test_check_cannot_start_or_continue_during_active_run(self):
        from second_scribe.app.routes.model_check import start_check, continue_check
        from unittest.mock import patch
        project = Mock()
        project.blocked = None
        run = Mock(spec=RunJob)
        run.active = True
        state = AppState(project=project, jobs={"run": run})
        for handler in (start_check, continue_check):
            with self.subTest(route=handler.__name__):
                with patch("second_scribe.app.jobs.ModelCheck.start") as start:
                    with self.assertRaises(ApiError) as caught:
                        handler(state, {}, {})
                    self.assertEqual(caught.exception.code, "run_busy")
                    start.assert_not_called()


class EstimateIsolationTests(unittest.TestCase):
    def test_changed_project_or_pair_refuses_stale_estimate(self):
        from second_scribe.app.routes.model_check import post_estimate
        from unittest.mock import patch
        for changed in ("project", "pair"):
            with self.subTest(changed=changed):
                project = Mock()
                project.blocked = None
                state = AppState(project=project)
                original_secrets = state.secrets
                def estimate(captured, rungs, *, secrets):
                    self.assertIs(captured, project)
                    self.assertIs(secrets, original_secrets)
                    if changed == "project":
                        state.project = Mock()
                    return {"low_s": 1}
                hashes = ["old", "new"] if changed == "pair" else ["old"]
                with patch("second_scribe.app.models.require_ready"), \
                     patch("second_scribe.app.models.pair_sha256", side_effect=hashes), \
                     patch("second_scribe.app.timing.estimate", side_effect=estimate):
                    with self.assertRaises(ApiError) as caught:
                        post_estimate(state, {}, {})
                self.assertEqual(caught.exception.code,
                                 "no_project" if changed == "project" else "families_changed")


class EstimateConcurrencyTests(unittest.TestCase):
    def test_inflight_estimate_excludes_estimate_project_run_and_check(self):
        import threading
        from unittest.mock import patch
        from second_scribe.app.routes.model_check import post_estimate, start_check, continue_check
        from second_scribe.app.routes.runs import start_run
        project = Mock()
        project.blocked = None
        state = AppState(project=project)
        entered, release = threading.Event(), threading.Event()
        answers = []

        def estimate(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("timeout")
            return {"low_s": 1}

        def worker():
            try:
                answers.append(post_estimate(state, {}, {}))
            except Exception as exc:
                answers.append(exc)

        with patch("second_scribe.app.models.require_ready"), \
             patch("second_scribe.app.models.pair_sha256", return_value="pair"), \
             patch("second_scribe.app.timing.estimate", side_effect=estimate) as probe:
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                for handler, body in (
                    (post_estimate, {}), (start_check, {}), (continue_check, {}),
                    (start_run, {"group": "A"}),
                    (open_project, {"path": "/unused-synthetic-project"}),
                    (create_project, {"path": "/unused-synthetic-project", "name": "New"}),
                ):
                    with self.subTest(route=handler.__name__):
                        with self.assertRaises(ApiError) as caught:
                            handler(state, body, {})
                        self.assertIn(caught.exception.code, ("project_busy", "run_busy"))
                self.assertEqual(probe.call_count, 1)
            finally:
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(answers, [{"estimate": {"low_s": 1}}])
            self.assertFalse(state.estimate_active)
            self.assertEqual(post_estimate(state, {}, {}), answers[0])

    def test_failure_releases_estimate_slot(self):
        from unittest.mock import patch
        from second_scribe.app.routes.model_check import post_estimate
        project = Mock()
        project.blocked = None
        state = AppState(project=project)
        for failure in (ValueError("no_transcripts"), RuntimeError("timeout"), OSError("synthetic failure")):
            with self.subTest(failure=type(failure).__name__), \
                 patch("second_scribe.app.models.require_ready"), \
                 patch("second_scribe.app.models.pair_sha256", return_value="pair"), \
                 patch("second_scribe.app.timing.estimate", side_effect=failure):
                with self.assertRaises((ApiError, OSError)):
                    post_estimate(state, {}, {})
                self.assertFalse(state.estimate_active)
