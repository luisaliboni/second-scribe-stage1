"""Task 38: one kit run config and one subprocess env per reference group (``second_scribe.app.orchestrate``).

The project is built through the app's own modules (Task 33 importer, Task 36 coding store, Task 34 models) from
four transcripts and maps of the kit's synthetic corpus, imported under neutral new names. No model is contacted:
the model check is a stored result over a file written here, and every address is loopback or a made-up host
classified with a fake resolver.
"""
import copy
import hashlib
import json
import re
import secrets as pysecrets
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from second_scribe import codehash, llm, prereg
from second_scribe.app import api, coding, importer, jobs, models, orchestrate, results, strings
from second_scribe.app.project import Project
from second_scribe.pipeline import RunConfig
from tests.addr import ip

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "demo" / "corpus"
TEMPLATE = json.loads((ROOT / "configs" / "real.template.json").read_text(encoding="utf-8"))
CONTEXT = {"domain": "food-truck licensing",
           "phases": ["Planning", "Permits", "Inspections", "Operating"],
           "roles": {"owner": "food-truck owner who applied for and holds the licences",
                     "officer": "municipal licensing or inspection officer who processes the applications"}}
# new pid <- shipped file stem; the reference type each pid gets
SOURCES = {"CASE-A01": "SYN-OW01", "CASE-A02": "SYN-OF02", "CASE-B01": "SYN-OW02", "CASE-C01": "SYN-OF01"}
ROLES = {"CASE-A01": "owner", "CASE-A02": "officer", "CASE-B01": "owner", "CASE-C01": "officer"}
TYPES = {"CASE-A01": "A", "CASE-A02": "A", "CASE-B01": "B", "CASE-C01": "C"}
GROUP_PIDS = {"A": ["CASE-A01", "CASE-A02"], "B": ["CASE-B01"], "C": ["CASE-C01"]}
FAKE_HOST = "remote-llm.mock.invalid"
LAN_HOST = ip(192, 168, 7, 20)
READING3_SHA = hashlib.sha256((ROOT / "prompts" / "prior_only.p0.v2.txt").read_bytes()).hexdigest()


def fake_key() -> str:
    return "sk" + "-test-" + pysecrets.token_hex(12)


def public_resolver(host, port):
    return [ip(104, 18, 0, 1)]


def role_entry(url, model, family, *, kind="local", provider="openai", address_class="loopback", digest=None):
    return {"kind": kind, "provider": provider, "url": url, "host": llm.endpoint_host(url),
            "address_class": address_class, "model": model, "family": family, "family_detected": family,
            "digest": digest, "server": "ollama",
            "connection": {"ok": True, "ms": 5, "thinking_seen": False, "reply_ok": True, "error": None}}


LOCAL = "http://127.0.0.1:11434/v1"
RECON_DIGEST = "a" * 64


def build_base(root: Path, *, lock_b: bool = True) -> Project:
    """Setup done by hand through the modules: context, imports, pairing, labels, maps, references, models."""
    project = Project.create(root, "Study")
    importer.set_context(project, CONTEXT)
    for pid, src in SOURCES.items():
        importer.import_file(project, "transcript", f"{pid}.docx",
                             _b64((CORPUS / "transcripts" / f"{src}.docx").read_bytes()))
        importer.import_file(project, "map", f"{pid}.vsdx", _b64((CORPUS / "maps" / f"{src}.vsdx").read_bytes()))
    table = importer.pairing(project)
    importer.confirm_pairing(project, table["table_sha256"])
    for pid in SOURCES:
        importer.confirm_labels(project, {"pid": pid, "participant": "PT", "interviewer": "IV", "role": ROLES[pid]})
        preview = importer.preview_map(project, pid)
        importer.confirm_map(project, {"pid": pid, "sha256": preview["sha256"]})
    for pid, t in TYPES.items():
        coding.set_reference(project, pid, t)
    for pid in GROUP_PIDS["B"]:
        add_one_mark(project, pid)
        if lock_b:
            coding.lock(project, pid)
    set_models(project)
    return project


def _b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode("ascii")


