"""The publish gate for a lab's profile file (schema ``orgsci-profile/v2``, PROFILE-SCHEMA.md sections 1-4, 9, 12).

``assert_publishable`` is the app's second gate on a lab profile that is about to leave the app (the exporter's own
privacy scan is the first). It fails closed: a key outside the allow-list, a group of people under k, a missing
people count, a per-map, per-interviewer, per-participant or per-role key, a 64-hex string outside ``run``, a long
string, a node label or quote, an address or a dated run id each refuse the whole file. Splits carried in VALUES
are refused too (audit K1): every row label (cell name and x, band member x, codes, sources) comes from a closed
vocabulary (``CATALOGUE``, PROFILE-SCHEMA §8, plus the §3/§6/§7 codes), a (row, name, x) appears once and a row
holds at most its catalogue's cells. The exporter's own §13 check (``profile_base.lab_string_problems``) runs inside
the gate (audit K2), and ``run.remote_endpoints`` carries only {role, provider, declaration_sha256} (§14.8, audit
K8/S2). The refusal names a code and a JSON path, never the offending text (an unknown key shows as ``<key N>``, its
position in its object).

The gate does not compute anything: the exporter owns the cells and the analysis session owns the public file.
Standalone: the standard library, ``second_scribe.report`` and ``second_scribe.profile_base`` (one §13 rule, not two).

CLI: ``python3 -m second_scribe.publish check --profile P.json [--k 10]`` exits 0, or 2 with ``code where`` lines
on stderr (the first line is the refusal ``assert_publishable`` raises).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import ipaddress
import json
import re
import sys
from pathlib import Path

from .profile_base import BAND_OVER, lab_string_problems, load_profile_strings
from .report import MAX_STRING, write_text_atomic

SCHEMAS = ("orgsci-profile/v2",)
DETECTION_LIMITS_SCHEMA = "orgsci-detection-limits/v1"
LAB_STATES = frozenset({"synthetic", "pending"})          # the app never exports "reported"


class PublishRefused(ValueError):
    """The gate's refusal: ``code`` names the rule, ``where`` a JSON path. Neither carries the offending text."""

    def __init__(self, code: str, where: str):
        super().__init__(f"{code} at {where}")
        self.code = code
        self.where = where


# ------------------------------------------------------------------ the allow-list (PROFILE-SCHEMA.md §1-§4, §12, §14)
# A spec is LEAF (a scalar), LIST (a list of strings: codes and cleared ids), "plain" (a small object of scalars,
# only in detection_limits), a dict {key: spec} (an object), a one-element list [spec] (a list of such objects), or
# OneOf(spec, ...) where one key has two schema forms (the study's v2 run block and the kit exporter's, §14).
# ``None`` is accepted for every key: a null carries nothing. A list where a scalar belongs is refused, so no
# per-map vector can ride in a value.
LEAF = "leaf"
LIST = "list"


class OneOf(tuple):
    """Alternative specs for one key. An object takes the first dict alternative that holds every one of its keys
    (so two forms never mix: {head, j} matches neither); a list takes the list alternative; a scalar takes LEAF.
    No alternative: ``not_allowed`` at the key itself."""

    def pick(self, x):
        for alt in self:
            if isinstance(x, dict):
                if isinstance(alt, dict) and all(isinstance(k, str) and k in alt for k in x):
                    return alt
            elif isinstance(x, (list, tuple)):
                if alt == LIST or isinstance(alt, list):
                    return alt
            elif alt == LEAF:
                return alt
        return None


_STATE_KEY = re.compile(r"^[a-z0-9_]+_state$")            # "<field>_state" notes, only where map_size has them
_DOSE_KEY = re.compile(r"^[a-z][a-z0-9_]*$")              # detection_limits: one object per dose type

BAND = {"lo": LEAF, "hi": LEAF, "over": LEAF, "narrow": LEAF,
        "members": [{"x": LEAF, "value": LEAF, "value_hi": LEAF}],
        "subbands": [{"name": LEAF, "lo": LEAF, "hi": LEAF}]}
CELL = {"name": LEAF, "x": LEAF, "n": LEAF, "unit": LEAF, "kind": LEAF, "value": LEAF, "ci_lo": LEAF,
        "ci_hi": LEAF, "ci_level": LEAF, "p": LEAF, "p_lt": LEAF, "p_holm": LEAF, "band": BAND, "rests_on": LIST,
        "qualifiers": LIST, "derived": LEAF, "formula_code": LEAF, "note": LEAF, "source": LEAF,
        "state": LEAF, "needs": LIST,                        # §12: cell state and needs
        "p_holm_lt": LEAF, "reason_code": LEAF, "missing": LIST}  # §3/§13 (18 Oct); free `reason` stays refused
ROW = {"row": LEAF, "label_key": LEAF, "population": LEAF, "n_people": LEAF, "cells": [CELL], "band": BAND,
       "rests_on": LIST, "source": LIST, "qualifiers": LIST}
MATCHER = {"lexical_threshold": LEAF, "judge_label": LEAF}
# The eight match settings (PROFILE-SCHEMA §3, band over "8_settings"), as the kit's run.settings.match_settings
# names them (§14.4).
MATCH_SETTINGS = {name: MATCHER for name in (
    "primary", "lenient", "lexical_only_04", "lexical_only_05", "lexical_only_06", "lexical_only_07",
    "judge_only_same", "judge_only_same_or_close")}
