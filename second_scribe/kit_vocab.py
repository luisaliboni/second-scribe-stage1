"""The kit's own words in the aggregates, for the aggregate-only guard (``report.assert_aggregate_only``).

The guard refuses any key or value that equals a map label or quote (case-insensitive). A care-pathway map may
well have a node labelled "Family", "Other" or "Role", and those words are also the kit's own schema keys
(``script_sources.<name>.family``) and enum values (the script family ``other`` / ``own``): the guard then refused
the kit's own text and M could not write its aggregates. These words come from the code (schema keys, reason and
verdict codes, rung names, the model-family table), never from an interview, so they are exempt from the label
check ONLY by exact, case-sensitive match, keys and values separately: ``family`` as a key passes, ``Family`` (as a
label would be written), ``family`` as a value, or ``role`` as a value do not. The length rule still applies to them.

Not listed, and so still checked like any other string: names that come from the config or the environment (run id,
script-source names, spec keys, match settings, model ids) and anonymous ids (``#3``, counts). A new metric with a
new word-like key or value fails ``tests/test_m_kit_words.py`` until it is listed here."""
from __future__ import annotations

KIT_KEYS: frozenset[str] = frozenset((
    "B", "a", "absences", "accepted_attempt", "actual_recall", "adequacy_flattening", "adequacy_flattening_rate",
    "adjusted", "agreement", "ai_attested_matched", "ai_below_endorsed", "ai_node_count", "ai_wished_share", "align",
    "all", "alpha", "alternative", "approximate", "arm_of_rung", "assignment", "attested_absent_from_map",
    "attested_line", "b", "base_rate", "better", "branching_share", "buckets", "by_arm", "by_draw", "by_field",
    "by_interviewer", "by_kind", "by_rung", "by_spec", "by_stage", "by_status", "call_order",
    "canonical_fallback_reasons", "canonical_used", "canonicalize", "ci", "ci_excludes_zero", "ci_hi", "ci_lo",
    "claim_blocked", "claim_pulled_toward_script", "claim_script_conditional_leakage", "complete", "concept",
    "concept_diff_A_minus_B", "config_sha256", "contaminated_share", "contradiction_rate", "contrasts", "controls",
    "copy_audit", "corpus_kind", "count", "counterfactual_leakage", "counterfactual_leakage_rate", "coverage",
    "cyclomatic", "dead_end_share", "declaration_check", "declared_on", "density", "descriptive", "descriptive_only",
    "diff", "differences", "different", "disagreement_reference", "disagreement_reference_draws_missing",
    "disagreement_vs_rung2", "disagreement_vs_rung2_experienced", "disagreements", "draw_identity", "draws",
    "draws_per_map", "edge_recall", "endorsed_wished_share", "estimate", "exact", "exact_max_nonzero",
    "excluded_reasons", "excludes_leak_drop_larger_than", "exclusions", "expected", "expected_rank_null", "failed",
    "fallback_counts", "fallback_reasons", "fallbacks", "false_wished_rate", "family", "family_reason", "files_without_speakers",
    "flagged_ids", "flip_rate", "flipped", "full", "full_spec", "gaps", "gating", "glued", "glued_only", "headline",
    "headline_eligible", "holm_leak", "homogenization", "in_transcript", "in_transcript_only", "interpretation",
    "items", "j_inputs_missing", "judge", "judge2_modality", "judge_channel_canonical", "judge_modality",
    "judge_only", "judge_shuffled", "judging", "kept", "kept_rate", "label", "label_source", "ladder", "leak_ci_hi",
    "leak_ci_lo", "leak_mean", "leak_p_holm", "leak_p_perm", "leak_quote_wished_rate", "leak_rate", "leak_rate_diff",
    "leak_rate_scripted", "leak_rate_unscripted", "leak_split", "leak_split_indeterminate", "leakage", "leaked",
    "leaked_as_existing", "leaked_quote_actual", "leaked_quote_other", "leaked_quote_unjudged", "leaked_quote_wished",
    "leaked_rate", "leaked_unanchored", "leaked_unanchored_rate", "limit", "linearization", "longest_path_ratio",
    "low_power", "m", "map_id", "map_mean", "map_mean_rate", "mapping_talk", "mapping_talk_confirm", "maps",
    "maps_with_coercion", "matched", "max", "mean", "mean_a", "mean_ai", "mean_ai_recall", "mean_b", "mean_diff",
    "mean_distance_a", "mean_distance_b", "mean_endorsed", "mean_jaccard_a", "mean_jaccard_b", "mean_map_share",
    "mean_prior_recall", "mean_rank", "measures", "metrics", "min", "min_cell_n", "min_maps", "min_n_required",
    "min_words", "mirror", "mirror_worsened", "miss_rate", "missed", "missed_rate", "missing",
    "missing_prior_samples", "modality_source", "model", "model_confirmed", "model_flagged", "model_only", "models",
    "n", "n_ai_wished", "n_analysed", "n_attested_elements", "n_boot_valid", "n_cells", "n_contaminated",
    "n_contradicting", "n_control", "n_derangements", "n_draws", "n_edges", "n_edges_eligible", "n_edges_recovered",
    "n_elements", "n_endorsed_not_judged", "n_endorsed_wished", "n_excluded", "n_flagged", "n_gating_checks",
    "n_ideals", "n_identical", "n_in_source", "n_interviewer_turns", "n_items", "n_leaked", "n_leaked_scripted",
    "n_leaked_unscripted", "n_low_n", "n_maps", "n_maps_all_identical", "n_maps_used", "n_maps_with_ideals",
    "n_maps_with_null", "n_matched_nodes", "n_missing", "n_nan", "n_neg", "n_nodes", "n_nontrivial_scc",
    "n_not_judged", "n_null_phase", "n_outputs", "n_pairs", "n_pairs_unknown_excluded", "n_participants", "n_perm",
    "n_pos", "n_raw_ai", "n_raw_endorsed", "n_raw_prior", "n_recall", "n_same_set", "n_screened", "n_scripted",
    "n_structure_features", "n_unscripted", "n_used", "n_wished", "n_zero", "name", "no_located_wished_quotes",
    "no_matched_ideals", "no_shuffled_judge", "no_wished_in_transcript", "node_recall", "node_recall_diff",
    "node_recall_mean", "nodes", "nominal_false_alarm_rate", "not_assessed", "not_judged", "not_judged_share", "note",
    "null_mean_diff", "null_p95", "null_phase", "observed_exceeds_null_p95", "observed_mean_diff", "off_record", "ok",
    "other_arms", "outage", "own", "p", "p_holm", "p_method", "p_perm", "paired_diff", "paired_diff_quote_wished",
    "parameters", "parse_failure_rate", "parse_failures", "per_map", "per_rung", "permutation_rank", "placebo",
    "placebo-rung2", "pooled", "pooled_share", "positive_and_stable", "present", "primary", "primary_low_power",
    "primary_setting", "primary_source", "primary_spec", "prior_mean_diff", "prior_minus_endorsed", "prior_only",
    "prior_only_baseline", "prior_only_contrast", "prior_specs", "prompt_manifest_ok", "prompt_set_sha256",
    "pull_toward_script", "quote_actual_marked_wished", "quote_cutoff_sensitivity", "quote_tiers", "quote_type",
    "quote_wished_marked_experienced", "rate_of_wished", "raw_labels", "real_recall_mean", "real_share_mean",
    "reason", "reason_codes", "recon", "reconstructor_family", "recovered_as_wished", "recovery_rate", "reference",
    "remedy", "remedy-placebo", "remedy_contrast", "removed", "removed_spec", "report", "require_holm",
    "require_kept_gain", "retries", "robustness", "role", "rows", "rule_flagged", "rule_only", "rules", "run_id",
    "rung1", "rung2", "rung2-rung1", "rung3", "rungs", "same", "script_conditional", "script_family",
    "script_prior_only", "script_source", "script_sources", "seed", "separate_samples", "settings", "share",
    "share_identical", "share_of_leaks", "share_rank1", "shuffled", "shuffled_pairs", "shuffled_quotes",
    "shuffled_recall_mean", "shuffled_share_mean", "sign", "sign_stability", "sign_stability_all_edges",
    "significant", "skipped", "source", "spec", "specs", "stability_grid", "stable", "status", "structure",
    "structure_distance_diff_A_minus_B", "symptom_of_script", "test", "three_way", "timeout_excluded_items",
    "transport_errors", "type_coercions", "type_share:actor", "type_share:actor_information_source",
    "type_share:information_source", "type_share:other", "type_share:scenario", "type_share:service",
    "type_share_affected", "unanchored", "unanchored_wished", "unattested", "unattested_rate", "unfound", "unknown",
    "unscreened_turns", "variation_specs", "verdict", "verdict_rules", "wished_rate_q", "wished_share", "worse",
    "worse_than_chance_check",
))