def add_one_mark(project, pid):
    for t in coding.turns(project, pid):
        if t.speaker == "participant" and len(t.text.split()) >= coding.MIN_WORDS + 2:
            text = " ".join(t.text.split(" ")[: coding.MIN_WORDS + 2])
            return coding.add_mark(project, pid, {"turn": t.idx, "start": 0, "end": len(text), "text": text,
                                                  "status": "happened", "step": "Read the how-to pages",
                                                  "phase": "Planning"})
    raise AssertionError("no participant turn with enough words")


def set_models(project, *, judge=True):
    models.set_role(project, "recon", role_entry(LOCAL, "qwen-mock:7b", "qwen", digest=RECON_DIGEST))
    fams = {"recon": "qwen"}
    if judge:
        models.set_role(project, "judge", role_entry(LOCAL, "gemma-mock:9b", "gemma"))
        fams["judge"] = "gemma"
    models.confirm_families(project, fams)
    project.save()
    pass_model_check(project)


def pass_model_check(project, passed=True):
    rel = f"{jobs.check_rel(project)}/model_check.json"
    f = project.path(rel)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"schema": "model-check/1", "passed": passed}) + "\n", encoding="utf-8")
    project.data["model_check"] = {"pair_sha256": models.pair_sha256(project), "path": rel,
                                   "sha256": jobs.sha256_file(f), "passed": passed, "failures": [], "models": {}}
    project.save()


