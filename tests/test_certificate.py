"""T41: the run certificate, built from the logs alone. No model is called: runs use the oracle client."""
import contextlib
import io
import json
import re
import shutil
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

from second_scribe import certificate as C
from second_scribe.audit.applog import AppLog
from second_scribe.audit.registry import AuditLog
from second_scribe.cli import build_clients, main as cli_main
from second_scribe.pipeline import RunConfig, run

ROOT = Path(__file__).resolve().parent.parent
DEMO_CFG = ROOT / "configs" / "demo.json"
DEMO_HASH = "ccc13208b1fe5e66b5f4bef970af228cb92af9aefca6f21d341a0ccaa18c1ba9"
HEX_A, HEX_B, HEX_C = "a" * 64, "b" * 64, "c" * 64
FIXED = datetime(2026, 10, 19, 9, 30, 0, tzinfo=timezone.utc)


def _now():
    return FIXED


def make_run(out: Path, *, tool=None, label="oracle", stages=("mapping_talk_confirm",), write_config=True,
             full=False) -> RunConfig:
    """Run the demo (oracle client, no model) into ``out``; ``tool`` makes it a tool run with a config.json."""
    d = json.loads(DEMO_CFG.read_text(encoding="utf-8"))
    d["base_dir"] = str(DEMO_CFG.parent)
    d["out_dir"] = str(out)
    if tool:
        d["tool"] = tool
    out.mkdir(parents=True, exist_ok=True)
    cfg = RunConfig(**d)
    clients = build_clients("oracle", cfg)
    next(iter(clients.values())).model_label = label           # one shared oracle: every role shows this label
    (out / "logs").mkdir(exist_ok=True)
    log = AuditLog(out / "logs" / "run.jsonl", cfg.run_id)
    try:
        run(cfg, list(stages), clients, log=log)
    finally:
        log.close()
    if write_config:
        (out / "config.json").write_text(json.dumps(d), encoding="utf-8")
    return cfg


def words(text):
    return re.findall(r"[a-z0-9']+", text.lower())


class CertBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="ss-tests-cert-")
        cls.root = Path(cls.tmp.name)
        # A: the full demo, no tool block, no config.json
        cls.a = cls.root / "A-full"
        make_run(cls.a, stages=("Q", "J", "M"), write_config=False)
        # B: a tool run with digests, a rung-1 prompt, a dated remote model id
        from second_scribe import toolcfg
        cls.tool_b = {"model_digests": {"recon": HEX_A, "judge": HEX_B}, "reference_type": "A",
                      "judge_rule": "study", "rung1_user": toolcfg.rung1_user_block("Draw the journey as steps."),
                      "remote_endpoints": [{"role": "recon", "host": "api.anthropic.com", "provider": "anthropic",
                                            "declaration_sha256": HEX_C}],
                      "endpoint_optins": [{"role": "judge", "host": "gpu-box.local"}]}
        cls.b = cls.root / "B-tool"
        make_run(cls.b, tool=cls.tool_b, label="claude-sonnet-4-5-20250929")
        # C: the same, with an alias id
        cls.c = cls.root / "C-alias"
        make_run(cls.c, tool={"reference_type": "C", "remote_endpoints": cls.tool_b["remote_endpoints"]},
                 label="claude-sonnet-4-5")
        # D: a run that started and never ended (run_start only)
        cls.d = cls.root / "D-cut"
        (cls.d / "logs").mkdir(parents=True)
        lg = AuditLog(cls.d / "logs" / "run.jsonl", "cut-run")
        lg.append("run_start", {"config_sha256": HEX_A, "prompt_set_sha256": HEX_B, "code_sha256": HEX_C,
                                "code_commit": None, "models": {"recon": "m1"}, "stages": ["recon"],
                                "corpus_kind": "real"})
        lg.close()
        # E: a folder with no log at all
        cls.e = cls.root / "E-nolog"
        cls.e.mkdir()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def copy_run(self, src: Path, name: str) -> Path:
        dst = Path(tempfile.mkdtemp(prefix="ss-tests-cert-copy-")) / name
        shutil.copytree(src, dst)
        self.addCleanup(shutil.rmtree, dst.parent, ignore_errors=True)
        return dst

    def cert(self, dirs, app_log=None, **kw):
        return C.build_certificate([Path(d) for d in dirs], app_log, now=_now, **kw)