RUN = {
    "gate_chain": {"head": LEAF, "entries": LEAF, "verified": LEAF, "verify_date": LEAF, "source": LEAF},
    "declaration_log": {"head": LEAF, "records": LEAF, "last_seq": LEAF, "verified": LEAF, "source": LEAF},
    "run_chain_heads": OneOf(({"q": LEAF, "j": LEAF, "m": LEAF, "source": LEAF},               # v2 (§4)
                              {"head": LEAF, "records": LEAF, "manifest_hash": LEAF, "source": LEAF})),  # §14.1
    "run_chain_heads_source": LEAF,       # a code (checked in _chain_heads_source): why run_chain_heads is null (§4, §14)
    "kit": {"release_commit": LEAF, "release_branch": LEAF,
            "run_commits": OneOf(({"q": LEAF, "j": LEAF, "m": LEAF}, LIST)),                   # v2 / §14.2
            "code_sha256": LEAF,                                                                # village E6
            "source": LEAF},
    "prompt_set": {"q_phase_sha256": LEAF, "j_m_phase_sha256": LEAF, "source": LEAF},
    "reading3_prompt": {"sha256": LEAF, "working_tree_sha256": LEAF, "agree": LEAF, "path": LEAF, "source": LEAF},
    "models": [{"role": LEAF, "family": LEAF, "model_id": LEAF, "quantisation": LEAF, "weights": LEAF,
                "thinking": LEAF, "source": LEAF}],
    "settings": {"matcher_primary": MATCHER, "matcher_lenient": MATCHER, "reconstructor_temperature": LEAF,
                 "reading3_temperature": LEAF, "reading3_draws": LEAF, "gate_k_people": LEAF,
                 "interval_method": LEAF, "bootstrap_resamples": LEAF, "bootstrap_seed": LEAF, "n_perm": LEAF,
                 "match_settings": MATCH_SETTINGS, "matcher_row4_lenient": MATCHER,                 # §14.4
                 "source": LEAF},
    "verify": {"q": LEAF, "j": LEAF, "m": LEAF, "gate_chain": LEAF, "source": LEAF},
    "remote_endpoints": [{"role": LEAF, "provider": LEAF, "declaration_sha256": LEAF}],        # §14.8: no host, no text
}
DOSE = {"smallest_detected": LEAF, "lowest_tried": LEAF, "at_or_below": LEAF}
DETECTION_LIMITS = {"schema": LEAF, "pull_planted": LEAF, "pull_detected": LEAF, "dose0_bands": "plain",
                    "settings": "plain", "kit_commit": LEAF, "limit": LEAF}
TOP = {
    "schema": LEAF, "state": LEAF,
    "population": {"label": LEAF, "n_people": LEAF, "n_total": LEAF},
    "reference": {"type": LEAF,
                  "map_size": {"ai_boxes": LEAF, "endorsed_boxes": LEAF, "ai_per_map": LEAF,
                               "endorsed_per_map": LEAF, "ai_per_endorsed": LEAF, "source": LEAF,
                               "reading3_map_size": {"nodes_per_map": LEAF, "edges_per_map": LEAF,
                                                     "over_ai_nodes": LEAF, "note": LEAF, "source": LEAF}}},
    "pooling": {"rule": LEAF, "within": LIST, "meta_analysis": LEAF},
    "rows": [ROW],
    "run": RUN,
    "detection_limits": DETECTION_LIMITS,
}

# 64-hex values allowed inside ``run`` (exempt from long_string): chain heads and prompt hashes.
HASH_KEYS = frozenset({"head", "sha256", "working_tree_sha256", "q_phase_sha256", "j_m_phase_sha256",
                       "reading3_prompt_sha256", "manifest_hash", "code_sha256"})
_HEAD_KEYS = frozenset({"q", "j", "m"})                    # ...and the run's own chain heads under run_chain_heads
DECLARATION_KEY = "declaration_sha256"                     # ...and, only at run.remote_endpoints[i], a declaration hash (§14.8)

PER_MAP_KEYS = frozenset({"per_map", "by_map", "maps", "items", "evidence"})
_PER_MAP_KEY = re.compile(r"^#\d+(#\d+)?$")
IDENTITY_WORDS = ("interview", "participant", "role", "record_hash", "entry", "map_id", "pair_id")   # §9
PEOPLE_UNITS = frozenset({"map", "maps", "person", "people", "participant", "participants", "pair", "pairs",
                          "transcript", "transcripts", "interview", "interviews"})   # one map per person

LONG_400 = frozenset()                                     # only detection_limits.limit (handled in _length);
                                                           # label_key is <= 40 like any lab string (audit K2);
                                                           # §13: lab exports carry formula_code, never formula
LONG_200 = frozenset({"source"})                           # plus "<field>_state" values
MODEL_ID_KEYS = frozenset({"model_id", "weights"})         # under run.models: <= 80, no host / date test
MODEL_ID_MAX = 80
RUN_ID_KEYS = frozenset({"run_id", "run_ids"})

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_HEX_RUN = re.compile(r"[0-9a-fA-F]{64}")                 # a 64-hex anywhere inside a string
_HOST = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}(:\d+)?$")
_IPV4 = re.compile(r"(?<![\d.])\d{1,3}(\.\d{1,3}){3}(?![\d.])")
_IPV6_BRACKET = re.compile(r"\[[0-9a-f:.]*:[0-9a-f:.]*\]")
_LOCALHOST = re.compile(r"\blocalhost\b")
_LABEL_KEY = re.compile(r"^profile(\.[a-z0-9_]+)+$")
_DATED = re.compile(r"\d{4}-?\d{2}-?\d{2}|\d{8}T\d{4,6}")

