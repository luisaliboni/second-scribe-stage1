"""K3 / K11: the reference modes of a tool run (``tool.reference_type``).

A: the interviewer-drawn, participant-endorsed map (the study's reference; aggregates as without a tool block).
B: a researcher's own span-coded reading (every node is in the transcript by construction: no in_transcript call;
   structure metrics are not available, because a coding has no drawn edges or types).
C: no reference (empty maps: no in_transcript call; every reference-based number reads "no_reference", while the
   contradiction rate, which needs no reference, stays numeric).

All runs are offline (the oracle client on the shipped synthetic demo corpus); no model is called.
"""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from second_scribe import reference
from second_scribe.audit.registry import AuditLog
from second_scribe.cli import DEMO_CONFIG, build_clients
from second_scribe.pipeline import KIT_ROOT, RunConfig, _Run, expand_stages, run
from second_scribe.schemas import MapGraph

DEMO_HASH = "ccc13208b1fe5e66b5f4bef970af228cb92af9aefca6f21d341a0ccaa18c1ba9"
DEMO = KIT_ROOT / "demo" / "corpus"
NO_REF = {"status": "no_reference"}
NOT_AVAIL = {"status": "not_available"}

_original_endorsed = _Run.endorsed


def _endorsed_any(self, p):
    """``_Run.endorsed`` that also reads a ``.mapgraph.json`` reference (the app's normalised map), so these tests
    do not depend on the reader dispatch of Task 6; a ``.vsdx`` goes through the kit's own path unchanged."""
    if not str(p.map_path).endswith(".mapgraph.json"):
        return _original_endorsed(self, p)
    if p.pid not in self._endorsed:
        g = MapGraph.from_json(Path(p.map_path).read_text(encoding="utf-8"))
        g.map_id, g.interviewer = p.pid, p.interviewer
        self._endorsed[p.pid] = g
    return self._endorsed[p.pid]


def _demo_corpus(cfg_corpus: dict, ref_dir: Path, build) -> dict:
    """The demo corpus with each map replaced by ``ref_dir/<pid>.mapgraph.json`` (written by ``build(pid)``) and the
    shipped transcripts. ``ref_dir`` holds a copy of the demo ``meta.json`` so the corpus stays synthetic. No
    ``synthetic_dir``: the demo's traps name node ids of the drawn demo maps, which a mode B or C reference lacks."""
    ref_dir.mkdir(parents=True)
    shutil.copyfile(DEMO / "meta.json", ref_dir / "meta.json")
    pairs = {}
    for pid in sorted(cfg_corpus["roles"]):
        f = ref_dir / f"{pid}.mapgraph.json"
        f.write_text(build(pid).to_json(), encoding="utf-8")
        pairs[pid] = [str(f), str(DEMO / "transcripts" / f"{pid}.docx")]
    return {**{k: v for k, v in cfg_corpus.items() if k != "synthetic_dir"}, "pairs": pairs}


def _mode_b_ref(pid: str) -> MapGraph:
    g = MapGraph.from_json((DEMO / "gold" / f"{pid}.json").read_text(encoding="utf-8"))
    g.origin, g.edges = "endorsed", []
    g.meta = {**g.meta, "reference_type": "B"}
    return g


def _run(out: Path, **overrides) -> dict:
    cfg = RunConfig.load(DEMO_CONFIG, out_dir=str(out), **overrides)
    clients = build_clients("oracle", RunConfig.load(DEMO_CONFIG, out_dir=str(out)))   # answers from the demo gold
    out.mkdir(parents=True)
    log = AuditLog(out / "run.jsonl", "r")
    try:
        with mock.patch.object(_Run, "endorsed", _endorsed_any):
            run(cfg, expand_stages("all"), clients, log=log)
    finally:
        log.close()
    agg_bytes = (out / "aggregates" / "aggregates.json").read_bytes()
    records = [json.loads(line) for line in (out / "run.jsonl").read_text(encoding="utf-8").splitlines()]
    return {"cfg": cfg, "bytes": agg_bytes, "agg": json.loads(agg_bytes), "records": records}