class Base(unittest.TestCase):
    """A fresh copy of one prepared project per test (the project folder holds only relative paths)."""

    lock_b = True

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="ss-tests-")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.base = Path(cls._tmp.name).resolve() / "base"
        build_base(cls.base, lock_b=cls.lock_b)

    def setUp(self):
        self.root = Path(self._tmp.name).resolve() / f"p-{pysecrets.token_hex(4)}"
        shutil.copytree(self.base, self.root)
        self.project = Project.open(self.root)

    def build(self, group="A", rungs=("rung2",), preset="standard", run_id=None):
        rid = run_id or orchestrate.new_run_id(group)
        return orchestrate.build_run_config(self.project, group, list(rungs) if isinstance(rungs, tuple) else rungs,
                                            preset, rid)

    def cfg(self, path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def refused(self, code, status=409, **kw):
        with self.assertRaises(api.ApiError) as cm:
            self.build(**kw)
        self.assertEqual((cm.exception.status, cm.exception.code), (status, code), cm.exception.message)
        return cm.exception


class TestPresets(Base):
    def test_standard_preset_fields(self):
        cfg = self.cfg(self.build("A", ["rung2"], "standard"))
        self.assertEqual(cfg["specs"], [{"arm": "removed", "variant": "p0", "temperature": 0.0, "replicate": 0}])
        self.assertEqual(cfg["ladder"], {"rungs": ["rung2"], "draws": 1})
        self.assertEqual(cfg["prior_only"], {"variants": ["p0"], "samples": 5, "temperature": 0.7})
        self.assertEqual(cfg["reading3"], {"variant": "p0", "samples": 1, "temperature": 0})
        self.assertEqual(cfg["settings"], TEMPLATE["settings"])
        self.assertEqual(len(cfg["settings"]), 8)
        self.assertEqual(cfg["stats"], TEMPLATE["stats"])
        self.assertEqual(cfg["models"], {"recon": "RECON", "judge": "JUDGE", "judge2": None})
        self.assertNotIn("script_sources", cfg)
        # rung 2 always runs, whatever the pick
        self.assertEqual(self.cfg(self.build("A", [], "standard"))["ladder"]["rungs"], ["rung2"])
        self.assertEqual(self.cfg(self.build("A", ["rung1"], "standard"))["ladder"]["rungs"], ["rung1", "rung2"])

    def test_rung3_adds_placebo(self):
        rungs = self.cfg(self.build("A", ["rung3"], "standard"))["ladder"]["rungs"]
        self.assertEqual(sorted(rungs), ["placebo", "rung2", "rung3"])
        self.assertEqual(sorted(self.cfg(self.build("A", ["rung1", "rung3"], "full"))["ladder"]["rungs"]),
                         ["placebo", "rung1", "rung2", "rung3"])

    def test_full_preset_from_template(self):
        cfg = self.cfg(self.build("A", ["rung2", "rung3"], "full"))
        for key in ("specs", "prior_only", "reading3", "settings", "stats"):
            self.assertEqual(cfg[key], TEMPLATE[key], key)
        self.assertEqual(cfg["ladder"]["draws"], TEMPLATE["ladder"]["draws"])
        self.assertEqual(sorted(cfg["ladder"]["rungs"]), ["placebo", "rung2", "rung3"])
        self.assertNotIn("script_sources", cfg)       # the template's other-family source is a Setup choice

    def test_bad_choices_refused(self):
        self.refused("bad_group", 422, group="D")
        self.refused("bad_rungs", 422, rungs=["rung9"])
        self.refused("bad_rungs", 422, rungs="rung2")
        self.refused("bad_preset", 422, preset="quick")
        self.refused("bad_run_id", 422, group="A", run_id="B-0123abcd")
        self.refused("bad_run_id", 422, group="A", run_id="A-2026-10-19")

    def test_presets_in_code_hash(self):
        files = codehash.code_files()
        for name in ("standard", "full"):
            self.assertIn(f"second_scribe/app/presets/{name}.json", files)
        with tempfile.TemporaryDirectory(prefix="ss-tests-") as tmp:
            dst = Path(tmp) / "second_scribe" / "app" / "presets"
            shutil.copytree(orchestrate.PRESETS_DIR, dst)
            before = codehash.code_sha256(Path(tmp))
            f = dst / "standard.json"
            doc = json.loads(f.read_text(encoding="utf-8"))
            doc["prior_only"]["samples"] = 6
            f.write_text(json.dumps(doc), encoding="utf-8")
            self.assertNotEqual(codehash.code_sha256(Path(tmp)), before)


class TestConfig(Base):
    def test_tool_block_and_app_log_head(self):
        head = self.project.log.head()
        rid = orchestrate.new_run_id("A")
        path = self.build("A", ["rung2"], "standard", rid)
        cfg = self.cfg(path)
        self.assertEqual(path, self.project.path(f"runs/{rid}/config.json"))
        self.assertEqual(cfg["run_id"], rid)
        self.assertEqual(cfg["out_dir"], str(self.project.path(f"runs/{rid}").resolve()))
        tool = cfg["tool"]
        self.assertEqual(tool["app_log_head"], {"records": head[0], "head": head[1]})
        self.assertEqual(tool["judge_rule"], "two_family")
        self.assertEqual(tool["model_families"], {"recon": "qwen", "judge": "gemma"})
        self.assertEqual(tool["model_digests"], {"recon": RECON_DIGEST})
        self.assertEqual(tool["model_check"], jobs.model_check_input(self.project))
        self.assertEqual(tool["reference_type"], "A")
        self.assertEqual(tool["reading3_prompt_sha256"], READING3_SHA)
        for absent in ("rung1_user", "remote_endpoints", "endpoint_optins"):
            self.assertNotIn(absent, tool)
        # run_started is the record after the head the config names
        recs = self.project.log.records("run_started")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["payload"], {"group": "A", "run_id": rid,
                                              "config_sha256": RunConfig.load(path).sha256()})
        self.assertEqual(self.project.log.head()[0], head[0] + 1)
        # the Results lookup (Task 40) reads the run where Task 38 wrote it
        self.assertEqual(results.find_run(self.project, rid).config_path.resolve(), path.resolve())

    def test_tool_block_carries_optins_declarations_and_rung1(self):
        lan = f"http://{LAN_HOST}:11434/v1"
        models.set_role(self.project, "recon", role_entry(lan, "qwen-mock:7b", "qwen", address_class="lan"))
        models.log_optin(self.project, "recon", f"{LAN_HOST}:11434")
        url = f"https://{FAKE_HOST}/v1"
        models.set_role(self.project, "judge", role_entry(url, "claude-mock-1", "claude", kind="anthropic",
                                                          provider="anthropic", address_class="remote"))
        models.log_declaration(self.project, "judge", "anthropic", FAKE_HOST, "Ethics approval covers it.", True,
                               resolver=public_resolver)
        models.confirm_families(self.project, {"recon": "qwen", "judge": "claude"})
        self.project.save()
        pass_model_check(self.project)
        results.set_instructions(self.project, {"rung1": "own", "rung1_text": "Draw the journey as steps.",
                                                "rungs": ["rung1", "rung2"]})
        tool = self.cfg(self.build("A", ["rung1", "rung2"]))["tool"]
        self.assertEqual(tool["endpoint_optins"], models.endpoint_optins(self.project))
        self.assertEqual(tool["remote_endpoints"], models.remote_endpoints(self.project))
        self.assertEqual(len(tool["remote_endpoints"]), 1)
        self.assertEqual(tool["rung1_user"], results.instructions(self.project)["rung1_user"])
        # rung 1 not picked: the user's prompt is not part of the run
        self.assertNotIn("rung1_user", self.cfg(self.build("A", ["rung2"]))["tool"])

    def test_corpus_block(self):
        cfg = self.cfg(self.build("A"))
        c = cfg["corpus"]
        self.assertEqual(c["kind"], "real")
        self.assertEqual(sorted(c["pairs"]), GROUP_PIDS["A"])
        for pid, (m, t) in c["pairs"].items():
            self.assertTrue(Path(m).is_absolute() and Path(t).is_absolute())
            self.assertEqual(Path(m), self.project.path(f"reference/{pid}.mapgraph.json").resolve())
            self.assertEqual(Path(t), self.project.path(f"inputs/transcripts/{pid}.docx").resolve())
        self.assertEqual(c["roles"], {p: ROLES[p] for p in GROUP_PIDS["A"]})
        self.assertEqual(c["participant_labels"], {p: ["PT"] for p in GROUP_PIDS["A"]})
        self.assertEqual(c["interviewer_labels"], {p: ["IV"] for p in GROUP_PIDS["A"]})
        self.assertEqual(c["participant_id_map"], str(self.project.path(importer.PID_MAP).resolve()))
        self.assertEqual(cfg["context"], {**CONTEXT, "extract": {"phase_names": CONTEXT["phases"]}})
        for g in ("B", "C"):
            self.assertEqual(sorted(self.cfg(self.build(g))["corpus"]["pairs"]), GROUP_PIDS[g])

    def test_config_loads_in_kit(self):
        for g in ("A", "B", "C"):
            for preset, rungs in (("standard", ["rung2"]), ("full", ["rung1", "rung3"])):
                with self.subTest(group=g, preset=preset):
                    path = self.build(g, rungs, preset)
                    cfg = RunConfig.load(path)
                    names = prereg.corpus_inputs(cfg.corpus, cfg.base_dir)
                    for pid in GROUP_PIDS[g]:
                        self.assertIn(f"pairs/{pid}/map", names)
                        self.assertIn(f"pairs/{pid}/transcript", names)
                    self.assertEqual(cfg.tool["reference_type"], g)

    def test_run_id_undated(self):
        from second_scribe import publish
        seen = set()
        for g in ("A", "B", "C"):
            for _ in range(20):
                rid = orchestrate.new_run_id(g)
                self.assertRegex(rid, rf"^{g}-[0-9a-f]{{8}}$")
                self.assertTrue(results.RUN_ID.fullmatch(rid))
                self.assertIsNone(publish._DATED.search(rid))
                seen.add(rid)
        self.assertEqual(len(seen), 60)
        # 8 hex characters can all be digits, which the publish gate reads as a date: drawn again
        with mock.patch.object(orchestrate._secrets, "token_hex", side_effect=["20261019", "15377382", "0a1b2c3d"]):
            self.assertEqual(orchestrate.new_run_id("A"), "A-0a1b2c3d")

    def test_resume_reuses_config_unchanged(self):
        rid = orchestrate.new_run_id("A")
        path = self.build("A", ["rung2"], "standard", rid)
        before = path.read_bytes()
        n = self.project.log.head()[0]
        again = self.build("A", ["rung3"], "full", rid)      # a resume never rewrites the config
        self.assertEqual(again, path)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.project.log.head()[0], n)       # and logs no second run_started

    def test_script_sources_optional_off_by_default(self):
        self.assertNotIn("script_sources", self.cfg(self.build("A")))
        src = [{"name": "otherfam", "role": "judge", "variants": ["p0"], "samples": 2, "temperature": 0.7}]
        self.project.data["script_sources"] = src
        self.project.save()
        path = self.build("A")
        self.assertEqual(self.cfg(path)["script_sources"], src)
        self.assertEqual(RunConfig.load(path).script_sources, src)

    def test_ai_maps_only_one_family(self):
        models.clear_role(self.project, "judge")
        models.confirm_families(self.project, {"recon": "qwen"})
        self.project.save()
        pass_model_check(self.project)
        tool = self.cfg(self.build("A"))["tool"]
        self.assertEqual(tool["model_families"], {"recon": "qwen"})
        # a same-family judge is not part of an AI-maps-only run: its family and digest stay out
        models.set_role(self.project, "judge", role_entry(LOCAL, "qwen-mock:14b", "qwen", digest="b" * 64))
        models.confirm_families(self.project, {"recon": "qwen", "judge": "qwen"})
        self.project.save()
        self.assertEqual(models.pipeline_mode(self.project), "ai_maps_only")
        pass_model_check(self.project)
        tool = self.cfg(self.build("A"))["tool"]
        self.assertEqual(tool["model_families"], {"recon": "qwen"})
        self.assertEqual(tool["model_digests"], {"recon": RECON_DIGEST})


