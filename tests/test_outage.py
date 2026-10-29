"""A2 outage: a model that could not be reached (connection refused, timeout, HTTP 5xx after the retries) leaves
items marked ``timeout_excluded``, which a rerun redoes; a model that answered with something unusable (parse or
validation failure) leaves a final ``failed`` / not-judged item. Synthetic corpus and fake clients only."""
import contextlib
import io
import json
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from second_scribe import cli as cli_module
from second_scribe.audit.registry import AuditLog
from second_scribe.cli import DEMO_CONFIG, main as cli_main
from second_scribe.llm import (HTTPClient, MockClient, ModelConfig, ModelResponse, TransportFailure, is_transport_error,
                               log_call)


def main(argv):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return cli_main(argv)


def records(log):
    return [json.loads(l) for l in Path(log).read_text().splitlines()]


class Flaky:
    """Wraps the oracle: ``fail(messages)`` returns an exception to raise, a text to answer with, or None (pass)."""
    model_label = "oracle"

    def __init__(self, inner, fail=None):
        self.inner, self.fail, self.calls = inner, fail, 0

    def complete(self, messages, **kw):
        self.calls += 1
        user = next(m["content"] for m in messages if m["role"] == "user")
        what = self.fail(user) if self.fail else None
        if isinstance(what, BaseException):
            raise what
        if isinstance(what, str):
            return ModelResponse(what, "stop", {}, 0, "x")
        return self.inner.complete(messages, **kw)


def is_recon(user):
    return '"nodes"' in user and "TRANSCRIPT:\n" in user


def is_modality(user):
    return '{"labels"' in user


def is_align(user):
    return "LIST A:" in user


class _Status(BaseHTTPRequestHandler):
    code = 503

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        raw = b'{"error": "x"}'
        self.send_response(self.code)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


class TestTransportErrorsAreTyped(unittest.TestCase):
    def _client(self, port, code=None):
        cfg = ModelConfig(name="recon", endpoint=f"http://127.0.0.1:{port}/v1", model_id="m", timeout_s=2)
        return HTTPClient(cfg, attempts=2, sleep=lambda s: None)

    def _server(self, code):
        h = type("H", (_Status,), {"code": code})
        srv = HTTPServer(("127.0.0.1", 0), h)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_port

    def test_connection_refused_is_a_transport_failure(self):
        srv = HTTPServer(("127.0.0.1", 0), _Status)
        port = srv.server_port
        srv.server_close()
        with self.assertRaises(TransportFailure):
            self._client(port).complete([{"role": "user", "content": "q"}], temperature=0, seed=1, max_tokens=5)

    def test_5xx_after_the_retries_is_a_transport_failure_but_4xx_is_not(self):
        with self.assertRaises(TransportFailure):
            self._client(self._server(503)).complete([{"role": "user", "content": "q"}], temperature=0, seed=1,
                                                     max_tokens=5)
        with self.assertRaises(RuntimeError) as cm:
            self._client(self._server(400)).complete([{"role": "user", "content": "q"}], temperature=0, seed=1,
                                                     max_tokens=5)
        self.assertNotIsInstance(cm.exception, TransportFailure)

    def test_a_reply_cut_off_mid_body_is_a_transport_failure(self):
        """Review M-1: a server killed while writing its reply (Content-Length 1000, 11 bytes sent, closed) is an
        outage: HTTPClient retries it and ends in TransportFailure, never a final RuntimeError."""
        class Cut(BaseHTTPRequestHandler):
            hits = 0

            def do_POST(self):
                type(self).hits += 1
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(200)
                self.send_header("Content-Length", "1000")
                self.end_headers()
                self.wfile.write(b"{\"choices\":")
                self.wfile.flush()
                self.close_connection = True

            def log_message(self, *a):
                pass
        srv = HTTPServer(("127.0.0.1", 0), Cut)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        with self.assertRaises(TransportFailure):
            self._client(srv.server_port).complete([{"role": "user", "content": "q"}], temperature=0, seed=1,
                                                   max_tokens=5)
        self.assertEqual(Cut.hits, 2)                       # retried (attempts=2), not given up on at once

    def test_incomplete_read_counts_as_a_transport_error(self):
        import http.client
        self.assertTrue(is_transport_error(http.client.IncompleteRead(b"abc", 10)))
        self.assertTrue(is_transport_error(http.client.BadStatusLine("x")))

    def _log(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return AuditLog(Path(d.name) / "run.jsonl", "r")

    def test_log_call_marks_transport_errors_only(self):
        for exc, flag in ((ConnectionError("refused"), True), (TimeoutError("slow"), True),
                          (TransportFailure("503 after retries"), True), (urllib.error.URLError("dns"), True),
                          (ValueError("bug"), False), (RuntimeError("HTTP 400"), False)):
            log = self._log()

            def boom(messages, exc=exc):
                raise exc
            text, recs = log_call(log, MockClient(fn=boom), [{"role": "user", "content": "q"}], stage="s",
                                  item_id="i", temperature=0, seed=1, max_tokens=5, max_attempts=2)
            self.assertIsNone(text)
            errs = [r["payload"] for r in recs if r["record_type"] == "call_error"]
            self.assertEqual(len(errs), 2)
            self.assertEqual([bool(e.get("transport")) for e in errs], [flag, flag], repr(exc))


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name)
        d = json.loads(Path(DEMO_CONFIG).read_text())
        d.update(out_dir=str(self.out / "run"), base_dir=str(Path(DEMO_CONFIG).parent))
        self.cfg = self.out / "config.json"
        self.cfg.write_text(json.dumps(d))
        self.work = self.out / "run" / "work"
        self.log = self.out / "run" / "logs" / "run.jsonl"
        self.err = io.StringIO()

    def run_stages(self, stages, recon_fail=None, judge_fail=None):
        real = cli_module.build_clients

        def build(kind, cfg, fixtures=None, **_):
            c = real("oracle", cfg, fixtures)
            return {**c, "recon": Flaky(c["recon"], recon_fail), "judge": Flaky(c["judge"], judge_fail)}
        with mock.patch.object(cli_module, "build_clients", build):
            return main(["run", "--config", str(self.cfg), "--stages", stages, "--client", "oracle"])

    def statuses(self, stage):
        out = {}
        for f in (self.work / stage).glob("*.json"):
            s = json.loads(f.read_text())["status"]
            out[s] = out.get(s, 0) + 1
        return out

    def calls_after(self, n):
        return [r["payload"]["stage"] for r in records(self.log)[n:] if r["record_type"] == "call_request"]


