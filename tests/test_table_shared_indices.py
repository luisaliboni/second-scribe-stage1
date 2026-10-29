"""A malformed spreadsheet index must not silently reuse another cell's text."""
import unittest
from xml.etree import ElementTree as ET

from second_scribe.maps.readers import MapRefused
from second_scribe.maps.table import _sheet_rows


class TestSharedStringIndices(unittest.TestCase):
    def rows(self, index):
        sheet = ET.fromstring(f'<worksheet><sheetData><row r="1">'
                              f'<c r="A1" t="s"><v>{index}</v></c>'
                              '</row></sheetData></worksheet>')
        return _sheet_rows(sheet, ["happened", "missing"], {"n_formula_cells": 0})

    def test_negative_reference_does_not_pick_last_shared_string(self):
        with self.assertRaises(MapRefused):
            self.rows(-1)

    def test_valid_references_keep_their_text(self):
        self.assertEqual(self.rows(0), [(1, ["happened"])])
        self.assertEqual(self.rows(1), [(1, ["missing"])])

    def test_out_of_bounds_reference_refused(self):
        with self.assertRaises(MapRefused):
            self.rows(2)
