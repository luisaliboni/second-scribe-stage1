"""K5 / app T13: the Windows lock shim (``audit.lock``) and the Windows-safe work-store folder names (``paths``).

Nothing here needs a Windows machine: the Windows branch is forced (``paths.WINDOWS = True``; ``lock._msvcrt`` set to a
fake). On POSIX the folder names of every stage key must stay byte-identical, because the existing work stores and the
run logs' work-integrity checks are keyed by them."""
import contextlib
import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from second_scribe import audits, jcounts, ladder, paths, pipeline, prereg
from second_scribe.audit import lock
from second_scribe.audit import verify as audit_verify
from second_scribe.audit.registry import AuditLog
from second_scribe.cli import DEMO_CONFIG, build_clients
from second_scribe.pipeline import STAGE_ORDER, RunConfig, Work, run

# every stage key a work store holds: the stages themselves and the ladder's "<stage>:<rung key>" stores
RUNG_KEYS = ("rung1", "rung2", "placebo", "rung3")
LADDER_STAGES = ("recon", "judge_modality", "align")
ALL_STAGE_KEYS = (*STAGE_ORDER, *(f"{s}:{k}" for s in LADDER_STAGES for k in RUNG_KEYS))


@contextlib.contextmanager
def windows(flag=True):
    with mock.patch.object(paths, "WINDOWS", flag):
        yield


class FakeMsvcrt:
    """Records ``locking`` calls; ``fail`` makes every lock attempt raise OSError (the byte is held elsewhere)."""
    LK_UNLCK, LK_LOCK, LK_NBLCK = 0, 1, 2

    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def locking(self, fd, mode, nbytes):
        self.calls.append({"mode": mode, "nbytes": nbytes, "pos": os.lseek(fd, 0, os.SEEK_CUR),
                           "inode": os.fstat(fd).st_ino})
        if self.fail and mode == self.LK_NBLCK:
            raise OSError(13, "locked")


class TestPosixLock(unittest.TestCase):
    def test_posix_lock_calls_flock(self):
        import fcntl
        with tempfile.TemporaryDirectory(prefix="ss-win-") as d:
            log = AuditLog(Path(d) / "run.jsonl", "r")
            with mock.patch.object(fcntl, "flock") as flock:
                log.append("x", {"a": 1})
            log.close()
        self.assertEqual([c.args[1] for c in flock.call_args_list], [fcntl.LOCK_EX, fcntl.LOCK_UN])

    def test_posix_leaves_no_sidecar(self):
        with tempfile.TemporaryDirectory(prefix="ss-win-") as d:
            log = AuditLog(Path(d) / "run.jsonl", "r")
            log.append("x", {"a": 1})
            log.close()
            self.assertEqual(sorted(p.name for p in Path(d).iterdir() if p.suffix == ".lock"), [])


class TestWindowsLock(unittest.TestCase):
    def test_fake_msvcrt_sidecar_lock(self):
        fake = FakeMsvcrt()
        with tempfile.TemporaryDirectory(prefix="ss-win-") as d:
            path = Path(d) / "run.jsonl"
            with mock.patch.object(lock, "_msvcrt", fake):
                log = AuditLog(path, "r")
                log.append("x", {"a": 1})
                log.append("x", {"a": 2})
                log.close()
            side = Path(str(path) + ".lock")
            self.assertTrue(side.is_file())
            self.assertEqual([c["mode"] for c in fake.calls],
                             [fake.LK_NBLCK, fake.LK_UNLCK, fake.LK_NBLCK, fake.LK_UNLCK])      # lock, unlock, per append
            # byte 0, one byte, on the sidecar and never on the log itself
            self.assertTrue(all(c["nbytes"] == 1 and c["pos"] == 0 for c in fake.calls))
            self.assertEqual({c["inode"] for c in fake.calls}, {side.stat().st_ino})
            self.assertNotEqual(side.stat().st_ino, path.stat().st_ino)
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 2)    # and the log was written

    def test_the_log_stays_readable_while_locked(self):
        """The log is never byte-locked, so head() and verify work from another handle during an append."""
        fake = FakeMsvcrt()
        with tempfile.TemporaryDirectory(prefix="ss-win-") as d:
            path = Path(d) / "run.jsonl"
            with mock.patch.object(lock, "_msvcrt", fake):
                log = AuditLog(path, "r")
                log.append("x", {"a": 1})
                with open(path, "ab") as fh, lock.exclusive(fh):
                    self.assertEqual(log.head()[0], 1)
                log.close()

    def test_lock_retry_then_error(self):
        fake = FakeMsvcrt(fail=True)
        clock = {"t": 0.0}

        def monotonic():
            clock["t"] += 1.0
            return clock["t"]
        with tempfile.TemporaryDirectory(prefix="ss-win-") as d, \
                mock.patch.object(lock, "_msvcrt", fake), \
                mock.patch.object(lock.time, "monotonic", monotonic), \
                mock.patch.object(lock.time, "sleep") as sleep:
            with open(Path(d) / "run.jsonl", "a+b") as fh:
                with self.assertRaises(RuntimeError) as cm:
                    with lock.exclusive(fh):
                        self.fail("the lock must not be granted")
        self.assertEqual(str(cm.exception), "audit log is locked by another process")
        self.assertGreater(len(fake.calls), 2)                     # retried, not given up at once
        self.assertTrue(all(c["mode"] == fake.LK_NBLCK for c in fake.calls))
        self.assertTrue(all(c.args == (0.05,) for c in sleep.call_args_list))      # every 50 ms
        self.assertLessEqual(clock["t"], 125)                       # and gives up at about 120 s

    def test_unlock_even_when_the_body_raises(self):
        fake = FakeMsvcrt()
        with tempfile.TemporaryDirectory(prefix="ss-win-") as d, mock.patch.object(lock, "_msvcrt", fake):
            with open(Path(d) / "run.jsonl", "a+b") as fh:
                with self.assertRaises(ZeroDivisionError):
                    with lock.exclusive(fh):
                        1 / 0
        self.assertEqual([c["mode"] for c in fake.calls], [fake.LK_NBLCK, fake.LK_UNLCK])


