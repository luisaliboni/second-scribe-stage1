"""Audit fix A (K1 K2 K8 S2): the publish gate's closed vocabulary, the §13 string rules and run.remote_endpoints.

K1: a split hidden in cell VALUES (x = a role, an interviewer, a map) must be refused, so the cell (name, x) pair,
the band labels and every other value-carrying label come from a closed vocabulary: the PROFILE-SCHEMA §8 row
catalogue (its concrete form is ``tests/data/profile_v2_skeleton.json``) plus the kit's ``profile_strings.json`` codes.
K2: ``label_key`` is no longer exempt up to 400 characters, and ``profile_base.lab_string_problems`` runs in the gate.
K8/S2: ``run.remote_endpoints = [{role, provider, declaration_sha256}]`` (PROFILE-SCHEMA §14.8) is allowed, and
nothing else in such an entry.
"""
import copy
import json
import unittest
from pathlib import Path
from unittest import mock

from second_scribe import publish
from tests.test_publish import HEX, SAMPLE, SCHEMA, GateCase, lab_doc

ROOT = Path(__file__).resolve().parent.parent
SKELETON = json.loads((ROOT / "tests" / "data" / "profile_v2_skeleton.json").read_text(encoding="utf-8"))


def codes(doc, k=10):
    return [code for code, _ in publish.violations(doc, k, SCHEMA)]


def found(doc, k=10):
    return publish.violations(doc, k, SCHEMA)


def row1(d):
    return d["rows"][1]


def kept(x=None, n=40, unit="ideal", name="kept"):
    c = {"name": name, "n": n, "unit": unit, "kind": "share", "value": 0.5}
    if x is not None:
        c["x"] = x
    return c


class TestCatalogue(unittest.TestCase):
    def test_catalogue_is_the_skeleton(self):
        """The gate's row catalogue is the §8 catalogue in (name, x) form: the same rows, label keys and pairs, in
        order, as the skeleton the exporter's own test holds its output to."""
        want = {r["row"]: (r["label_key"], tuple((c["name"], c.get("x")) for c in r["cells"]))
                for r in SKELETON["rows"]}
        self.assertEqual(list(publish.CATALOGUE), [r["row"] for r in SKELETON["rows"]])
        self.assertEqual(dict(publish.CATALOGUE), want)

    def test_label_key_not_exempt(self):
        self.assertNotIn("label_key", publish.LONG_400)


