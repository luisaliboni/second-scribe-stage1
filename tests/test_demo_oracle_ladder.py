"""Wire-b: the oracle client answers the prompts of every rung of the instruction ladder (rung 1, rung 2, the remedy and
the placebo) from the synthetic gold, so the demo can run the ladder offline. It answers the same gold map for every
rung (the oracle has no wording to react to): the demo tests the code path, not the remedy."""
import json
import tempfile
import unittest
from pathlib import Path

from second_scribe import ladder
from second_scribe.cli import DEMO_CONFIG, build_clients
from second_scribe.pipeline import PROMPTS_DIR, RunConfig, _Run
from second_scribe.audit.registry import AuditLog
from second_scribe.reconstruct import ReconSpec, build_messages


class TestOracleAnswersEveryRung(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="ss-orc-")
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.cfg = RunConfig.load(DEMO_CONFIG, out_dir=str(Path(cls._tmp.name) / "out"))
        cls.clients = build_clients("oracle", cls.cfg)
        cls.oracle = cls.clients["recon"]
        Path(cls.cfg.out_dir).mkdir(parents=True)
        cls.log = AuditLog(Path(cls.cfg.out_dir) / "r.jsonl", "r")
        cls.addClassCleanup(cls.log.close)
        cls.r = _Run(cls.cfg, cls.clients, cls.log)

    def answer(self, rung_key, pid, arm="removed"):
        p = next(x for x in self.r.people if x.pid == pid)
        t = self.r.rule_flagged(p)
        messages = ladder.rung_messages(PROMPTS_DIR, ladder.get_rung(rung_key), self.r.ctx(p.role), t.text(arm))
        resp = self.oracle.complete(messages, temperature=0.0, seed=1, max_tokens=8000)
        return json.loads(resp.text)

    def test_every_rung_gets_the_gold_map_not_the_generic_pathway(self):
        for pid in ("SYN-OW01", "SYN-OF03"):
            main = self.answer("rung2", pid)
            self.assertGreater(len(main["nodes"]), 3)
            self.assertTrue(any(n["quote"] for n in main["nodes"]))
            for key in ("rung1", "rung3", "placebo"):
                with self.subTest(pid=pid, rung=key):
                    self.assertEqual(self.answer(key, pid), main)

    def test_the_full_arm_too(self):
        main = self.answer("rung2", "SYN-OW02", "full")
        for key in ("rung1", "rung3", "placebo"):
            self.assertEqual(self.answer(key, "SYN-OW02", "full"), main)


if __name__ == "__main__":
    unittest.main()
