"""Cell strings in lab exports (Task 5S; PROFILE-SCHEMA section 13).

Lab exports carry codes, never sentences: ``reason_code`` / ``formula_code`` / ``missing`` / ``note`` codes whose English
sentences live in ``second_scribe/profile_strings.json``; every string in a cell is at most 40 characters (``source`` and
``*_state`` at most 200). ``profile_base.lab_string_problems`` checks all of it. The demo test runs every row builder on
the oracle demo (zero model calls) with and without the ladder.
"""
import ast
import json
import re
import tempfile
import unittest
from pathlib import Path

from second_scribe import profile_base as pb
from second_scribe import profile_common as pc
from second_scribe import profile_coverage as cov
from second_scribe import profile_floors as pf
from second_scribe import profile_labellers as lb
from second_scribe import profile_ladder as pl
from second_scribe import profile_links as lk
from second_scribe import profile_slant as sl
from second_scribe import toolcfg
from second_scribe.cli import DEMO_CONFIG
from tests.profile_fixture import REPO, contexts

STRINGS = REPO / "second_scribe" / "profile_strings.json"
MODULES = ("profile_common", "profile_f1", "profile_floors", "profile_labellers", "profile_slant", "profile_coverage",
           "profile_links", "profile_ladder", "profile_export")
BANNED = re.compile(r"\b(first|validated|ground[ -]truth|replicable)\b", re.I)


def _rows(cells, row="t"):
    return {"rows": [{"row": row, "cells": cells}]}