class TestK1ValueSplits(GateCase):
    """The audit's probe cases (K1): each passed the gate before this fix; each must now be refused."""

    def test_baseline_passes(self):
        self.passes(lab_doc())

    def test_role_split_in_x(self):
        d = lab_doc()
        row1(d)["cells"] = [kept("nurse"), kept("manager")]
        self.refuse(d, "unknown_label", secret="nurse")
        self.assertIn(("unknown_label", "$.rows[1].cells[0].x"), found(d))
        self.assertIn(("unknown_label", "$.rows[1].cells[1].x"), found(d))

    def test_interviewer_in_x(self):
        d = lab_doc()
        row1(d)["cells"] = [kept("interviewer_A")]
        e = self.refuse(d, "unknown_label", secret="interviewer_A")
        self.assertEqual(e.where, "$.rows[1].cells[0].x")

    def test_per_map_vector_in_x(self):
        d = lab_doc()
        row1(d)["cells"] = [kept(f"m{i}", n=3) for i in range(12)]
        self.refuse(d, "too_many_cells", secret="m0")     # 12 cells; row 1 has 6 in the catalogue
        self.assertIn(("unknown_label", "$.rows[1].cells[0].x"), found(d))
        self.assertIn(("unknown_label", "$.rows[1].cells[11].x"), found(d))
        row1(d)["cells"] = row1(d)["cells"][:6]          # under the cap: still refused, by the vocabulary
        self.refuse(d, "unknown_label", secret="m0")

    def test_per_map_vector_in_name(self):
        d = lab_doc()
        row1(d)["cells"] = [kept(name=f"kept_m{i}") for i in range(3)]
        e = self.refuse(d, "unknown_label", secret="kept_m0")
        self.assertEqual(e.where, "$.rows[1].cells[0].name")

    def test_name_of_another_row(self):
        d = lab_doc()
        row1(d)["cells"] = [kept(name="precision", x="primary")]      # a row-3 cell, not a row-1 one
        self.refuse(d, "unknown_label")

    def test_x_valid_elsewhere_but_not_for_this_name(self):
        d = lab_doc()
        row1(d)["cells"] = [kept("primary")]                          # row 1 has kept at x = None only
        self.refuse(d, "unknown_label")

    def test_duplicate_cell(self):
        d = lab_doc()
        row1(d)["cells"] = [kept(), kept()]
        e = self.refuse(d, "duplicate_label")
        self.assertEqual(e.where, "$.rows[1].cells[1]")

    def test_duplicate_cell_with_null_x(self):
        d = lab_doc()
        c = kept()
        c2 = kept()
        c2["x"] = None                                                 # absent and null x are the same cell
        row1(d)["cells"] = [c, c2]
        self.refuse(d, "duplicate_label")

    def test_cell_cap(self):
        d = lab_doc()
        cells = [kept(), kept(name="coarsened"), kept(name="omitted"), kept(name="r_star"),
                 kept(name="attested_recall", x="primary"), kept(name="attested_recall", x="lenient")]
        row1(d)["cells"] = cells
        self.passes(d)                                                 # all six catalogue cells: fine
        row1(d)["cells"] = cells + [kept()]
        self.assertIn(("too_many_cells", "$.rows[1].cells"), found(d))

    def test_duplicate_row(self):
        d = lab_doc()
        d["rows"].append(copy.deepcopy(row1(d)))
        e = self.refuse(d, "duplicate_label")
        self.assertEqual(e.where, "$.rows[2].row")

    def test_duplicate_row_cannot_split_a_cell(self):
        """Two rows "1", each with kept: one (row, name, x) twice across the file."""
        d = lab_doc()
        row1(d)["cells"] = [kept()]
        d["rows"].append(copy.deepcopy(row1(d)))
        self.assertIn("duplicate_label", codes(d))

    def test_unknown_row(self):
        for rid in (10, "nurse", "1", True, 1.0):
            with self.subTest(rid=rid):
                d = lab_doc()
                row1(d)["row"] = rid
                self.assertIn("unknown_label", codes(d))

    def test_label_key_closed(self):
        for lk in ("profile.the_nurse_said", "profile.row1", "nurse", "profile.lead"):
            with self.subTest(lk=lk):
                d = lab_doc()
                row1(d)["label_key"] = lk
                e = self.refuse(d, "unknown_label", secret=lk)
                self.assertEqual(e.where, "$.rows[1].label_key")

    def test_population_shape(self):
        for pop in ("nurses: 12", "12 of 12 nurses", "twelve"):
            with self.subTest(pop=pop):
                d = lab_doc()
                row1(d)["population"] = pop
                self.refuse(d, "unknown_label", secret=pop)

    def test_band_member_x_closed(self):
        d = lab_doc()
        d["rows"][0]["cells"][0]["band"]["members"] = [{"x": "nurse", "value": 0.3}, {"x": "manager", "value": 0.4}]
        e = self.refuse(d, "unknown_label", secret="nurse")
        self.assertEqual(e.where, "$.rows[0].cells[0].band.members[0].x")

    def test_band_member_duplicate(self):
        d = lab_doc()
        d["rows"][0]["cells"][0]["band"]["members"] = [{"x": "primary", "value": 0.3}, {"x": "primary", "value": 0.4}]
        self.refuse(d, "duplicate_label")

    def test_row_band_member_closed(self):
        d = lab_doc()
        d["rows"][0]["band"]["members"] = [{"x": "m3", "value": 0.3}]
        e = self.refuse(d, "unknown_label")
        self.assertEqual(e.where, "$.rows[0].band.members[0].x")

    def test_band_member_values_pass(self):
        d = lab_doc()
        d["rows"][0]["cells"][0]["band"]["members"] = [
            {"x": x, "value": 0.3} for x in ("primary", "lenient", "lexical_only_04", "lexical_only_05",
                                             "lexical_only_06", "lexical_only_07", "judge_only_same",
                                             "judge_only_same_or_close")]
        self.passes(d)
        d["rows"][0]["cells"][0]["band"] = {"lo": 0.1, "hi": 0.2, "over": "labellers", "members": [
            {"x": "judge", "value": 0.1}, {"x": "judge2", "value": 0.2}, {"x": "keyword", "value": 0.15}]}
        self.passes(d)

    def test_band_over_and_subband_closed(self):
        for mutate in (lambda b: b.__setitem__("over", "interviewers"),
                       lambda b: b["subbands"][0].__setitem__("name", "nurses"),
                       lambda b: b["subbands"].append({"name": "judge_using", "lo": 0.1, "hi": 0.2})):
            d = lab_doc()
            mutate(d["rows"][0]["cells"][0]["band"])
            self.assertTrue({"unknown_label", "duplicate_label"} & set(codes(d)))

    def test_cell_codes_closed(self):
        for key, value in (("unit", "nurse"), ("kind", "per_nurse"), ("state", "withheld_nurse"),
                           ("derived", "nurse_sum"), ("rests_on", ["R", "NURSE"]), ("qualifiers", ["nurse"]),
                           ("source", "the nurse said"), ("source", "kit:a1+nurse")):
            with self.subTest(key=key, value=value):
                d = lab_doc()
                d["rows"][0]["cells"][0][key] = value
                self.assertIn("unknown_label", codes(d))

    def test_row_codes_closed(self):
        for key, value in (("rests_on", ["R", "NURSE"]), ("qualifiers", ["nurse"]), ("source", ["the nurse"])):
            with self.subTest(key=key):
                d = lab_doc()
                d["rows"][0][key] = value
                self.assertIn("unknown_label", codes(d))

    def test_known_codes_pass(self):
        d = lab_doc()
        c = d["rows"][0]["cells"][0]
        c.update(unit="ai_box", kind="upper_bound", rests_on=["R", "P", "J2", "LX"],
                 qualifiers=["ceiling", "not_a_test"], source="kit:a1_recall_matchers+kit:a10_band")
        d["rows"][0]["source"] = ["kit:a10_band", "cleared/013"]
        d["rows"][0]["cells"][1]["derived"] = "per_map_mean"
        self.passes(d)

    def test_note_codes(self):
        for note in ("pooled_only", "pooled_only;ci_undefined", "leak_split_indeterminate:3"):
            with self.subTest(note=note):
                d = lab_doc()
                d["rows"][0]["cells"][0]["note"] = note
                self.passes(d)
        for note in ("pooled_only;nurse", "nurse", "leak_split_indeterminate:nurse", "pooled_only;"):
            with self.subTest(note=note):
                d = lab_doc()
                d["rows"][0]["cells"][0]["note"] = note
                self.assertTrue({"unknown_label", "lab_string"} & set(codes(d)), note)

    def test_reason_code_suffix(self):
        d = lab_doc()
        nr = d["rows"][0]["cells"][3]
        nr["reason_code"] = "group_empty:12"
        self.passes(d)
        nr["reason_code"] = "group_empty:nurse"
        self.refuse(d, "unknown_label", secret="nurse")

    def test_types_refused(self):
        for key, value in (("name", 3), ("x", 1), ("x", True)):
            with self.subTest(key=key):
                d = lab_doc()
                d["rows"][0]["cells"][0][key] = value
                self.assertIn("unknown_label", codes(d))


