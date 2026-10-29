"""Ladder item 4: the dated `declared_contrasts` record in the hash-chained log (a record, not a lock: L6)."""
import json
import os
import tempfile
import unittest

from second_scribe.audit.registry import DECLARED_CONTRASTS, AuditLog, RECORD_TYPES
from second_scribe.audit.verify import verify_internal_chain, verify_run
from second_scribe.ladder_metrics import (PRIMARY_CONTRASTS, analysis_config, compare_to_declaration, declare_contrasts,
                                          read_declarations)


def clock():
    n = {"i": 0}

    def f():
        n["i"] += 1
        return f"2026-10-14T10:00:{n['i']:02d}+00:00"
    return f


class TestDeclaredContrasts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "run.jsonl")
        self.log = AuditLog(self.path, "run-1", clock=clock())

    def declare(self, **kw):
        args = dict(rung="remedy", contrasts=["remedy-placebo", "placebo-rung2"], outcomes=["leaked_rate", "false_wished_rate"],
                    draws={"remedy": 3, "placebo": 3, "rung2": 3}, n_perm=10000, seed=7, B=2000, corpus_kind="real")
        args.update(kw)
        return declare_contrasts(self.log, **args)

    def test_type_is_registered(self):
        self.assertEqual(DECLARED_CONTRASTS, "declared_contrasts")
        self.assertIn(DECLARED_CONTRASTS, RECORD_TYPES)

    def test_record_lists_contrasts_outcomes_draws_and_is_dated(self):
        rec = self.declare(note="primary setting, 3 draws")
        self.assertEqual(rec["record_type"], "declared_contrasts")
        pl = rec["payload"]
        self.assertEqual(pl["rung"], "remedy")
        self.assertEqual(pl["contrasts"], ["remedy-placebo", "placebo-rung2"])
        self.assertEqual(pl["outcomes"], ["leaked_rate", "false_wished_rate"])
        self.assertEqual(pl["draws"], {"remedy": 3, "placebo": 3, "rung2": 3})
        self.assertEqual((pl["parameters"]["n_perm"], pl["parameters"]["seed"], pl["parameters"]["B"]), (10000, 7, 2000))
        self.assertNotIn("declared_on", pl)                              # the hashed ts is the only date
        self.assertEqual(read_declarations(self.path)[0]["declared_on"], rec["ts"][:10])
        self.assertIsNone(pl["amends"])
        self.assertEqual(pl["kind"], "declaration")
        self.assertEqual(pl["note"], "primary setting, 3 draws")

    def test_the_date_cannot_be_set_by_the_caller(self):
        with self.assertRaises(TypeError):
            declare_contrasts(self.log, rung="remedy", date="2020-01-01")

    def test_record_stores_the_verdict_rules(self):
        rules = self.declare(require_kept_gain=True, require_holm=False)["payload"]["verdict_rules"]
        self.assertEqual(rules["gating"], {"false_wished_rate": "lower", "actual_recall": "higher",
                                           "ai_node_count": "higher", "flip_rate": "lower", "missed_rate": "lower"})
        self.assertTrue(rules["require_kept_gain"])
        self.assertFalse(rules["require_holm"])
        self.assertEqual((rules["alpha"], rules["n_perm"], rules["seed"], rules["B"]), (0.05, 10000, 7, 2000))
        self.assertIn("disagreement_vs_rung2", rules["descriptive"])
        self.assertEqual(rules["remedy_contrast"], "remedy-placebo")

    def test_defaults_are_the_three_primary_contrasts(self):
        rec = declare_contrasts(self.log, rung="rung2", draws={"rung2": 3})
        self.assertEqual(rec["payload"]["contrasts"], [f"{a}-{b}" for a, b in PRIMARY_CONTRASTS])
        self.assertIn("leaked_rate", rec["payload"]["outcomes"])

    def test_chain_and_manifest_verify(self):
        self.declare()
        self.log.append("run_note", {"x": 1})
        self.declare(rung="placebo")
        ok, detail = verify_internal_chain(self.path)
        self.assertTrue(ok, detail)
        self.log.close()
        ok, detail = verify_run(self.path)
        self.assertTrue(ok, detail)
        self.assertEqual(detail["records"], 3)

    def test_amendment_is_a_new_record_pointing_at_the_original(self):
        first = self.declare()
        amend = self.declare(contrasts=["remedy-placebo"], amends=first["hash"], note="drop placebo-rung2 after pilot")
        self.assertEqual(amend["payload"]["amends"], first["hash"])
        self.assertEqual(amend["payload"]["kind"], "amendment")
        self.assertNotEqual(amend["hash"], first["hash"])
        self.assertEqual(amend["prev_hash"], first["hash"])             # chained after it, the original is untouched
        ok, detail = verify_internal_chain(self.path)
        self.assertTrue(ok, detail)

    def test_amendment_chain_and_reader(self):
        a = self.declare()
        b = self.declare(amends=a["hash"], contrasts=["remedy-placebo"])
        c = self.declare(amends=b["hash"], contrasts=["remedy-placebo", "rung2-rung1"])
        decl = read_declarations(self.path)
        self.assertEqual([d["id"] for d in decl], [a["hash"], b["hash"], c["hash"]])
        self.assertEqual([d["superseded"] for d in decl], [True, True, False])
        self.assertEqual(decl[0]["amended_by"], [b["hash"]])
        self.assertEqual(decl[2]["amends_found"], True)
        self.assertEqual(decl[2]["payload"]["contrasts"], ["remedy-placebo", "rung2-rung1"])
        self.log.close()
        ok, detail = verify_run(self.path)
        self.assertTrue(ok, detail)

    def test_never_blocks_amends_an_unknown_id_is_recorded_and_flagged(self):
        rec = self.declare(amends="f" * 64)
        self.assertEqual(rec["payload"]["amends"], "f" * 64)
        self.assertFalse(rec["payload"]["amends_found"])
        ok, detail = verify_internal_chain(self.path)
        self.assertTrue(ok, detail)

    def test_no_preconditions_empty_lists_and_late_declaration_are_recorded(self):
        self.log.append("call_request", {"request_id": "r1"})
        self.log.append("call_response", {"request_id": "r1"})
        rec = declare_contrasts(self.log, rung="remedy", contrasts=[], outcomes=[], draws={})
        self.assertEqual(rec["payload"]["contrasts"], [])
        ok, detail = verify_internal_chain(self.path)
        self.assertTrue(ok, detail)

    def test_resumed_log_can_take_an_amendment(self):
        a = self.declare()
        self.log.close()
        log2 = AuditLog(self.path, "run-1", clock=clock())
        amend = declare_contrasts(log2, rung="remedy", contrasts=["remedy-placebo"], draws={"remedy": 2, "placebo": 2}, amends=a["hash"])
        log2.close()
        ok, detail = verify_run(self.path)
        self.assertTrue(ok, detail)
        self.assertEqual(amend["payload"]["amends_found"], True)

    def test_unreadable_lines_do_not_make_the_declaration_or_the_reader_throw(self):
        first = self.declare()
        with open(self.path) as fh:
            line = fh.readline()
        with open(self.path, "w") as fh:
            fh.write(line + "{this is not json\n" + line)               # a damaged line in the middle, a valid tail
        self.assertEqual(len(read_declarations(self.path)), 2)
        rec = self.declare(amends=first["hash"])
        self.assertTrue(rec["payload"]["amends_found"])
        self.assertEqual(rec["payload"]["log_unreadable_lines"], 1)

    def test_written_line_is_plain_json(self):
        self.declare()
        with open(self.path) as fh:
            rec = json.loads(fh.readline())
        self.assertEqual(rec["record_type"], "declared_contrasts")


