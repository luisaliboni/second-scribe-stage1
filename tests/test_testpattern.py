"""Task 21: the labelled test pattern (``second_scribe.app.testpattern``).

Offline only: ``build_view`` over the oracle demo run (gold labels, no model), once plain (the shippable pattern) and
once with the dev-only ``DriftingOracle`` wrapped round the reconstructor. Nothing here calls a model.
"""
import contextlib
import hashlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from second_scribe import export_view as ev
from second_scribe.app import testpattern as tp
from second_scribe.pipeline import KIT_ROOT

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "second_scribe" / "app" / "static"
SITE_DATA = ROOT / "site" / "data"
ALL_FATES = {"kept", "leaked", "recovered", "flipped", "missed", "unattested"}
RUNGS = ["rung1", "rung2", "placebo", "rung3"]
PAYLOAD = re.compile(r'window\.SS_DATA\["[A-Za-z_][A-Za-z0-9_]*"\]\s*=\s*(.*);\s*$', re.S)


def fates_of(view: dict, rung: str) -> list:
    return [it["fate"].get(rung) for m in view["maps"] for it in m["items"]]


def payload_of(path: Path):
    """The JSON object a ``view_to_js`` data file assigns, or None when the file is not of that form."""
    m = PAYLOAD.search(path.read_text(encoding="utf-8"))
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