class TestRefusals(Base):
    def test_refusals_table(self):
        def changed_transcript(p):
            f = p.path("inputs/transcripts/CASE-A02.docx")
            f.write_bytes(f.read_bytes() + b"\0")

        def no_id_map(p):
            p.path(importer.PID_MAP).unlink()

        def empty_id_map(p):
            p.path(importer.PID_MAP).write_text(json.dumps({"map": {}}), encoding="utf-8")

        def labels_off(p):
            p.data["labels"]["CASE-A01"]["role"] = "inspector"      # no longer a Setup role
            p.save()

        def map_off(p):
            p.data["maps"]["CASE-A02"]["node_phase"] = {"zz": "Planning"}
            p.save()

        def failed_check(p):
            pass_model_check(p, passed=False)

        def no_check(p):
            p.data.pop("model_check")
            p.save()

        def lan_no_optin(p):
            models.set_role(p, "recon", role_entry(f"http://{LAN_HOST}:11434/v1", "qwen-mock:7b", "qwen",
                                                   address_class="lan"))
            models.confirm_families(p, {"recon": "qwen", "judge": "gemma"})
            p.save()
            pass_model_check(p)

        def remote_undeclared(p):
            models.set_role(p, "judge", role_entry(f"https://{FAKE_HOST}/v1", "claude-mock-1", "claude",
                                                   kind="anthropic", provider="anthropic", address_class="remote"))
            models.confirm_families(p, {"recon": "qwen", "judge": "claude"})
            p.save()
            pass_model_check(p)

        def cloud(p):
            models.set_role(p, "judge", role_entry(LOCAL, "gemma-mock:9b-cloud", "gemma"))
            models.confirm_families(p, {"recon": "qwen", "judge": "gemma"})
            p.save()
            pass_model_check(p)

        def families(p):
            models.set_role(p, "judge", role_entry(LOCAL, "llama-mock:8b", "llama"))
            p.save()

        def no_group(p):
            coding.set_reference(p, "CASE-C01", "A")

        def ref_edited(p):
            f = p.path("reference/CASE-A01.mapgraph.json")
            f.write_bytes(f.read_bytes() + b" ")

        def ref_gone(p):
            p.path("reference/CASE-C01.mapgraph.json").unlink()

        def no_context(p):
            p.data["context"] = {}
            p.save()

        table = [
            (changed_transcript, "A", "file_changed"),
            (no_id_map, "A", "id_map_missing"),
            (empty_id_map, "A", "id_map_missing"),
            (labels_off, "A", "labels_unconfirmed"),
            (map_off, "A", "map_unconfirmed"),
            (failed_check, "A", "model_check_failed"),
            (no_check, "A", "model_check_missing"),
            (lan_no_optin, "A", "optin_missing"),
            (remote_undeclared, "A", "declaration_missing"),
            (cloud, "A", "cloud_model"),
            (families, "A", "families_unconfirmed"),
            (no_group, "C", "group_empty"),
            (ref_edited, "A", "reference_changed"),
            (ref_gone, "C", "reference_missing"),
            (no_context, "A", "no_context"),
        ]
        for make, group, code in table:
            with self.subTest(code=code, case=make.__name__):
                self.setUp()
                make(self.project)
                n = self.project.log.head()[0]
                runs = sorted(p.name for p in self.project.path("runs").iterdir())
                e = self.refused(code, group=group)
                self.assertTrue(e.message)
                self.assertEqual(self.project.log.head()[0], n)                        # nothing logged
                self.assertEqual(sorted(p.name for p in self.project.path("runs").iterdir()), runs)   # nor written
        # the changed file is named
        self.setUp()
        changed_transcript(self.project)
        e = self.refused("file_changed")
        self.assertIn("CASE-A02.docx changed since you imported it", e.message)

    def test_every_refusal_has_a_message(self):
        for code in orchestrate.ERROR_CODES:
            with self.subTest(code=code):
                self.assertTrue(strings.t(f"orchestrate.error.{code}"))


