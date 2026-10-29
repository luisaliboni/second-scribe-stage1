"""Council 30 (MUST 5): no "X, not Y" contrast and no "rather than" in any reader-facing string.

Scans every value of the app's string table as the app serves it (``strings.load_strings``: en.json plus the kit's
``profile_strings.json`` sentences, which the Technical details print). A hit is ", not " followed by a word (the
contrast between two clauses or phrases, "a file, not a folder") or the phrase "rather than", any case. The
allow-list is for technical strings only, one reason each; it is empty because every hit could be rewritten.
"""
import re
import unittest

from second_scribe.app import strings

CONTRAST = re.compile(r",\s+not\s+\w", re.IGNORECASE)
RATHER = re.compile(r"\brather\s+than\b", re.IGNORECASE)
# key -> reason. Only a technical string (a code, a command, a file format) may be listed, never prose.
ALLOW: dict[str, str] = {}


def hits(table: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in table.items()
            if k not in ALLOW and isinstance(v, str) and (CONTRAST.search(v) or RATHER.search(v))}


class TestNoContrastWording(unittest.TestCase):
    def test_no_x_comma_not_y_and_no_rather_than(self):
        self.assertEqual(hits(strings.load_strings("en")), {})

    def test_allow_list_names_real_keys_with_a_reason(self):
        table = strings.load_strings("en")
        for k, why in ALLOW.items():
            self.assertIn(k, table)
            self.assertTrue(why.strip())

    def test_detector_catches_each_form(self):
        for bad in ("This path is a file, not a folder.", "agreement, not distance", "Rather than a map, a table.",
                    "red is set by a style rather than on the box", "one,  Not two"):
            self.assertTrue(hits({"k": bad}), bad)
        for fine in ("It does not measure distance.", "The note is not shown.", "a quote, nothing else", "knot, notary"):
            self.assertFalse(hits({"k": fine}), fine)


if __name__ == "__main__":
    unittest.main()