class TestStageDirnames(unittest.TestCase):
    def test_stage_dirname_roundtrip(self):
        with windows():
            self.assertEqual(paths.stage_dirname("recon:rung1"), "recon~rung1")
            self.assertEqual(paths.stage_from_dirname("recon~rung1"), "recon:rung1")
            for k in ALL_STAGE_KEYS:
                self.assertNotIn(":", paths.stage_dirname(k))
                self.assertEqual(paths.stage_from_dirname(paths.stage_dirname(k)), k)
        with windows(False):
            self.assertEqual(paths.stage_dirname("recon:rung1"), "recon:rung1")
            self.assertEqual(paths.stage_from_dirname("recon:rung1"), "recon:rung1")

    def test_default_follows_the_os(self):
        self.assertEqual(paths.WINDOWS, os.name == "nt")

    def test_no_tilde_in_any_kit_stage_key(self):
        keys = [*STAGE_ORDER, *(r.key for r in ladder.RUNGS), *ALL_STAGE_KEYS]
        self.assertEqual(sorted(r.key for r in ladder.RUNGS), sorted(RUNG_KEYS))
        for k in keys:
            self.assertNotIn("~", k)
        self.assertEqual({k for k in ALL_STAGE_KEYS if ":" in k},
                         {f"{s}:{r}" for s in LADDER_STAGES for r in RUNG_KEYS})

    def test_posix_names_unchanged_for_every_stage_key(self):
        """The study must not change on macOS/Linux: the folder of every current stage key is the key itself."""
        self.assertFalse(paths.WINDOWS and os.name != "nt")
        with windows(False), tempfile.TemporaryDirectory(prefix="ss-win-") as d:
            w = Work(d)
            rw = pipeline.RungWork(w, "rung3")
            for k in ALL_STAGE_KEYS:
                self.assertEqual(paths.stage_dirname(k), k)
                self.assertEqual(paths.stage_from_dirname(k), k)
                self.assertEqual(str(w.path(k, "n")), os.path.join(d, "work", k, "n.json"), k)
            for stage in ("recon", "judge_modality", "prior_only"):
                self.assertEqual(str(rw.path(stage, "n")),
                                 os.path.join(d, "work", stage + (":rung3" if stage != "prior_only" else ""), "n.json"))
            self.assertEqual(str(rw.path("align", "a__ai_x")), os.path.join(d, "work", "align:rung3", "a__ai_x.json"))

    def test_windows_work_paths_have_no_colon(self):
        with windows(), tempfile.TemporaryDirectory(prefix="ss-win-") as d:
            w = Work(d)
            rw = pipeline.RungWork(w, "rung3")
            self.assertEqual(w.path("recon:rung1", "n"), Path(d) / "work" / "recon~rung1" / "n.json")
            self.assertEqual(rw.path("recon", "n"), Path(d) / "work" / "recon~rung3" / "n.json")
            self.assertEqual(rw.path("prior_only", "n"), Path(d) / "work" / "prior_only" / "n.json")
            for k in ALL_STAGE_KEYS:
                w.put(k, "n", {"status": "ok"})
                self.assertEqual(w.names(k), ["n"])
                self.assertEqual(w.get(k, "n"), {"status": "ok"})
            self.assertEqual(sorted(p.name for p in (Path(d) / "work").iterdir() if ":" in p.name), [])