class TestBOpen(Base):
    lock_b = False

    def test_a_group_runs_while_b_open(self):
        self.assertEqual(coding.waiting(self.project), GROUP_PIDS["B"])
        for g in ("A", "C"):
            self.assertTrue(self.build(g).is_file())
        self.refused("coding_unlocked", group="B")
        coding.lock(self.project, "CASE-B01")
        self.assertTrue(self.build("B").is_file())


class TestDeclarations(Base):
    """Audit K5: a remote role's declaration must be in the app log, hashing to the one the config names, before the
    run config is written or run_started is logged."""

    def _remote_judge(self):
        url = f"https://{FAKE_HOST}/v1"
        models.set_role(self.project, "judge", role_entry(url, "claude-mock-1", "claude", kind="anthropic",
                                                          provider="anthropic", address_class="remote"))
        models.log_declaration(self.project, "judge", "anthropic", FAKE_HOST, "Ethics approval covers it.", True,
                               resolver=public_resolver)
        models.confirm_families(self.project, {"recon": "qwen", "judge": "claude"})
        self.project.save()
        pass_model_check(self.project)

    def _refused_clean(self, rid):
        before = self.project.log.head()
        with self.assertRaises(api.ApiError) as cm:
            orchestrate.build_run_config(self.project, "A", ["rung2"], "standard", rid)
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "declaration_missing"))
        self.assertFalse(self.project.path(f"runs/{rid}/config.json").exists())
        self.assertEqual(self.project.log.records("run_started"), [])
        self.assertEqual(self.project.log.head(), before)

    def test_matching_declaration_passes(self):
        self._remote_judge()
        cfg = self.cfg(self.build("A"))
        self.assertEqual(len(cfg["tool"]["remote_endpoints"]), 1)
        self.assertEqual(len(self.project.log.records("run_started")), 1)

    def test_declaration_missing_from_the_log_refused(self):
        self._remote_judge()
        rid = orchestrate.new_run_id("A")
        entry = models.remote_endpoints(self.project)[0]
        ghost = {**entry, "host": "other-llm.mock.invalid"}       # no declaration was logged for this host
        fields = models.tool_fields(self.project)
        fields["remote_endpoints"] = [ghost]
        with mock.patch.object(models, "tool_fields", return_value=fields):
            self._refused_clean(rid)

    def test_declaration_hash_mismatch_refused(self):
        self._remote_judge()
        rid = orchestrate.new_run_id("A")
        entry = models.remote_endpoints(self.project)[0]
        fields = models.tool_fields(self.project)
        fields["remote_endpoints"] = [{**entry, "declaration_sha256": "0" * 64}]
        with mock.patch.object(models, "tool_fields", return_value=fields):
            self._refused_clean(rid)


