"""K8 / Task 19: the view-data exporter (``second_scribe.export_view``, schema ``view-data/1``).

Offline only: the oracle client on the shipped synthetic demo corpus (all four rungs, one draw), with the remedy's
replies damaged so the remedy rung has leaks to count. No model is called and nothing outside the temp dirs is written.
"""
import contextlib
import dataclasses
import hashlib
import io
import json
import math
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from second_scribe import export_view as ev
from second_scribe import ladder_metrics, reference, stats
from second_scribe.audit.registry import AuditLog
from second_scribe.cli import DEMO_CONFIG, build_clients
from second_scribe.pipeline import KIT_ROOT, RunConfig, expand_stages, ladder_params, run
from second_scribe.schemas import MapGraph

DEMO_HASH = "ccc13208b1fe5e66b5f4bef970af228cb92af9aefca6f21d341a0ccaa18c1ba9"
DEMO = KIT_ROOT / "demo" / "corpus"
FIXTURE = KIT_ROOT / "tests" / "fixtures" / "views" / "mini.view.json"
ALL_RUNGS = ["rung1", "rung2", "placebo", "rung3"]           # display order


class LeakyRemedy:
    """The oracle, with every node and edge of the REMEDY prompt's reply drawn as experienced (a leak for each
    wished step it matches) plus one extra node no reference step has (an AI node that matches no item)."""
    model_label = "oracle"

    def __init__(self, inner):
        self.inner = inner

    def complete(self, messages, **kw):
        resp = self.inner.complete(messages, **kw)
        user = next(m["content"] for m in messages if m["role"] == "user")
        if "according to the participant" in user and "Two worked examples" in user:
            d = json.loads(resp.text)
            for n in d["nodes"]:
                n["status"] = "experienced"
            for e in d["edges"]:
                e["status"] = "experienced"
            d["nodes"].append({"id": "nx", "label": "Remedy only extra", "type": "service", "phase": None,
                               "status": "experienced", "quote": d["nodes"][0]["quote"]})
            return dataclasses.replace(resp, text=json.dumps(d))
        return resp


def _run(out: Path, stages: str = "all", roles=None, wrap=None, config=DEMO_CONFIG, **overrides) -> RunConfig:
    cfg = RunConfig.load(config, out_dir=str(out), **overrides)
    clients = build_clients("oracle", RunConfig.load(config, out_dir=str(out)), **({"roles": roles} if roles else {}))
    if wrap:
        clients = {k: wrap(v) if k == "recon" else v for k, v in clients.items()}
    (out / "logs").mkdir(parents=True)
    log = AuditLog(out / "logs" / "run.jsonl", cfg.run_id)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            run(cfg, expand_stages(stages), clients, log=log)
    finally:
        log.close()
    return cfg


def _empty_refs(cfg_corpus: dict, ref_dir: Path) -> dict:
    """The demo corpus with each map replaced by an empty mode-C reference (``reference.empty_reference``)."""
    ref_dir.mkdir(parents=True)
    shutil.copyfile(DEMO / "meta.json", ref_dir / "meta.json")
    pairs = {}
    for pid in sorted(cfg_corpus["roles"]):
        f = ref_dir / f"{pid}.mapgraph.json"
        f.write_text(reference.empty_reference(pid).to_json(), encoding="utf-8")
        pairs[pid] = [str(f), str(DEMO / "transcripts" / f"{pid}.docx")]
    return {**{k: v for k, v in cfg_corpus.items() if k != "synthetic_dir"}, "pairs": pairs}


