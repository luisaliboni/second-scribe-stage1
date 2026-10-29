# Pre-registration template

The pre-registration fixes the analysis before any real-data model call. `prereg freeze` writes
`<out_dir>/prereg.json` as canonical JSON and appends one `prereg_frozen` record, holding the file's sha256, to the
run's hash-chained audit log (`<out_dir>/logs/run.jsonl`). `prereg check` and the guard in `pipeline.run` (active only when the
config sets `"prereg_required": true`; a pre-registration is optional, not required for real runs) confirm that the file still has the logged hash, that the
prompt files, the judge label conventions (`docs/prereg-judge-labels.md`), the config and the code are the frozen ones,
and that no model call was logged before the freeze. Synthetic demo runs keep `prereg_required` false, and so does a real run that does not opt in.

    python3 -m second_scribe.cli prereg freeze --config CFG [--calibration CALIBRATION.json]
    python3 -m second_scribe.cli prereg check  --config CFG

Model ids and weight identifiers come from `SS_<PREFIX>_MODEL` and `SS_<PREFIX>_WEIGHTS` (a weight-file sha256 or a
Hugging Face revision; `pending` is accepted for a synthetic corpus only). For a real corpus `freeze` also refuses when the matcher
calibration aggregates or the off-record calibration aggregates (`--calibration`, keys `matcher` and `in_transcript`)
are missing. A real run does not need a freeze: `prereg_required` is an optional opt-in (default false).
Endpoints and keys never enter the file; a model id is cut to its last path part. `freeze` also refuses when the
working tree has uncommitted changes under `second_scribe/` or `prompts/`.

What a freeze refuses, and what the check proves afterwards:

- `freeze` refuses when `<out_dir>/work` or `<out_dir>/aggregates` already hold files, or when the log already holds a
  model call: a freeze after the run proves nothing, so start from a fresh output folder. Calibration and benchmark runs
  write to their own log.
- Every work item written under `prereg_required` lists the `request_ids` of the calls that produced it. `check` and the
  guard refuse an item whose request ids are not calls logged after the freeze in this log. A model-stage item (mapping
  talk, in_transcript, recon, prior_only, the judge stages, canonicalize, align) with status ok or fallback must name at
  least one call; only items that make no call by construction are exempt (no speaker labels or nothing flagged, no labels
  to ask about, nothing to align, a judge item served by the cache or by rule, and the statuses too_long_excluded and
  no_speakers_excluded). Forging request ids with write access to the output folder is out of scope.
- The judge label cache is keyed on the judge model id and params as well as the prompt, and each cache entry records the
  `request_id` of the call that produced it. A judge item served from the cache lists those ids; `check` refuses a cache
  entry (or an item) whose ids are not judge calls logged after the freeze in this log, so a cache copied in from another
  run fails even with the same model. An item also names only calls of its own stage whose `item_id` is its own name (judge
  items may add the calls behind cached labels, listed in `stats.cache_request_ids`), and no call is claimed by two items.
  Every model-stage item except `too_long_excluded`, `no_speakers_excluded` and the zero-call `ok` cases must name at
  least one call, `failed` included.
- The config hash covers the corpus inputs as a sorted list of (logical name, file sha256): `maps/<path relative to the
  glob's folder>` and `transcripts/<...>` for globs, `pairs/<pid>/map|transcript` for explicit `pairs` (read as literal
  paths, never globbed), and `synthetic/<relative path>` for every file under `synthetic_dir`. Moving the corpus folder is
  fine; changing, renaming, swapping or symlink-duplicating a file is not (symlinked sub-folders of `synthetic_dir` are followed; a link loop ends). A pid listed in `roles` or `pairs` with no map
  or no transcript file makes the hash (and so `freeze` and `check`) fail.
- A corpus labelled `synthetic` is checked file by file with `realpath`; a `..` after a glob's wildcard is refused.
- Every file under `second_scribe/` and `prompts/` is hashed from disk at the freeze and again at the check, so git
  index flags (skip-worktree, assume-unchanged, info/exclude) cannot hide an edit. The same map covers every `*.py` at the
  kit root and every top-level package folder, and a root file or package named like a standard-library module (a stray
  `random.py`) makes the check, the guard and `freeze` refuse. That covers every importable suffix (`.py`, `.pyc`, `.so`,
  `.pyd`, `random.cpython-*.so`, packages) at the root and directly in each top-level folder; those files are also in the
  hashed map. Only `__pycache__` folders of the kit's own code are looked at (`second_scribe/`, `tests/`, top-level packages,
  and the one at the kit root; dot-folders, `.venv`, `venv`, `out/` and `.superpowers/` are skipped). A `.pyc` for
  another Python version, or an `.opt-N` one when `sys.flags.optimize` is not N, is ignored; any other must be the
  compile of its current source (source present); a stale one Python would recompile is ignored. Delete the
  `__pycache__` folder to clear a refusal. The git ancestor check is an extra.