class TestEnv(Base):
    def test_env_both_roles_and_thinking_off(self):
        env = orchestrate.run_env(self.project, api.Secrets(), None)
        for p, model in (("RECON", "qwen-mock:7b"), ("JUDGE", "gemma-mock:9b")):
            self.assertEqual(env[f"SS_{p}_URL"], LOCAL)
            self.assertEqual(env[f"SS_{p}_MODEL"], model)
            self.assertEqual(env[f"SS_{p}_PROVIDER"], "openai")
            self.assertEqual(json.loads(env[f"SS_{p}_PARAMS"]), {"chat_template_kwargs": {"enable_thinking": False}})
        self.assertNotIn("SS_JUDGE2_URL", env)
        # a stray SS_ variable of the app's own shell never reaches the kit
        with mock.patch.dict("os.environ", {"SS_RECON_URL": "http://127.0.0.1:1/v1", "SS_STRAY": "x"}):
            env = orchestrate.run_env(self.project, api.Secrets(), ("recon", "judge"))
        self.assertEqual(env["SS_RECON_URL"], LOCAL)
        self.assertNotIn("SS_STRAY", env)
        # Q only: the judge's variables are not set
        q = orchestrate.run_env(self.project, api.Secrets(), ("recon",))
        self.assertIn("SS_RECON_URL", q)
        self.assertNotIn("SS_JUDGE_URL", q)
        with self.assertRaises(ValueError):
            orchestrate.run_env(self.project, api.Secrets(), ("recon", "judge2"))

    def test_ai_maps_only_writes_no_judge_env(self):
        models.set_role(self.project, "judge", role_entry(LOCAL, "qwen-mock:14b", "qwen"))
        models.confirm_families(self.project, {"recon": "qwen", "judge": "qwen"})
        self.project.save()
        for roles in (None, ("recon", "judge")):
            env = orchestrate.run_env(self.project, api.Secrets(), roles)
            self.assertIn("SS_RECON_URL", env)
            self.assertFalse([k for k in env if k.startswith("SS_JUDGE")])

    def _remote_judge(self, secrets):
        url = f"https://{FAKE_HOST}/v1"
        models.set_role(self.project, "judge", role_entry(url, "claude-mock-1", "claude", kind="anthropic",
                                                          provider="anthropic", address_class="remote"))
        models.log_declaration(self.project, "judge", "anthropic", FAKE_HOST, "Ethics approval covers it.", True,
                               resolver=public_resolver)
        models.confirm_families(self.project, {"recon": "qwen", "judge": "claude"})
        self.project.save()
        pass_model_check(self.project)
        key = fake_key()
        secrets.set("judge", key)
        return key

    def test_params_build_model_config(self):
        secrets = api.Secrets()
        self._remote_judge(secrets)
        env = orchestrate.run_env(self.project, secrets, None)
        self.assertEqual(json.loads(env["SS_JUDGE_PARAMS"]), {})
        with mock.patch.dict("os.environ", env, clear=True):
            local = llm.ModelConfig.from_env("RECON")
            remote = llm.ModelConfig.from_env("JUDGE")
        self.assertEqual((local.endpoint, local.provider, local.params),
                         (LOCAL, "openai", {"chat_template_kwargs": {"enable_thinking": False}}))
        self.assertEqual((remote.provider, remote.params), ("anthropic", {}))
        self.assertTrue(remote.api_key_env)
        self.assertIsNone(local.api_key_env)
        for cfg in (local, remote):
            self.assertFalse(set(cfg.params) & set(llm.RESERVED_PARAMS))

    def test_key_missing_refused(self):
        secrets = api.Secrets()
        self._remote_judge(secrets)
        secrets.clear("judge")
        with self.assertRaises(api.ApiError) as cm:
            orchestrate.run_env(self.project, secrets, None)
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "key_missing"))

    def test_key_only_in_env(self):
        secrets = api.Secrets()
        key = self._remote_judge(secrets)
        path = self.build("A")
        env = orchestrate.run_env(self.project, secrets, None)
        self.assertEqual(env[env["SS_JUDGE_KEY_ENV"]], key)
        self.assertNotIn(key, path.read_text(encoding="utf-8"))
        for f in self.project.root.rglob("*"):
            if f.is_file():
                self.assertNotIn(key.encode("utf-8"), f.read_bytes(), f)
        self.assertNotIn(key, repr(env.get("SS_JUDGE_KEY_ENV")))


if __name__ == "__main__":
    unittest.main()