def _snapshot(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def _records(cfg) -> list[dict]:
    path = Path(cfg.out_dir) / "logs" / "run.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _agg(cfg) -> dict:
    return json.loads((Path(cfg.out_dir) / "aggregates" / "aggregates.json").read_text(encoding="utf-8"))


class TestExportView(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory(prefix="ss-tests-exportview-")
        cls.addClassCleanup(tmp.cleanup)
        cls.root = Path(tmp.name)
        cls.cfg = _run(cls.root / "lad", wrap=LeakyRemedy)
        cls.agg = _agg(cls.cfg)
        cls.before = _snapshot(Path(cls.cfg.out_dir))
        cls.view = ev.build_view(cls.cfg, source="synthetic", reference_type=None)
        cls.after = _snapshot(Path(cls.cfg.out_dir))
        rep = cls.agg["ladder"]["report"]
        cls.tw = {r: rep["rungs"][cls.agg["ladder"]["arm_of_rung"][r]]["three_way"] for r in cls.agg["ladder"]["rungs"]}

    # ---- the contract

    def test_valid_view(self):
        self.assertEqual(ev.validate_view(self.view), [])
        v = self.view
        self.assertEqual(v["schema"], "view-data/1")
        self.assertEqual(v["source"], "synthetic")
        self.assertIsNone(v["reference_type"])
        self.assertEqual(v["rungs"], ALL_RUNGS)
        self.assertEqual(v["arm_of_rung"], {"rung1": "rung1", "rung2": "rung2", "placebo": "placebo", "rung3": "remedy"})
        self.assertEqual(v["phases"], self.cfg.context["phases"])
        self.assertEqual(len(v["maps"]), 12)
        self.assertNotIn("test_pattern", v)          # a recorded result, not a test pattern
        json.dumps(v, allow_nan=False)               # no NaN anywhere

    def test_mini_fixture_validates(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(ev.validate_view(fixture), [])

    def test_validate_view_refusals(self):
        def broken(change):
            v = json.loads(json.dumps(self.view))
            change(v)
            return ev.validate_view(v)
        item = next(i for m in self.view["maps"] for i in m["items"])
        mi = next(k for k, m in enumerate(self.view["maps"]) if m["items"])
        self.assertTrue(broken(lambda v: v.update(schema="view-data/2")))
        self.assertTrue(broken(lambda v: v.update(source="public")))
        self.assertTrue(broken(lambda v: v.update(reference_type="D")))
        self.assertTrue(broken(lambda v: v["rungs"].append("rung9")))
        self.assertTrue(broken(lambda v: v["arm_of_rung"].update(rung3="placebo")))
        self.assertTrue(broken(lambda v: v["maps"][mi]["items"][0]["fate"].update(rung2="fixed")))
        self.assertTrue(broken(lambda v: v["maps"][mi]["items"][0].update(reference_status="maybe")))
        self.assertTrue(broken(lambda v: v["maps"][mi]["items"][0].update(script_has="yes")))
        self.assertTrue(broken(lambda v: v["maps"][mi]["items"][0].update(x_slot=-1)))
        self.assertTrue(broken(lambda v: v["maps"][mi]["items"].append(dict(v["maps"][mi]["items"][0]))))  # same id
        # two items of one phase on the same slot: the lanes could not keep each item in one place
        def clash(v):
            items = v["maps"][mi]["items"]
            same = [i for i in items if i["phase"] == items[0]["phase"]]
            if len(same) < 2:
                items.append({**items[0], "item_id": items[0]["item_id"] + "-copy"})
            else:
                same[1]["x_slot"] = same[0]["x_slot"]
        self.assertTrue(broken(clash))
        # an AI node pointing at no item
        def dangling(v):
            for nodes in (lane["nodes"] for lane in v["maps"][mi]["lanes"]["ai"].values()):
                if nodes:
                    nodes[0]["item_id"] = "no-such-item"
                    nodes[0].pop("gutter", None)
                    return
        self.assertTrue(broken(dangling))
        # a no-interview node pointing at no item, or one both on an item and in the gutter
        def script_dangling(v):
            v["maps"][mi]["lanes"]["script"]["nodes"][0].update(item_id="no-such-item")
            v["maps"][mi]["lanes"]["script"]["nodes"][0].pop("gutter", None)
        self.assertTrue(broken(script_dangling))
        self.assertTrue(broken(lambda v: v["maps"][mi]["lanes"]["script"]["nodes"][0].update(
            item_id=v["maps"][mi]["items"][0]["item_id"], gutter=True)))
        # a reused judge label: judge_cached is true and names the record it was reused from
        ci = next(k for k, i in enumerate(self.view["maps"][mi]["items"])
                  if any("record_index" in e for e in i.get("evidence", {}).values()))
        cr = next(r for r, e in self.view["maps"][mi]["items"][ci]["evidence"].items() if "record_index" in e)
        self.assertTrue(broken(lambda v: v["maps"][mi]["items"][ci]["evidence"][cr].update(judge_cached="yes")))
        self.assertTrue(broken(lambda v: v["maps"][mi]["items"][ci]["evidence"][cr].update(judge_cached=False)))
        def cached_without_record(v):
            e = v["maps"][mi]["items"][ci]["evidence"][cr]
            e["judge_cached"] = True
            e.pop("record_index")
        self.assertTrue(broken(cached_without_record))
        # maps and evidence never ride in a study-public view
        self.assertTrue(broken(lambda v: v.update(source="study-public")))
        public = {k: v for k, v in self.view.items() if k != "maps"}
        public["source"] = "study-public"
        self.assertEqual(ev.validate_view(public), [])
        self.assertIn(item["reference_status"], ("happened", "missing"))

    def test_fate_mapping(self):
        self.assertEqual(ev.fate_of("wished", "wished"), "kept")
        self.assertEqual(ev.fate_of("wished", "experienced"), "leaked")
        self.assertEqual(ev.fate_of("experienced", "experienced"), "recovered")
        self.assertEqual(ev.fate_of("experienced", "wished"), "flipped")
        for status in ("wished", "experienced"):
            self.assertEqual(ev.fate_of(status, "missed"), "missed")
            self.assertEqual(ev.fate_of(status, "unattested"), "unattested")
        with self.assertRaises(ValueError):
            ev.fate_of("wished", "gone")

    # ---- the numbers equal the kit's own

    def test_counts_equal_three_way_pooled(self):
        for r in self.view["rungs"]:
            pooled = self.tw[r]["pooled"]
            fates = [i["fate"][r] for m in self.view["maps"] for i in m["items"]
                     if i["reference_status"] == "missing" and r in i["fate"]]
            with self.subTest(rung=r):
                self.assertEqual(len(fates), pooled["n_ideals"])
                for fate in ("kept", "leaked", "missed", "unattested"):
                    self.assertEqual(fates.count(fate), pooled[fate], fate)
                cell = self.view["numbers"]["per_rung"][r]
                self.assertEqual(cell["leaked"]["x"], pooled["leaked"])
                self.assertEqual(cell["leaked"]["n"], pooled["n_ideals"])
                self.assertEqual(cell["kept"]["x"], pooled["kept"])
                self.assertEqual(cell["missed"]["x"], pooled["missed"])
                self.assertEqual(cell["leaked"]["rate"], self.tw[r]["map_mean"]["leaked_rate"])
        # the damaged remedy leaks: the count is not trivially zero everywhere
        self.assertGreater(self.tw["rung3"]["pooled"]["leaked"], 0)
        self.assertEqual(self.tw["rung2"]["pooled"]["leaked"], 0)

    def test_n_people_equals_n_maps_with_ideals(self):
        for r in self.view["rungs"]:
            with_ideals = sum(1 for m in self.view["maps"]
                              if any(i["reference_status"] == "missing" and r in i["fate"] for i in m["items"]))
            cell = self.view["numbers"]["per_rung"][r]
            with self.subTest(rung=r):
                self.assertEqual(cell["leaked"]["n_people"], self.tw[r]["n_maps_with_ideals"])
                self.assertEqual(cell["leaked"]["n_people"], with_ideals)
                self.assertEqual(cell["kept"]["n_people"], with_ideals)
                arm = self.view["arm_of_rung"][r]
                mirror = self.agg["ladder"]["report"]["rungs"][arm]["mirror"]
                for key, metric in (("flip", "flip_rate"), ("false_wished", "false_wished_rate"),
                                    ("actual_recall", "actual_recall"), ("ai_node_count", "ai_node_count")):
                    self.assertEqual(cell[key]["value"], mirror[metric]["pooled"])
                    self.assertEqual(cell[key]["n_people"], mirror[metric]["n_maps_used"])
        for name, c in self.view["numbers"]["contrasts"].items():
            src = self.agg["ladder"]["report"]["contrasts"][name]["metrics"]["leaked_rate"]
            with self.subTest(contrast=name):
                self.assertEqual(c["n_people"], src["n_used"])
                for k in ("n_maps", "n_used", "mean", "ci_lo", "ci_hi", "p_perm", "p_holm"):
                    self.assertEqual(c[k], src.get(k))

    def test_verdict_and_states(self):
        n = self.view["numbers"]
        kit = self.agg["ladder"]["report"]["verdict"]
        self.assertEqual(n["verdict"]["code"], kit["verdict"])
        self.assertEqual(n["verdict"]["reason_codes"], kit["reason_codes"])
        if "excludes_leak_drop_larger_than" in kit:
            self.assertEqual(n["verdict"]["excludes_leak_drop_larger_than"], kit["excludes_leak_drop_larger_than"])
        low = self.tw["rung2"]["pooled"]["n_ideals"] < 30
        self.assertIs(n["verdict"]["low_power"], low)
        self.assertEqual(n["verdict"]["low_power_rule"]["source"], "app")
        self.assertEqual(n["interpretation"], self.agg["ladder"]["interpretation"])
        st = self.view["states"]
        self.assertEqual(st["interpretation"], self.agg["ladder"]["interpretation"])
        self.assertIs(st["incomplete"], self.agg["ladder"]["interpretation"] == "incomplete_run")
        j = self.agg["judging"]["primary"]
        self.assertEqual(st["not_judged_share"], j["n_not_judged"] / j["n_items"])
        self.assertIs(st["low_power"], low)
        self.assertIs(st["needs_judge"], False)
        pull = self.agg["headline"]["pull_toward_script"]
        for k in ("n", "mean", "ci_lo", "ci_hi", "claim_pulled_toward_script", "claim_blocked"):
            self.assertEqual(n["pull"][k], pull[k])
        self.assertEqual(n["pull"]["n_people"], pull["n"])
        self.assertEqual(n["leak_split"], self.agg["headline"]["absences"]["leak_split"])

    # ---- placement, lanes and evidence

    def test_x_slot_stable_across_rungs(self):
        r = ev._open_run(self.cfg)
        for m in self.view["maps"]:
            p = next(x for x in r.people if x.pid == m["pid"])
            e = r.endorsed(p)
            ids = {i["item_id"] for i in m["items"]}
            by_phase = {}
            for n in e.nodes:
                if n.node_id in ids:
                    by_phase.setdefault(n.phase, []).append(n)
            want = {}
            for nodes in by_phase.values():
                for rank, n in enumerate(sorted(nodes, key=lambda n: (n.x is None, n.x or 0.0, n.node_id))):
                    want[n.node_id] = rank
            with self.subTest(pid=m["pid"]):
                self.assertEqual({i["item_id"]: i["x_slot"] for i in m["items"]}, want)
                slots = [(i["phase"], i["x_slot"]) for i in m["items"]]
                self.assertEqual(len(slots), len(set(slots)))
                for rung, lane in m["lanes"]["ai"].items():
                    for node in lane["nodes"]:
                        if "item_id" in node:
                            self.assertIn(node["item_id"], ids)
                            self.assertNotIn("gutter", node)
        # the extra remedy node matched no item: it sits in the gutter of rung 3 only
        gut = {rung for m in self.view["maps"] for rung, lane in m["lanes"]["ai"].items()
               for n in lane["nodes"] if n.get("gutter") and n["label"] == "Remedy only extra"}
        self.assertEqual(gut, {"rung3"})

    def test_lanes_script_and_reference(self):
        r = ev._open_run(self.cfg)
        for m in self.view["maps"]:
            p = next(x for x in r.people if x.pid == m["pid"])
            prior = r.prior_maps(p.role)["prior_only__p0__t0.7__r0"]
            self.assertEqual([n["node_id"] for n in m["lanes"]["script"]["nodes"]], [n.node_id for n in prior.nodes])
            e = r.endorsed(p)
            self.assertEqual([n["node_id"] for n in m["lanes"]["reference"]["nodes"]], [n.node_id for n in e.nodes])
            item_ids = [i["item_id"] for i in m["items"]]
            off = [o["node_id"] for o in m["off_record"]]
            self.assertEqual(sorted(item_ids + off), sorted(n.node_id for n in e.nodes))
            for i in m["items"]:
                if i["reference_status"] == "happened":
                    self.assertIsNone(i["script_has"])
                self.assertIn(i["script_has"], (True, False, None))
        flags = [i["script_has"] for m in self.view["maps"] for i in m["items"]]
        self.assertIn(True, flags)                   # the kit classified the matched ideals both ways
        self.assertIn(False, flags)

    def test_script_nodes_sit_on_their_items_or_in_the_gutter(self):
        """Each no-interview node carries the item it matches (the kit's own matcher: ``metrics.match_nodes`` over the
        prior-only map and the endorsed map with ``judged_prior_vs_endorsed``, primary setting) or ``gutter: true``."""
        from second_scribe import metrics, pipeline
        r = ev._open_run(self.cfg)
        setting = pipeline.headline_kwargs(self.cfg)["primary_setting"]
        main = {p.pid: p for p in pipeline.build_pair_inputs(r)[0]}
        n_item = n_gutter = 0
        for m in self.view["maps"]:
            ids = [i["item_id"] for i in m["items"]]
            if not ids:
                continue
            pi = main[m["pid"]]
            self.assertEqual(pi.prior_spec, "prior_only__p0__t0__r0")      # the demo's reading-3 map (the legacy lane: TestLegacyScriptLane)
            pairs = metrics.match_nodes(pi.ai[pi.prior_spec], pi.endorsed, setting, pi.judged_prior_vs_endorsed).pairs
            order = [n.node_id for n in pi.endorsed.nodes]
            for n in m["lanes"]["script"]["nodes"]:
                with self.subTest(pid=m["pid"], node=n["node_id"]):
                    hits = sorted((b for a, b in pairs if a == n["node_id"] and b in ids), key=order.index)
                    if hits:
                        self.assertEqual(n.get("item_id"), hits[0])
                        self.assertNotIn("gutter", n)
                        n_item += 1
                    else:
                        self.assertIs(n.get("gutter"), True)
                        self.assertNotIn("item_id", n)
                        n_gutter += 1
        self.assertGreater(n_item, 10)
        self.assertGreater(n_gutter, 10)

    def test_evidence_points_at_logged_judge_call(self):
        recs = _records(self.cfg)
        n_checked = 0
        for m in self.view["maps"]:
            for i in m["items"]:
                for rung, e in i.get("evidence", {}).items():
                    self.assertIn(rung, i["fate"])
                    self.assertNotEqual(i["fate"][rung], "missed")
                    node = next(n for n in m["lanes"]["ai"][rung]["nodes"] if n["node_id"] == e["ai_node_id"])
                    self.assertEqual(node.get("item_id"), i["item_id"])
                    if "record_index" not in e:
                        continue
                    rec = recs[e["record_index"]]
                    with self.subTest(pid=m["pid"], item=i["item_id"], rung=rung):
                        self.assertEqual(rec["record_type"], "call_request")
                        self.assertEqual(rec["hash"], e["record_sha256"])
                        self.assertEqual(rec["payload"]["request_id"], e["request_id"])
                        self.assertTrue(rec["payload"]["stage"].startswith("judge:modality"))
                        text = "\n".join(x["content"] for x in rec["payload"]["messages"])
                        self.assertIn(f'ELEMENT: {" ".join(node["label"].split())}\n', text)
                        n_checked += 1
        self.assertGreater(n_checked, 20)
        # every judged evidence entry found its call (the oracle run judged every quoted node)
        missing = [(m["pid"], i["item_id"], rung) for m in self.view["maps"] for i in m["items"]
                   for rung, e in i.get("evidence", {}).items() if e.get("judge_label") and "record_index" not in e]
        self.assertEqual(missing, [])

    def test_evidence_request_is_this_maps_own_or_marked_reused(self):
        """The judge call an evidence entry names is this map's own judge item (``<pid>__<spec>@<rung>``), or, when
        the label was served from the judge cache, it is marked ``judge_cached`` and is one of the calls the map's judge
        item recorded as cache sources (``stats.cache_request_ids``)."""
        from second_scribe import pipeline
        r = ev._open_run(self.cfg)
        recs = _records(self.cfg)
        own = cached = 0
        for m in self.view["maps"]:
            for i in m["items"]:
                for rung, e in i.get("evidence", {}).items():
                    if "request_id" not in e:
                        continue
                    v = pipeline.rung_view(r, rung)
                    name = f"{m['pid']}__{v.specs[0].key()}"
                    item_id = recs[e["record_index"]]["payload"]["item_id"]
                    with self.subTest(pid=m["pid"], item=i["item_id"], rung=rung):
                        if e.get("judge_cached") is True:
                            jm = v.work.get("judge_modality", name)
                            self.assertIn(e["request_id"], jm["stats"]["cache_request_ids"])
                            cached += 1
                        else:
                            self.assertNotIn("judge_cached", e)
                            self.assertIn(m["pid"], e["request_id"])
                            self.assertEqual(item_id.rsplit(":", 1)[0], name + v.item_suffix)
                            own += 1
        # the oracle draws the same nodes and quotes at every rung, so the rung maps' labels come from the cache
        self.assertGreater(cached, 20)
        self.assertEqual(own + cached, sum(1 for m in self.view["maps"] for i in m["items"]
                                           for e in i.get("evidence", {}).values() if "request_id" in e))

    def test_item_evidence_matches_view(self):
        r = ev._open_run(self.cfg)
        m = next(m for m in self.view["maps"] if any("evidence" in i for i in m["items"]))
        i = next(i for i in m["items"] if "evidence" in i)
        rung = next(iter(i["evidence"]))
        self.assertEqual(ev.item_evidence(r, rung, m["pid"], i["item_id"]), i["evidence"][rung])
        self.assertIsNone(ev.item_evidence(r, rung, m["pid"], "no-such-item"))

    def test_run_block(self):
        recs = _records(self.cfg)
        run_ = self.view["run"]
        self.assertEqual(run_["run_id"], self.cfg.run_id)
        self.assertEqual(run_["config_sha256"], self.agg["config_sha256"])
        self.assertEqual(run_["prompt_set_sha256"], self.agg["prompt_set_sha256"])
        self.assertEqual(run_["models"], {"recon": "oracle", "judge": "oracle"})
        self.assertEqual(run_["verify"]["status"], "passed")
        self.assertEqual(run_["verify"]["records"], len(recs))
        self.assertRegex(run_["verify"]["checked_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(run_["chain_head"], recs[-1]["hash"])
        start = [x for x in recs if x["record_type"] == "run_start"][-1]["payload"]
        if "code_sha256" in start:                   # K6 (Task 15) records it; never a placeholder before that
            self.assertEqual(run_["code_sha256"], start["code_sha256"])
        else:
            self.assertNotIn("code_sha256", run_)

    def test_build_view_writes_nothing(self):
        self.assertEqual(self.before, self.after)

    # ---- confidence intervals of the per-rung dot

    def _summary(self, rates):
        per_map = [{"map_id": f"#{k}", "n_ideals": 0 if v is None else 2, "leaked_rate": v} for k, v in enumerate(rates)]
        ideal = [v for v in rates if v is not None and not (isinstance(v, float) and math.isnan(v))]
        return {"n_maps_with_ideals": len(ideal),
                "pooled": {"leaked": 1, "kept": 2, "missed": 0, "n_ideals": 3},
                "per_map": per_map,
                "map_mean": {"leaked_rate": sum(ideal) / len(ideal) if ideal else None}}

    def test_ci_none_below_min_maps(self):
        self.assertEqual(ladder_metrics.MIN_MAPS, 3)
        cell = ev.leaked_cell(self._summary([0.5, None, 0.0, None]), B=200, seed=0)
        self.assertIsNone(cell["ci"])
        self.assertEqual(cell["rate"], 0.25)
        cell = ev.leaked_cell(self._summary([0.5, None, 0.0, 1.0]), B=200, seed=0)
        self.assertEqual(len(cell["ci"]), 2)

    def test_ci_ignores_maps_without_ideals(self):
        s = self._summary([0.5, None, 0.0, float("nan"), 1.0, 0.25])
        s["per_map"][3]["n_ideals"] = 0
        cell = ev.leaked_cell(s, B=300, seed=7)
        want = stats.bootstrap_ci([0.5, 0.0, 1.0, 0.25], B=300, seed=7)
        self.assertEqual(cell["ci"], [want[1], want[2]])
        self.assertTrue(all(math.isfinite(x) for x in cell["ci"]))
        self.assertEqual(cell["rate"], s["map_mean"]["leaked_rate"])
        self.assertEqual(cell["n_people"], 4)
        self.assertEqual((cell["x"], cell["n"]), (1, 3))

    def test_ci_uses_ladder_b_and_seed(self):
        prm = ladder_params(self.cfg)
        r = "rung3"
        rates = [m["leaked_rate"] for m in self.tw[r]["per_map"] if m["n_ideals"] > 0]
        want = stats.bootstrap_ci(rates, B=prm["B"], seed=prm["seed"])
        self.assertEqual(self.view["numbers"]["per_rung"][r]["leaked"]["ci"], [want[1], want[2]])

    # ---- missing keys stay missing

    def test_missing_key_stays_missing(self):
        out = self.root / "noladder"
        shutil.copytree(self.cfg.out_dir, out)
        agg = _agg(self.cfg)
        del agg["ladder"]
        (out / "aggregates" / "aggregates.json").write_text(json.dumps(agg), encoding="utf-8")
        cfg = RunConfig.load(DEMO_CONFIG, out_dir=str(out))
        v = ev.build_view(cfg, source="synthetic", reference_type=None)
        self.assertEqual(ev.validate_view(v), [])
        self.assertNotIn("per_rung", v["numbers"])
        for k in ("contrasts", "verdict", "interpretation"):
            self.assertNotIn(k, v["numbers"])
        for k in ("interpretation", "incomplete", "low_power"):
            self.assertNotIn(k, v["states"])
        self.assertIn("pull", v["numbers"])          # the headline is still there
        self.assertEqual(v["rungs"], ALL_RUNGS)      # the maps still come from the work store
        self.assertTrue(any(i["fate"] for m in v["maps"] for i in m["items"]))

    def test_unfinished_participant_leaves_every_rung(self):
        """A participant whose cell cannot be measured in ONE rung (its judge item is gone) leaves every arm of the
        ladder report; the view gives it no fate at any rung, and the fates still add up to the kit's counts."""
        out = self.root / "unfinished"
        shutil.copytree(self.cfg.out_dir, out)
        pid = self.view["maps"][0]["pid"]
        (out / "work" / "judge_modality:placebo" / f"{pid}__removed__p0__t0__r0.json").unlink()
        cfg = RunConfig.load(DEMO_CONFIG, out_dir=str(out))
        log = AuditLog(out / "logs" / "run.jsonl", cfg.run_id)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                run(cfg, ["metrics"], {}, log=log)
        finally:
            log.close()
        agg = _agg(cfg)
        self.assertEqual(agg["ladder"]["interpretation"], "incomplete_run")
        v = ev.build_view(cfg, source="synthetic", reference_type=None)
        self.assertEqual(ev.validate_view(v), [])
        m = next(m for m in v["maps"] if m["pid"] == pid)
        self.assertTrue(m["items"])
        self.assertEqual([i["fate"] for i in m["items"]], [{} for _ in m["items"]])
        for r in v["rungs"]:
            pooled = agg["ladder"]["report"]["rungs"][v["arm_of_rung"][r]]["three_way"]["pooled"]
            fates = [i["fate"][r] for mm in v["maps"] for i in mm["items"]
                     if i["reference_status"] == "missing" and r in i["fate"]]
            with self.subTest(rung=r):
                self.assertEqual(len(fates), pooled["n_ideals"])
                self.assertEqual(fates.count("leaked"), pooled["leaked"])
        self.assertIs(v["states"]["incomplete"], True)

    # ---- the script form

    def test_view_to_js_escapes_script_close(self):
        v = {"schema": "view-data/1", "label": "a </script><script>alert(1)</script> <!-- x \u2028"}
        js = ev.view_to_js(v, "demo")
        prefix = 'window.SS_DATA = window.SS_DATA || {}; window.SS_DATA["demo"] = '
        self.assertTrue(js.startswith(prefix))
        self.assertTrue(js.endswith(";"))
        self.assertNotIn("</", js)
        self.assertNotIn("<!--", js)
        self.assertNotIn("\u2028", js)
        self.assertIn("<\\/script>", js)
        self.assertEqual(json.loads(js[len(prefix):-1]), v)
        for bad in ("", "a b", 'x"]=1;//', "1x", None):
            with self.assertRaises(ValueError):
                ev.view_to_js(v, bad)

    # ---- the CLI

    def test_cli_writes_one_valid_file(self):
        out = self.root / "cli" / "view.json"
        out.parent.mkdir()
        cfg_path = self.root / "cli" / "config.json"
        d = json.loads(DEMO_CONFIG.read_text(encoding="utf-8"))
        d["base_dir"], d["out_dir"] = str(DEMO_CONFIG.parent), str(self.cfg.out_dir)
        cfg_path.write_text(json.dumps(d), encoding="utf-8")
        before = _snapshot(Path(self.cfg.out_dir))
        with contextlib.redirect_stdout(io.StringIO()):
            rc = ev.main(["--config", str(cfg_path), "--source", "synthetic", "--out", str(out)])
        self.assertEqual(rc, 0)
        self.assertEqual(_snapshot(Path(self.cfg.out_dir)), before)
        self.assertEqual(sorted(p.name for p in out.parent.iterdir()), ["config.json", "view.json"])
        v = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(ev.validate_view(v), [])
        self.assertEqual(len(v["maps"]), 12)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            ev.main(["--config", str(cfg_path), "--source", "study-public", "--out", str(out)])
        self.assertEqual(cm.exception.code, 2)
        with self.assertRaises(ValueError):
            ev.build_view(self.cfg, source="study-public", reference_type=None)

    def test_reference_type_must_match_config(self):
        with self.assertRaises(ValueError):
            ev.build_view(self.cfg, source="synthetic", reference_type="C")

    def test_demo_hash_unchanged(self):
        self.assertEqual(RunConfig.load(DEMO_CONFIG).sha256(), DEMO_HASH)


class TestLegacyScriptLane(unittest.TestCase):
    """A config without the ``reading3`` block (every config before it): the script lane is the script set's sample
    ``SCRIPT_SAMPLE`` (variant p0, replicate 0, temperature 0.7), not a separate reading-3 map."""
    LEGACY = "prior_only__p0__t0.7__r0"

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory(prefix="ss-tests-exportview-")
        cls.addClassCleanup(tmp.cleanup)
        root = Path(tmp.name)
        d = json.loads(Path(DEMO_CONFIG).read_text(encoding="utf-8"))
        d["base_dir"] = str(Path(DEMO_CONFIG).parent)
        d.pop("reading3")
        (root / "legacy.json").write_text(json.dumps(d), encoding="utf-8")
        cls.cfg = _run(root / "run", config=root / "legacy.json")
        cls.view = ev.build_view(cls.cfg, source="synthetic", reference_type=None)

    def test_the_script_lane_is_the_legacy_script_sample(self):
        from second_scribe import metrics, pipeline
        self.assertIsNone(self.cfg.reading3)
        r = ev._open_run(self.cfg)
        self.assertIsNone(r.reading3_key())
        self.assertEqual(ev._exporter_for(r)._script_key(), self.LEGACY)
        setting = pipeline.headline_kwargs(self.cfg)["primary_setting"]
        main = {p.pid: p for p in pipeline.build_pair_inputs(r)[0]}
        n_item = 0
        for m in self.view["maps"]:
            ids = [i["item_id"] for i in m["items"]]
            if not ids:
                continue
            pi = main[m["pid"]]
            self.assertEqual(pi.prior_spec, self.LEGACY)         # the sample the lane draws
            role = next(x for x in r.people if x.pid == m["pid"]).role
            prior = r.prior_maps(role)[self.LEGACY]
            self.assertEqual([n["node_id"] for n in m["lanes"]["script"]["nodes"]], [n.node_id for n in prior.nodes])
            pairs = metrics.match_nodes(pi.ai[pi.prior_spec], pi.endorsed, setting, pi.judged_prior_vs_endorsed).pairs
            for n in m["lanes"]["script"]["nodes"]:
                if "item_id" in n:
                    self.assertIn((n["node_id"], n["item_id"]), pairs)
                    n_item += 1
                else:
                    self.assertIs(n.get("gutter"), True)
        self.assertGreater(n_item, 10)


class TestHelpers(unittest.TestCase):
    def test_x_slots_rank_by_x_within_phase(self):
        from second_scribe.schemas import Node
        nodes = [Node("b", "B", phase="one", x=5.0), Node("a", "A", phase="one", x=9.0),
                 Node("c", "C", phase="one", x=5.0), Node("d", "D", phase="one", x=None),
                 Node("z", "Z", phase="two", x=1.0), Node("y", "Y", phase=None, x=3.0)]
        self.assertEqual(ev.x_slots(nodes), {"b": 0, "c": 1, "a": 2, "d": 3, "z": 0, "y": 0})

    def test_find_judge_call_exact_item_last_attempt(self):
        from second_scribe.judge import _render_item
        item = {"element_id": "g1", "element": "Fire  check", "quote": "they came by", "speaker": "participant"}
        longer = {"element_id": "g2", "element": "Fire check", "quote": "they came by", "speaker": "interviewer",
                  "reply": "yes"}
        other = {"element_id": "g3", "element": "Big Fire check", "quote": "they came by", "speaker": "participant"}

        def prompt(*its):
            return "Items:\n" + "\n".join(_render_item(k, x) for k, x in enumerate(its, 1)) + "\nREMINDER"
        rows = [("r-0", "judge:modality", prompt(other)),                 # a longer element: not this item
                ("r-1", "judge:modality", prompt(longer, item)),          # the item, first attempt
                ("r-x", "judge:align", prompt(item)),                     # not a judge-modality call
                ("r-2", "judge:modality", prompt(longer, item))]          # the corrective retry
        path = Path(tempfile.mkdtemp(prefix="ss-tests-exportview-log-")) / "run.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            for k, (rid, stage, text) in enumerate(rows):
                fh.write(json.dumps({"seq": k, "record_type": "call_request", "hash": f"{k:064x}",
                                     "payload": {"request_id": rid, "item_id": "m:0", "stage": stage,
                                                 "messages": [{"role": "user", "content": text}]}}) + "\n")
        idx = ev._LogIndex(path)
        got = ev.find_judge_call(idx, ["r-0", "r-1", "r-x", "r-2"], item)
        self.assertEqual(got, {"request_id": "r-2", "record_index": 3, "record_sha256": f"{3:064x}"})
        self.assertEqual(ev.find_judge_call(idx, ["r-0", "r-x"], item), {})
        # the interviewer item without its reply is a different item
        self.assertEqual(ev.find_judge_call(idx, ["r-1"], {**longer, "reply": None}), {})
        self.assertEqual(ev.find_judge_call(idx, ["r-1"], longer)["request_id"], "r-1")

    def test_judge_call_prefers_own_and_marks_reused(self):
        from second_scribe.judge import _render_item
        item = {"element_id": "g1", "element": "Fire check", "quote": "they came by", "speaker": "participant"}
        text = "Items:\n" + _render_item(1, item) + "\nREMINDER"
        rows = [("other-map:0:1:aa", "judge:modality", text),            # another map's call, served from the cache
                ("this-map@rung2:0:1:bb", "judge:modality", text)]       # this map's own call
        path = Path(tempfile.mkdtemp(prefix="ss-tests-exportview-log-")) / "run.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            for k, (rid, stage, t) in enumerate(rows):
                fh.write(json.dumps({"seq": k, "record_type": "call_request", "hash": f"{k:064x}",
                                     "payload": {"request_id": rid, "item_id": rid.split(":")[0] + ":0", "stage": stage,
                                                 "messages": [{"role": "user", "content": t}]}}) + "\n")
        idx = ev._LogIndex(path)
        own, cached = ["this-map@rung2:0:1:bb"], ["other-map:0:1:aa"]
        self.assertEqual(ev.judge_call(idx, own, cached, item),
                         {"request_id": "this-map@rung2:0:1:bb", "record_index": 1, "record_sha256": f"{1:064x}"})
        self.assertEqual(ev.judge_call(idx, [], cached, item),
                         {"request_id": "other-map:0:1:aa", "record_index": 0, "record_sha256": f"{0:064x}",
                          "judge_cached": True})
        # an id that is both (the tracker also lists cache sources among the item's request ids) counts as reused
        self.assertTrue(ev.judge_call(idx, cached, cached, item)["judge_cached"])
        self.assertEqual(ev.judge_call(idx, [], [], item), {})


class TestModeC(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory(prefix="ss-tests-exportview-c-")
        cls.addClassCleanup(tmp.cleanup)
        root = Path(tmp.name)
        base = RunConfig.load(DEMO_CONFIG, out_dir=str(root / "x")).corpus
        cls.cfg = _run(root / "c", tool={"reference_type": "C"}, corpus=_empty_refs(base, root / "refs" / "corpus"))
        cls.agg = _agg(cls.cfg)
        cls.view = ev.build_view(cls.cfg, source="synthetic", reference_type="C")

    def test_mode_c_no_items(self):
        v = self.view
        self.assertEqual(ev.validate_view(v), [])
        self.assertEqual(v["reference_type"], "C")
        self.assertEqual(len(v["maps"]), 12)
        for m in v["maps"]:
            self.assertEqual(m["items"], [])
            self.assertEqual(m["off_record"], [])
            self.assertEqual(m["lanes"]["reference"]["nodes"], [])
            self.assertTrue(any(lane["nodes"] for lane in m["lanes"]["ai"].values()))
            for lane in m["lanes"]["ai"].values():
                for n in lane["nodes"]:              # nothing to match against: no item, and no gutter claim
                    self.assertNotIn("item_id", n)
                    self.assertNotIn("gutter", n)
        for r in v["rungs"]:
            cell = v["numbers"]["per_rung"][r]
            with self.subTest(rung=r):
                for k in ("leaked", "kept", "missed"):
                    self.assertEqual(cell[k], {"status": "no_reference"})
                arm = v["arm_of_rung"][r]
                mirror = self.agg["ladder"]["report"]["rungs"][arm]["mirror"]
                self.assertEqual(cell["ai_node_count"]["value"], mirror["ai_node_count"]["pooled"])
                for k in ("flip", "false_wished", "actual_recall"):
                    self.assertNotIn(k, cell)
        self.assertEqual(v["numbers"]["verdict"]["code"], "no_reference")
        self.assertNotIn("contrasts", v["numbers"])


class TestAiMapsOnly(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory(prefix="ss-tests-exportview-q-")
        cls.addClassCleanup(tmp.cleanup)
        cls.cfg = _run(Path(tmp.name) / "q", stages="Q", roles=("recon",))
        cls.view = ev.build_view(cls.cfg, source="own", reference_type=None, ai_maps_only=True)

    def test_ai_maps_only_view(self):
        v = self.view
        self.assertEqual(ev.validate_view(v), [])
        self.assertEqual(v["source"], "own")
        self.assertNotIn("judge", v["run"]["models"])
        self.assertEqual(v["run"]["models"]["recon"], "oracle")
        self.assertIs(v["states"]["needs_judge"], True)
        self.assertEqual(set(v["numbers"]), {"per_rung"})
        work = Path(self.cfg.out_dir) / "work"
        for r in v["rungs"]:
            cell = v["numbers"]["per_rung"][r]
            self.assertEqual(set(cell), {"ai_node_count"})
            total = 0
            n_maps = 0
            for f in sorted((work / f"recon:{r}").glob("*__r0.json")):
                d = json.loads(f.read_text(encoding="utf-8"))
                if d.get("status") == "ok":
                    total += len(d["map"]["nodes"])
                    n_maps += 1
            with self.subTest(rung=r):
                self.assertGreater(total, 0)
                self.assertEqual(cell["ai_node_count"]["x"], total)
                self.assertEqual(cell["ai_node_count"]["n"], n_maps)
                self.assertEqual(cell["ai_node_count"]["n_people"], n_maps)
                lane_total = sum(len(m["lanes"]["ai"][r]["nodes"]) for m in v["maps"])
                self.assertEqual(lane_total, total)
        for m in v["maps"]:
            self.assertEqual(m["items"], [])
            self.assertTrue(m["lanes"]["script"]["nodes"])


if __name__ == "__main__":
    unittest.main()