def _in_transcript_calls(records) -> int:
    return sum(1 for d in records if d["record_type"] == "call_request"
               and d["payload"].get("stage") == "judge:in_transcript")


class TestReferenceModes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="ss-refmodes-")
        cls.addClassCleanup(cls._tmp.cleanup)
        root = Path(cls._tmp.name)
        base = RunConfig.load(DEMO_CONFIG, out_dir=str(root / "x")).corpus
        with mock.patch.object(reference, "mask_aggregates", wraps=reference.mask_aggregates) as spy:
            cls.plain = _run(root / "plain")
            cls.plain_mask_calls = spy.call_count
        cls.mode_a = _run(root / "a", tool={"reference_type": "A"})
        cls.mode_b = _run(root / "b", tool={"reference_type": "B"},
                          corpus=_demo_corpus(base, root / "refs-b" / "corpus", _mode_b_ref))
        cls.mode_c = _run(root / "c", tool={"reference_type": "C"},
                          corpus=_demo_corpus(base, root / "refs-c" / "corpus", reference.empty_reference))
        # the same study run with the reference code path switched off: no tool lookup, no masking
        with mock.patch.object(reference, "reference_type", lambda cfg: None), \
                mock.patch.object(reference, "mask_aggregates", side_effect=AssertionError("masked a study run")):
            cls.off = _run(root / "off")

    # ---- study behaviour unchanged

    def test_demo_hash_unchanged(self):
        self.assertEqual(RunConfig.load(DEMO_CONFIG).sha256(), DEMO_HASH)

    def test_no_tool_aggregates_byte_identical_with_code_path_off(self):
        self.assertEqual(self.plain_mask_calls, 0)
        self.assertEqual(self.plain["bytes"], self.off["bytes"])
        self.assertGreater(_in_transcript_calls(self.plain["records"]), 0)

    def test_mode_a_unchanged(self):
        a, plain = dict(self.mode_a["agg"]), dict(self.plain["agg"])
        self.assertNotEqual(a.pop("config_sha256"), plain.pop("config_sha256"))
        self.assertEqual(set(a.pop("chain")), {"head", "records", "verified"})   # K6: tool runs only
        self.assertNotIn("chain", plain)
        self.assertEqual(a, plain)

    # ---- mode C: no reference

    def test_mode_c_no_reference_and_contradiction_numeric(self):
        agg = self.mode_c["agg"]
        self.assertEqual(agg["ladder"]["report"]["verdict"],
                         {"verdict": "no_reference", "reason_codes": ["no_reference"]})
        cr = agg["headline"]["absences"]["contradiction_rate"]
        self.assertIsInstance(cr["quote_wished_marked_experienced"]["n"], int)
        self.assertEqual(_in_transcript_calls(self.mode_c["records"]), 0)

    def test_mode_c_masks_reference_blocks(self):
        agg = self.mode_c["agg"]
        self.assertEqual(agg["absences"], NO_REF)
        self.assertEqual(agg["buckets"], NO_REF)
        h = agg["headline"]
        for k in ("n_participants", "primary_spec", "primary_setting"):
            self.assertEqual(h[k], self.plain["agg"]["headline"][k])
        for k, v in h.items():
            if k not in ("n_participants", "primary_spec", "primary_setting", "absences"):
                self.assertEqual(v, NO_REF, k)
        for k, v in h["absences"].items():
            if k != "contradiction_rate":
                self.assertEqual(v, NO_REF, k)
        rep = agg["ladder"]["report"]
        for key, rung in rep["rungs"].items():
            self.assertEqual(rung["three_way"], NO_REF, key)
            self.assertEqual(sorted(rung["mirror"]), ["ai_node_count"], key)
            self.assertIsInstance(rung["mirror"]["ai_node_count"]["map_mean"], float)
        for key, c in rep["contrasts"].items():
            self.assertEqual(sorted(c["metrics"]), ["ai_node_count"], key)

    # ---- mode B: a researcher's reading

    def test_mode_b_in_transcript_all_true_without_call(self):
        out = Path(self.mode_b["cfg"].out_dir) / "work" / "in_transcript"
        files = sorted(out.glob("*.json"))
        self.assertEqual(len(files), 12)
        for f in files:
            d = json.loads(f.read_text(encoding="utf-8"))
            self.assertEqual(d["status"], "ok")
            self.assertTrue(d["in_transcript"])
            self.assertTrue(all(v is True for v in d["in_transcript"].values()), f.name)
        self.assertEqual(_in_transcript_calls(self.mode_b["records"]), 0)

    def test_mode_b_items_say_why_they_make_no_call(self):
        """L33: the skip marks each item, and records the reference file's own type that audit K9 just agreed with."""
        for f in sorted((Path(self.mode_b["cfg"].out_dir) / "work" / "in_transcript").glob("*.json")):
            d = json.loads(f.read_text(encoding="utf-8"))
            self.assertEqual((d["by_construction"], d["reference_file_type"]), ("mode_b", "B"), f.name)
        for run_ in (self.plain, self.mode_a, self.mode_c):        # only the mode-B skip writes the marker
            for f in sorted((Path(run_["cfg"].out_dir) / "work" / "in_transcript").glob("*.json")):
                d = json.loads(f.read_text(encoding="utf-8"))
                self.assertNotIn("by_construction", d, f.name)
                self.assertNotIn("reference_file_type", d, f.name)

    def test_run_start_names_the_reference_type_of_a_tool_run_only(self):
        def starts(run_):
            return [d["payload"] for d in run_["records"] if d["record_type"] == "run_start"]
        for run_, want in ((self.mode_a, "A"), (self.mode_b, "B"), (self.mode_c, "C")):
            self.assertEqual([s["reference_type"] for s in starts(run_)], [want])
        for run_ in (self.plain, self.off):                          # a study config: run_start unchanged
            self.assertNotIn("reference_type", starts(run_)[0])

    def test_mode_b_structure_not_available(self):
        agg = self.mode_b["agg"]
        self.assertEqual(agg["headline"]["linearization"], NOT_AVAIL)
        self.assertEqual(agg["headline"]["homogenization"], NOT_AVAIL)
        self.assertIsInstance(agg["absences"]["primary"]["n"], int)
        for block in [agg["buckets"]["primary"], *agg["buckets"]["by_spec"].values()]:
            for k in ("edge_recall", "n_edges_eligible", "n_edges_recovered"):
                self.assertEqual(block[k], NOT_AVAIL, k)
            self.assertIsInstance(block["node_recall"], float)

        def type_share_values(d):
            if isinstance(d, dict):
                for k, v in d.items():
                    if k.startswith("type_share:"):
                        yield v
                    yield from type_share_values(v)
            elif isinstance(d, list):
                for v in d:
                    yield from type_share_values(v)
        self.assertTrue(all(v == NOT_AVAIL for v in type_share_values(agg)))

    # ---- the pure function

    def test_mask_is_pure(self):
        agg = self.plain["agg"]
        before = copy.deepcopy(agg)
        for t in reference.REFERENCE_TYPES:
            out = reference.mask_aggregates(agg, t)
            self.assertIsNot(out, agg)
        self.assertEqual(agg, before)
        self.assertEqual(reference.mask_aggregates(agg, "A"), before)

    def test_mask_refuses_unknown_type(self):
        with self.assertRaises(ValueError):
            reference.mask_aggregates({}, "D")

    def test_reference_type_and_empty_reference(self):
        self.assertIsNone(reference.reference_type(self.plain["cfg"]))
        self.assertEqual(reference.reference_type(self.mode_c["cfg"]), "C")
        g = reference.empty_reference("SYN-OW01")
        self.assertEqual((g.map_id, g.origin, g.nodes, g.edges), ("SYN-OW01", "endorsed", [], []))
        self.assertEqual(g.meta, {"reference_type": "C"})
        self.assertEqual(MapGraph.from_json(g.to_json()), g)
        self.assertEqual(reference.REFERENCE_TYPES, ("A", "B", "C"))