class TestK2Strings(GateCase):
    """profile_base.lab_string_problems runs inside the gate (the two gates no longer disagree)."""

    def test_long_label_key_refused(self):
        d = lab_doc()
        d["rows"][0]["label_key"] = "profile." + "the_nurse_said_" * 10
        self.assertIn("long_string", codes(d))

    def test_not_run_without_needs_or_reason_code(self):
        d = lab_doc()
        del d["rows"][0]["cells"][3]["needs"]
        e = self.refuse(d, "lab_string")
        self.assertEqual(e.where, "$.rows[0].cells[3]")

    def test_missing_free_text(self):
        d = lab_doc()
        c = d["rows"][0]["cells"][0]
        c["state"] = "partial_not_run"
        c["missing"] = ["anything goes here"]
        e = self.refuse(d, "lab_string", secret="anything goes here")
        self.assertEqual(e.where, "$.rows[0].cells[0]")
        c["missing"] = ["band"]
        self.passes(d)

    def test_formula_code_must_resolve(self):
        d = lab_doc()
        d["rows"][0]["cells"][2]["formula_code"] = "nurse_formula"
        self.refuse(d, "lab_string", secret="nurse_formula")

    def test_derived_true_needs_formula_code(self):
        d = lab_doc()
        del d["rows"][0]["cells"][2]["formula_code"]
        self.refuse(d, "lab_string")

    def test_reason_code_must_resolve(self):
        d = lab_doc()
        d["rows"][0]["cells"][3]["reason_code"] = "nurse_left"
        self.refuse(d, "lab_string", secret="nurse_left")