def _log_with_real_run_start(path: Path) -> AuditLog:
    log = AuditLog(path, "r")
    log.append("run_start", {"corpus_kind": "real"})
    return log


class TestDirnamesRead(unittest.TestCase):
    """The places that read the work folders back turn a folder name into the stage key again."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ss-win-")
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name)
        (self.out / "logs").mkdir()

    def test_work_integrity_reads_windows_folders(self):
        with windows():
            log = _log_with_real_run_start(self.out / "logs" / "run.jsonl")
            w = Work(self.out, log=log)
            w.put("recon:rung1", "p1", {"status": "ok", "map": None})
            w.put("recon", "p1", {"status": "ok", "map": None})
            log.close()
            self.assertTrue((self.out / "work" / "recon~rung1" / "p1.json").is_file())
            self.assertIsNone(audit_verify.work_integrity_problem(self.out / "logs" / "run.jsonl", self.out / "work"))
            # an item changed after it was logged is still caught, and named by its stage key
            (self.out / "work" / "recon~rung1" / "p1.json").write_text('{"status": "ok", "map": 1}\n', encoding="utf-8")
            why = audit_verify.work_integrity_problem(self.out / "logs" / "run.jsonl", self.out / "work")
            self.assertIn("recon:rung1/p1", why or "")
            # a logged item that vanished is caught too
            (self.out / "work" / "recon~rung1" / "p1.json").unlink()
            why = audit_verify.work_integrity_problem(self.out / "logs" / "run.jsonl", self.out / "work")
            self.assertIn("recon:rung1/p1", why or "")

    def test_prereg_work_problem_reads_windows_folders(self):
        calls = {"r1": ("recon:full:rung1", "p1__x@rung1"), "r2": ("recon:full", "p1__x")}
        with windows():
            w = Work(self.out)
            w.put("recon:rung1", "p1__x", {"status": "ok", "request_ids": ["r1"]})
            w.put("recon", "p1__x", {"status": "ok", "request_ids": ["r2"]})
            self.assertTrue((self.out / "work" / "recon~rung1" / "p1__x.json").is_file())
            self.assertIsNone(prereg._work_problem(self.out / "work", calls))
            # a ladder item naming the main run's call is refused, and named by its stage key
            w.put("recon:rung1", "p1__x", {"status": "ok", "request_ids": ["r2"]})
            why = prereg._work_problem(self.out / "work", calls)
            self.assertIn("recon:rung1/p1__x", why or "")

    def test_audits_find_the_ladder_stores(self):
        with windows():
            w = Work(self.out)
            for k in ("recon", "recon:rung1", "recon:placebo", "judge_modality:rung1"):
                w.put(k, "p1__x", {"status": "ok"})
            self.assertEqual(audits._ladder_stores(self.out / "work", "recon"), ["recon:placebo", "recon:rung1"])
            self.assertEqual(audits._stage_names(self.out / "work", "recon:rung1"), ["p1__x"])

    def test_outcomes_and_outage_counts_fold_rung_stores(self):
        with windows():
            w = Work(self.out)
            w.put("judge_modality:rung1", "a", {"status": "timeout_excluded"})
            w.put("judge_modality", "a", {"status": "ok"})
            o = jcounts.outcomes(self.out / "work")
            self.assertEqual(o["judge_modality"]["items"], 2)
            r = types.SimpleNamespace(work=w)
            self.assertEqual(pipeline._outage_counts(r)["by_stage"], {"judge_modality:rung1": 1})

    def test_quarantine_takes_back_rung_items_of_the_stage(self):
        with windows():
            log = _log_with_real_run_start(self.out / "logs" / "run.jsonl")
            w = Work(self.out, log=log)
            w.put("recon:rung1", "p1", {"status": "ok"})
            w.put("judge_modality:rung1", "p1", {"status": "ok"})
            ctx = types.SimpleNamespace(cfg=types.SimpleNamespace(out_dir=str(self.out)), session_start_ns=0,
                                        session_id="s", log=log)
            self.assertEqual(audits.quarantine_session(ctx, only=("recon",)), 1)
            self.assertEqual(w.read_any("recon:rung1", "p1")["status"], "quarantined")
            self.assertEqual(w.read_any("judge_modality:rung1", "p1")["status"], "ok")
            log.close()
            # the log announced the item under its stage key, so the integrity check still agrees
            self.assertIsNone(audit_verify.work_integrity_problem(self.out / "logs" / "run.jsonl", self.out / "work"))

    def test_fork_reads_stage_keys_back(self):
        from second_scribe import fork
        with windows():
            w = Work(self.out)
            w.put("recon", "p1__x", {"status": "ok"})
            w.put("recon:rung1", "p1__x", {"status": "ok"})
            stores = fork._source_items(self.out / "work")
        self.assertEqual(sorted(stores), ["recon", "recon:rung1"])
        self.assertEqual(stores["recon:rung1"], ["p1__x"])


def _demo_cfg(out: Path) -> RunConfig:
    d = json.loads(DEMO_CONFIG.read_text(encoding="utf-8"))
    d["base_dir"] = str(DEMO_CONFIG.parent)
    d["out_dir"] = str(out)
    d["ladder"] = {"rungs": list(RUNG_KEYS), "draws": 1, "n_perm": 50, "B": 50, "seed": 0}
    d["stats"] = {**d["stats"], "B": 50, "n_perm": 50}
    return RunConfig(**d)


def _run_demo(out: Path):
    cfg = _demo_cfg(out)
    (out / "logs").mkdir(parents=True)
    log_path = out / "logs" / "run.jsonl"
    log = AuditLog(log_path, cfg.run_id)
    try:
        run(cfg, list(STAGE_ORDER), build_clients("oracle", cfg), log=log)
    finally:
        log.close()
    return cfg, log_path


def _json_tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(root.rglob("*.json"))} if root.exists() else {}


def _scrub(x, *outs):
    s = json.dumps(x, sort_keys=True)
    for o in outs:
        s = s.replace(str(o), "<OUT>")
    return json.loads(s)


class TestWindowsNamesEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="ss-win-")
        cls.addClassCleanup(cls._tmp.cleanup)
        root = Path(cls._tmp.name)
        cls.posix_out, cls.win_out = root / "posix" / "out", root / "win" / "out"
        with windows(False):
            cls.posix_cfg, cls.posix_log = _run_demo(cls.posix_out)
        with windows(True):
            cls.win_cfg, cls.win_log = _run_demo(cls.win_out)

    def test_no_work_path_contains_a_colon(self):
        work = self.win_out / "work"
        folders = sorted(p.name for p in work.iterdir() if p.is_dir())
        self.assertIn("recon~rung1", folders)
        self.assertIn("judge_modality~rung3", folders)
        self.assertIn("align~placebo", folders)
        for p in work.rglob("*"):
            self.assertNotIn(":", p.relative_to(work).as_posix(), p)

    def test_posix_run_keeps_the_colon_names(self):
        folders = sorted(p.name for p in (self.posix_out / "work").iterdir() if p.is_dir())
        self.assertIn("recon:rung1", folders)
        self.assertEqual([f for f in folders if "~" in f], [])

    def test_the_windows_log_verifies_and_matches(self):
        self.assertEqual(audit_verify.main([str(self.win_log)]), 0)
        self.assertEqual(audit_verify.main([str(self.posix_log)]), 0)
        # the same items are announced under the same stage keys, whatever the folder is called
        def announced(log):
            recs = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
            return sorted((r["payload"]["stage"], r["payload"]["name"], r["payload"]["sha256"])
                          for r in recs if r["record_type"] == audit_verify.WORK_ITEM)
        a, b = announced(self.win_log), announced(self.posix_log)
        self.assertTrue(any(s.startswith("recon:") for s, _n, _h in a))
        self.assertEqual([x[:2] for x in a], [x[:2] for x in b])

    def test_work_integrity_agrees_in_both_modes(self):
        """The folder-reading check, run as if the log recorded a real corpus, passes on both stores."""
        for out, log_path, flag in ((self.posix_out, self.posix_log, False), (self.win_out, self.win_log, True)):
            recs = [json.loads(x) for x in log_path.read_text(encoding="utf-8").splitlines()]
            with windows(flag):
                real = out / "logs" / "real.jsonl"
                relog = AuditLog(real, "r")
                relog.append("run_start", {"corpus_kind": "real"})
                for r in recs:
                    if r["record_type"] == audit_verify.WORK_ITEM:
                        relog.append(audit_verify.WORK_ITEM, r["payload"])
                relog.close()
                self.assertIsNone(audit_verify.work_integrity_problem(real, out / "work"), flag)

    def test_aggregates_and_audits_equal_the_posix_run(self):
        pa = _scrub(_json_tree(self.posix_out / "aggregates"), self.posix_out)
        wa = _scrub(_json_tree(self.win_out / "aggregates"), self.win_out)
        self.assertTrue(pa)
        self.assertEqual(wa, pa)
        pu = _scrub(_json_tree(self.posix_out / "audits"), self.posix_out)
        wu = _scrub(_json_tree(self.win_out / "audits"), self.win_out)
        self.assertTrue(pu)
        self.assertEqual(wu, pu)

    def test_work_items_are_the_same_set(self):
        def items(out, flag):
            with windows(flag):
                return sorted((paths.stage_from_dirname(p.parent.name), p.stem)
                              for p in (out / "work").rglob("*.json") if p.parent != out / "work")
        self.assertEqual(items(self.win_out, True), items(self.posix_out, False))


if __name__ == "__main__":
    unittest.main()