class TestConstructors(unittest.TestCase):
    def test_not_run_requires_needs_or_reason_code(self):
        for bad in ((), None, [], False):
            with self.assertRaises(ValueError):
                pb.not_run("a", bad)
        with self.assertRaises(ValueError):
            pb.not_run("a", [])                           # neither needs nor a reason_code
        self.assertEqual(pb.not_run("a", ["ladder"])["needs"], ["ladder"])
        c = pb.not_run("a", [], reason_code="grid_spec_missing")
        self.assertEqual((c["needs"], c["reason_code"], c["state"], c["value"]), ([], "grid_spec_missing", "not_run", None))
        both = pb.not_run("a", ["J2"], reason_code="needs_J2", source="kit:a10_band")
        self.assertEqual((both["needs"], both["reason_code"]), (["J2"], "needs_J2"))
        for gone in ("note", "reason"):                   # the old keywords are gone
            with self.assertRaises(TypeError):
                pb.not_run("a", ["J2"], **{gone: "x"})
        self.assertNotIn("reason", pb.not_run("a", ["J2"]))

    def test_missing_codes_restricted(self):
        for ok in (["band"], ["n"], ["ci"], ["n", "band"], ["band", "n", "ci"]):
            self.assertEqual(pb.partial_not_run("a", 1, missing=ok)["missing"], ok)
        for bad in ([], None, "band", ["bands"], ["band", "x"], [""], [1], ["BAND"]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                pb.partial_not_run("a", 1, missing=bad)
        c = pb.partial_not_run("a", 1, missing=["band"], reason_code="labeller_band_short")
        self.assertEqual((c["state"], c["reason_code"]), ("partial_not_run", "labeller_band_short"))

    def test_reason_code_length_capped(self):
        pb.not_run("a", [], reason_code="x" * 40)
        for bad in ("", "x" * 41, 7, None):
            if bad is None:                               # None with no needs is the "neither" error
                continue
            with self.assertRaises(ValueError, msg=repr(bad)):
                pb.not_run("a", [], reason_code=bad)
            with self.assertRaises(ValueError, msg=repr(bad)):
                pb.not_run("a", ["ladder"], reason_code=bad)
        with self.assertRaises(ValueError):
            pb.partial_not_run("a", 1, missing=["band"], reason_code="y" * 41)
        pb.cell("a", 1, derived=True, formula_code="z" * 40)
        for bad in ("", "z" * 41, 3):
            with self.assertRaises(ValueError, msg=repr(bad)):
                pb.cell("a", 1, derived=True, formula_code=bad)
        with self.assertRaises(TypeError):
            pb.cell("a", 1, derived=True, formula="text")     # formula text is gone

    def test_cell_formula_code(self):
        c = pb.cell("yield_per_box", 0.614, x="ai", derived=True, formula_code="yield_per_box:ai", source="kit:a1")
        self.assertEqual((c["formula_code"], c["derived"]), ("yield_per_box:ai", True))
        self.assertNotIn("formula", c)


class TestLabStringProblems(unittest.TestCase):
    def good(self):
        return [pb.cell("ai_recall", 0.5, x="primary", unit="node", kind="share", ci=(0.4, 0.6), source="kit:a1"),
                pb.not_run("r_star", ["sidejudge"], unit="node", kind="ratio", source="kit:a2_calibration"),
                pb.not_run("cell", [], x="primary", reason_code="cell_under_10"),
                pb.partial_not_run("w", 0.1, missing=["band", "n"], x="primary"),
                pb.cell("y", 0.6, x="ai", derived=True, formula_code="yield_per_box:ai", note="ci_undefined"),
                pb.cell("b", 1, band=pb.labeller_band([{"x": "judge", "value": 1}, {"x": "keyword", "value": 2}]))]

    def test_clean_profile_has_no_problems(self):
        self.assertEqual(pb.lab_string_problems(_rows(self.good())), [])
        self.assertEqual(pb.lab_string_problems({"rows": []}), [])

    def test_lab_string_problems_flags_planted_violations(self):
        secret = "SECRETVALUE" + "q" * 60
        planted = {
            "long_string": pb.cell("a", 1, x="primary", rests_on=[secret]),
            "long_in_band_member": pb.cell("b", 1, band={"lo": 1, "hi": 2, "over": "labellers",
                                                         "members": [{"x": secret, "value": 1}]}),
            "long_note": pb.cell("c", 1, note=secret),
            "reason_key": {**pb.cell("d", 1), "reason": "a free sentence"},
            "formula_key": {**pb.cell("e", 1), "formula": "M / N"},
            "unknown_reason_code": pb.not_run("f", [], reason_code="no_such_code_xyz"),
            "unknown_formula_code": pb.cell("g", 1, derived=True, formula_code="no_such_formula_xyz"),
            "unknown_missing": {**pb.partial_not_run("h", 1, missing=["band"]), "missing": ["nope"]},
            "unknown_note": pb.cell("i", 1, note="no_such_note_xyz"),
            "overlong_source": pb.cell("j", 1, source="s" * 201),
            "overlong_state": {**pb.cell("k", 1), "judge_state": "t" * 201},
            "not_run_neither": {"name": "l", "value": None, "state": "not_run"},
            "not_run_empty_needs": {"name": "m", "value": None, "state": "not_run", "needs": []},
            "partial_no_missing": {**pb.cell("n", 1), "state": "partial_not_run"},
            "derived_without_code": pb.cell("o", 1, derived=True),
            "reason_code_not_str": {**pb.cell("p", 1), "reason_code": 5},
        }
        for tag, c in planted.items():
            with self.subTest(tag):
                probs = pb.lab_string_problems(_rows([c], row=3))
                self.assertTrue(probs, tag)
                self.assertTrue(all(p.startswith("row 3 cell[0]") for p in probs), probs)    # position, not name (A2)
                self.assertFalse(any(secret in p or "s" * 50 in p or "t" * 50 in p for p in probs), "echoed a value")
        self.assertEqual(len(pb.lab_string_problems(_rows([planted["long_string"], planted["reason_key"]]))), 2)
        # an exact 40-character string and a 200-character source pass
        ok = pb.cell("q", 1, rests_on=["r" * 40], source="s" * 200, note="ci_undefined")
        ok["diag_state"] = "t" * 200
        self.assertEqual(pb.lab_string_problems(_rows([ok])), [])
        self.assertTrue(pb.lab_string_problems(_rows([pb.cell("q", 1, qualifiers=["r" * 41])])))

    def test_message_names_row_and_cell_position(self):
        """Final review A2: the message names the kit row id and the cell's position, never the name or x (values from
        the input)."""
        c = pb.cell("ai_recall", 1, x="primary", rests_on=["z" * 41])
        (msg,) = pb.lab_string_problems(_rows([c], row="lead"))
        self.assertIn("row lead", msg)
        self.assertIn("cell[0]", msg)
        self.assertNotIn("ai_recall", msg)
        self.assertNotIn("primary", msg)
        self.assertNotIn("z" * 41, msg)
        (msg,) = pb.lab_string_problems(_rows([c], row="SYN-OW07"))         # not a kit row id: its position
        self.assertTrue(msg.startswith("row [0] cell[0]"), msg)
        self.assertNotIn("SYN", msg)
        long_name = {**pb.cell("n", 1), "name": "N" * 70, "x": "X" * 70, "reason": "r"}
        self.assertFalse(any("N" * 50 in p or "X" * 50 in p for p in pb.lab_string_problems(_rows([long_name]))))

    def test_row_level_reason_and_formula_keys_are_flagged(self):
        prof = {"rows": [{"row": 1, "reason": "x", "cells": [pb.cell("a", 1)]}]}
        self.assertEqual(len(pb.lab_string_problems(prof)), 1)

    def test_row_level_strings_are_limited(self):
        """5S minor 7: every row string is at most 40 characters, including list items and label_key; row ``source`` items
        at most 200. Top-level and run-block strings are exempt (hashes, kit commit ids, long labels)."""
        secret = "SECRETROW" + "q" * 60

        def prof(**row_fields):
            return {"rows": [{"row": 3, "label_key": "profile.row3.support", "population": "12 of 12", "n_people": 12,
                              "cells": [pb.cell("a", 1)], "rests_on": ["R", "LX"], "source": ["kit:a1"],
                              "qualifiers": ["one_draw"], **row_fields}]}
        self.assertEqual(pb.lab_string_problems(prof()), [])
        planted = {
            "rests_on_item": prof(rests_on=["R", secret]), "qualifiers_item": prof(qualifiers=[secret]),
            "label_key": prof(label_key=secret), "population": prof(population=secret),
            "band_member": prof(band={"lo": 1, "hi": 2, "over": "labellers", "members": [{"x": secret, "value": 1}]}),
            "band_over": prof(band={"lo": 1, "hi": 2, "over": secret}),
            "source_item_201": prof(source=["kit:a1", "s" * 201]), "source_str_201": prof(source="s" * 201),
            "extra_field": prof(note=secret)}
        for tag, p in planted.items():
            with self.subTest(tag):
                probs = pb.lab_string_problems(p)
                self.assertTrue(probs, tag)
                self.assertTrue(all(m.startswith("row 3") for m in probs), probs)
                self.assertFalse(any(secret in m or "s" * 50 in m for m in probs), "echoed a value")
        # exact limits pass: 40 for ordinary row strings, 200 for a source item
        self.assertEqual(pb.lab_string_problems(prof(rests_on=["r" * 40], qualifiers=["q" * 40], label_key="l" * 40,
                                                     source=["s" * 200, "kit:a1"])), [])

    def test_top_level_and_run_strings_are_exempt(self):
        long = "x" * 300
        p = {"schema": "orgsci-profile/v2", "state": "synthetic", "population": {"label": long},
             "reference": {"type": long, "map_size": {"source": long, "note": long}},
             "pooling": {"within": [long]}, "run": {"commit": long, "hash": "0" * 64, "models": [{"family": long}]},
             "rows": [{"row": 1, "label_key": "profile.row1.coverage", "cells": [pb.cell("a", 1)]}]}
        self.assertEqual(pb.lab_string_problems(p), [])

    def test_codes_with_a_colon_suffix_use_the_code_before_it(self):
        c = pb.cell("a", 1, note="leak_split_indeterminate:3")
        self.assertEqual(pb.lab_string_problems(_rows([c])), [])
        c = pb.not_run("b", [], reason_code="leak_split_indeterminate:12")
        self.assertEqual(pb.lab_string_problems(_rows([c])), [])
        c = pb.cell("a", 1, note="ci_undefined;holm_family_lt_2")
        self.assertEqual(pb.lab_string_problems(_rows([c])), [])


class TestStringsFile(unittest.TestCase):
    def test_strings_file_flat_and_valid(self):
        raw = STRINGS.read_text(encoding="utf-8")
        d = json.loads(raw)
        self.assertIsInstance(d, dict)
        self.assertTrue(d)
        pat = re.compile(r"^profile\.(reason|missing|provider|role)\.[A-Za-z0-9_]+$|^profile\.formula\.[A-Za-z0-9_]+(:[A-Za-z0-9_]+)?$")
        for k, v in d.items():
            self.assertRegex(k, pat)
            self.assertIsInstance(v, str, k)
            self.assertGreaterEqual(len(v.strip()), 5 if k.startswith("profile.role.") else 10, k)    # role labels are short
            self.assertNotIn("\n", v, k)
            self.assertIsNone(BANNED.search(v), (k, "banned wording"))
            # the app shows these sentences; SPEC v5 V1 allows no user-visible "script"
            # DECISIONS L67 (council 23 D8): "no-interview script(s)" is the one allowed phrase (as in test_wording.py)
            self.assertNotRegex(re.sub(r"(?i)no-interview scripts?\b", " ", v), r"(?i)\bscript\w*", k)
        self.assertEqual(pb.load_profile_strings(), d)
        for code in ("band", "n", "ci"):                          # every missing code has its sentence
            self.assertIn(f"profile.missing.{code}", d)
        self.assertEqual({k for k in d if k.startswith("profile.missing.")},
                         {f"profile.missing.{c}" for c in pb.MISSING_CODES})
        self.assertEqual(json.loads(raw, object_pairs_hook=lambda ps: dict(ps)), d)     # nothing nested
        keys = [k for k, _ in json.loads(raw, object_pairs_hook=lambda ps: ps)]
        self.assertEqual(len(keys), len(set(keys)), "duplicate keys")

    def test_provider_strings_are_the_toolcfg_providers(self):
        """PROFILE-SCHEMA 14.8: ``run.remote_endpoints[*].provider`` is a ``toolcfg.PROVIDERS`` code (at most 40
        characters); each has a ``profile.provider.<code>`` sentence and no other such entry exists."""
        d = pb.load_profile_strings()
        self.assertEqual({k for k in d if k.startswith("profile.provider.")},
                         {f"profile.provider.{c}" for c in toolcfg.PROVIDERS})
        for c in toolcfg.PROVIDERS:
            self.assertLessEqual(len(c), 40, c)

    def test_role_strings_are_the_exporter_roles(self):
        """Ruling 18 Oct: ``run.remote_endpoints[*].role`` is a kit client name (recon, judge, judge2); each has a
        ``profile.role.<code>`` sentence and no other such entry exists."""
        from second_scribe import profile_export
        d = pb.load_profile_strings()
        self.assertEqual(tuple(profile_export.REMOTE_ROLES), ("recon", "judge", "judge2"))
        self.assertEqual({k for k in d if k.startswith("profile.role.")},
                         {f"profile.role.{c}" for c in profile_export.REMOTE_ROLES})
        self.assertEqual(d["profile.role.recon"], "Reconstructor (also draws the no-interview map)")
        self.assertEqual(d["profile.role.judge"], "Judge")
        self.assertEqual(d["profile.role.judge2"], "Second judge family")

    def test_the_four_formula_codes_exist(self):
        d = pb.load_profile_strings()
        for code in ("yield_per_box:ai", "yield_per_box:no_interview", "equal_size_bound", "listening_edge_equal_size"):
            self.assertIn("profile.formula." + code, d)                  # the key is the whole code
            self.assertTrue(d["profile.formula." + code].strip())
        self.assertNotEqual(d["profile.formula.yield_per_box:ai"], d["profile.formula.yield_per_box:no_interview"])

    def test_ci_undefined_fits_a_computed_cell_and_a_not_run_cell(self):
        """5S residual: one sentence serves both uses, so it must not talk of a value being given."""
        d = pb.load_profile_strings()
        sentence = d["profile.reason.ci_undefined"]
        self.assertNotRegex(sentence, r"(?i)value is given|so the value|given without")
        self.assertIn("interval", sentence)

    def test_bound_released_sentence_does_not_claim_the_present_case(self):
        sentence = pb.load_profile_strings()["profile.reason.bound_released_only_when_no_sign_of_it"]
        self.assertNotIn("here it does not", sentence)
        self.assertIn("includes 0", sentence)
        self.assertNotIn("so no bound", sentence)
        self.assertIn("not released on this run", sentence)

    def test_no_dead_judgescript_entry(self):
        self.assertNotIn("profile.reason.no_judgescript_source", pb.load_profile_strings())

    def test_export_codes_have_sentences(self):
        d = pb.load_profile_strings()
        for code in ("not_computable", "no_determinism_audit", "unjudged_nodes"):
            self.assertIn(f"profile.reason.{code}", d)

    def test_strings_file_is_a_file_next_to_the_package_modules(self):
        self.assertEqual(STRINGS.parent, Path(pb.__file__).resolve().parent)


# ------------------------------------------------------------------ every emittable code has a string
_CODE = re.compile(r"^[a-z][a-z0-9_]*$")


def _consts(node, whole=False):
    """The code-like strings an expression can evaluate to: constants in value positions (through if/else, or, tuples,
    lists and comprehension elements), not constants used as subscripts, comparands or call arguments. A reason code or note
    counts by the part before any ':' (a JoinedStr contributes its literal prefix); a formula code (``whole``) whole."""
    out = set()

    def add(text):
        t = text if whole else text.split(":")[0]
        if _CODE.match(t.replace(":", "_") if whole else t):
            out.add(t)

    def walk(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            add(n.value)
        elif isinstance(n, ast.JoinedStr):
            if n.values and isinstance(n.values[0], ast.Constant):
                add(str(n.values[0].value))
        elif isinstance(n, ast.IfExp):
            walk(n.body)
            walk(n.orelse)
        elif isinstance(n, ast.BoolOp):
            for v in n.values:
                walk(v)
        elif isinstance(n, (ast.Tuple, ast.List)):
            for v in n.elts:
                walk(v)
        elif isinstance(n, (ast.ListComp, ast.GeneratorExp)):
            walk(n.elt)
            walk(n.generators[0].iter)
    walk(node)
    return out


# positional index of the code argument, per (module, function)
_POSITIONAL = {("profile_common", "_nr"): 2, ("profile_floors", "_nr"): 2, ("profile_slant", "_nr"): 2,
               ("profile_slant", "_a7_not_run"): 0, ("profile_links", "_undefined"): 4}
_KEYWORDS = {"note": "reason", "extra_note": "reason", "reason_code": "reason", "formula_code": "formula",
             "run_chain_heads_source": "reason"}          # R25: the run block's source code for withheld chain heads
_CALL_KEYWORDS = {("profile_ladder", "_nr"): "code"}          # a helper whose keyword is not called note / reason_code
_NOTE_VARS = {"note", "notes", "floor_note"}
_RETURN_FUNCS = {"_ci_note", "_why"}


def emittable_codes() -> dict:
    """{"reason": {...}, "formula": {...}} found in the profile modules' source by AST."""
    found = {"reason": set(), "formula": set(), "provider": set(), "role": set()}
    for mod in MODULES:
        tree = ast.parse((REPO / "second_scribe" / f"{mod}.py").read_text(encoding="utf-8"))
        if mod == "profile_export" and any(isinstance(n, ast.Name) and n.id == "PROVIDERS" for n in ast.walk(tree)):
            found["provider"] |= set(toolcfg.PROVIDERS)      # run.remote_endpoints[*].provider: any code of the allow-list
        if mod == "profile_export" and any(isinstance(n, ast.Name) and n.id == "REMOTE_ROLES" for n in ast.walk(tree)):
            from second_scribe import profile_export
            found["role"] |= set(profile_export.REMOTE_ROLES)   # run.remote_endpoints[*].role
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                for kw in n.keywords:
                    if kw.arg in _KEYWORDS:
                        found[_KEYWORDS[kw.arg]] |= _consts(kw.value, kw.arg == "formula_code")
                fn = n.func.id if isinstance(n.func, ast.Name) else None
                if (mod, fn) in _CALL_KEYWORDS:
                    for kw in n.keywords:
                        if kw.arg == _CALL_KEYWORDS[(mod, fn)]:
                            found["reason"] |= _consts(kw.value)
                idx = _POSITIONAL.get((mod, fn))
                if idx is not None and len(n.args) > idx:
                    found["reason"] |= _consts(n.args[idx])
            elif isinstance(n, ast.Dict):
                for k, v in zip(n.keys, n.values):
                    if isinstance(k, ast.Constant) and k.value in _KEYWORDS:
                        found[_KEYWORDS[k.value]] |= _consts(v, k.value == "formula_code")
            elif isinstance(n, ast.Assign):
                if any(isinstance(t, ast.Name) and t.id in _NOTE_VARS for t in n.targets):
                    found["reason"] |= _consts(n.value)
                for t in n.targets:                       # cell["note"] = "code" (profile_export)
                    if (isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) and t.slice.value in _KEYWORDS):
                        found[_KEYWORDS[t.slice.value]] |= _consts(n.value, t.slice.value == "formula_code")
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = n.args.args + n.args.kwonlyargs
                defaults = [None] * (len(n.args.args) - len(n.args.defaults)) + list(n.args.defaults) + list(n.args.kw_defaults)
                for a, d in zip(args, defaults):
                    if d is not None and a.arg in _KEYWORDS:
                        found[_KEYWORDS[a.arg]] |= _consts(d)
                if n.name in _RETURN_FUNCS:
                    for r in ast.walk(n):
                        if isinstance(r, ast.Return) and r.value is not None:
                            found["reason"] |= _consts(r.value)
    # the codes that are built at run time are listed in a module constant that the builder reads
    found["reason"] |= set(pc.DYNAMIC_CODES)
    # audit K4: the certificate's remote sentences name every allow-list provider and remote role by these strings
    from second_scribe import certificate
    found["provider"] |= set(certificate.REMOTE_PROVIDERS)
    found["role"] |= set(certificate.REMOTE_ROLES)
    found["reason"] |= set(pc.PUBLIC_PROFILE_CODES)
    return found


class TestEveryCodeHasAString(unittest.TestCase):
    def test_every_emittable_code_has_a_string(self):
        d = pb.load_profile_strings()
        found = emittable_codes()
        # the scanner itself must see the known codes, or it has rotted
        for code in ("ci_undefined", "cell_under_10", "pooled_only", "ladder_unfinished", "rung_not_configured",
                     "empty_denominator", "no_own_script", "variant_does_not_cover_all_maps", "leak_split_indeterminate",
                     "not_computable", "no_determinism_audit", "unjudged_nodes"):
            self.assertIn(code, found["reason"], f"scanner lost {code}")
        self.assertEqual(found["formula"], {"yield_per_box:ai", "yield_per_box:no_interview", "equal_size_bound",
                                            "listening_edge_equal_size"})
        self.assertGreaterEqual(len(found["reason"]), 20)
        self.assertEqual([c for c in sorted(found["reason"]) if f"profile.reason.{c}" not in d], [])
        self.assertEqual([c for c in sorted(found["formula"]) if f"profile.formula.{c}" not in d], [])
        self.assertEqual(found["provider"], set(toolcfg.PROVIDERS), "scanner lost the provider codes")
        self.assertEqual([c for c in sorted(found["provider"]) if f"profile.provider.{c}" not in d], [])
        self.assertEqual(found["role"], {"recon", "judge", "judge2"}, "scanner lost the role codes")
        self.assertEqual([c for c in sorted(found["role"]) if f"profile.role.{c}" not in d], [])

    def test_every_string_entry_is_used(self):
        d = pb.load_profile_strings()
        found = emittable_codes()
        used = ({f"profile.reason.{c}" for c in found["reason"]} | {f"profile.formula.{c}" for c in found["formula"]}
                | {f"profile.missing.{c}" for c in pb.MISSING_CODES}
                | {f"profile.provider.{c}" for c in found["provider"]} | {f"profile.role.{c}" for c in found["role"]})
        self.assertEqual(sorted(set(d) - used), [], "strings nobody can emit")

    def test_dynamic_codes_constant(self):
        self.assertEqual(tuple(pc.DYNAMIC_CODES), ("leak_split_indeterminate",))

    def test_public_profile_codes_have_strings(self):
        # the study's public profile (analysis/public/profile.json) writes model source "not_recorded_public"
        self.assertEqual(tuple(pc.PUBLIC_PROFILE_CODES), ("not_recorded_public",))
        d = json.loads(STRINGS.read_text(encoding="utf-8"))
        for c in pc.PUBLIC_PROFILE_CODES + ("available_from_authors",):
            self.assertIn(f"profile.reason.{c}", d)


# ------------------------------------------------------------------ the demo rows
def _no_ladder_ctx(tmp):
    import subprocess
    import sys
    d = json.loads(Path(DEMO_CONFIG).read_text(encoding="utf-8"))
    d["base_dir"] = str(Path(DEMO_CONFIG).parent)
    d.pop("ladder", None)
    cfg = Path(tmp) / "config.json"
    cfg.write_text(json.dumps(d), encoding="utf-8")
    out = Path(tmp) / "demo"
    r = subprocess.run([sys.executable, "-m", "second_scribe.cli", "demo", "--config", str(cfg), "--out", str(out),
                        "--client", "oracle"], cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-1500:]
    return pb.open_context(cfg, out)


def assemble(ctx) -> dict:
    """Every row builder of Tasks 2-5, the way the exporter will call them."""
    labs = lb.labeller_numbers(ctx)
    ladder = pl.rung1_size(ctx)
    cells_by_row = {
        "lead": pf.lead_cells(ctx), "ledger": cov.ledger_cells(ctx, labs), 1: cov.row1_cells(ctx),
        2: cov.row2_cells(ctx, ladder), 3: cov.row3_cells(ctx, labs), 4: lk.row4_cells(ctx), 5: pf.row5_cells(ctx),
        6: sl.row6_cells(ctx), 7: pf.row7_cells(ctx), 8: pl.row8_cells(ctx),
        9: cov.row9_floor_cells(ctx) + pl.row9_rejudge_cells(ctx)}
    return {"rows": [{"row": k, "cells": v} for k, v in cells_by_row.items()]}


class TestDemoRows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.demo, cls.grid = contexts()
        tmp = tempfile.TemporaryDirectory(prefix="ss-tests-e2-t5s-")
        cls.addClassCleanup(tmp.cleanup)
        cls.noladder = _no_ladder_ctx(tmp.name)

    def check(self, ctx):
        prof = assemble(ctx)
        self.assertGreater(sum(len(r["cells"]) for r in prof["rows"]), 100)
        self.assertEqual(pb.lab_string_problems(prof), [])
        return prof

    def test_demo_row_cells_pass_lab_limits(self):
        self.check(self.demo)
        self.check(self.grid)

    def test_demo_row_cells_pass_lab_limits_without_the_ladder(self):
        prof = self.check(self.noladder)
        nr = [c for r in prof["rows"] for c in r["cells"] if c.get("state") == "not_run"]
        self.assertTrue(any(c["needs"] == ["ladder"] for c in nr))

    def test_small_populations_pass_lab_limits(self):
        """1, 2 and 5 analysed maps reach the withheld / too-few / undefined-interval codes the full demo never does."""
        import dataclasses
        for ctx, k in ((self.grid, 1), (self.grid, 2), (self.demo, 5)):
            prof = self.check(dataclasses.replace(ctx, analysed=ctx.analysed[:k]))
            codes = {c["reason_code"].split(":")[0] for r in prof["rows"] for c in r["cells"] if "reason_code" in c}
            self.assertTrue(codes, k)
            if k == 1:
                self.assertIn("too_few_maps", codes)

    def test_no_cell_carries_a_sentence_field(self):
        for ctx in (self.demo, self.grid, self.noladder):
            for r in assemble(ctx)["rows"]:
                for c in r["cells"]:
                    self.assertFalse({"reason", "formula"} & set(c), (r["row"], c["name"]))

    def test_derived_cells_carry_formula_codes(self):
        prof = assemble(self.grid)
        got = {c["formula_code"] for r in prof["rows"] for c in r["cells"] if c.get("derived") is True}
        self.assertEqual(got, {"yield_per_box:ai", "yield_per_box:no_interview", "equal_size_bound",
                               "listening_edge_equal_size"})


if __name__ == "__main__":
    unittest.main()