class TestK8RemoteEndpoints(GateCase):
    """run.remote_endpoints (PROFILE-SCHEMA §14.8): {role, provider, declaration_sha256}; no host, no basis."""

    def entry(self, **kw):
        e = {"role": "judge", "provider": "anthropic", "declaration_sha256": HEX}
        e.update(kw)
        return e

    def doc(self, *entries):
        d = lab_doc()
        d["run"]["remote_endpoints"] = list(entries)
        return d

    def test_accepts(self):
        self.passes(self.doc())                                         # empty: no cloud model used
        self.passes(self.doc(self.entry()))
        self.passes(self.doc(self.entry(role="recon", provider="openai_compat"),
                             self.entry(role="judge2", provider="anthropic")))
        d = lab_doc()
        d["run"]["remote_endpoints"] = None
        self.passes(d)

    def test_accepts_every_known_role(self):
        self.assertEqual(publish.REMOTE_ROLES, {"recon", "judge", "judge2"})
        for role in publish.REMOTE_ROLES:
            with self.subTest(role=role):
                self.passes(self.doc(self.entry(role=role)))

    def test_refuses_host(self):
        host = "api.anthropic.com"
        d = self.doc(self.entry(host=host))
        self.refuse(d, "endpoint_host", secret=host)
        self.assertIn(("not_allowed", "$.run.remote_endpoints[0].<key 3>"), found(d))
        d = self.doc(self.entry(host="unclear"))                       # any host key, address-like or not
        self.refuse(d, "not_allowed", secret="unclear")

    def test_refuses_basis(self):
        basis = "consent form 7 says ok"
        self.refuse(self.doc(self.entry(basis=basis)), "not_allowed", secret=basis)

    def test_refuses_extra_key(self):
        for key in ("model_id", "sha256", "note", "ticked"):
            with self.subTest(key=key):
                d = self.doc(self.entry(**{key: "x"}))
                self.assertIn("not_allowed", codes(d))

    def test_refuses_long_provider(self):
        d = self.doc(self.entry(provider="a" * 41))
        self.assertIn(("remote_endpoint", "$.run.remote_endpoints[0].provider"), found(d))
        self.refuse(d, "long_string")

    def test_refuses_provider_shape(self):
        for provider in ("Anthropic", "anthropic inc", "a/b", "", 7, None):
            with self.subTest(provider=provider):
                d = self.doc(self.entry(provider=provider))
                self.assertIn(("remote_endpoint", "$.run.remote_endpoints[0].provider"), found(d))

    def test_provider_must_be_a_known_code(self):
        for provider in ("openai_compat", "anthropic"):
            with self.subTest(provider=provider):
                self.passes(self.doc(self.entry(provider=provider)))
        d = self.doc(self.entry(provider="mistral"))                   # right shape, but no code for it
        self.assertIn(("remote_endpoint", "$.run.remote_endpoints[0].provider"), found(d))
        real = publish.load_profile_strings()
        with mock.patch.object(publish, "load_profile_strings",
                               lambda: {**real, "profile.provider.mistral": "Mistral AI."}):
            self.assertEqual(found(d), [])                             # a strings-file code is accepted

    def test_refuses_bad_hash(self):
        for h in ("abc", HEX[:-1], HEX.upper(), HEX + "0", "g" * 64, None, 5):
            with self.subTest(h=h):
                d = self.doc(self.entry(declaration_sha256=h))
                self.assertTrue(set(codes(d)) & {"remote_endpoint", "record_hash"}, h)
                self.assertIn("remote_endpoint", codes(d))

    def test_refuses_unknown_role(self):
        for role in ("nurse", "interviewer", "", None, "Judge",
                     "reconstructor", "reading3", "second_judge_family"):   # schema names: not the kit's
            with self.subTest(role=role):
                d = self.doc(self.entry(role=role))
                self.assertIn(("remote_endpoint", "$.run.remote_endpoints[0].role"), found(d))

    def test_refuses_missing_key(self):
        for key in ("role", "provider", "declaration_sha256"):
            with self.subTest(key=key):
                e = self.entry()
                del e[key]
                self.assertIn("remote_endpoint", codes(self.doc(e)))

    def test_refuses_duplicate_entry(self):
        d = self.doc(self.entry(), self.entry())
        self.assertIn("duplicate_label", codes(d))

    def test_refuses_wrong_shapes(self):
        d = lab_doc()
        d["run"]["remote_endpoints"] = self.entry()                      # an object, not a list
        self.assertIn(("not_allowed", "$.run.remote_endpoints"), found(d))
        d["run"]["remote_endpoints"] = ["anthropic"]
        self.refuse(d, "not_allowed")

    def test_hash_allowed_only_under_declaration_sha256(self):
        d = self.doc(self.entry(provider=HEX))
        self.assertIn("record_hash", codes(d))
        d = lab_doc()
        d["run"]["settings"]["declaration_sha256"] = HEX
        self.assertIn("not_allowed", codes(d))
        d = lab_doc()
        d["rows"][0]["cells"][0]["declaration_sha256"] = HEX
        self.assertIn("hex_outside_run", codes(d))

    def test_remote_block_on_real_exporter_output(self):
        from tests.e2_demo import demo_export
        _run, path, _sha = demo_export()
        d = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(found(d), [])
        d["run"]["remote_endpoints"] = [self.entry(role="judge2")]
        self.assertEqual(found(d), [])


