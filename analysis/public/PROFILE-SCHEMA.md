# Distance profile file: schema `orgsci-profile/v2`

A profile file holds the numbers of one run of the kit, as aggregates. The same schema serves three uses:
1. the study's own profile (`profile.json`, `state: "reported"`), shown on the app's "The study" page;
2. what a lab's run exports (`make export-profile`, `state: "synthetic"` or `"pending"`) and what a pool receives;
3. the technical appendix table (the same rows, the same numbers).

**Contract.** The file holds numbers and codes. Sentences, labels and wording live in the kit's strings files (the
app's strings, keyed by `label_key`, and `second_scribe/profile_strings.json` for reason, missing, formula, provider
and role codes). In the study's file every number is copied from a gate-cleared export, or is arithmetic on
gate-cleared exports and marked `derived`. Nothing in a profile is per map, per interviewer or per role; no cell
rests on fewer than k = 10 people; there are no per-record hashes.

Readers accept `schema` values `orgsci-profile/v2` and `orgsci-profile/v2.<minor>`; any other major version is
unreadable.

## 1. Top level

| Field | Type | Meaning |
|---|---|---|
| `schema` | string | `"orgsci-profile/v2"`. |
| `state` | string | `"reported"`: a real run, completed, verified and gate-cleared (only the study's file). A lab export is `"synthetic"` or `"pending"`. |
| `population` | object | `label` (the study: `"43 of 46"`), `n_people` (people analysed, 43), `n_total` (people in the corpus, 46). |
| `reference` | object | `type`: what the AI map is measured against. The study: `"participant-endorsed"` (the interviewer-drawn, participant-endorsed map). `map_size`: see section 4.3. |
| `pooling` | object | `rule`, `within`, `meta_analysis`: section 5. |
| `rows` | array | Panels in render order: `lead`, `ledger`, then rows `1` to `9` (exactly these eleven). |
| `run` | object | The run block (section 4). |
| `detection_limits` | object, optional | Lab exports only (section 8). |

## 2. Row

| Field | Type | Meaning |
|---|---|---|
| `row` | `1`-`9`, `"lead"` or `"ledger"` | Which panel. Integers for the nine profile rows. |
| `label_key` | string | Key into the app's strings file. The row title, the cell labels and every sentence hang off it. |
| `population` | string | `"43 of 46"` in the study. |
| `n_people` | integer | People behind the row; always at least k = 10. |
| `cells` | array | Ordered; render order is array order (for the ledger this order is required). |
| `band` | object, optional | The row's headline band (section 3.1). |
| `rests_on` | array of codes | Model and code steps the row rests on (section 6). The union over the row's cells; a cell may carry a narrower `rests_on`. |
| `source` | array of strings | Where the row's numbers come from. In the study: the gate chain entries the row reads, for example `["gate chain entry 003", "gate chain entry 017"]`. An array, because most rows read several entries. |
| `qualifiers` | array of codes | Section 7. |

## 3. Cell

| Field | Type | Meaning |
|---|---|---|
| `name` | string | Cell id; `name` together with `x` is unique inside the row. |
| `x` | string, optional | The setting, rung, variant, labeller or comparison the value belongs to (`"primary"`, `"lenient"`, `"rung1"`, `"judge2"`, ...). |
| `n` | integer, optional | Units the value is computed over (the denominator). For a count, the universe it is drawn from (for example 1,268 AI boxes). |
| `unit` | string, optional | `node`, `ai_box`, `edge`, `ideal`, `map`, `request`, `item`, `label`, `element`. A "box" is a node of the AI's map; `element` is the generic unit for a map element. |
| `kind` | string, optional | `share`, `count`, `difference`, `ratio`, `rank`, `rate`, `upper_bound`, `index`, `mean` (an arithmetic mean over the stated units). |
| `value` | number | The value (or the derived value; see `derived`). `null` only when `state` is `not_run`. |
| `ci_lo`, `ci_hi` | number, optional | Interval ends. |
| `ci_level` | number or `null` | `0.95`, `0.9`, or `null` when there is no interval. With `ci_lo`/`ci_hi`, the level of that two-sided interval. For `kind: "upper_bound"` there are two cases. **With an interval** (`largest_excess_ruled_out`), the value is the interval's upper end and its one-sided level is (1 + `ci_level`) / 2, so a 90% interval gives a one-sided 95% bound. **Without an interval** (`counterfactual_upper_bound`, exact bounds for zero counts, such as .033 for 0 of 90), `ci_level` is the one-sided level itself (0.95). Print, for example, "one-sided 95% (upper end of the 90% interval [−.017, .026])", never "90% one-sided". |
| `p`, `p_lt`, `p_holm`, `p_holm_lt` | number, optional | Test p-value (`p_holm`: Holm-adjusted). p is printed to 3 decimals, so a p that would print 0.000 is written `p_lt: 0.001` (and a Holm value that would print 0.000 is `p_holm_lt: 0.001`), never `p: 0`. |
| `band` | object, optional | Cell-level band (section 3.1). |
| `rests_on`, `qualifiers` | arrays, optional | Narrower than the row's, when they differ. |
| `derived` | string or `true`, optional | Present when the value is arithmetic on other reported numbers. A string names the formula (section 3.3); `true` means the formula is given by `formula_code` (and, in the study's file, also spelled out in `formula`). |
| `formula_code` | string, optional | With `derived: true`: a code (at most 40 characters) whose fixed text is `profile.formula.<code>` in `profile_strings.json`: `yield_per_box:ai`, `yield_per_box:no_interview`, `equal_size_bound`, `listening_edge_equal_size` (section 3.4). |
| `formula` | string, optional | The study's file only: the same arithmetic in words, naming the gate chain entries it reads. Lab exports never write it (section 9). |
| `note` | string, optional | A code. `"pooled_only"`: the value is a pooled point with no interval, because an interval would expose per-role sizes. None is ever invented. |
| `state`, `needs`, `reason_code`, `missing` | optional | Lab exports: cells that could not be computed (section 8). An absent `state` means computed. |
| `source` | string | Where the value comes from. In the study: `"gate chain entry 013"`, or for derived values the entries joined with `+` (`"gate chain entries 003+021+023"`). The number is the entry's sequence number in the authors' gate chain (section 4.1); the archive publishes the identifier, not that record. |

### 3.1 Band
`band` is `{"lo", "hi", "over", "narrow"?, "members"?, "subbands"?}`.
- `over` is one of:
  - `8_settings`: the 8 match settings (lexical only at .4, .5, .6, .7; judge only "same"; judge only "same or close"; primary = lexical .6 with "same"; lenient = lexical .4 with "same or close");
  - `lenient`: the primary setting against the row's lenient setting;
  - `labellers`: judge 1, the second judge family and the keyword labeller;
  - `ci95`: the 95% interval;
  - `variants`: the range over pull variants or instruction rungs.
- `narrow` (optional, labeller bands): whether the band is narrow, copied from the gate-cleared export's
  `band_narrow` claim where one exists.
- `members` (optional): the values the band is the range of, one per setting or labeller: `[{"x": "primary", "value": 304}, ...]`. With `over: "labellers"` the members name the labellers, so a pool shows which labellers each lab used. Ledger members with a ±1 uncertainty also carry `value_hi` (section 3.2); `lo`/`hi` are then the minimum of `value` and the maximum of `value_hi`.
- `subbands` (optional): `[{"name": "judge_using", "lo", "hi"}, {"name": "lexical_only", "lo", "hi"}]`. The four judge-using settings are the two judge-only settings, primary and lenient; lexical only is the other four.
- A band that could not be computed (lab exports) is `{"state": "not_run", "over", ...}`.

### 3.2 The ledger band (arithmetic on gate chain entry 003)
The 304 counts quote-attested AI boxes unmatched to the endorsed map. It is a conditional ceiling on true additions
within the generated AI set, assuming accepted matches and attestation are correct. Missed true matches inflate
the count; false accepted matches can hide additions. Content omitted by both maps is not enumerated. The 365
unanchored boxes are not attested by their supplied quote, which does not establish absence everywhere in the transcript.
- N = AI boxes judged = 1,268; U = unanchored boxes = 365 (the same in every setting, because it uses no matcher).
  Attested AI boxes A = N − U = 903.
- For each match setting s, attested precision p_s is printed to 3 decimals. The number of attested AI boxes on the
  map, m, is an integer with `round(m / N, 3) == p_s`; there are one or two such integers.
- **Attested, not on the map = A − m = 903 − m**, reported in `band.members` as `value` (low) and `value_hi` (high)
  per setting. At the primary setting the count is exact (304, with 599 on the map).
- The ledger band is [min `value`, max `value_hi`] over the 8 settings, with sub-bands for the judge-using and
  lexical-only settings.
- The ledger closes: on the map + attested and absent + unanchored = AI boxes (599 + 304 + 365 = 1,268).

### 3.3 Named formulas (`derived` as a string)
`ledger_band` (section 3.2); `ratio_ai_per_endorsed` = AI boxes / endorsed boxes (1,268 / 2,085);
`per_map_mean` = pooled boxes / `n_people`; `sum_of_cleared_counts` (for example "unanchored ideals" = leaked and
unanchored + unanchored wished; "changed" = gained + lost); `ratio_of_cleared_counts`; `net` = gained − lost.

### 3.4 The four derived lines beside the lead (`derived: true`)
Descriptive arithmetic on reported numbers, never typed in. Each cell carries `derived: true`, `formula_code` and the
qualifier `not_a_test`, and is printed only as "arithmetic on reported numbers, with no statistical test", beside the lead and the
map sizes.
- `yield_per_box`, x = `ai`: endorsed steps recovered per AI box drawn = M_ai / N, with M_ai = 778 (endorsed steps
  matched by any AI node at the primary setting) and N = 1,268 AI boxes judged (entry 003). = .614.
- `yield_per_box`, x = `no_interview`: M_p / B_p, with M_p = no-interview recall (.152, primary, entry 021) × the
  1,977 in-transcript endorsed steps (entry 003), and B_p = no-interview nodes per map (15.465, entry 023) × 43
  people. = .452.
- `equal_size_bound`, x = `no_interview`: what the no-interview map would recall if its yield per box held at the AI
  map's size = r_p × S_ai / S_p, with r_p = .152 (entry 021), S_ai = 29.488 (entry 020) and S_p = 15.465 nodes per
  map (entry 023). = .290. This is a constant-yield extrapolation. If average yield does not increase as the map
  grows, .290 is an upper bound on the extrapolated no-interview recall, not a floor on that recall.
- `listening_edge_equal_size`, x = `no_interview`: r_ai − `equal_size_bound`, with r_ai = AI recall (.394,
  primary, entry 013). = .104. The historical code `floor_not_estimate` denotes a lower bound on this contrast only
  under the nonincreasing-yield assumption. Arithmetic under a constant-yield assumption, not used in the study's paper; the
  box-for-box cells give the expected recall of the AI map cut at random to the no-interview map's size.

The four lines are arithmetic on reported numbers and are not used in the study's paper. Section 3.5 describes the
box-for-box cells.

The numeric file and its historical codes are preserved. These interpretation notes qualify them. Likewise,
`listening_added` is a paired descriptive pooled-ratio contrast between configurations that differ in more than
interview access. Its cluster-bootstrap interval holds the generated maps fixed and excludes new-draw uncertainty.

### 3.5 The two box-for-box cells beside the lead
`rarefied_ai_recall` and `rarefied_ai_minus_prior_only_recall`, both at x = `primary` (entry 022), sit in the lead
row with the map sizes and carry `size_beside_lead`. For each AI map, `rarefied_ai_recall` is the expected recall if
the map were cut at random down to the size of its role's no-interview map. It is averaged over every possible cut by
an exact formula, with no random draw, so the number is the same on every run. For a step matched by m of the map's N
boxes and a cut to k boxes, the chance that the step survives is 1 − C(N − m, k) / C(N, k). The value is pooled over
maps, and its 95% interval is the cluster bootstrap (B = 2000) of the rarefaction script r14. Every AI box matched to
a step counts, as the matches are stored (many to many). A run that counts each AI box for at most one step marks
both cells with the optional qualifier `matching_one_to_one`. `rarefied_ai_minus_prior_only_recall` is that recall
minus the no-interview recall. In the study the two cells are .217 [.199, .238] and +.065 [.043, .086].

The two cells answer a narrower question than the lead, whether listening helps with equally many boxes. They
lean against the AI map, because a model asked for fewer boxes would keep the important steps rather than a random
subset. The lab export path (`second_scribe/publish.py`, `CATALOGUE`) does not yet accept the two cells.

## 4. Run block (`run`)
Chain heads and declared settings only, never records. The archive includes the profile and this schema, not the
underlying gate-cleared records, gate chain or full declaration log. `verified` fields report authors' checks;
a head lets anyone who later receives the records repeat those checks, but the head alone does not show what the records hold or their order.
A run has three phases: **Q** (the reconstructor's stages:
confirming mapping talk, drawing the AI map, drawing the no-interview map), **J** (the judge's stages) and **M**
(metrics and report, no model calls).

| Field | Study file (`state: "reported"`) | Lab export |
|---|---|---|
| `gate_chain` | `{head, entries, verified, verify_date, source}` (section 4.1). | `null`: it belongs to the study's gate. |
| `declaration_log` | `{head, records, last_seq, verified, source}` (section 4.2). | `null`. |
| `run_chain_heads` | `null`, with `run_chain_heads_source: "available_from_authors"`: the heads of the real run's own hash chains are held by the authors and are available from them. | `{head, records, manifest_hash, source}` for the kit's one run chain, or `null` with `run_chain_heads_source: "available_from_authors"` when the lab withholds it. |
| `run_chain_heads_source` | `"available_from_authors"`, a code whose sentence is `profile.reason.available_from_authors`. | Present only when the heads are withheld. |
| `kit` | `release_commit: null` (the kit release is the public repository's release); `run_commits`: the Q, J and M run commits, in that order; `source`. | `release_commit`: the kit's HEAD commit at export (40 hex, or `"[SOURCE]"` when unknown); `run_commits`: the `code_commit` of every run start, in order; `code_sha256`: one 64-hex code-content hash when every run start records the same hash, otherwise `null`; `source`. |
| `prompt_set` | `q_phase_sha256`, `j_m_phase_sha256`: the `set_sha256` of `prompts/MANIFEST.json` for the Q phase and for the J and M phases. In the study the two sets differ only in the judge prompts. | Same. |
| `reading3_prompt` | `path` (`prompts/prior_only.p0.v2.txt`), `sha256` of the file as run, `source`. Nothing else. | Same; may also carry `agree: null`. |
| `models` | One object per model (section 4.4). | Same; `"[SOURCE]"` where the export cannot know a field. |
| `settings` | Declared configuration, not results (section 4.5). | Adds `bootstrap_seed`, `match_settings` (all 8 settings) and `matcher_row4_lenient`. |
| `verify` | Dates on which the gate chain and the J and M run logs were verified (`gate_chain`, `j`, `m`), `source`. | Not written. |
| `remote_endpoints` | Absent: no cloud model was used. | Section 4.6. |

`run_commits` is always a list with no phase keys, and `release_branch` is not written. Readers also accept an
older per-phase form (`run_chain_heads` or `run_commits` as `{q, j, m}`); exporters never write it.

### 4.1 Gate chain
Every gate-cleared export of the study is one entry of a hash chain. An entry holds `seq` (1, 2, ...), `file`,
`sha256` (of the export file), `prev` (the previous entry's `entry_sha256`; 64 zeros for entry 1) and
`entry_sha256` = sha256 of the entry without `entry_sha256`, serialised as JSON with sorted keys and no spaces.
`head` is the `entry_sha256` of the last entry; `entries` is the count (the study: 23, head `2469916c…`). A cell's `source`
"gate chain entry 013" names the entry with `seq` 13. Every link, entry hash and file hash is recomputed when the
profile is built, and the build stops if one breaks.

### 4.2 Declaration log
The study's declaration log is a hash chain of records, each with `seq` (from 0), `prev_hash` and `hash` = sha256
of the record without `hash` (same serialisation). `head` is the last record's `hash`; `records` is the count;
`last_seq` the last `seq` (the study: 21 records, last seq 20, head `b3736796…`). Every link and record hash is recomputed when the
profile is built.

### 4.3 `reference.map_size`
`ai_boxes` (1,268), `endorsed_boxes` (2,085), `ai_per_map`, `endorsed_per_map` (pooled boxes / 43; a mean, not a
per-map row), `ai_per_endorsed` (.608), and `reading3_map_size`, the size of the no-interview maps (entry 023):
`nodes_per_map` (15.465), `edges_per_map` (15.488), `over_ai_nodes` (.524), `note: "pooled_only"`, `source`. These are
points only: no interval is given, because one would expose per-role sizes.

### 4.4 `models`
Each object: `role`, `family`, `model_id` (the served id), `quantisation`, and where known `weights` and `thinking`;
`source`. A field with no public record is `null`, and that model's `source` is `"not_recorded_public"`. The study's
roles are `reconstructor` (draws the AI map and the no-interview map), `reconstructor_secondary`, `judge` and
`second_judge_family`.

### 4.5 `settings`
`matcher_primary` and `matcher_lenient` (`lexical_threshold`, `judge_label`; the study: .6 with `same`, .4 with
`same_or_close`), `reconstructor_temperature`, `reading3_temperature`, `reading3_draws`, `bootstrap_resamples`,
`interval_method` (`cluster_bootstrap`), `gate_k_people` (10), `source`. In the study both temperatures are 0 and
`reading3_draws` is 1.

When the run config has a `reading3` block, `reading3_temperature` and `reading3_draws` come from it, and
`reading3_draws` is its `samples` value, the number of no-interview maps drawn per role. A config without the block
scores as its no-interview map the `prior_only` draft whose key sorts lowest; both fields then come from `prior_only`,
and `reading3_draws` counts draws per prompt variant (a run with several variants draws variants times samples drafts
per role).

The `reading3` block sets the no-interview map, and the `prior_only` block sets the no-interview drafts behind the
model's no-interview script, which the pull and additions rows (row 6) need; one run draws both sets. The study's
reading 3 was one map per role with variant p0 at temperature 0 (`{"variant": "p0", "samples": 1, "temperature": 0}`),
and the model's no-interview script came from 15 drafts per role (variants p0–p2, five samples each) at temperature 0.7.

### 4.6 `run.remote_endpoints` (lab exports)
A list of `{role, provider, declaration_sha256}`, absent when no cloud model was used. `role` is one of the kit's
client roles: `recon` (the reconstructor, which also draws the no-interview map), `judge`, or `judge2` (the second
judge family); its label is `profile.role.<role>`. `provider` is a code of at most 40 characters from the kit's
provider allow-list (for example `anthropic`, `openai_compat`); its sentence is `profile.provider.<code>`.
`declaration_sha256` is the hash of the declaration record in the app's log, so the run's printed summary can be
matched to it. No host and no basis text appear in the file; the typed ethics basis is printed in full on the run's
printed summary, the report and the methods paragraph.

## 5. Pooling
Profiles are pooled **side by side**: `pooling` = `{"rule": "side_by_side", "within": ["reference_type",
"prompt_set_pair", "state"], "meta_analysis": false}`. A pool is a table with one profile per lab, placed side by
side only within one reference type, one (Q, J/M) prompt-set pair and one file state. Anything else is listed
separately and never merged. It is never a meta-analysis, never a combined estimate and never a common interval.
- `make pool` writes a pool file of schema `orgsci-pool/v1`, with `separate_keys`, `groups[].n_analysed` and
  `groups[].settings_differ`. Export and pool both refuse `n_analysed` below 10.
- Readers also accept the older `within: ["reference_type", "prompt_set"]`.

Pool accepts lab exports with schema `orgsci-profile/v2` or `orgsci-profile/v2.<minor>`. The study's
`profile.json` is an app reading copy with gate-chain sources; pool refuses it and asks for an `export-profile`
file from a finished run. Different `run.kit.code_sha256` values, including a missing value beside a known one,
set `code_differs` and `settings_differ`; differing bootstrap settings set `bootstrap_differs` and
`settings_differ`. These flags qualify the side-by-side table rather than combining results.

Pool checks duplicate file content and repeated run-chain heads, plus schema and cell bounds. These checks
do not authenticate the submitting lab or prove that a supplied number came from its run. Retain the original
exports and externally saved manifest hashes.

## 6. `rests_on` codes
- Model steps: `R` the AI map (transcript minus confirmed mapping talk); `P` the model's own no-interview maps (the
  "script" in the code); `JS` the other family's no-interview map; `IT` judge call "in transcript"; `AL` alignment
  with the endorsed map; `AS` alignments with a script; `CN` canonical names; `MO` modality; `LD` alignment asked
  again in the ladder phase; `J2` the second judge family.
- Code steps: `Q` quote anchoring (also reads the mapping-talk flags); `LX` the lexical matcher.

## 7. `qualifiers` codes
`ceiling` (conditional upper bound within generated AI boxes, under section 3.2's assumptions); `not_bounded_below`; `descriptive` (no rule reads it); `one_draw`
(one draw, no draw-to-draw variance in the interval); `package_contrast` (the rungs differ in more than one thing);
`ci_excludes_draw_variance` (the interval on the no-interview map's score carries no draw-to-draw variance);
`paired_by_map`; `attested_line_one_sided`; `derived_arithmetic`; `no_interval`; `zero_count` (a count of zero,
printed with its bounds); `labeller_dependent`; `partly_lexical`; `no_structural_claim`; `seam_withheld_k` (a cell
under k was withheld by the gate); `recall_is_a_band`; `no_ratio` (never set two ledger cells against each other);
`not_reproducible_to_the_digit` ("re-runnable, not reproducible to the digit"); `size_beside_lead` (a size printed
beside the lead; never call the lead "size-free"); `pooled_only` (a pooled point with no interval); `not_a_test`
(arithmetic on reported numbers); `floor_not_estimate` (historical code for the conditional extrapolation in
section 3.4; the no-interview recall is an upper bound and the corresponding contrast a lower bound under that assumption);
`matching_one_to_one` (each AI box counts for at most one step; optional on the two box-for-box cells of section 3.5).

## 8. Lab exports: cells not computed, detection limits, estimator
The kit's exporter writes the same schema for any lab's run.
- **Cell `state`** (optional). `"not_run"`: the value is `null` and the cell says why, with `needs` (a list drawn
  from `"J2"` (second judge family), `"sidejudge"`, `"ladder"`, `"judge_script"`) or a `reason_code`, or both,
  never neither. `"partial_not_run"`: the value is computed but a band, a denominator or an interval is missing,
  listed in `missing`. An absent `state` means computed.
- **`missing`** (with `state: "partial_not_run"`): a non-empty list drawn from `"band"`, `"n"`, `"ci"`.
- **`reason_code`**: a short code for why a cell is `not_run` or partial, for example `cleared_absent:009`,
  `needs_J2`, `grid_spec_missing`. The strings file maps each code (the part before any `:`) to its sentence,
  `profile.reason.<code>`.
- **File `state`** in a lab export is `"synthetic"` or `"pending"`. Only the study's gate-cleared file is
  `"reported"`.
- **`detection_limits`** (schema `"orgsci-detection-limits/v1"`), written by `make phantom` (public synthetic
  corpus, kit defaults) or `make calibrate CONFIG=` (the lab's own grid, B, seed and kit commit): per dose type
  `smallest_detected`, `lowest_tried`, `at_or_below`; `pull_planted`, `pull_detected`, `dose0_bands`, `settings`,
  `kit_commit`; and `limit`, the stated caveat that this tests the gauge's arithmetic on planted doses and says
  nothing about matching on real data.

The five dose types are:

| Code | Planted change |
|---|---|
| D1 | Drop AI steps and their attached links. |
| D2 | Drop AI links, keeping the steps. |
| D3 | Merge the endpoints of selected AI links into fewer steps. |
| D4 | Redraw matched wished-for steps as received, keeping their quotes and modality labels. |
| D5 | Replace AI steps with steps from the model's no-interview map, keeping their ids and links. |

`smallest_detected` is the smallest tried dose meeting the check, not a universal detection threshold.
`at_or_below` marks a limit at the lowest tried dose; an undetected dose type remains undetected in the output.

- **Estimator.** Every cell is computed with the profile estimator: pooled ratio of sums, percentile cluster
  bootstrap over maps, B = 2000 and seed 20261017 by default, all recorded in `run.settings`. Per-map rows are never
  exported.

## 9. Strings in lab exports
Lab exports carry codes, never sentences; the sentences live in `second_scribe/profile_strings.json`, a flat map from
`profile.reason.<code>`, `profile.missing.<code>`, `profile.formula.<code>`, `profile.provider.<code>` and
`profile.role.<code>` to an English sentence. A kit test checks that every code the exporter can emit has an entry,
and the app reads this file rather than keeping a copy.
- Any string field in a lab-export cell is at most 40 characters, except `source` and `*_state` diagnostics (at most
  200). The app's publish gate enforces this.
- A derived cell carries `formula_code`, never `formula`; a cell never carries a free `reason` sentence. A code admits
  only known text, where a length exemption would let any string through.
- An exporter adds a new code only together with its strings-file entry.
- The study's own file is not a lab export: its derived cells carry both `formula_code` and the longer `formula`.

## 10. Privacy rules
Every profile obeys these; the study's build and the kit's exporter stop rather than write a file that breaks one:
- every row and every record used has `n_people` of at least k = 10;
- no cell is one the gate withheld (a cell resting on fewer than 10 people);
- no key containing `interview`, `participant`, `role`, `record_hash`, `entry`, `map_id` or `pair_id` appears outside
  `run`;
- no 64-hex string appears outside `run`. Inside `run` the only 64-hex values are chain heads and prompt hashes, never
  per-record hashes;
- no `x` value names an interviewer, a role or a map; every `unit: "map"` cell is computed over the whole population.
Small *element* counts (for example 4 flattened ideals, 0 counterfactual) are allowed by the gate's policy, because
they count items, not people.

## 11. Row catalogue (cell names)
Cells appear in this order; x values are in parentheses. The numbers in brackets are the gate chain entries the cells
read.

- **lead** (`profile.lead`; row band `lenient`): `ai_recall`, `no_interview_recall`, `listening_added`,
  `listening_added_attested_line`, each at primary and lenient (the headline is primary: +.241 [.205, .277]); the
  wrong-transcript floor beside it at primary: `real_recall`, `shuffled_recall`, `wrong_transcript_difference` (with
  its labeller band), `own_map_mean_rank`, `rank_null_expectation` [013, 021]. Then the sizes: `ai_nodes_per_map`,
  `no_interview_nodes_per_map` (point only), `ai_minus_no_interview_nodes_per_map`, `ai_edges_per_map`,
  `no_interview_edges_per_map` (point only), `ai_minus_no_interview_edges_per_map`, `endorsed_nodes_per_map`,
  `no_interview_over_ai_nodes` (point only) [020, 023]; then the derived `yield_per_box` (ai, no_interview),
  `equal_size_bound` (no_interview), `listening_edge_equal_size` (no_interview) (section 3.4) [003, 020, 021, 023];
  then the box-for-box cells `rarefied_ai_recall`, `rarefied_ai_minus_prior_only_recall` (primary) (section 3.5)
  [022]. The size, derived and box-for-box cells carry `size_beside_lead`; the derived ones also `not_a_test`.
- **ledger** (`profile.ledger`; row band `8_settings`): in this order, `omitted_share` [003];
  `attested_absent_from_map` at primary (304; 8-setting band 200-809 with the per-setting members; qualifiers
  `ceiling`, `not_bounded_below`) [001, 003, 004]; `unanchored` at primary (365; labeller band 365-446, not narrow)
  [001, 018]; `off_record_endorsed` at primary (108) [001]; `attested_absent_from_map` at labellers (labeller band
  265-304, narrow) [018]; the closing cells `ai_boxes_total` [003] and `ai_boxes_attested_on_map` at primary [004]
  (599 + 304 + 365 = 1,268). Row qualifier `no_ratio`: never set 304 against 108.
- **1 coverage** (row band `8_settings`): `kept` (with the 8-setting band), `coarsened`, `omitted` [003];
  `attested_recall` (primary, with its 8-setting band; lenient) [003]; `r_star` [017]. Row qualifier
  `recall_is_a_band`.
- **2 size**: `ai_boxes` [003], `endorsed_boxes` [009], `ai_per_endorsed` (also at rung1) [003, 009, 014],
  `ai_per_map` [003, 014], `endorsed_per_map` [009]; `ai_nodes_per_map`, `ai_edges_per_map`,
  `endorsed_nodes_per_map` with their 95% intervals [020]. The no-interview sizes sit in the lead row, because row 2
  rests on R only.
- **3 support** (row band `8_settings`): `precision` (primary, 8-setting band) [003]; `attested_recall`,
  `attested_absent_from_map`, `unanchored` (primary, labeller bands) [001, 003, 018].
- **4 links** (row band `lenient`): `endpoint_lost` (primary, lenient), `collapsed`, `eligible_share` (primary,
  lenient), `eligible_edges` (primary, lenient), `direct` (primary, lenient; band `lenient`), `direct_null_expected`,
  `direct_over_null`, `undirected`, `path_le2`, `collapsed_share_of_matched` [005].
- **5 absences** (row band `labellers`): `n_ideals`; `n_matched`, `recovered_as_wished`, `flattened`,
  `counterfactual`, `missed` (primary, lenient); `unanchored`, `flattening_rate` (primary) [013];
  `counterfactual_upper_bound` (all_ideals, matched, matched_lenient) [013]; `recovery_rate` [007];
  `matcher_free_rate` (judge, judge2, keyword; labeller band) [010, 018].
- **6 slant** (row band `variants`): `pull` (canonical; band over variants), `pull_90` (canonical), `pull_variant`
  (raw_wording, judge_only, in_transcript_only) [007]; `scripted_share_unanchored`, `scripted_share_attested_absent`
  (own_script), `scripted_share_difference` (own_script, other_family_script), `largest_excess_ruled_out`
  (other_family_script) [009].
- **7 floors** (row band `lenient`): the lead's recall, difference and floor cells, at the same x values [013, 021].
- **8 instruction** (row band `variants`): `ai_per_map` and `size_adjusted_recall` (rung1, rung2, remedy, placebo);
  `size_adjusted_recall_2_minus_1` (rung2-rung1); remedy-minus-placebo side effects
  `lived_recall_remedy_minus_placebo`, `ai_nodes_remedy_minus_placebo`, `false_wished_remedy_minus_placebo`,
  `flip_remedy_minus_placebo` [014]; `leak_rate` (remedy, placebo), `leak_rate_remedy_minus_placebo` (95%),
  `leak_rate_remedy_minus_placebo_90` (90%), `mde80_leak_contrast` (remedy-placebo) [015].
- **9 instrument error** (row band `labellers`): `lexical_floor` (lexical_only_04), `any_node_recall` (primary) [003];
  `rejudge_changed` and `rejudge_net` (lived_steps, ideals), `rejudge_changed_share` (lived_steps) [014]; the
  labeller-band cells at x = judge: `attested_recall`, `attested_absent_from_map`, `unanchored`, `matcher_free_rate`,
  `quote_actual_drawn_as_wished`, `contaminated_share` [018]; judge repeats: `flip_rate` (modality, alignment),
  `identical_requests` (modality, alignment, canonical_names, run2_determinism_audit), `canonical_ari`
  (canonical_names), `label_order_changed_rate` (modality) [019].

## 12. Files
- `profile.json`: the study's profile in this form. Every cell value, interval, band, member and qualifier is the
  gate-cleared number; the file carries the gate-chain head and the declaration-log head, and the real run's own
  chain heads are available from the authors.
- `PROFILE-SCHEMA.md`: this schema in the distributed archive.
- In the kit: `second_scribe/profile_strings.json` (the code sentences), `second_scribe/app/profile_view.py`
  (`load_profile`, the reader that checks a file against this schema), and the exporter (`make export-profile`).