class TestReconOutage(Base):
    def test_an_unreachable_model_leaves_timeout_excluded_that_a_rerun_redoes(self):
        self.assertEqual(self.run_stages("Q", recon_fail=lambda u: ConnectionError("refused") if is_recon(u) else None), 0)
        st = self.statuses("recon")
        self.assertEqual(set(st), {"timeout_excluded"}, st)
        n = len(records(self.log))
        self.assertEqual(self.run_stages("Q"), 0)               # the model is back
        self.assertEqual(self.statuses("recon"), {"ok": sum(st.values())})
        self.assertTrue(any(s.startswith("recon:") for s in self.calls_after(n)))

    def test_a_timeout_is_the_same_as_connection_refused(self):
        self.run_stages("Q", recon_fail=lambda u: TimeoutError("slow") if is_recon(u) else None)
        self.assertEqual(set(self.statuses("recon")), {"timeout_excluded"})

    def test_an_unusable_answer_stays_final_and_is_not_redone(self):
        self.run_stages("Q", recon_fail=lambda u: "this is not JSON" if is_recon(u) else None)
        st = self.statuses("recon")
        self.assertEqual(set(st), {"failed"}, st)
        n = len(records(self.log))
        self.run_stages("Q")
        self.assertEqual(self.statuses("recon"), st)
        self.assertFalse([s for s in self.calls_after(n) if s.startswith("recon:")])

    def test_a_bug_exception_is_not_an_outage(self):
        self.run_stages("Q", recon_fail=lambda u: ValueError("bug") if is_recon(u) else None)
        self.assertEqual(set(self.statuses("recon")), {"failed"})

    def test_an_answer_anywhere_in_the_call_makes_the_failure_final(self):
        """Review M-6: attempt 1 answers garbage (the model's own invalid answer), attempts 2 and 3 hit a dead line.
        The model did answer, so the item stays failed; redoing it would give it 3 fresh attempts (6 in all) and
        lower the parse-failure rate the ladder reports."""
        n = []

        def garbage_then_drop(u):          # per item: attempt 1 answers garbage, attempts 2 and 3 hit a dead line
            if not is_recon(u):
                return None
            n.append(1)
            return "not json" if len(n) % 3 == 1 else ConnectionError("drop")
        self.run_stages("Q", recon_fail=garbage_then_drop)
        st = self.statuses("recon")
        self.assertEqual(set(st), {"failed"}, st)
        calls = len(records(self.log))
        self.run_stages("Q")                                       # a rerun does not redo them
        self.assertEqual(self.statuses("recon"), st)
        self.assertFalse([s for s in self.calls_after(calls) if s.startswith("recon:")])

    def test_an_answer_after_a_dead_line_that_is_unusable_is_final(self):
        n = []

        def drop_then_garbage(u):
            if not is_recon(u):
                return None
            n.append(1)
            return ConnectionError("drop") if len(n) % 3 == 1 else "not json"
        self.run_stages("Q", recon_fail=drop_then_garbage)
        self.assertEqual(set(self.statuses("recon")), {"failed"})