- Each call after the freeze must name the frozen model id (its basename and the sha256 of the full id) and the frozen
  inference params of its stage. If the judge will also be called in letter (logprob) mode, freeze with `--letter-mode`:
  the letter-mode params (`logprobs`, `top_logprobs`) are then frozen with the judge, and a letter-mode call is held to the
  model's params plus those; without the flag, or with one of them changed, a letter-mode call is refused.
- A real freeze also needs the default primary setting, a settings list that covers the whole grid, weights that are a
  64-hex sha256 or a short revision (never a path), and calibration aggregates that pass the aggregate-only string guard.
- `freeze` writes `prereg.sha256` (one line, `<sha256> <run_id>`), next to `prereg.json` or in `--anchor-dir`. Commit it
  to the private runner repository, outside the output folder; `prereg check --require-anchor FILE` then proves that the
  file in use is the one that was anchored. `freeze` checks that the anchor folder is writable before it freezes; if writing
  the anchor still fails afterwards it prints the digest and run id to anchor by hand and exits non-zero. Whether to
  publish the anchor is a decision for the researcher.

## What `prereg.json` holds

- run id, creation time, corpus kind, code commit, config sha256;
- the prompt files with their sha256 and the hash of the whole set (`prompts/MANIFEST.json`);
- the judge label conventions (sha256 and full text);
- spec keys (primary: `removed__p0__t0__r0`), match settings (primary first) and the full settings grid, prior-only
  design, stats options, the seed rule `derive_seed(item_id, spec_key) / derive_seed(item_id, batch_index)`;
- model ids and weight identifiers per role;
- thresholds: `min_words` 8 (sensitivity grid 5, 8, 12), `max_unfound_share` 0.50, script support 2, not-judged limit 0.10;
- the coverage rule, the canonical gate for the pull claim, the script-conditional leakage claim rule, the not-judged rule, the
  canonicalization batching rule (batches of 50 in a seeded order), and the judge pass rule on the synthetic traps;
- the headline order: absences, pull_toward_script, homogenization, linearization;
- the calibration aggregates of the matcher and the off-record detector (counts and rates only);
- the decision rules below, verbatim.

## Decision rules (frozen verbatim)

1. Primary estimate: spec `removed__p0__t0__r0`, setting `PRIMARY_SETTING` (as set by calibration).
2. A directional claim is made only if the 95% paired bootstrap CI excludes 0 for the primary estimate **and** the sign is stable across all 8 match settings × variation specs (p1, p2, T=0.7).
3. Maps with failed/excluded reconstructions are dropped from paired comparisons; counts reported.
4. Transcripts with `speaker_format == "none"` are excluded from the removed arm and from speaker-gated metrics; counts reported.
5. Linearization is two-sided. Homogenization is reported overall and by interviewer.
6. Thresholds are never changed after freezing; any post-hoc analysis is labelled exploratory.
7. Coverage: a sign-stability claim needs every cell of the grid (match settings × variation specs) to have a value (no nan), to be valid (at least max(3, 50% of the primary cell's n) maps) and to have the same sign. A failed spec or a thin cell blocks the claim; it is never skipped.
8. Pull toward the script is claimed only on canonical labels: the canonical gate requires canonical labels and canonical alignments for every participant, every spec of the grid and every prior-only sample. Raw-label and judge-only lines are robustness checks, not claims.
9. Script-conditional leakage (the lead test) is claimed only from an other-family script source: among matched in-transcript endorsed ideals, the leak rate of the scripted ideals minus that of the unscripted ones, with the script flag permuted within each map. The claim needs the permutation p below 0.05 and the bootstrap CI lower bound above 0. The reconstructor's own script is a sensitivity row and never sets the flag. The base rate (the AI's wished share against the endorsed wished share) and the worse_than_chance_check (the within-map status-permutation null) are reported, never headlines and never gates.
10. A spec with more than 10% of its elements not judged (for the primary spec, the quote-shuffled re-judge included) is reported as `reported_not_interpreted`.

## Wording

Maps are "interviewer-drawn, participant-endorsed maps"; AI reconstructions are compared with them, and no claim
treats a drawn map as a complete record of what happened.