# ------------------------------------------------------------------ the closed vocabulary (audit K1)
# A split by role, interviewer or map can ride in a VALUE as well as in a key (x = "nurse", twelve cells x = "m0" ..
# "m11"). So every value-carrying label in a row comes from a closed list: the cell (name, x) pairs of the PROFILE-
# SCHEMA §8 row catalogue, the §3/§6/§7 codes, and the strings-file codes of §13 (checked by
# profile_base.lab_string_problems). A (row, name, x) appears once, and a row holds at most its catalogue's cells.
# CATALOGUE is §8 in (name, x) form, row -> (label_key, cells in order); a test holds it equal to
# tests/data/profile_v2_skeleton.json, the skeleton the kit exporter's own output is tested against.
CATALOGUE = {
    "lead": ("profile.lead", (
        ("ai_recall", "primary"), ("no_interview_recall", "primary"), ("listening_added", "primary"),
        ("ai_recall", "lenient"), ("no_interview_recall", "lenient"), ("listening_added", "lenient"),
        ("listening_added_attested_line", "primary"), ("listening_added_attested_line", "lenient"),
        ("real_recall", "primary"), ("shuffled_recall", "primary"), ("wrong_transcript_difference", "primary"),
        ("own_map_mean_rank", "primary"), ("rank_null_expectation", "primary"), ("ai_nodes_per_map", None),
        ("no_interview_nodes_per_map", None), ("ai_minus_no_interview_nodes_per_map", None),
        ("ai_edges_per_map", None), ("no_interview_edges_per_map", None),
        ("ai_minus_no_interview_edges_per_map", None), ("endorsed_nodes_per_map", None),
        ("no_interview_over_ai_nodes", None), ("yield_per_box", "ai"), ("yield_per_box", "no_interview"),
        ("equal_size_bound", "no_interview"), ("listening_edge_equal_size", "no_interview"),
    )),
    "ledger": ("profile.ledger", (
        ("omitted_share", None), ("attested_absent_from_map", "primary"), ("unanchored", "primary"),
        ("off_record_endorsed", "primary"), ("attested_absent_from_map", "labellers"), ("ai_boxes_total", None),
        ("ai_boxes_attested_on_map", "primary"),
    )),
    1: ("profile.row1.coverage", (
        ("kept", None), ("coarsened", None), ("omitted", None), ("attested_recall", "primary"),
        ("attested_recall", "lenient"), ("r_star", None),
    )),
    2: ("profile.row2.size", (
        ("ai_boxes", None), ("endorsed_boxes", None), ("ai_per_endorsed", None), ("ai_per_map", None),
        ("endorsed_per_map", None), ("ai_per_endorsed", "rung1"), ("ai_nodes_per_map", None),
        ("ai_edges_per_map", None), ("endorsed_nodes_per_map", None),
    )),
    3: ("profile.row3.support", (
        ("precision", "primary"), ("attested_recall", "primary"), ("attested_absent_from_map", "primary"),
        ("unanchored", "primary"),
    )),
    4: ("profile.row4.links", (
        ("endpoint_lost", "primary"), ("collapsed", "primary"), ("eligible_share", "primary"),
        ("eligible_edges", "primary"), ("direct", "primary"), ("direct_null_expected", "primary"),
        ("direct_over_null", "primary"), ("undirected", "primary"), ("path_le2", "primary"),
        ("collapsed_share_of_matched", "primary"), ("endpoint_lost", "lenient"), ("eligible_share", "lenient"),
        ("eligible_edges", "lenient"), ("direct", "lenient"),
    )),
    5: ("profile.row5.absences", (
        ("n_ideals", "primary"), ("n_matched", "primary"), ("recovered_as_wished", "primary"),
        ("flattened", "primary"), ("counterfactual", "primary"), ("missed", "primary"), ("unanchored", "primary"),
        ("n_matched", "lenient"), ("recovered_as_wished", "lenient"), ("flattened", "lenient"),
        ("counterfactual", "lenient"), ("missed", "lenient"), ("flattening_rate", "primary"),
        ("counterfactual_upper_bound", "all_ideals"), ("counterfactual_upper_bound", "matched"),
        ("counterfactual_upper_bound", "matched_lenient"), ("recovery_rate", "primary"),
        ("matcher_free_rate", "judge"), ("matcher_free_rate", "judge2"), ("matcher_free_rate", "keyword"),
    )),
    6: ("profile.row6.slant", (
        ("pull", "canonical"), ("pull_90", "canonical"), ("pull_variant", "raw_wording"),
        ("pull_variant", "judge_only"), ("pull_variant", "in_transcript_only"),
        ("scripted_share_unanchored", "own_script"), ("scripted_share_attested_absent", "own_script"),
        ("scripted_share_difference", "own_script"), ("scripted_share_difference", "other_family_script"),
        ("largest_excess_ruled_out", "other_family_script"),
    )),
    7: ("profile.row7.floors", (
        ("ai_recall", "primary"), ("no_interview_recall", "primary"), ("listening_added", "primary"),
        ("ai_recall", "lenient"), ("no_interview_recall", "lenient"), ("listening_added", "lenient"),
        ("listening_added_attested_line", "primary"), ("listening_added_attested_line", "lenient"),
        ("real_recall", "primary"), ("shuffled_recall", "primary"), ("wrong_transcript_difference", "primary"),
        ("own_map_mean_rank", "primary"), ("rank_null_expectation", "primary"),
    )),
    8: ("profile.row8.instruction", (
        ("ai_per_map", "rung1"), ("ai_per_map", "rung2"), ("ai_per_map", "remedy"), ("ai_per_map", "placebo"),
        ("size_adjusted_recall", "rung1"), ("size_adjusted_recall", "rung2"), ("size_adjusted_recall", "remedy"),
        ("size_adjusted_recall", "placebo"), ("size_adjusted_recall_2_minus_1", "rung2-rung1"),
        ("lived_recall_remedy_minus_placebo", "remedy-placebo"), ("ai_nodes_remedy_minus_placebo", "remedy-placebo"),
        ("false_wished_remedy_minus_placebo", "remedy-placebo"), ("flip_remedy_minus_placebo", "remedy-placebo"),
        ("leak_rate", "remedy"), ("leak_rate", "placebo"), ("leak_rate_remedy_minus_placebo", "remedy-placebo"),
        ("leak_rate_remedy_minus_placebo_90", "remedy-placebo"), ("mde80_leak_contrast", "remedy-placebo"),
    )),
    9: ("profile.row9.instrument_error", (
        ("lexical_floor", "lexical_only_04"), ("any_node_recall", "primary"), ("rejudge_changed", "lived_steps"),
        ("rejudge_changed_share", "lived_steps"), ("rejudge_net", "lived_steps"), ("rejudge_changed", "ideals"),
        ("rejudge_net", "ideals"), ("attested_recall", "judge"), ("attested_absent_from_map", "judge"),
        ("unanchored", "judge"), ("matcher_free_rate", "judge"), ("quote_actual_drawn_as_wished", "judge"),
        ("contaminated_share", "judge"), ("flip_rate", "modality"), ("identical_requests", "modality"),
        ("flip_rate", "alignment"), ("identical_requests", "alignment"), ("identical_requests", "canonical_names"),
        ("canonical_ari", "canonical_names"), ("label_order_changed_rate", "modality"),
        ("identical_requests", "run2_determinism_audit"),
    )),
}
UNITS = frozenset({"node", "ai_box", "edge", "ideal", "map", "request", "item", "label", "element"})   # §3 + kit
KINDS = frozenset({"share", "count", "difference", "ratio", "rank", "rate", "upper_bound", "index", "mean"})
CELL_STATES = frozenset({"not_run", "partial_not_run"})                                       # §12
DERIVED = frozenset({"ratio_ai_per_endorsed", "per_map_mean", "sum_of_cleared_counts", "net",  # §3.1, §3.2 + kit
                     "ledger_band", "ratio_of_cleared_counts", "sum_of_counts"})