class TestTestPattern(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ship = tp.make_test_pattern(drift=False)
        cls.drift = tp.make_test_pattern(drift=True)

    def test_flag_set(self):
        for v in (self.ship, self.drift):
            self.assertIs(v["test_pattern"], True)
            self.assertEqual(v["source"], "synthetic")
            self.assertEqual(v["schema"], "view-data/1")

    def test_valid_view(self):
        self.assertEqual(ev.validate_view(self.ship), [])
        self.assertEqual(ev.validate_view(self.drift), [])

    def test_pattern_has_every_fate(self):
        v = self.drift
        seen = {f for r in v["rungs"] for f in fates_of(v, r)}
        self.assertEqual(seen, ALL_FATES)
        self.assertEqual(v["rungs"], RUNGS)
        # the rungs differ from each other (every pair), so a rung switch visibly changes the AI lane
        vecs = {r: fates_of(v, r) for r in v["rungs"]}
        for i, a in enumerate(RUNGS):
            for b in RUNGS[i + 1:]:
                with self.subTest(a=a, b=b):
                    self.assertNotEqual(vecs[a], vecs[b])

    def test_pattern_has_a_verdict_and_mixed_maps(self):
        n = self.drift["numbers"]
        self.assertNotEqual(n["verdict"]["code"], "no_reference")
        leaked = {r: n["per_rung"][r]["leaked"] for r in RUNGS}
        self.assertTrue(any(c.get("x", 0) > 0 for c in leaked.values()), leaked)

    def test_shippable_pattern_has_no_drift(self):
        v = self.ship
        self.assertNotIn("dev_only", v)
        for r in v["rungs"]:
            self.assertFalse({"leaked", "flipped"} & set(fates_of(v, r)), r)
        # the oracle reads only the transcript: every rung gives the same fates
        first = fates_of(v, v["rungs"][0])
        for r in v["rungs"][1:]:
            self.assertEqual(fates_of(v, r), first)

    def test_drift_pattern_marked_dev_only(self):
        v = self.drift
        self.assertIs(v["dev_only"], True)
        self.assertIs(v["test_pattern"], True)
        self.assertEqual(v["run"]["models"]["recon"], "test-pattern")
        self.assertNotIn("oracle", json.dumps(v["run"]["models"]))

    def test_no_local_paths_in_the_data(self):
        for v in (self.ship, self.drift):
            text = json.dumps(v)
            for bad in ("/Users/", "/home/", "ss-tests", "/var/folders", "/tmp/"):
                self.assertNotIn(bad, text)

    def test_cli_writes_one_js_file(self):
        with tempfile.TemporaryDirectory(prefix="ss-tests-testpattern-") as d:
            out = Path(d) / "tryit.js"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = tp.main(["--drift", "--out", str(out)])
            self.assertEqual(rc, 0)
            text = out.read_text(encoding="utf-8")
            self.assertTrue(text.startswith('window.SS_DATA = window.SS_DATA || {};'))
            self.assertIn('window.SS_DATA["tryit"] = ', text)
            doc = payload_of(out)
            self.assertIs(doc["dev_only"], True)
            self.assertEqual(ev.validate_view(doc), [])
            out2 = Path(d) / "ship.js"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(tp.main(["--out", str(out2)]), 0)
            self.assertNotIn("dev_only", payload_of(out2))

    def test_cli_requires_out(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            tp.main([])


class TestDriftingOracle(unittest.TestCase):
    """The wrapper alone, over a stub inner client (no corpus, no run)."""

    PROMPTS = KIT_ROOT / "prompts"

    class Stub:
        model_label = "oracle"

        def __init__(self, reply):
            self.reply = reply
            self.calls = 0

        def complete(self, messages, **kw):
            from second_scribe.llm import ModelResponse
            self.calls += 1
            return ModelResponse(text=json.dumps(self.reply), finish_reason="stop", usage={}, latency_ms=0,
                                 raw_sha256="0" * 64)

    @staticmethod
    def reply(n_wished=6, n_exp=4):
        nodes = [{"id": f"w{i}", "label": f"wished {i}", "type": "service", "phase": "P", "status": "wished",
                  "quote": f"q {i}"} for i in range(n_wished)]
        nodes += [{"id": f"e{i}", "label": f"exp {i}", "type": "service", "phase": "P", "status": "experienced",
                   "quote": f"q e{i}"} for i in range(n_exp)]
        return {"nodes": nodes, "edges": [{"source": "w0", "target": "e0", "status": "experienced", "quote": "q"}],
                "emotion_word": None}

    def prompt(self, name, transcript="[P] hello there"):
        text = (self.PROMPTS / name).read_text(encoding="utf-8")
        for k in ("role", "domain", "phases", "schema", "definitions"):
            text = text.replace("{" + k + "}", k)
        return text.replace("{transcript}", transcript)

    def test_rung_of_prompt(self):
        want = {"reconstruct.p0.v1.txt": "rung1", "reconstruct.p0.v2.txt": "rung2",
                "reconstruct.p0.placebo.v1.txt": "placebo", "reconstruct.p0.remedy.v1.txt": "rung3"}
        for name, rung in want.items():
            with self.subTest(name=name):
                self.assertEqual(tp.rung_of_prompt(self.prompt(name)), rung)
        self.assertIsNone(tp.rung_of_prompt("LIST A:\n1. x\nLIST B:\n1. y"))
        self.assertIsNone(tp.rung_of_prompt(self.prompt("prior_only.p0.v2.txt")))

    def call(self, client, name, transcript="[P] hello there"):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": self.prompt(name, transcript)}]
        return json.loads(client.complete(msgs, temperature=0.0, seed=1, max_tokens=100).text)

    def test_deterministic_and_marked(self):
        a = tp.DriftingOracle(self.Stub(self.reply()))
        b = tp.DriftingOracle(self.Stub(self.reply()))
        self.assertEqual(a.model_label, "test-pattern")
        for name in ("reconstruct.p0.v1.txt", "reconstruct.p0.remedy.v1.txt"):
            self.assertEqual(self.call(a, name), self.call(b, name))
            self.assertEqual(self.call(a, name), self.call(a, name))

    def test_flips_wished_to_experienced_and_one_experienced_to_wished(self):
        base = self.reply(n_wished=40, n_exp=10)
        client = tp.DriftingOracle(self.Stub(base))
        counts = {}
        for name, rung in (("reconstruct.p0.v1.txt", "rung1"), ("reconstruct.p0.v2.txt", "rung2"),
                           ("reconstruct.p0.placebo.v1.txt", "placebo"), ("reconstruct.p0.remedy.v1.txt", "rung3")):
            got = self.call(client, name)
            status = {n["id"]: n["status"] for n in got["nodes"]}
            flipped = [i for i in status if i.startswith("w") and status[i] == "experienced"]
            raised = [i for i in status if i.startswith("e") and status[i] == "wished"]
            self.assertEqual(len(raised), 1, rung)
            counts[rung] = len(flipped)
            self.assertEqual([n["id"] for n in got["nodes"]], [n["id"] for n in base["nodes"]])
            self.assertEqual([n["quote"] for n in got["nodes"]], [n["quote"] for n in base["nodes"]])
        # the seeded shares (rung1 40%, rung2 30%, placebo 30%, rung3 15% of 40 wished nodes), within sampling slack
        for rung, share in tp.DRIFT_SHARES.items():
            self.assertLessEqual(abs(counts[rung] - share * 40), 8, (rung, counts))
        self.assertGreater(counts["rung1"], counts["rung3"])

    def test_other_calls_pass_through(self):
        inner = self.Stub({"labels": [{"n": 1, "modality": "actual"}]})
        client = tp.DriftingOracle(inner)
        msgs = [{"role": "user", "content": 'ITEMS:\n1. x\nReturn {"labels": [...]}'}]
        out = client.complete(msgs, temperature=0.0, seed=1, max_tokens=10)
        self.assertEqual(json.loads(out.text), {"labels": [{"n": 1, "modality": "actual"}]})
        self.assertEqual(inner.calls, 1)
        # a prior-only (no-interview) prompt is not drifted either
        inner2 = self.Stub(self.reply())
        got = self.call(tp.DriftingOracle(inner2), "prior_only.p0.v2.txt")
        self.assertEqual(got, self.reply())


class TestEveryDataFileFlagged(unittest.TestCase):
    def view_files(self):
        out = []
        for pat in ("second_scribe/app/static/**/data/*.js", "site/data/*.js"):
            for f in sorted(ROOT.glob(pat)):
                doc = payload_of(f)
                if isinstance(doc, dict) and str(doc.get("schema", "")).startswith("view-data/"):
                    out.append((f, doc))
        return out

    def test_every_data_file_flagged(self):
        files = self.view_files()
        self.assertTrue(files, "no view-data files found")
        sources_path = SITE_DATA / "SOURCES.json"
        sources = json.loads(sources_path.read_text(encoding="utf-8")) if sources_path.exists() else {}
        for f, doc in files:
            rel = f.relative_to(ROOT).as_posix()
            with self.subTest(file=rel):
                if doc.get("test_pattern") is True:
                    continue
                self.assertTrue(rel.startswith("site/data/"), f"{rel}: not flagged test_pattern")
                entry = sources.get(f.stem)
                self.assertIsInstance(entry, dict, f"{rel}: no entry in site/data/SOURCES.json")
                self.assertIs(entry.get("recorded"), True)
                self.assertEqual(entry.get("sha256"), hashlib.sha256(f.read_bytes()).hexdigest())

    def test_dev_only_data_never_in_site(self):
        for f, doc in self.view_files():
            if doc.get("dev_only") is True:
                self.assertTrue(f.relative_to(ROOT).as_posix().startswith("second_scribe/app/static/dev/"), f)

    def test_dev_tryit_is_the_drift_pattern(self):
        f = STATIC / "dev" / "data" / "tryit.js"
        self.assertTrue(f.exists(), "run: python3 -m second_scribe.app.testpattern --drift --out " + str(f))
        doc = payload_of(f)
        self.assertIs(doc["dev_only"], True)
        self.assertIs(doc["test_pattern"], True)
        self.assertEqual(ev.validate_view(doc), [])
        seen = {fate for m in doc["maps"] for it in m["items"] for fate in it["fate"].values()}
        self.assertEqual(seen, ALL_FATES)


if __name__ == "__main__":
    unittest.main()
