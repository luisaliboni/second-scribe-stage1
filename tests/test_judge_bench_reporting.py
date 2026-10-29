"""Reporting fixes after the audit and the task-11 re-review: both_wrong skips item-excluded items, by-kind rows
separate excluded from scored items, the report states what its tables cannot show."""
import unittest

from second_scribe import judge_bench as jb
from second_scribe.judge_bench import both_wrong, score_modality


def res(rows):
    items = [{"id": f"i{k}", "expected": e, "kind": kind, "style": "prose", "pred": p, "judged": True,
              "excluded": ex} for k, (e, kind, p, ex) in enumerate(rows)]
    return {"modality": {"items": items}}


class TestBothWrongSkipsItemExcluded(unittest.TestCase):
    def test_an_item_excluded_one_by_one_leaves_its_class_row_and_all(self):
        # i0: scored wished, both wrong and agree. i1: wished item excluded (fill contradicts label), both wrong and
        # agree. i2: mapping_talk (an excluded KIND): keeps its own row but is not in "all".
        a = res([("wished", "wished", "actual", False), ("wished", "wished", "actual", True),
                 ("actual", "mapping_talk", "wished", True)])
        b = res([("wished", "wished", "actual", False), ("wished", "wished", "actual", True),
                 ("actual", "mapping_talk", "wished", True)])
        bw = both_wrong(a, b)
        self.assertEqual(bw["wished"]["n"], 1)
        self.assertEqual(bw["wished"]["both_wrong_agree"], 1)
        self.assertEqual(bw["all"]["n"], 1)
        self.assertEqual(bw["mapping_talk"]["n"], 1)

    def test_a_class_with_only_excluded_items_has_no_row(self):
        a = res([("negated", "negated", "actual", True)])
        self.assertNotIn("negated", both_wrong(a, a))


class TestByKindSplit(unittest.TestCase):
    def items(self):
        mk = lambda i, exp, kind, **kw: {"element_id": f"i{i}", "expected": exp, "kind": kind, "style": "prose", **kw}
        return [mk(0, "wished", "wished"), mk(1, "wished", "wished", exclude=jb.FILL_REASON),
                mk(2, "wished", "wished", exclude=jb.FILL_REASON), mk(3, "actual", "mapping_talk"),
                mk(4, "negated", "negated", exclude=jb.FILL_REASON)]

    def test_counts_and_accuracy_of_scored_items_are_separate(self):
        labels = {"i0": "wished", "i1": "actual", "i2": "wished", "i3": "actual", "i4": "negated"}
        s = score_modality(self.items(), labels, set())
        self.assertEqual(s["by_kind_n"]["wished"], 3)
        self.assertEqual(s["by_kind_excluded_n"]["wished"], 2)
        self.assertAlmostEqual(s["by_kind"]["wished"], 2 / 3)           # all judged
        self.assertEqual(s["by_kind_scored"]["wished"], 1.0)            # the one scored item
        self.assertEqual(s["by_kind_excluded_n"]["mapping_talk"], 1)    # a whole excluded kind
        self.assertIsNone(s["by_kind_scored"]["mapping_talk"])
        self.assertEqual(s["by_kind_excluded_n"]["negated"], 1)
        self.assertIsNone(s["by_kind_scored"]["negated"])


class TestReportNotes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import shutil
        import tempfile
        from pathlib import Path
        from second_scribe.audit.registry import AuditLog
        tmp = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, tmp, True)
        root = Path(__file__).resolve().parent.parent
        cls.r = jb.bench(jb.OracleClient(root / "demo" / "corpus"), root / "prompts", root / "demo" / "corpus",
                         AuditLog(Path(tmp) / "l.jsonl", "l"), mode="generative")
        cls.md = jb.render_markdown(cls.r)

    def test_the_keyword_floor_is_labelled_reference_only(self):
        self.assertIn("Reference only: the bench is built to defeat keyword cues", self.md)

    def test_per_seed_rows_say_they_are_not_like_for_like(self):
        self.assertIn("Per-seed rows are not like-for-like", self.md)

    def test_the_by_kind_table_has_the_split_columns_and_the_note(self):
        self.assertIn("| kind | n judged | of which not scored |", self.md)
        section = self.md.split("| kind | n judged | of which not scored |", 1)[1]
        row = next(l for l in section.splitlines() if l.startswith("| wished |"))
        self.assertEqual(len(row.split("|")), 7)          # kind, n judged, not scored, accuracy all, accuracy scored
        self.assertIn("never scored", self.md)


if __name__ == "__main__":
    unittest.main()