class TestChainHeadsSource(GateCase):
    """run.run_chain_heads_source (PROFILE-SCHEMA §4, §14): the code that says why run_chain_heads is null, as a lab
    export made with --withhold-chain-heads writes it. A code of at most 40 characters from a-z, 0-9, _ . -; never
    free text."""

    def doc(self, source="available_from_authors", heads=None):
        d = lab_doc()
        d["run"]["run_chain_heads"] = heads
        if source is not None:
            d["run"]["run_chain_heads_source"] = source
        return d

    def test_accepts_null_heads_with_a_code(self):
        self.passes(self.doc())
        self.passes(self.doc(source="a.b-c_9"))
        self.passes(self.doc(source="x" * 40))
        self.passes(self.doc(source=None))                                # null heads, no source: still fine

    def test_accepts_the_withheld_form_of_the_kit_exporter(self):
        d = json.loads(SAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(found(d), [])
        d["run"]["run_chain_heads"] = None
        d["run"]["run_chain_heads_source"] = "available_from_authors"
        self.assertEqual(found(d), [])
        del d["run"]["run_chain_heads_source"]
        self.assertEqual(found(d), [])

    def test_refuses_free_text_and_long_values(self):
        for bad in ("x" * 41, "The heads are held by the authors", "Upper", "a b", "", "a/b", "see https://x.org",
                    "ab" * 40, 7, True, ["available_from_authors"], {"a": "b"}):
            with self.subTest(bad=bad):
                self.assertIn(("not_allowed", "$.run.run_chain_heads_source"), found(self.doc(source=bad)))

    def test_a_hash_is_not_a_code(self):
        d = self.doc(source=HEX)
        self.assertTrue(set(codes(d)) & {"record_hash", "not_allowed"}, codes(d))

    def test_the_key_is_allowed_only_at_the_run_block(self):
        d = lab_doc()
        d["run"]["settings"]["run_chain_heads_source"] = "available_from_authors"
        self.assertIn("not_allowed", codes(d))
        d = lab_doc()
        d["run_chain_heads_source"] = "available_from_authors"
        self.assertIn("not_allowed", codes(d))


class TestRealOutputs(GateCase):
    """The real exporter's output on the demo and the E2 M1 sample still pass with the closed vocabulary."""

    def test_every_cell_the_kit_exporter_emits_passes_the_gate(self):
        """Every cell the kit exporter emits passes the gate. When release-rc merges, this runs against the RC
        exporter, so a CATALOGUE drift fails here."""
        from tests.e2_demo import demo_export
        _run, path, _sha = demo_export()
        self.assertEqual(found(json.loads(path.read_text(encoding="utf-8"))), [])

    def test_sample_fixture_passes(self):
        self.assertEqual(found(json.loads(SAMPLE.read_text(encoding="utf-8"))), [])


if __name__ == "__main__":
    unittest.main()