class TestJudgeAndHelperOutage(Base):
    def test_a_judge_that_cannot_be_reached_leaves_timeout_excluded_items_that_rerun(self):
        self.assertEqual(self.run_stages("Q"), 0)
        self.assertEqual(self.run_stages("judge_modality",
                                         judge_fail=lambda u: ConnectionError("x") if is_modality(u) else None), 0)
        st = self.statuses("judge_modality")
        self.assertEqual(set(st), {"timeout_excluded"}, st)
        self.assertEqual(self.run_stages("judge_modality"), 0)
        self.assertEqual(self.statuses("judge_modality"), {"ok": sum(st.values())})

    def test_a_judge_that_answers_garbage_leaves_not_judged_and_final(self):
        self.assertEqual(self.run_stages("Q"), 0)
        self.run_stages("judge_modality", judge_fail=lambda u: "garbage" if is_modality(u) else None)
        st = self.statuses("judge_modality")
        self.assertEqual(set(st), {"ok"}, st)
        n = len(records(self.log))
        self.run_stages("judge_modality")
        self.assertFalse([s for s in self.calls_after(n) if s == "judge:modality"])

    def test_alignments_that_time_out_are_timeout_excluded_not_fallback(self):
        self.assertEqual(self.run_stages("Q,judge_modality,canonicalize"), 0)
        self.run_stages("align", judge_fail=lambda u: TimeoutError("slow") if is_align(u) else None)
        st = self.statuses("align")
        self.assertEqual(set(st), {"timeout_excluded"}, st)
        self.run_stages("align")
        self.assertEqual(set(self.statuses("align")), {"ok"})

    def test_alignments_with_garbage_answers_are_fallback_as_before(self):
        self.assertEqual(self.run_stages("Q,judge_modality,canonicalize"), 0)
        self.run_stages("align", judge_fail=lambda u: "garbage" if is_align(u) else None)
        self.assertEqual(set(self.statuses("align")), {"fallback"})


class TestAggregatesShowTheOutage(Base):
    def test_outage_block_counts_the_items_and_is_zero_for_a_clean_run(self):
        self.assertEqual(self.run_stages("all"), 0)
        clean = json.loads((self.out / "run" / "aggregates" / "aggregates.json").read_text())
        self.assertEqual(clean["outage"]["timeout_excluded_items"], 0)

    def test_outage_block_after_an_outage(self):
        self.run_stages("all", recon_fail=lambda u: ConnectionError("x") if is_recon(u) else None)
        agg = json.loads((self.out / "run" / "aggregates" / "aggregates.json").read_text())
        self.assertGreater(agg["outage"]["timeout_excluded_items"], 0)
        self.assertIn("recon", agg["outage"]["by_stage"])


class TestIncompleteRunIsSaidInTheHeadlineAndTheReport(Base):
    """Review M-2 / M-5: a run that still has timeout_excluded items is not a finished run: the headline says so."""

    def partial_outage(self):
        seen = []

        def drop_first_three(u):                  # one reconstruction (3 attempts) never reaches the model
            if is_recon(u) and len(seen) < 3:
                seen.append(1)
                return ConnectionError("drop")
            return None
        self.assertEqual(self.run_stages("all", recon_fail=drop_first_three), 0)
        return json.loads((self.out / "run" / "aggregates" / "aggregates.json").read_text())

    def test_the_headline_is_incomplete_run_and_the_report_says_so(self):
        agg = self.partial_outage()
        self.assertEqual(agg["outage"]["timeout_excluded_items"], 1)
        self.assertEqual(agg["headline"]["interpretation"], "incomplete_run")
        for blk in agg["other_arms"].values():
            self.assertEqual(blk["interpretation"], "incomplete_run")
        text = next((self.out / "run").rglob("report.md")).read_text()
        self.assertIn("run incomplete", text)
        self.assertIn("timeout_excluded", text)

    def test_a_clean_rerun_makes_it_interpreted_again(self):
        self.partial_outage()
        self.assertEqual(self.run_stages("all"), 0)             # the model is back: only the missing item is redone
        agg = json.loads((self.out / "run" / "aggregates" / "aggregates.json").read_text())
        self.assertEqual(agg["outage"]["timeout_excluded_items"], 0)
        self.assertEqual(agg["headline"]["interpretation"], "interpreted")

    def test_a_total_outage_still_says_incomplete_in_the_report(self):
        self.run_stages("all", recon_fail=lambda u: ConnectionError("x") if is_recon(u) else None)
        text = next((self.out / "run").rglob("report.md")).read_text()
        self.assertIn("run incomplete", text)


class TestCliWarns(Base):
    def test_the_run_command_warns_on_stderr_when_items_are_timeout_excluded(self):
        real = cli_module.build_clients

        def build(kind, cfg, fixtures=None, **_):
            c = real("oracle", cfg, fixtures)
            return {**c, "recon": Flaky(c["recon"], lambda u: ConnectionError("x") if is_recon(u) else None)}
        err = io.StringIO()
        with mock.patch.object(cli_module, "build_clients", build), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            cli_main(["run", "--config", str(self.cfg), "--stages", "Q", "--client", "oracle"])
        self.assertIn("timeout_excluded", err.getvalue())
        self.assertIn("rerun", err.getvalue())


if __name__ == "__main__":
    unittest.main()