RESTS_ON = frozenset({"R", "P", "JS", "IT", "AL", "AS", "CN", "MO", "LD", "J2", "Q", "LX"})   # §6
QUALIFIERS = frozenset({                                                                       # §7
    "ceiling", "not_bounded_below", "descriptive", "one_draw", "package_contrast", "ci_excludes_draw_variance",
    "paired_by_map", "attested_line_one_sided", "derived_arithmetic", "no_interval", "zero_count",
    "labeller_dependent", "partly_lexical", "no_structural_claim", "seam_withheld_k", "recall_is_a_band", "no_ratio",
    "not_reproducible_to_the_digit", "size_beside_lead", "pooled_only", "not_a_test", "floor_not_estimate"})
SUBBANDS = frozenset({"judge_using", "lexical_only"})                                          # §3 band
# band.members[].x: a setting, labeller, rung or variant (§3 band): the catalogue's x codes, the 8 match settings
# and the three labellers.
MEMBER_X = (frozenset(x for _key, cells in CATALOGUE.values() for _name, x in cells if x is not None)
            | frozenset(MATCH_SETTINGS) | frozenset({"judge", "judge2", "keyword"}))
_SOURCE_ID = r"(?:kit:[a-z0-9_]+|cleared/\d{3})"
_SOURCE = re.compile(rf"^{_SOURCE_ID}(?:\+{_SOURCE_ID})*$")       # a cell's source: ids joined with "+" (§3)
_SOURCE_ONE = re.compile(rf"^{_SOURCE_ID}$")                      # one item of a row's source list (§2)
_POPULATION = re.compile(r"^\d{1,9} of \d{1,9}$")                 # "43 of 46" (§2)
_CODE = re.compile(r"^[A-Za-z0-9_]+(?::\d{1,9})?$")              # reason_code / a note part: a code (§13: needs_J2),
                                                                 # maybe ":<count>" (leak_split_indeterminate:3)

# run.remote_endpoints (§14.8): the kit's client role (Reading 3 goes through recon; judge2 is the second judge
# family), a provider code, the declaration record's sha256 (ruling 18 Oct, analysis via the coordinator).
REMOTE_ROLES = frozenset({"recon", "judge", "judge2"})
KNOWN_PROVIDERS = frozenset({"openai_compat", "anthropic"})       # toolcfg.PROVIDERS
_PROVIDER = re.compile(r"^[a-z0-9_.-]{1,40}$")
_PROVIDER_PREFIX = "profile.provider."


def _provider_codes() -> frozenset:
    """The provider codes a profile may name: the kit's known ones, plus any with a ``profile.provider.<code>`` sentence
    in the kit's strings file."""
    return KNOWN_PROVIDERS | frozenset(k[len(_PROVIDER_PREFIX):] for k in load_profile_strings()
                                       if k.startswith(_PROVIDER_PREFIX))
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _known_keys(spec, out: set) -> set:
    if isinstance(spec, dict):
        for k, v in spec.items():
            out.add(k)
            _known_keys(v, out)
    elif isinstance(spec, OneOf):
        for alt in spec:
            _known_keys(alt, out)
    elif isinstance(spec, list):
        _known_keys(spec[0], out)
    return out