class TestCompareToDeclaration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "run.jsonl")
        self.log = AuditLog(self.path, "run-1", clock=clock())
        self.cfg = dict(contrasts=["remedy-placebo", "placebo-rung2"], outcomes=["leaked_rate", "false_wished_rate"],
                        draws={"remedy": 3, "placebo": 3, "rung2": 3}, n_perm=10000, seed=7, B=2000)
        self.first = declare_contrasts(self.log, rung="remedy", **self.cfg)

    def test_same_analysis_is_as_declared(self):
        r = compare_to_declaration(self.path, analysis_config(**self.cfg), rung="remedy")
        self.assertEqual(r["label"], "as_declared")
        self.assertEqual(r["differences"], [])
        self.assertEqual(r["declaration_id"], self.first["hash"])

    def test_a_changed_rule_or_parameter_is_labelled_exploratory_and_listed(self):
        cfg = dict(self.cfg, seed=8)
        r = compare_to_declaration(self.path, analysis_config(**cfg, require_holm=False), rung="remedy")
        self.assertEqual(r["label"], "exploratory_after_real_data")
        fields = {d["field"] for d in r["differences"]}
        self.assertEqual(fields, {"parameters.seed", "verdict_rules.seed", "verdict_rules.require_holm"})
        d = {x["field"]: x for x in r["differences"]}
        self.assertEqual((d["parameters.seed"]["declared"], d["parameters.seed"]["analysis"]), (7, 8))

    def test_a_changed_contrast_list_or_draw_count_is_listed(self):
        cfg = dict(self.cfg, contrasts=["remedy-placebo"], draws={"remedy": 2, "placebo": 2, "rung2": 2})
        r = compare_to_declaration(self.path, analysis_config(**cfg), rung="remedy")
        self.assertEqual(r["label"], "exploratory_after_real_data")
        self.assertEqual({d["field"] for d in r["differences"]}, {"contrasts", "draws"})

    def test_order_of_contrasts_and_outcomes_does_not_matter(self):
        cfg = dict(self.cfg, contrasts=list(reversed(self.cfg["contrasts"])), outcomes=list(reversed(self.cfg["outcomes"])))
        self.assertEqual(compare_to_declaration(self.path, analysis_config(**cfg), rung="remedy")["label"], "as_declared")

    def test_the_latest_record_wins_amendments_included(self):
        cfg2 = dict(self.cfg, contrasts=["remedy-placebo"])
        amend = declare_contrasts(self.log, rung="remedy", amends=self.first["hash"], **{**cfg2})
        now = compare_to_declaration(self.path, analysis_config(**cfg2), rung="remedy")
        self.assertEqual((now["label"], now["declaration_id"]), ("as_declared", amend["hash"]))
        old = compare_to_declaration(self.path, analysis_config(**self.cfg), rung="remedy")
        self.assertEqual(old["label"], "exploratory_after_real_data")

    def test_no_declaration_is_a_label_not_an_error(self):
        r = compare_to_declaration(self.path, analysis_config(**self.cfg), rung="placebo")
        self.assertEqual(r["label"], "undeclared")
        r = compare_to_declaration(os.path.join(self.tmp.name, "missing.jsonl"), analysis_config(**self.cfg))
        self.assertEqual(r["label"], "undeclared")

    def test_comparison_writes_nothing(self):
        with open(self.path) as fh:
            before = fh.read()
        compare_to_declaration(self.path, analysis_config(**dict(self.cfg, seed=1)), rung="remedy")
        with open(self.path) as fh:
            self.assertEqual(fh.read(), before)


if __name__ == "__main__":
    unittest.main()