def _mode_a_ref(pid: str) -> MapGraph:
    g = _mode_b_ref(pid)
    g.meta = {**g.meta, "reference_type": "A"}
    return g


class TestModeBMismatch(unittest.TestCase):
    """Audit K9: the mode-B "in the transcript by construction" skip needs BOTH the run's ``tool.reference_type`` and
    the file's ``meta.reference_type`` to be "B"; a mismatch either way is refused before any judge call."""

    def go(self, name: str, tool, build):
        tmp = tempfile.TemporaryDirectory(prefix="ss-refmodes-k9-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        base = RunConfig.load(DEMO_CONFIG, out_dir=str(root / "x")).corpus
        kw = {"tool": tool} if tool is not None else {}
        cfg = RunConfig.load(DEMO_CONFIG, out_dir=str(root / name), corpus=_demo_corpus(base, root / "refs", build),
                             **kw)
        clients = build_clients("oracle", RunConfig.load(DEMO_CONFIG, out_dir=str(root / name)))
        (root / name).mkdir(parents=True)
        log = AuditLog(root / name / "run.jsonl", "r")
        try:
            with mock.patch.object(_Run, "endorsed", _endorsed_any), self.assertRaises(ValueError) as cm:
                run(cfg, expand_stages("all"), clients, log=log)
        finally:
            log.close()
        records = [json.loads(x) for x in (root / name / "run.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(_in_transcript_calls(records), 0)
        self.assertFalse(list((root / name / "work" / "in_transcript").glob("*.json")))
        return str(cm.exception)

    def test_mode_a_run_with_b_file_refused(self):
        msg = self.go("a", {"reference_type": "A"}, _mode_b_ref)
        self.assertIn("reference type", msg)
        self.assertIn('"A"', msg)
        self.assertIn('"B"', msg)

    def test_study_run_with_b_file_refused(self):
        msg = self.go("s", None, _mode_b_ref)
        self.assertIn("reference type", msg)

    def test_mode_b_run_with_a_file_refused(self):
        msg = self.go("b", {"reference_type": "B"}, _mode_a_ref)
        self.assertIn("reference type", msg)


class TestModeBRealRunIsValid(unittest.TestCase):
    """L33: a mode-B run on a real corpus passes the run-validity check (its in_transcript items make no call by
    construction); the same work store fails it once an item loses its marker or a node is marked absent."""

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory(prefix="ss-tests-modeb-real-")
        cls.addClassCleanup(tmp.cleanup)
        root = Path(tmp.name)
        base = RunConfig.load(DEMO_CONFIG, out_dir=str(root / "x")).corpus
        corpus = _demo_corpus(base, root / "refs-b" / "corpus", _mode_b_ref)
        idmap = root / "participant_id_map.json"
        idmap.write_text(json.dumps({"_meta": {}, "map": {"QZ": {"canonical_id": "KC99"}}}), encoding="utf-8")
        cls.root = root
        cls.b = _run(root / "b", tool={"reference_type": "B"},
                     corpus={**corpus, "kind": "real", "participant_id_map": str(idmap)})

    def problem(self, edit=None):
        from second_scribe import prereg
        work = self.root / f"work-{self._testMethodName}"
        shutil.copytree(Path(self.b["cfg"].out_dir) / "work", work)
        if edit:
            f = sorted((work / "in_transcript").glob("*.json"))[0]
            f.write_text(json.dumps(edit(json.loads(f.read_text(encoding="utf-8")))), encoding="utf-8")
        return prereg.run_validity_problem(Path(self.b["cfg"].out_dir) / "run.jsonl", work)

    def refused(self, edit):
        return self.problem(edit) or ""

    def test_the_run_is_real_and_valid(self):
        self.assertTrue(any(d["record_type"] == "run_start" and d["payload"]["corpus_kind"] == "real"
                            for d in self.b["records"]))
        self.assertEqual(_in_transcript_calls(self.b["records"]), 0)
        self.assertIsNone(self.problem())

    def test_an_item_without_the_marker_fails(self):
        self.assertIn("names no model call", self.refused(lambda d: {k: v for k, v in d.items()
                                                                     if k != "by_construction"}))

    def test_an_item_with_a_node_marked_absent_fails(self):
        def one_false(d):
            k = sorted(d["in_transcript"])[0]
            return {**d, "in_transcript": {**d["in_transcript"], k: False}}
        self.assertIn("names no model call", self.refused(one_false))


if __name__ == "__main__":
    unittest.main()