class TestRuns(CertBase):
    def test_demo_config_hash_unchanged(self):
        self.assertEqual(RunConfig.load(DEMO_CFG).sha256(), DEMO_HASH)

    def test_per_run_fields_from_the_log(self):
        cert = self.cert([self.a])
        r = cert["runs"][0]
        self.assertEqual(cert["schema"], "certificate/1")
        self.assertEqual(r["run_id"], "demo")
        self.assertEqual(r["outcome"], "complete")
        self.assertEqual(r["config_sha256"], DEMO_HASH)
        for k in ("code_sha256", "prompt_set_sha256"):
            self.assertRegex(r[k], r"^[0-9a-f]{64}$")
        self.assertIn("code_commit", r)
        self.assertEqual(r["models"]["recon"]["id"], "oracle")
        self.assertEqual(r["verify"]["records"], r["verify"]["records"])
        self.assertRegex(r["verify"]["head_hash"], r"^[0-9a-f]{64}$")
        self.assertNotIn("rung1_user_prompt_sha256", r)            # a study config carries no tool fields
        self.assertNotIn("reading3_prompt_sha256", r)

    def test_tool_run_carries_prompt_hashes(self):
        r = self.cert([self.b])["runs"][0]
        self.assertEqual(r["rung1_user_prompt_sha256"], self.tool_b["rung1_user"]["text_sha256"])
        self.assertRegex(r["reading3_prompt_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(r["reference_type"], "A")
        self.assertEqual(r["optins"], [{"role": "judge", "host": "gpu-box.local"}])

    def test_lists_failed_runs(self):
        cert = self.cert([self.a, self.d, self.e])
        by_dir = {r["dir"]: r for r in cert["runs"]}
        self.assertEqual(list(by_dir), ["A-full", "D-cut", "E-nolog"])          # every run, in the order given
        self.assertEqual(by_dir["D-cut"]["run_id"], "cut-run")
        self.assertEqual(by_dir["D-cut"]["outcome"], "incomplete")
        self.assertTrue(by_dir["D-cut"]["failed"])
        self.assertEqual(by_dir["E-nolog"]["outcome"], "no_log")
        self.assertTrue(by_dir["E-nolog"]["failed"])
        self.assertFalse(by_dir["A-full"]["failed"])
        md = C.render_certificate_md(cert)
        for name in ("A-full", "D-cut", "E-nolog", "cut-run"):
            self.assertIn(name, md)
        self.assertIn("this run did not finish cleanly", md)
        self.assertFalse(cert["all_logs_verify"])

    def test_verify_fresh_and_dated(self):
        cert = self.cert([self.a])
        v = cert["runs"][0]["verify"]
        self.assertTrue(v["ok"], v)
        self.assertEqual(v["checked_at"], "2026-10-19T09:30:00+00:00")
        self.assertEqual(cert["checked_at"], v["checked_at"])
        self.assertIn("2026-10-19", cert["limit"])
        # fresh: the verdict follows the log as it is now, not an earlier result
        cp = self.copy_run(self.a, "A-copy")
        self.assertTrue(self.cert([cp])["runs"][0]["verify"]["ok"])
        with open(cp / "logs" / "run.jsonl", "a", encoding="utf-8") as fh:
            fh.write("{broken\n")
        self.assertFalse(self.cert([cp])["runs"][0]["verify"]["ok"])

    def test_tampered_log_shows_fail(self):
        cp = self.copy_run(self.a, "A-tampered")
        p = cp / "logs" / "run.jsonl"
        lines = p.read_text(encoding="utf-8").splitlines()
        i = next(n for n, l in enumerate(lines) if '"stage_end"' in l)
        rec = json.loads(lines[i])
        rec["payload"]["new_items"] = rec["payload"]["new_items"] + 1
        lines[i] = json.dumps(rec)
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cert = self.cert([cp])
        r = cert["runs"][0]
        self.assertFalse(r["verify"]["ok"])
        self.assertEqual(r["outcome"], "chain_fail")
        self.assertIn("hash_mismatch", r["verify"]["errors"])
        md = C.render_certificate_md(cert)
        self.assertIn("FAIL (hash_mismatch", md)
        self.assertNotIn("PASS", md)
        self.assertFalse(cert["all_logs_verify"])

    def test_truncated_log_shows_fail(self):
        cp = self.copy_run(self.a, "A-cut")
        p = cp / "logs" / "run.jsonl"
        lines = p.read_text(encoding="utf-8").splitlines()
        p.write_text("\n".join(lines[:-3]) + "\n", encoding="utf-8")
        self.assertFalse(self.cert([cp])["runs"][0]["verify"]["ok"])


class TestLimits(CertBase):
    LAB = ("The chain can reveal edits when checked against a trusted head; it does not show what the input was, "
           "nor that the models' answers are right. Full verification needs the logs, which hold participant text, "
           "so it was run on this machine on 2026-10-19.")

    def test_limit_text_exact(self):
        cert = self.cert([self.a])
        self.assertEqual(cert["limit"], self.LAB)
        self.assertTrue(C.render_certificate_md(cert).rstrip().endswith(self.LAB))
        study = self.cert([self.a], study=True)
        self.assertEqual(study["limit"], self.LAB.replace("on this machine", "by the authors"))
        self.assertIn("so it was run by the authors on 2026-10-19.", C.render_certificate_md(study))

    def test_attests_integrity_not_correctness(self):
        md = C.render_certificate_md(self.cert([self.a]))
        self.assertIn("This document attests the integrity of the logs and what was run. "
                      "It does not attest that the results are right.", md)
        self.assertTrue(md.startswith("# Run certificate\n"))

    def test_wording_never_list(self):
        from tests.test_wording import find_hits
        for dirs in ([self.a], [self.a, self.b, self.c, self.d, self.e]):
            cert = self.cert(dirs, study=True)
            for text in (C.render_certificate_md(cert), json.dumps(cert)):
                hits = [h for h in find_hits(text.replace("Run certificate", "Run record")
                                             .replace("This document", "This text")) if h != "certificate"]
                self.assertEqual(hits, [])
                for term in ("first", "validated", "ground truth", "replicable"):
                    self.assertNotRegex(text.lower(), rf"\b{term}\b")
        # "certificate" appears only as the name of the document
        md = C.render_certificate_md(self.cert([self.a]))
        self.assertEqual(len(re.findall("certificate", md, re.I)), 1)


class TestAppLog(CertBase):
    def app_log(self, *, coding=False, with_optin=True) -> Path:
        d = Path(tempfile.mkdtemp(prefix="ss-tests-cert-app-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        lg = AppLog(d / "logs" / "app.jsonl", "proj")
        lg.append("project_created", {"name": "x"})
        if with_optin:
            lg.append("optin", {"role": "judge", "host": "gpu-box.local"})
            lg.append("declaration", declaration_payload())        # the record as app/models.py logs it
        if coding:
            lg.append("coding_locked", {"pid": "P-SECRET-01", "sha256": HEX_A, "n_marks": 3, "coding_seconds": 99})
            lg.append("coding_locked", {"pid": "P-SECRET-02", "sha256": HEX_B, "n_marks": 4, "coding_seconds": 77})
            lg.append("reveal", {})
        return d / "logs" / "app.jsonl"

    def test_blindness_limit_when_coding_locked(self):
        with_lock = self.cert([self.a], self.app_log(coding=True))
        self.assertEqual(with_lock["blindness_limit"], C.BLINDNESS_LIMIT)
        md = C.render_certificate_md(with_lock)
        self.assertIn(C.BLINDNESS_LIMIT, md)
        self.assertIn("blindness is enforced by this interface and recorded in the log order; it is not "
                      "cryptographic, so someone could open the run folder by hand.", md)
        self.assertIn(with_lock["limit"], md)                        # next to the chain limit
        self.assertEqual(with_lock["app_log"]["coding_locked"]["count"], 2)
        self.assertEqual(with_lock["app_log"]["reveal"]["count"], 1)
        self.assertTrue(with_lock["app_log"]["locks_before_reveal"])
        for none in (self.cert([self.a], self.app_log(coding=False)), self.cert([self.a], None)):
            self.assertIsNone(none["blindness_limit"])
            self.assertNotIn("blindness", C.render_certificate_md(none))

    def test_declarations_and_optins_listed_without_participant_ids(self):
        cert = self.cert([self.a], self.app_log(coding=True))
        app = cert["app_log"]
        self.assertTrue(app["verify"]["ok"], app["verify"])
        self.assertEqual(app["optins"], [{"role": "judge", "host": "gpu-box.local"}])
        self.assertEqual(app["declarations"], [{"role": "recon", "provider": "anthropic", "host": "api.anthropic.com",
                                                "sha256": declaration_payload()["sha256"], "basis": BASIS, "seq": 2,
                                                "text_matches": True}])
        blob = json.dumps(cert) + C.render_certificate_md(cert)
        self.assertNotIn("P-SECRET", blob)
        self.assertNotIn(HEX_A, blob)                                  # a lock's own hash stays out too

    def test_lock_after_reveal_is_visible(self):
        d = Path(tempfile.mkdtemp(prefix="ss-tests-cert-app-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        lg = AppLog(d / "app.jsonl", "proj")
        lg.append("reveal", {})
        lg.append("coding_locked", {"pid": "p", "sha256": HEX_A, "n_marks": 1, "coding_seconds": 1})
        cert = self.cert([self.a], d / "app.jsonl")
        self.assertFalse(cert["app_log"]["locks_before_reveal"])
        self.assertIn("Every lock precedes the reveal in the log: NO", C.render_certificate_md(cert))

    def test_tampered_app_log_shows_fail(self):
        p = self.app_log(coding=True)
        text = p.read_text(encoding="utf-8").replace("gpu-box.local", "gpu-box.example")
        p.write_text(text, encoding="utf-8")
        cert = self.cert([self.a], p)
        self.assertFalse(cert["app_log"]["verify"]["ok"])
        self.assertFalse(cert["all_logs_verify"])

    def test_missing_app_log_is_a_fail_line(self):
        cert = self.cert([self.a], Path(tempfile.gettempdir()) / "ss-tests-no-such-app.jsonl")
        self.assertEqual(cert["app_log"]["verify"]["errors"], ["log_missing"])


class TestDigestsAndRemote(CertBase):
    def test_digests_from_config_checked_against_hash(self):
        r = self.cert([self.b])["runs"][0]
        self.assertEqual(r["digest_check"]["status"], "match", r["digest_check"])
        self.assertEqual(r["models"]["recon"]["digest"], HEX_A)
        self.assertEqual(r["models"]["judge"]["digest"], HEX_B)
        # a config edited after the run: the digest no longer belongs to the hash the run logged
        cp = self.copy_run(self.b, "B-edited")
        cfg = json.loads((cp / "config.json").read_text(encoding="utf-8"))
        cfg["tool"]["model_digests"]["recon"] = "d" * 64
        (cp / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        cert = self.cert([cp])
        self.assertEqual(cert["runs"][0]["digest_check"]["status"], "mismatch")
        md = C.render_certificate_md(cert)
        self.assertRegex(md, r"Digest check: mismatch;.*not confirmed")
        self.assertIn("Digest check: match;", C.render_certificate_md(self.cert([self.b])))

    def test_missing_or_unreadable_config_is_unchecked(self):
        r = self.cert([self.a])["runs"][0]
        self.assertEqual(r["digest_check"]["status"], "unchecked")
        cp = self.copy_run(self.b, "B-badcfg")
        (cp / "config.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.cert([cp])["runs"][0]["digest_check"]["status"], "unchecked")

    def test_remote_lines_and_alias_warning(self):
        b = self.cert([self.b])["runs"][0]
        ep = b["remote"][0]
        self.assertEqual((ep["role"], ep["host"], ep["provider"], ep["declaration_sha256"], ep["model_id"]),
                         ("recon", "api.anthropic.com", "anthropic", HEX_C, "claude-sonnet-4-5-20250929"))
        self.assertFalse(ep["alias_warning"])
        md_b = C.render_certificate_md(self.cert([self.b]))
        self.assertIn("remote model: weights not inspectable; the provider may change the model behind this id", md_b)
        self.assertNotIn("alias", md_b)
        c = self.cert([self.c])["runs"][0]["remote"][0]
        self.assertTrue(c["alias_warning"])
        md_c = C.render_certificate_md(self.cert([self.c]))
        self.assertIn("remote model: weights not inspectable; the provider may change the model behind this id", md_c)
        self.assertIn("warning: model id claude-sonnet-4-5 has no date or version suffix", md_c)

    def test_alias_rule_table(self):
        for pinned in ("claude-sonnet-4-5-20250929", "gpt-4o-2024-08-06", "gpt-4-0613", "model@2", "m:v2"):
            self.assertFalse(C.is_alias_id(pinned), pinned)
        for alias in ("claude-sonnet-4-5", "gpt-4o", "gemini-pro-latest", "claude-opus-latest"):
            self.assertTrue(C.is_alias_id(alias), alias)

    def test_a_local_run_has_no_remote_line(self):
        self.assertNotIn("remote model", C.render_certificate_md(self.cert([self.a])))



BASIS = ("Approved by the research ethics board, protocol 2026-117; participants consented to transcript text going to "
         "a cloud model under the provider's data terms.")
ANTH = {"role": "recon", "host": "api.anthropic.com", "provider": "anthropic"}
SENTENCE = ("Transcript text was sent to Anthropic's API (api.anthropic.com) for reconstructor (also draws the "
            "no-interview map), under: " + BASIS)


def declaration_payload(basis=BASIS, **kw):
    """An app-log ``declaration`` payload as ``app/models.py`` logs it (schema, role, provider, host, basis, sha256)."""
    doc = {"schema": "declaration/1", **ANTH, **kw, "basis": basis}
    return {**doc, "sha256": C.declaration_record_sha256(doc)}


class TestDeclarations(CertBase):
    """Audit K4/S1 (the typed basis is stamped, one sentence per remote role) and K5/S4 (the config's
    declaration_sha256 must match an app-log ``declaration`` record written before the run started)."""

    def setup_case(self, *, before=None, after=None, sha=None, head=True):
        """An app log with ``before`` payloads, a tool run whose app_log_head is the log at that moment and whose
        remote endpoint carries ``sha`` (default: the first payload's), then ``after`` payloads."""
        d = Path(tempfile.mkdtemp(prefix="ss-tests-cert-decl-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        lg = AppLog(d / "app.jsonl", "proj")
        lg.append("project_created", {"name": "x"})
        for p in before or []:
            lg.append("declaration", p)
        n, h = lg.head()
        sha = sha or (before or after)[0]["sha256"]
        tool = {"reference_type": "A", "remote_endpoints": [{**ANTH, "declaration_sha256": sha}]}
        if head:
            tool["app_log_head"] = {"records": n, "head": h}
        make_run(d / "run", tool=tool, label="claude-sonnet-4-5-20250929")
        for p in after or []:
            lg.append("declaration", p)
        return d / "run", d / "app.jsonl"

    def ep(self, run, app):
        return self.cert([run], app)["runs"][0]["remote"][0]

    def test_record_hash_is_the_apps(self):
        from second_scribe.app import models
        doc = models.declaration_doc("recon", "anthropic", "api.anthropic.com", BASIS)
        self.assertEqual(C.declaration_record_sha256(doc), models.declaration_sha256(doc))
        self.assertEqual(C.declaration_record_sha256({**doc, "sha256": "x", "ticked": True}),
                         models.declaration_sha256(doc))                 # only the declaration's own keys count

    def test_matching_declaration_before_the_run(self):
        run, app = self.setup_case(before=[declaration_payload()])
        cert = self.cert([run], app)
        ep = cert["runs"][0]["remote"][0]
        self.assertEqual(ep["declaration"]["status"], "found_before", ep["declaration"])
        self.assertTrue(ep["declaration"]["ok"])
        self.assertEqual(ep["declaration"]["seq"], 1)
        self.assertEqual(ep["declaration"]["basis"], BASIS)
        self.assertEqual(ep["sentence"], SENTENCE)
        self.assertEqual(cert["runs"][0]["remote_sentences"], [SENTENCE])
        self.assertTrue(cert["all_declarations_ok"])
        md = C.render_certificate_md(cert)
        self.assertIn(SENTENCE, md)
        self.assertIn("declaration: found, before the run (app log record 1)", md)
        self.assertEqual(cert["app_log"]["declarations"][0]["basis"], BASIS)

    def test_missing_declaration(self):
        run, app = self.setup_case(before=[declaration_payload()], sha=HEX_C)
        cert = self.cert([run], app)
        ep = cert["runs"][0]["remote"][0]
        self.assertEqual(ep["declaration"]["status"], "not_found")
        self.assertFalse(ep["declaration"]["ok"])
        self.assertFalse(cert["all_declarations_ok"])
        md = C.render_certificate_md(cert)
        self.assertIn("declaration: NOT FOUND in the app log", md)
        self.assertNotIn("under: ", md)
        self.assertIn("Transcript text was sent to Anthropic's API (api.anthropic.com) for reconstructor (also "
                      "draws the no-interview map); no declaration with this hash is in the app log.", md)

    def test_declaration_written_after_the_run_start(self):
        run, app = self.setup_case(after=[declaration_payload()])
        ep = self.ep(run, app)
        self.assertEqual(ep["declaration"]["status"], "found_after")
        self.assertFalse(ep["declaration"]["ok"])
        self.assertIn("declaration: FLAG, app log record 1 was written after the run started",
                      C.render_certificate_md(self.cert([run], app)))

    def test_hash_of_different_text(self):
        # a record that names the config's hash but whose own text hashes to something else
        good = declaration_payload()
        forged = {**declaration_payload(basis="Some other basis typed later."), "sha256": good["sha256"]}
        run, app = self.setup_case(before=[forged], sha=good["sha256"])
        ep = self.ep(run, app)
        self.assertEqual(ep["declaration"]["status"], "text_mismatch")
        self.assertFalse(ep["declaration"]["ok"])
        self.assertNotIn("Some other basis", ep["sentence"])
        self.assertIn("does not hash to", C.render_certificate_md(self.cert([run], app)))
        # a genuine record for different text (its own hash is right) does not stand in for the config's hash
        other = declaration_payload(basis="Some other basis typed later.")
        run, app = self.setup_case(before=[other], sha=good["sha256"])
        self.assertEqual(self.ep(run, app)["declaration"]["status"], "not_found")

    def test_declaration_for_another_role_or_host_does_not_count(self):
        for kw in ({"role": "judge"}, {"host": "api.other.example"}, {"provider": "openai_compat"}):
            p = declaration_payload(**kw)
            run, app = self.setup_case(before=[p], sha=p["sha256"])
            self.assertEqual(self.ep(run, app)["declaration"]["status"], "endpoint_mismatch", kw)

    def test_run_without_app_log_position(self):
        run, app = self.setup_case(before=[declaration_payload()], head=False)
        ep = self.ep(run, app)
        self.assertEqual(ep["declaration"]["status"], "order_unchecked")
        self.assertFalse(ep["declaration"]["ok"])

    def test_app_log_that_is_not_the_runs(self):
        run, _ = self.setup_case(before=[declaration_payload()])
        d = Path(tempfile.mkdtemp(prefix="ss-tests-cert-decl-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        lg = AppLog(d / "app.jsonl", "other-project")
        lg.append("project_created", {"name": "y"})
        lg.append("declaration", declaration_payload())
        self.assertEqual(self.ep(run, d / "app.jsonl")["declaration"]["status"], "head_mismatch")

    def test_no_app_log_given(self):
        run, _ = self.setup_case(before=[declaration_payload()])
        cert = self.cert([run], None)
        ep = cert["runs"][0]["remote"][0]
        self.assertEqual(ep["declaration"]["status"], "unchecked")
        self.assertIsNone(ep["declaration"]["ok"])
        self.assertIsNone(cert["all_declarations_ok"])
        self.assertIn("declaration: not checked (no app log given)", C.render_certificate_md(cert))

    def test_basis_shown_on_one_line(self):
        p = declaration_payload(basis="Line one.\n## injected heading\tend")
        run, app = self.setup_case(before=[p])
        ep = self.ep(run, app)
        self.assertEqual(ep["declaration"]["status"], "found_before")
        self.assertTrue(ep["sentence"].endswith("under: Line one. ## injected heading end"))
        self.assertNotIn("\n## injected", C.render_certificate_md(self.cert([run], app)))

    def test_remote_sentences_pure(self):
        p = declaration_payload()
        recs = [{"seq": 0, "record_type": "project_created", "payload": {}},
                {"seq": 1, "record_type": "declaration", "payload": p}]
        cfg = {"tool": {"remote_endpoints": [{**ANTH, "declaration_sha256": p["sha256"]},
                                             {"role": "judge2", "host": "llm.example.invalid",
                                              "provider": "openai_compat", "declaration_sha256": HEX_A}]}}
        self.assertEqual(C.remote_sentences(recs, cfg), [
            SENTENCE,
            "Transcript text was sent to an OpenAI-compatible cloud endpoint (llm.example.invalid) for second judge "
            "family; no declaration with this hash is in the app log."])
        self.assertEqual(C.remote_sentences(recs, {"tool": {}}), [])
        self.assertEqual(C.remote_sentences(recs, {}), [])               # a study config: no remote endpoint
        self.assertEqual(C.remote_sentences(recs, types.SimpleNamespace(tool=cfg["tool"]))[0], SENTENCE)

    def test_cli_exit_1_when_a_declaration_fails(self):
        run, app = self.setup_case(after=[declaration_payload()])
        out = Path(tempfile.mkdtemp(prefix="ss-tests-cert-out-"))
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        err, so = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(so), contextlib.redirect_stderr(err):
            rc = cli_main(["certificate", "--run", str(run), "--app-log", str(app), "--out", str(out / "c.json")])
        self.assertEqual(rc, 1)
        self.assertIn("declaration check FAILED", so.getvalue())
        run, app = self.setup_case(before=[declaration_payload()])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = cli_main(["certificate", "--run", str(run), "--app-log", str(app), "--out", str(out / "d.json")])
        self.assertEqual(rc, 0)

class TestNoParticipantText(CertBase):
    def log_ngrams(self, run_dir: Path, n=8) -> set:
        """Every n-word window of the text the run sent to the (oracle) model: transcript text and map labels."""
        grams = set()
        for line in (run_dir / "logs" / "run.jsonl").read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec["record_type"] != "call_request":
                continue
            for m in rec["payload"].get("messages") or []:
                w = words(m.get("content", "") if isinstance(m, dict) else "")
                grams.update(" ".join(w[i:i + n]) for i in range(len(w) - n + 1))
        return grams

    def test_no_participant_text(self):
        grams = self.log_ngrams(self.a)
        self.assertGreater(len(grams), 1000, "the harness must see transcript text to be able to find it")
        app = TestAppLog.app_log(self, coding=True)
        cert = self.cert([self.a, self.b, self.d, self.e], app)
        out = " ".join(words(C.render_certificate_md(cert) + " " + json.dumps(cert, ensure_ascii=False)))
        found = [g for g in grams if g in out]
        self.assertEqual(found, [])
        # the harness itself bites: a transcript sentence planted in the output is found
        planted = next(iter(sorted(grams)))
        self.assertIn(planted, " ".join(words("note " + planted + " end")))


class TestCli(CertBase):
    def run_cli(self, argv):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = cli_main(argv)
        return rc, err.getvalue()

    def out_dir(self):
        d = Path(tempfile.mkdtemp(prefix="ss-tests-cert-out-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d

    def test_cli_writes_json_and_md(self):
        out = self.out_dir() / "cert" / "certificate.json"
        rc, _ = self.run_cli(["certificate", "--run", str(self.a), "--run", str(self.b), "--out", str(out)])
        self.assertEqual(rc, 0)
        cert = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(cert["schema"], "certificate/1")
        self.assertEqual([r["dir"] for r in cert["runs"]], ["A-full", "B-tool"])
        md = out.with_suffix(".md").read_text(encoding="utf-8")
        self.assertEqual(md, C.render_certificate_md(cert))

    def test_cli_exit_1_when_a_log_fails_but_files_are_written(self):
        out = self.out_dir() / "c.json"
        rc, _ = self.run_cli(["certificate", "--run", str(self.a), "--run", str(self.e), "--out", str(out)])
        self.assertEqual(rc, 1)
        self.assertTrue(out.is_file() and out.with_suffix(".md").is_file())

    def test_cli_study_flag_and_app_log(self):
        out = self.out_dir() / "c.json"
        app = TestAppLog.app_log(self, coding=True)
        rc, _ = self.run_cli(["certificate", "--run", str(self.a), "--app-log", str(app), "--study", "--out", str(out)])
        self.assertEqual(rc, 0)
        self.assertIn("by the authors", json.loads(out.read_text(encoding="utf-8"))["limit"])

    def test_cli_needs_a_run(self):
        rc, err = self.run_cli(["certificate", "--out", str(self.out_dir() / "c.json")])
        self.assertEqual(rc, 2)
        self.assertIn("--run", err)


if __name__ == "__main__":
    unittest.main()