# Key names that may appear in a refusal's path: the schema's own words. Any other key shows as "<key N>".
_SHOWN_KEYS = frozenset(_known_keys(TOP, set()) | _known_keys(DOSE, set()) | HASH_KEYS | RUN_ID_KEYS)


def _step(where: str, key, i: int) -> str:
    return f"{where}.{key}" if isinstance(key, str) and key in _SHOWN_KEYS else f"{where}.<key {i}>"


def _norm(s: str) -> str:
    return " ".join(s.split()).casefold()


def _people(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _looks_like_address(s: str, *, host_shape: bool = True) -> bool:
    c = s.strip().casefold()
    if "://" in c or (host_shape and _HOST.fullmatch(c)):
        return True
    if _IPV4.search(c) or _IPV6_BRACKET.search(c) or _LOCALHOST.search(c):
        return True
    try:
        ipaddress.ip_address(c.strip("[]"))
        return True
    except ValueError:
        return False


# ------------------------------------------------------------------ pass 1: keys and contents, the whole tree
def _content(x, path: tuple, where: str, bad: frozenset):
    """Yield (code, where) for privacy rules that hold anywhere, whatever the allow-list says: per-map and identity
    keys, 64-hex outside run, labels and quotes, addresses and dated run ids. ``path`` holds the real keys (list
    positions as ints); ``where`` the printable path."""
    in_run = len(path) > 0 and path[0] == "run"
    if isinstance(x, dict):
        for i, (k, v) in enumerate(x.items()):
            w = _step(where, k, i)
            if not isinstance(k, str):
                yield "not_allowed", w
                continue
            here = f"{where}.<key {i}>"                       # the key IS the offending text: position only
            if k in PER_MAP_KEYS or _PER_MAP_KEY.fullmatch(k):
                yield "per_map_key", here
            if not in_run and any(word in k.casefold() for word in IDENTITY_WORDS):   # §9: outside run
                yield "identity_key", here
            if k not in _SHOWN_KEYS:                          # the schema's own words are not labels
                yield from _string(k, path + (k,), here, bad, as_key=True)
            yield from _content(v, path + (k,), w, bad)
    elif isinstance(x, (list, tuple)):
        for i, v in enumerate(x):
            yield from _content(v, path + (i,), f"{where}[{i}]", bad)
    elif isinstance(x, str):
        yield from _string(x, path, where, bad)


def _string(s: str, path: tuple, where: str, bad: frozenset, *, as_key: bool = False):
    in_run = len(path) > 0 and path[0] == "run"
    if _HEX_RUN.search(s):
        if not in_run:
            yield "hex_outside_run", where
        elif as_key or not (_HEX64.fullmatch(s) and _is_hash_path(path)):
            yield "record_hash", where                    # inside run, only a whole digest under HASH_KEYS
    if _norm(s) in bad:
        yield "label_or_quote", where
    if as_key:
        return
    model_id = _is_model_id(path)
    # A strings-file key ("profile.row1.coverage") has a host's shape; it is tested for everything else.
    label_key = bool(path) and path[-1] == "label_key" and bool(_LABEL_KEY.fullmatch(s))
    if not model_id and _looks_like_address(s, host_shape=not label_key):
        yield "endpoint_host", where
    if not model_id and _is_run_id(path) and _DATED.search(s):
        yield "dated_run_id", where


def _is_hash_path(path: tuple) -> bool:
    keys = [p for p in path if isinstance(p, str)]
    if not keys or keys[0] != "run":
        return False
    if keys[-1] == DECLARATION_KEY:                           # exactly run.remote_endpoints[<int>].declaration_sha256
        return (len(path) == 4 and keys == ["run", "remote_endpoints", DECLARATION_KEY]
                and isinstance(path[2], int) and not isinstance(path[2], bool))
    return keys[-1] in HASH_KEYS or (keys[-1] in _HEAD_KEYS and len(keys) >= 2 and keys[-2] == "run_chain_heads")


def _is_model_id(path: tuple) -> bool:
    return (len(path) == 4 and path[0] == "run" and path[1] == "models" and isinstance(path[2], int)
            and path[3] in MODEL_ID_KEYS)


def _is_run_id(path: tuple) -> bool:
    keys = [p for p in path if isinstance(p, str)]
    if not keys:
        return False
    last = keys[-1]
    return last == "run_id" or last.endswith("_run_id") or (last == "run_ids" and isinstance(path[-1], int))


# ------------------------------------------------------------------ pass 2: the allow-list, lengths and k
def _structure(x, spec, path: tuple, where: str, k: int):
    if x is None:
        return
    if isinstance(spec, OneOf):
        spec = spec.pick(x)
        if spec is None:
            yield "not_allowed", where
            return
    if spec == LEAF:
        if isinstance(x, (dict, list, tuple)):
            yield "not_allowed", where
        else:
            yield from _length(x, path, where)
        return
    if spec in (LIST, "plain_list"):                       # strings only; "plain_list": any scalars
        if not isinstance(x, (list, tuple)):
            yield "not_allowed", where
            return
        for i, v in enumerate(x):
            if isinstance(v, (dict, list, tuple)) or (spec == LIST and not isinstance(v, str)):
                yield "not_allowed", f"{where}[{i}]"
            else:
                yield from _length(v, path + (i,), f"{where}[{i}]")
        return
    if spec == "plain":                                    # a small object of scalars (detection_limits blocks)
        if not isinstance(x, dict):
            yield "not_allowed", where
            return
        for i, (key, v) in enumerate(x.items()):
            w = _step(where, key, i)
            if not isinstance(key, str) or not _DOSE_KEY.fullmatch(key):
                yield "not_allowed", w
                continue
            if len(key) > MAX_STRING:
                yield "long_string", w
            sub = ("plain" if isinstance(v, dict) and len(path) < 4
                   else "plain_list" if isinstance(v, (list, tuple)) else LEAF)
            yield from _structure(v, sub, path + (key,), w, k)
        return
    if isinstance(spec, list):
        if not isinstance(x, (list, tuple)):
            yield "not_allowed", where
            return
        for i, v in enumerate(x):
            yield from _structure(v, spec[0], path + (i,), f"{where}[{i}]", k)
        return
    if not isinstance(x, dict):
        yield "not_allowed", where
        return
    for i, (key, v) in enumerate(x.items()):
        w = _step(where, key, i)
        sub = spec.get(key) if isinstance(key, str) else None
        if sub is None and isinstance(key, str):
            if path == ("reference", "map_size") and _STATE_KEY.fullmatch(key):
                sub = LEAF
            elif path == ("detection_limits",) and _DOSE_KEY.fullmatch(key) and isinstance(v, dict):
                sub = DOSE
        if sub is None:
            yield "not_allowed", w
            continue
        if len(key) > MAX_STRING:                            # keys are never exempt
            yield "long_string", w
        yield from _structure(v, sub, path + (key,), w, k)
    yield from _people_rules(x, path, where, k)


def _people_rules(x: dict, path: tuple, where: str, k: int):
    """Fail closed on groups: population and every row must carry an int people count of at least k; a cell whose
    unit counts people (one map per person) must have n >= k."""
    is_row = len(path) == 2 and path[0] == "rows" and isinstance(path[1], int)
    is_cell = len(path) == 4 and path[0] == "rows" and path[2] == "cells"
    if path == ("population",) or is_row:
        n = x.get("n_people")
        if not _people(n):
            yield "no_people_count", f"{where}.n_people"
        elif n < k:
            yield "cell_below_k", f"{where}.n_people"
    elif is_cell and isinstance(x.get("unit"), str) and x["unit"].strip().casefold() in PEOPLE_UNITS and "n" in x:
        n = x["n"]
        if n is None:
            return
        if not _people(n):
            yield "no_people_count", f"{where}.n"
        elif n < k:
            yield "cell_below_k", f"{where}.n"


def _length(s, path: tuple, where: str):
    """Strings (keys included) are at most report.MAX_STRING characters, with the schema's exemptions."""
    if not isinstance(s, str) or len(s) <= MAX_STRING:
        return
    if _HEX64.fullmatch(s) and _is_hash_path(path):
        return                                             # a chain head or prompt hash inside run
    keys = [p for p in path if isinstance(p, str)]
    last = keys[-1] if keys else ""
    limit = MAX_STRING
    if last in LONG_400 or (path[:2] == ("detection_limits", "limit") and len(path) == 2):
        limit = 400
    elif last in LONG_200 or _STATE_KEY.fullmatch(last):
        limit = 200
    elif _is_model_id(path):
        limit = MODEL_ID_MAX
    if len(s) > limit:
        yield "long_string", where


# ------------------------------------------------------------------ pass 3: the closed vocabulary (audit K1)
def _known_row(rid) -> bool:
    return type(rid) in (int, str) and rid in CATALOGUE          # True and 1.0 are not row 1


def _codes_in(items, allowed: frozenset, where: str):
    if isinstance(items, (list, tuple)):
        for i, v in enumerate(items):
            if not (isinstance(v, str) and v in allowed):
                yield "unknown_label", f"{where}[{i}]"


def _band_labels(band, where: str):
    if not isinstance(band, dict):
        return
    over = band.get("over")
    if over is not None and not (isinstance(over, str) and over in BAND_OVER):
        yield "unknown_label", f"{where}.over"
    for key, field, allowed in (("members", "x", MEMBER_X), ("subbands", "name", SUBBANDS)):
        items = band.get(key)
        if not isinstance(items, (list, tuple)):
            continue
        seen = set()
        for i, m in enumerate(items):
            if not isinstance(m, dict):
                continue
            v = m.get(field)
            if not (isinstance(v, str) and v in allowed):
                yield "unknown_label", f"{where}.{key}[{i}].{field}"
            elif v in seen:
                yield "duplicate_label", f"{where}.{key}[{i}]"
            else:
                seen.add(v)


def _code_text(v, *, joined: bool) -> bool:
    """A reason code (or, ``joined``, a note of codes joined with ";"): each part a code, with at most a ":<count>"."""
    if not isinstance(v, str):
        return False
    return all(_CODE.fullmatch(part) for part in (v.split(";") if joined else [v]))


def _cell_labels(c: dict, where: str):
    for key, allowed in (("unit", UNITS), ("kind", KINDS), ("state", CELL_STATES)):
        v = c.get(key)
        if v is not None and not (isinstance(v, str) and v in allowed):
            yield "unknown_label", f"{where}.{key}"
    d = c.get("derived")
    if d is not None and d is not True and not (isinstance(d, str) and d in DERIVED):
        yield "unknown_label", f"{where}.derived"
    yield from _codes_in(c.get("rests_on"), RESTS_ON, f"{where}.rests_on")
    yield from _codes_in(c.get("qualifiers"), QUALIFIERS, f"{where}.qualifiers")
    src = c.get("source")
    if src is not None and not (isinstance(src, str) and _SOURCE.fullmatch(src)):
        yield "unknown_label", f"{where}.source"
    if c.get("reason_code") is not None and not _code_text(c["reason_code"], joined=False):
        yield "unknown_label", f"{where}.reason_code"
    if c.get("note") is not None and not _code_text(c["note"], joined=True):
        yield "unknown_label", f"{where}.note"
    yield from _band_labels(c.get("band"), f"{where}.band")


def _vocabulary(doc: dict):
    """Every row label from the closed lists; (row, name, x) once; at most the catalogue's cells per row."""
    rows = doc.get("rows")
    if not isinstance(rows, (list, tuple)):
        return
    seen_rows, seen_cells = set(), set()
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        w = f"$.rows[{i}]"
        rid = row.get("row")
        known = _known_row(rid)
        if not known:
            yield "unknown_label", f"{w}.row"
        elif rid in seen_rows:
            yield "duplicate_label", f"{w}.row"
        else:
            seen_rows.add(rid)
        lk = row.get("label_key")
        if lk is not None and not (isinstance(lk, str) and _LABEL_KEY.fullmatch(lk)
                                   and (not known or lk == CATALOGUE[rid][0])):
            yield "unknown_label", f"{w}.label_key"
        pop = row.get("population")
        if pop is not None and not (isinstance(pop, str) and _POPULATION.fullmatch(pop)):
            yield "unknown_label", f"{w}.population"
        yield from _codes_in(row.get("rests_on"), RESTS_ON, f"{w}.rests_on")
        yield from _codes_in(row.get("qualifiers"), QUALIFIERS, f"{w}.qualifiers")
        src = row.get("source")
        if isinstance(src, (list, tuple)):
            for j, v in enumerate(src):
                if not (isinstance(v, str) and _SOURCE_ONE.fullmatch(v)):
                    yield "unknown_label", f"{w}.source[{j}]"
        yield from _band_labels(row.get("band"), f"{w}.band")
        cells = row.get("cells")
        if not isinstance(cells, (list, tuple)):
            continue
        pairs = CATALOGUE[rid][1] if known else ()
        if known and len(cells) > len(pairs):
            yield "too_many_cells", f"{w}.cells"
        names = {n for n, _x in pairs}
        for j, c in enumerate(cells):
            if not isinstance(c, dict):
                continue
            wc = f"{w}.cells[{j}]"
            name, x = c.get("name"), c.get("x")
            if not isinstance(name, str) or (known and name not in names):
                yield "unknown_label", f"{wc}.name"
            elif x is not None and not isinstance(x, str):
                yield "unknown_label", f"{wc}.x"
            elif known and (name, x) not in pairs:
                yield "unknown_label", f"{wc}.x"
            elif known and (rid, name, x) in seen_cells:
                yield "duplicate_label", wc
            elif known:
                seen_cells.add((rid, name, x))
            yield from _cell_labels(c, wc)


# ------------------------------------------------------------------ pass 4: §13 strings (audit K2) and §14.8
def _lab_strings(doc: dict):
    """profile_base.lab_string_problems, the exporter's own §13 check, located per row and per cell. Its messages are
    not passed on (the gate names a path, never the text); a check that cannot read the row refuses it."""
    rows = doc.get("rows")
    if not isinstance(rows, (list, tuple)):
        return

    def problems(profile) -> bool:
        try:
            return bool(lab_string_problems(profile))
        except Exception:                                  # a shape the check cannot read: fail closed
            return True

    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        if problems({"rows": [{k: v for k, v in row.items() if k != "cells"}]}):
            yield "lab_string", f"$.rows[{i}]"
        cells = row.get("cells")
        if not isinstance(cells, (list, tuple)):
            continue
        for j, c in enumerate(cells):
            if isinstance(c, dict) and problems({"rows": [{"row": row.get("row"), "cells": [c]}]}):
                yield "lab_string", f"$.rows[{i}].cells[{j}]"


def _remote_endpoints(doc: dict):
    """run.remote_endpoints (§14.8): each entry carries a kit client role (recon, judge, judge2), a provider code
    (<= 40 of [a-z0-9_.-], known or with a profile.provider.<code> string) and a lowercase 64-hex
    declaration_sha256, all three, once each. Other keys are refused by the allow-list."""
    run = doc.get("run")
    eps = run.get("remote_endpoints") if isinstance(run, dict) else None
    if not isinstance(eps, (list, tuple)):
        return
    seen = set()
    providers = _provider_codes()
    for i, e in enumerate(eps):
        if not isinstance(e, dict):
            continue
        w = f"$.run.remote_endpoints[{i}]"
        role, provider, sha = e.get("role"), e.get("provider"), e.get("declaration_sha256")
        ok = True
        if not (isinstance(role, str) and role in REMOTE_ROLES):
            ok = False
            yield "remote_endpoint", f"{w}.role"
        if not (isinstance(provider, str) and _PROVIDER.fullmatch(provider) and provider in providers):
            ok = False
            yield "remote_endpoint", f"{w}.provider"
        if not (isinstance(sha, str) and _SHA256.fullmatch(sha)):
            ok = False
            yield "remote_endpoint", f"{w}.declaration_sha256"
        if ok:
            if (role, provider, sha) in seen:
                yield "duplicate_label", w
            seen.add((role, provider, sha))


def _chain_heads_source(doc: dict):
    """run.run_chain_heads_source (§4, §14): the code that says why the run chain heads are null (for a lab export made
    with --withhold-chain-heads: ``available_from_authors``). A code of at most 40 characters from a-z, 0-9, _ . -,
    never a sentence: its sentence is the kit's ``profile.reason.<code>``."""
    run = doc.get("run")
    if not isinstance(run, dict) or run.get("run_chain_heads_source") is None:
        return
    v = run["run_chain_heads_source"]
    if not (isinstance(v, str) and _PROVIDER.fullmatch(v)):
        yield "not_allowed", "$.run.run_chain_heads_source"


# ------------------------------------------------------------------ the gate
def _violations(doc, k: int, schema: str, forbidden=frozenset()):
    """Every refusal of ``doc``, in order: (code, where). ``assert_publishable`` raises the first one."""
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError("k must be an int >= 1")
    if schema not in SCHEMAS or not isinstance(doc, dict) or doc.get("schema") != schema:
        yield "unknown_schema", "$.schema" if isinstance(doc, dict) else "$"
        return
    if doc.get("state") not in LAB_STATES:
        yield "state_not_lab", "$.state"
    run = doc.get("run")
    if isinstance(run, dict):
        for key in ("gate_chain", "declaration_log"):
            if run.get(key) is not None:
                yield "lab_lock_field", f"$.run.{key}"
        r3 = run.get("reading3_prompt")
        if isinstance(r3, dict) and r3.get("agree") is not None:
            yield "lab_lock_field", "$.run.reading3_prompt.agree"
    dl = doc.get("detection_limits")
    if isinstance(dl, dict) and dl.get("schema") not in (None, DETECTION_LIMITS_SCHEMA):
        yield "unknown_schema", "$.detection_limits.schema"
    bad = frozenset(_norm(s) for s in forbidden if isinstance(s, str) and s.strip())
    yield from _content(doc, (), "$", bad)
    yield from _structure(doc, TOP, (), "$", k)
    yield from _vocabulary(doc)
    yield from _lab_strings(doc)
    yield from _remote_endpoints(doc)
    yield from _chain_heads_source(doc)


def assert_publishable(doc: dict, k: int, schema: str, *, forbidden: frozenset[str] = frozenset()) -> None:
    """Refuse (``PublishRefused``) a lab profile that may not leave the app; return None when it may.

    ``k``: the smallest group of people allowed (10 by default in the CLI). ``forbidden``: node labels and quotes
    of the run (held in memory, never written), compared whitespace-collapsed and casefolded with every string."""
    for code, where in _violations(doc, k, schema, forbidden):
        raise PublishRefused(code, where)


def violations(doc: dict, k: int, schema: str, *, forbidden: frozenset[str] = frozenset()) -> list[tuple[str, str]]:
    """All refusals as (code, where) pairs, for the CLI and the export preview. Empty means publishable."""
    return list(_violations(doc, k, schema, forbidden))


def differs_by_fewer_than_k(prev: dict, new: dict, k: int) -> bool:
    """True when two exports' ``population.n_people`` differ by 1..k-1 (subtracting them would expose a group under
    k; the app warns, F25). A missing or non-int count also warns (fail closed)."""
    try:
        a, b = prev["population"]["n_people"], new["population"]["n_people"]
    except (KeyError, TypeError):
        return True
    if not (_people(a) and _people(b)):
        return True
    return 0 < abs(a - b) < k


# ------------------------------------------------------------------ files
CSV_COLUMNS = ("row", "name", "x", "value", "ci_lo", "ci_hi", "ci_level", "n", "unit", "state")


def _csv_value(v) -> str:
    return "" if v is None else str(v)


def write_public(doc: dict, json_path: Path, csv_path: Path) -> tuple[str, str]:
    """Write the profile as canonical JSON (``sort_keys``, ``indent=2``, ASCII, newline-terminated) and as a CSV with
    one line per cell in file order, both atomically. Returns (sha256 of the JSON, sha256 of the CSV). The caller
    runs ``assert_publishable`` first; a null stays empty in the CSV, never 0."""
    text = json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for row in doc.get("rows") or []:
        for c in row.get("cells") or []:
            w.writerow([_csv_value(row.get("row"))] + [_csv_value(c.get(col)) for col in CSV_COLUMNS[1:]])
    table = buf.getvalue()
    write_text_atomic(json_path, text)
    write_text_atomic(csv_path, table)
    return (hashlib.sha256(text.encode("utf-8")).hexdigest(), hashlib.sha256(table.encode("utf-8")).hexdigest())


# ------------------------------------------------------------------ CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m second_scribe.publish",
                                 description="Check a lab profile (orgsci-profile/v2) against the publish gate.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    chk = sub.add_parser("check", help="exit 0 if the profile may leave the app, else 2 with 'code where' lines")
    chk.add_argument("--profile", required=True, type=Path)
    chk.add_argument("--k", type=int, default=10)
    args = ap.parse_args(argv)
    if args.k < 1:
        ap.error("--k must be at least 1")
    try:
        doc = json.loads(args.profile.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        print("unknown_schema $", file=sys.stderr)       # unreadable: never echo the content
        return 2
    found = violations(doc, args.k, SCHEMAS[0])
    if found:
        for code, where in found:
            print(f"{code} {where}", file=sys.stderr)
        return 2
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