KIT_VALUES: frozenset[str] = frozenset((
    "as_declared", "boot_maps", "canonical", "ci_excludes_zero_bad_side", "claude", "condition-major", "deepseek",
    "different_family", "disagreement_vs_rung2", "disagreement_vs_rung2_experienced", "exact", "family_unknown",
    "gemma", "glm", "gpt-oss", "greater", "grid_incomplete", "higher", "interpreted", "judge", "kept_rate", "kimi",
    "leak_ci_includes_zero", "leaked_rate", "llama", "lower", "minimax", "mistral", "no_test", "not_applicable",
    "null", "ok", "other", "own", "phi", "placebo", "qwen", "real", "recon", "recon_model_not_on_record", "remedy",
    "remedy-placebo", "round_robin_by_role", "rung1", "rung2", "rung3", "same_model_or_family",
    "script_is_qwen_family", "script_model_not_on_record", "several_script_models", "signflip", "synthetic",
    "two-sided", "type_share_includes_coerced_other", "unattested_rate",
))

# verdicts, statuses and reason codes the code writes on paths the demo does not take (a remedy that helps or
# harms, an outage, an over-limit not-judged share...): listed from the code so the full run does not trip on them
KIT_VALUES = KIT_VALUES | frozenset((
    "contrast_not_run", "failed", "fallback", "harms", "helps", "incomplete", "inconclusive", "leak_drop_holm_fails",
    "leak_drop_no_mirror_worse", "leak_rose", "leak_rose_holm_fails", "leak_too_few_maps", "mixed",
    "no_script_units", "no_speakers_excluded", "not_helps", "not_judged_over_limit", "not_run", "quarantined",
    "raw_fallback", "reported_not_interpreted", "timeout_excluded", "too_long_excluded",
))

# 2.4 (U05): the duplicate-transcript count, present only when two transcript files are byte-identical
KIT_KEYS = KIT_KEYS | frozenset(("copies", "duplicate_transcripts", "groups"))

# the optional config block ``reading3`` (reading 3 drawn apart from the script set), named beside ``prior_only``
KIT_KEYS = KIT_KEYS | frozenset(("reading3",))

# K6: the chain block (``chain.head``, ``chain.records``, ``chain.verified``) every aggregates file carries
KIT_KEYS = KIT_KEYS | frozenset(("chain", "head", "records", "verified"))

# work-item statuses, which ``items.<stage>`` counts BY status (keys): the demo has only some of them
KIT_KEYS = KIT_KEYS | frozenset((
    "failed", "fallback", "no_script_units", "no_speakers_excluded", "not_applicable", "not_judged", "ok",
    "quarantined", "timeout_excluded", "too_long_excluded",
))
