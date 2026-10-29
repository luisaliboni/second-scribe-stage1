# What did listening add? Kit for Stage 1

A local, inspectable gauge for one question: **what did listening add?**

Interviewers who draw a participant's journey while they talk, and participants who endorse the drawing, produce
an **interviewer-drawn, participant-endorsed map**. Researchers now also hand the same interviews to an AI and
ask it to draw the map. The kit puts those readings side by side, measures each one against the same
transcript, and exports only a gate-cleared **distance profile**, so labs that can never share their interviews
can still set results next to each other.

- Paper and appendix: *What did listening add? Human and AI readings of confidential interviews* (2026), accompanying this anonymous Stage 1 archive.
- Repository: this archive (a Git repository, one commit); the kit is also kept in a public repository, withheld here for blind review
- Tests: 3,953 passed; 11 checks skipped (3,964 collected).
- Maintenance changes since the study run: see `CHANGES.md`.

Python standard library only. No dependencies. MIT licence.

## What the kit does

**Three readings of each interview**, side by side:

1. **The endorsed map.** The map the interviewer drew live and the participant endorsed.
2. **The AI's map.** A model rebuilds the map from the transcript alone, with the interviewer's map talk removed.
   The prompt requests a participant quote for every element; the checks below assess the returned evidence.
3. **The no-interview map.** The same model draws a map of the official pathway (prompt quoted) without the interview. It is given
   only the domain, the role and the phases; one map per role, one draw, at temperature 0 in the study. In the config
   this reading is the `reading3` block, and the `prior_only` block holds the temperature 0.7 drafts behind the model's
   no-interview script. The code stores the no-interview map's file under the name `prior_only`, beside those drafts.

**The lead question, "what did listening add?"**, compares reading 2 with reading 3, and measures both against
reading 1. The yardstick is the set of endorsed steps that actually appear in the transcript. Because the AI map
and the no-interview map differ in size, the kit prints map sizes beside every headline number. It never presents
the difference as size-free. This is a descriptive contrast between the two configurations. Sizes and prompts
differ, and the run settings show each map's temperature; the contrast does not isolate interview access.

**Two readings of one transcript.** The endorsed map and the AI's map are each measured against the transcript. The
kit keeps a ledger with four lines: matched and quote-attested AI boxes, quote-attested AI boxes unmatched to the map,
AI boxes not attested by their supplied quote, and endorsed steps the judge classifies as absent from the transcript.
These checks do not enumerate everything in the transcript. Endorsement shows that the
participant agreed with the drawing; it does not show that the drawing is complete. The ledger counts both sides.

**Checks that run in code, not by a model.** Code checks that each AI quote is in the transcript: word for word
("exact"), or with at least 85% of its words in order inside one same-speaker turn and the same negations, modals and words of wanting or condition
("approximate"; the tier is recorded per element). Code also checks who said it. A separate judge model, from
another family than the reader, checks the semantic relation between element and quote, then labels whether the
step happened, was wished for, was denied, was hypothetical, or happened to someone else.

**Controls and instrument error.** The kit reports its own error next to its results: matcher settings from strict
to lenient, a second judge family, a quote-shuffled control, repeated requests, and a known-distance test. The
instrument is re-runnable, not reproducible to the digit: a model asked the same question twice can answer
differently, and the profile says by how much.

**The distance profile.** One JSON file with a lead row, a ledger row and nine numbered rows (coverage, size, support,
links, absences, slant, floors, instruction, instrument error). Each row records which model steps it rests on and
which qualifiers apply. The schema is in `analysis/public/PROFILE-SCHEMA.md`.

**Pooling profiles, never data.** Each lab runs the kit on its own interviews and exports only its profile. `cli pool`
places profiles side by side (`make pool` shows it on two synthetic labs). It is a table with one profile per lab, not a meta-analysis and not a combined
estimate. See "Quick start" and the schema, section 5.

## Quick start

Python 3.10 or newer. Nothing to install.

    make test          # 3,953 passed; 11 checks skipped
    make test-fast     # shorter subset; omits the slow suites listed in tests/fast.py
    make test-js       # the app's JavaScript tests; needs Node.js, not part of `make test`
    make demo          # offline replay of the study models' recorded answers on the synthetic corpus; zero model calls
    make demo-oracle   # the same demo answered by the oracle client (a "perfect AI" built from the gold)
    make pool          # no arguments, offline, synthetic labs only; place profiles side by side, within one reference type and prompt set
    make phantom       # no arguments, offline; known-distance test on the public synthetic corpus, kit defaults
    make calibrate     # needs CONFIG= and PROFILE=; adds detection limits to your profile: make calibrate CONFIG=<your config> PROFILE=<your profile>
    make app STUDY=analysis/public/profile.json   # builds the offline site into site/; open site/index.html in a browser (no server, no network)

The full test suite takes about 45 minutes and `make test-fast` about 30 minutes on an idle Mac; time varies by machine.
`make test-fast` is for feedback while editing; run `make test` for the complete check. Eight optional parity tests need
`SS_PARITY_DIR` to point to the authors' analysis pack. Two additional checks opt into a local study copy
(`SS_ANALYSIS_PUBLIC`) or rewriting a development page (`SS_WRITE_STUDY_DEV_PAGE`). All ten are skipped in
the default public run. One older check for an absent Explain module also skips because that module is now
included and covered by its own tests. A skip means that check was not run.
To work with your own project in the local app, run `python3 -m second_scribe.app` and open the loopback address it prints.
For your own data the app needs two local models (see "The local app" below and INSTALL.md); the demo and the
offline site need none.

`make demo` runs on the 12-interview synthetic food-truck corpus in `demo/corpus/`. It replays answers that the
study's own models gave when the demo was recorded, under the demo's configuration (`configs/demo.json`): Qwen3.8-Flash-Next as reconstructor and DeepSeek V4 Flash as
judge, both running locally. No model is called. It writes `report.md`, `aggregates.json` and `traps.md` in `out/demo/aggregates/` (the paths the console prints; the demo
also leaves a copy of each directly in `out/demo/`) and the audit log `out/demo/logs/run.jsonl`, prints the trap table, and verifies the log.
The replay reproduced the recorded run's aggregates, report and trap table exactly when we re-ran it
(`tests/test_fixtures_in_order.py` covers the replay mechanism).

Expected with the shipped recording: **133 of 144 trap checks pass**, and `determinism_judge` shows a non-blocking
FAIL (identical share .75); the command exits 0. The 11 trap misses and differing repeat answers are the recorded
models' outcomes. With the oracle, all 144 trap checks pass. `make demo` resets the generated demo outputs under
the folder's run lock, preserving unrelated files. To resume
instead, repeat `python3 -m second_scribe.cli demo --out out/demo --client replay`.

The console also prints the client, model calls (zero for replay and oracle), replayed responses, output paths,
per-stage times and the manifest hash. Save that hash outside the run folder; see step 4 below.

What the three newer targets do, in one line each:

- **`make pool`** takes no arguments: it builds the synthetic oracle labs, and lab9 is refused at the k floor. A lab's
  own pooling is `python3 -m second_scribe.cli pool --profiles A.json B.json [...] --out pool.json`, which writes
  `pool.json` and `pool.md`, side by side and never combined. A single profile is accepted and listed on its own.
  Profiles that differ in reference type, prompt set or file state are listed separately and never merged.
  "Prompt set" means the pinned prompt manifest (the hash of the prompt files), not which prompt variants a lab ran.
  A lab that ran only a subset of the prompt variants (for example one of three no-interview variants) has the same
  prompt set as a lab that ran all of them, and the pool does not flag it. Each lab should state which variants it ran.
  Both export and pool refuse a lab with fewer than 10 people analysed.
  Use files written by `export-profile`. The study's `analysis/public/profile.json` is the app's reading copy
  and cannot be pooled. Pool checks schema, cell bounds, duplicate file content and repeated run-chain heads;
  it does not authenticate a lab or prove that supplied values came from its run. Keep the original exports and
  compare externally saved manifest hashes. Different code hashes or bootstrap settings are flagged in the table.
- **`make phantom`** takes no arguments and writes `out/phantom/phantom.json` and `phantom.md`. Plants known doses of distance in copies of the public synthetic
  corpus, with no model calls, and records the smallest dose the gauge detects for each dose type. It does not touch
  any profile.
- **`make calibrate`** is `make calibrate CONFIG=run-config.json PROFILE=profile.json [OUTDIR=out]`; `CONFIG` and
  `PROFILE` are required. It runs the same test on the public synthetic corpus at the kit's fixed matcher settings,
  with your bootstrap size, seed and kit commit, and writes the result into your profile as a `detection_limits`
  block. Both tests check the gauge's arithmetic on planted doses. They do not check matching on
  real data, and the profile says so.

How recorded fixtures work. A replay serves each recorded response by the hash of the request, and stops with a
non-zero exit at the earliest request it has no recording for. A live model is asked the same messages more than once
(the no-interview samples, drawn with different seeds, and the determinism audits, which re-send logged requests
and may be answered differently), so the recording keeps every response in call order: a request made once is stored
as one record, a request made again as `{"calls": [r1, r2, ...]}`, and the replay serves the i-th call the
i-th entry. Asking for more calls than were recorded is a miss and stops the replay. If you change
`configs/demo.json`, the demo corpus or any prompt file, the request hashes change and the shipped fixtures stop
replaying; record a new set with `make demo-live` (live models, endpoints from the environment variables below).
That writes the untracked `out/demo-live-fixtures.json`; review it before copying it over `demo/fixtures.json`.
`--record` is refused for any corpus that is not synthetic, because a fixtures file holds every prompt and reply
verbatim.

## Reproduce Table 15.1

Section 15 of `docs/technical-appendix-full.md` gives Tables 15.1 and 15.2, the values that the synthetic food-truck
corpus produces. This section uses `make demo` and `make demo-oracle`, introduced in the Quick start. To redo the
tables, run these five commands in order from the unzipped folder. All are offline and call no model. Each took about
ten seconds or less on our computer. Without `make`, type the `python3 -m second_scribe.cli` form given under each
target.

    # 1. Replay the study models' recorded answers on the 12 synthetic interviews; prints the trap table.
    make demo
    #    without make: python3 -m second_scribe.cli demo --out out/demo --client replay --fresh

    # 2. Write the synthetic profile, demo-profile.json, with the same rows as the study's profile.
    make export-profile CONFIG=configs/demo.json PROFILE=demo-profile.json WITHHOLD=1
    #    without make: python3 -m second_scribe.cli export-profile --config configs/demo.json --profile demo-profile.json --withhold-chain-heads

    # 3. Print a fingerprint (a SHA-256 hash) of the profile's rows.
    python3 -c "import json,hashlib;r=json.load(open('demo-profile.json'))['rows'];print(hashlib.sha256(json.dumps(r,sort_keys=True,separators=(',',':')).encode()).hexdigest())"

    # 4. Run the same demo with the oracle client, a "perfect AI" that answers from the answer key.
    make demo-oracle
    #    without make: python3 -m second_scribe.cli demo --out out/demo-oracle --client oracle --fresh

    # 5. Regenerate the detection limits of the known-distance test.
    make phantom
    #    without make: python3 -m second_scribe.cli phantom --reps 20 --out out/phantom

What you should see:

- **Step 1.** 133 of 144 trap checks pass, and the run log is verified. The demo's report, `out/demo/report.md`
  (a copy is in `out/demo/aggregates/`), shows a FAIL line for `determinism_judge`. It reports the judge's recorded
  behaviour: 75% of the repeated judge answers in the recording are identical. It does not stop the run, and the
  command exits 0.
- **Step 2.** The profile has 147 cells. Of these, 136 are computed and 11 are marked as not run. One of these cells
  needs the second judge family and eight need a second judge run, and both kinds need live models, so they are not in
  the offline demo. The other two cells, in the slant row, compare the boxes the AI added with the other family's
  no-interview script, which matches none of them. Both shares in the comparison are therefore 0%, and the kit's declared
  rule reports a comparison of 0% against 0% as not run. Table 15.1 lists the values.
- **Step 3.** The fingerprint is
  `58fa98534bf6bd6b9ebc6dc143f5e43668df1f75f7bab74b1207f22ecbbc0204`. We hash the rows and not the whole file,
  because the file also records the kit's version, which changes with every release.
- **Step 4.** All 144 trap checks pass. Table 15.2 sets the oracle beside the recorded models.
- **Step 5.** `out/phantom/phantom.json` and `phantom.md` are written again.

The demo runs the study's pipeline with the study's two models, and its no-interview map is drawn as in the study. The
synthetic profile differs from the study's in six ways.

- (a) The model's no-interview script comes from 6 drafts per role, against 15 in the study.
- (b) The cells that need the judge asked again or the second judge family are not in the offline demo, because they need
  live models.
- (c) The two cells that compare the maps box for box come from code outside the kit, so the synthetic profile has
  147 of the study profile's 149 cells.
- (d) The corpus is 12 synthetic interviews with planted answers, so most values are high by construction.
- (e) The wrong-transcript control pairs each map with 10 other transcripts in the demo and with 20 in the study.
- (f) The demo draws 200 bootstrap samples and 200 permutations, where the study draws 2,000 bootstrap samples and
  1,000 or 10,000 permutations depending on the test, so the demo's intervals and p-values are coarser.

A match with Tables 15.1 and 15.2 therefore shows that the kit computes each measure as the technical appendix describes
it, under the demo's settings, and it shows how the study's two models answer interviews whose answers are known. The
values estimate nothing about food trucks or about any service. A reader who runs the two open-weight models locally can
also record new answers with `make demo-live` and repeat the steps, and the numbers will then be close to those in Table
15.1 but may differ, because the judge does not always repeat its answers and because `make demo-live` draws the drafts
of the model's and the other family's no-interview scripts again at random, at temperature 0.7. The config's `reading3`
block keeps the no-interview map itself at temperature 0, as in the study.

## What the synthetic demo shows, and what it does not

`demo/corpus/` holds 12 synthetic food-truck licensing interviews (owners and city licensing officers). Each has a
transcript (.docx), an endorsed map (.vsdx), a gold graph of what the transcript supports, and a list of
**planted traps**: a wished-for service, a denied one, one that happened to a cousin, a hypothetical, a step only
the interviewer voiced, a step that exists only in the interviewer's map talk, a workaround, a loop, a dead end, an
edge drawn but never said, and a box drawn but never mentioned. `traps.md` lists, per trap and per arm, what should
happen and what did.

Recovering the planted traps shows that the code path works from end to end: parsing, quote checks, matching,
bucketing, the controls and the report. It is a unit test of the code. It is not evidence that the method suits any
real set of interviews, and it says nothing about how a model behaves on them. With the oracle client every number is
the code's answer to a known question.

<!-- app-section:start -->
## The local app

A local app that shows what listening added when an AI turns interviews into maps. You give it your interview
transcripts and the maps your team drew; two language models from different families, running on your own
computer, read the transcripts; the app shows the results next to your maps. It is inspectable and re-runnable:
model calls go into hash-chained logs, and a run certificate lists what was run and checks the logs against the
chain, which can reveal edits when compared with a trusted head.

    python3 -m second_scribe.app        # on Windows: py -m second_scribe.app

The terminal prints a link; open it in your browser. You need Python 3.10 or newer and two local models, such as
Ollama or LM Studio with models of different families. Python's standard library is all the code uses, so there is
nothing to install with pip.

- **[INSTALL.md](INSTALL.md)**: Python, the code, the models, `OLLAMA_NO_CLOUD=1`, and starting the app.
- **[DATAFLOW.md](DATAFLOW.md)**: where your data goes. By default nothing leaves your machine. It covers the
  remote option (an https-only allow-list, a declaration for each remote role, keys the app holds kept in memory only), what
  the run certificate attests, and a ten-minute check for a data steward.
- **Profile for comparison**: the file made to leave your project folder. It holds counts, rates and intervals,
  with the run's hashes and model names, and never quotes, and the exporter refuses to write it for fewer than 10 people.
- **The offline site**: `make app` builds Explain, The study, Try it and Install into `site/`, which opens from a
  file with no network.

The app is a separate layer on top of the kit described below. The kit's own commands and its privacy rules apply
to everything the app runs.
<!-- app-section:end -->

## Running on your own data

You need interview transcripts and the human-made maps for them. The default path needs no extra coding hours.

| Input | Accepted formats | Example or template |
|---|---|---|
| Transcript | `.docx`, UTF-8 `.txt` | `demo/corpus/transcripts/`; text turns such as `Participant: I applied for a permit.` |
| Visio map | `.vsdx` | `demo/corpus/maps/` |
| diagrams.net map | `.drawio` | `tests/fixtures/maps/journey_plain.drawio` and `journey_compressed.drawio` |
| Excalidraw map | `.excalidraw` | `tests/fixtures/maps/permit-flow.excalidraw` |
| Mermaid flowchart | `.mmd` | `tests/fixtures/maps/permit_flow.mmd` |
| Table map | `.csv`, `.xlsx` | `second_scribe/app/static/templates/map-template.csv` and `map-template.xlsx` |
| Kit graph | `.mapgraph.json` | The kit's `MapGraph` JSON format; `X.mapgraph.json` pairs with transcript `X`. |

CSV headers: `kind,id,label,phase,status,missing_kind,from,to`. All except `phase` and `missing_kind` are required
columns. Every row needs a `status`: a `box` row also needs `id` and `label`, an `arrow` row also needs `from` and `to`.
Status is `happened` or `missing` (`experienced` and `wished` also work). Optional `missing_kind` is `never came`, `wished for` or `denied`.
Put phases in the `phase` column, or Mermaid `subgraph` groups, rather than drawing them as boxes. A map with no
boxes is refused, except the app's deliberate empty reference in automatic-checks-only mode.

Transcripts must be in English. The word splitter used by the quote check and the word counts keeps only the ASCII letters
a-z, digits and apostrophes, so accented and other non-English text is cut into fragments; non-English transcripts are a known limit of
the kit, not a supported input.

Transcripts are assumed to be two-party interviews. Configure both speaker-label sets. A label in neither set
is read as an interviewer turn and produces a warning; this does not support group interviews with distinct roles.

**1. Point the kit at your models.** Reconstruction needs a long-context model (`RECON`). Judging, alignment and
canonical labels need a judge (`JUDGE`; optionally a second judge, `JUDGE2`). Any OpenAI-compatible
`/v1/chat/completions` endpoint works: a local server, a hosted API, or an institutional gateway. Set environment
variables:

    export SS_RECON_URL=http://localhost:8000/v1   SS_RECON_MODEL=<model id>
    export SS_JUDGE_URL=http://localhost:8001/v1   SS_JUDGE_MODEL=<model id>
    # optional: SS_<PREFIX>_KEY_ENV=<NAME of the variable that holds an API key>
    #           SS_<PREFIX>_PARAMS='{"chat_template_kwargs": {"enable_thinking": false}}'

A key is referenced by the name of an environment variable. The value is read at request time, sent only in the
`Authorization` header, and never logged.

Local is the default, and it is the only mode we ran the study in. A study config refuses every hosted endpoint. A
researcher whose ethics approval allows it (for example under a zero-retention agreement) can declare a hosted
endpoint in the app: https only, with no redirects, and only after they type the basis for it. The basis is printed
on the run certificate, the report and the methods paragraph, and the exported profile records which provider served
which role. Whatever you use, sending interviews to a hosted service is a decision for
your ethics board, not for the kit.

For a `"kind": "real"` corpus the kit refuses to start (exit 2, before any request) unless every endpoint is on this
machine or a private network (see "Privacy model"), every model has thinking switched off in its `SS_<PREFIX>_PARAMS`
(`chat_template_kwargs.enable_thinking` set to `false`, as above), and no judge is Qwen-family or the same model as
the reconstructor.

**2. Write a config.** Start from `configs/real.template.json` (a real-corpus config with placeholders; copy it
outside the repository). `configs/demo.json` is the synthetic demo and stays synthetic. Relative paths resolve
against the config's folder. A corpus labelled `synthetic` must live under `demo/` or beside a `meta.json` with a
`domain` (what `synth.build` writes), or the config is refused. Shape of the file:

    {"run_id": "study-1",
     "corpus": {"kind": "real", "maps_glob": "../data/maps/*.vsdx", "transcripts_glob": "../data/transcripts/*.docx",
                "pairs": null, "participant_labels": {"*": ["P"]}, "interviewer_labels": {"*": ["Interviewer"]},
                "roles": {"<id>": "parent"}, "interviewers": {"<id>": "I1"},
                "participant_id_map": "../private/participant_id_map.json"},
     "context": {"domain": "<a noun phrase, e.g. food-truck licensing in a mid-sized city>",
                 "roles": {"parent": "<role as a noun phrase>"}, "phases": ["..."],
                 "extract": {"phase_names": ["..."]}},
     "specs": [{"arm": "removed", "variant": "p0", "temperature": 0.0, "replicate": 0}, "..."],
     "prior_only": {"variants": ["p0", "p1", "p2"], "samples": 5, "temperature": 0.7},
     "reading3": {"variant": "p0", "samples": 1, "temperature": 0},
     "settings": [{"lex_threshold": 0.6, "judge_accept": "same"}, "..."],
     "models": {"recon": "RECON", "judge": "JUDGE", "judge2": null},
     "out_dir": "../private/study-1", "stats": {"B": 2000}}

The leading spec is the primary one; the leading setting is the primary matching setting. Map files and transcripts are
paired by file name unless `pairs` lists them. The `reading3` block configures reading 3, the no-interview map,
and the `prior_only` block configures the no-interview drafts behind the model's no-interview script, which the pull and
additions rows (row 6) need. One run draws both sets. The study's reading 3 was one map per role with variant p0 at
temperature 0, as in `{"variant": "p0", "samples": 1, "temperature": 0}`, and the model's no-interview script came from 15 drafts per role
(variants p0–p2, five samples each) at temperature 0.7, as in the `prior_only` line above. A config without a `reading3`
block scores as its no-interview map the `prior_only` draft whose key sorts lowest.
The audit log and the automated checks below are what the kit relies on for rigor.
Duplicate stems are refused; keep one file per participant or name the files explicitly in `corpus.pairs`.
Paired files absent from `corpus.roles` are warned about and counted as `not_in_roles`, rather than analysed.
A transcript above 45,000 words is not sent to the reconstructor and is excluded by default. The limit is
`context.max_input_words` in the config (default 45000); the aggregates count the exclusion as `too_long_excluded`
and the report lists it. Raise the value only if your model's context window can hold the longer text.

**3. Run the stages.** Each group can run on its own, when its endpoint is up:

    python3 -m second_scribe.cli run --config CFG --stages Q    # reconstructor: mapping-talk check, off-record check, reconstructions, no-interview maps
    python3 -m second_scribe.cli run --config CFG --stages J    # judge: modality, quote-shuffled control, canonical labels, alignments
    python3 -m second_scribe.cli run --config CFG --stages M    # metrics and report, no model calls
    python3 -m second_scribe.cli run --config CFG --only recon  # one stage
    python3 -m second_scribe.cli report --config CFG            # recompute the report from work/

Stage order: Q = mapping_talk_confirm, recon, prior_only; J = in_transcript (the off-record check, run on the judge
model it is calibrated on, so the judge must hold a whole transcript), judge_modality, judge_shuffled,
`script_prior_only` (only with `script_sources` in the config: the judge-family model writes its own no-interview
map), canonicalize, align, judge2_modality (canonicalize comes before align because the canonical alignments need
the canonical names); M = metrics, report.

An element whose judge batch never got a valid answer is "not judged": it is counted neither as attested nor as not attested by its own quote, is
counted as `n_not_judged`, and a spec with more than 10% of its items not judged (the quote-shuffled re-judge
included, for the primary spec) is marked `reported_not_interpreted`. A helper call (mapping-talk check, off-record
check, canonical labels, alignment) that never gets a valid answer leaves its item as `fallback`: counted in
`fallbacks`, never used, and redone on the next run.

Audit-log stage names: `q:mapping_talk`, `judge:in_transcript`, `recon:full`, `recon:removed`, `prior_only`,
`judge:modality`, `judge:shuffled`, `judge:script_prior_only`, `judge:canon`, `judge:align`, `judge2:modality`.

Every item is one JSON file under `out_dir/work/<stage>/`. An interrupted run resumes by running the same command
again: finished items (failures included) are skipped, while audits may re-send requests. Inputs, config and prompt
hashes must still match the earlier session, including for synthetic runs. After changing them, use a new output
folder (`--out` for `demo`, `out_dir` in a run config). A second process cannot write to an active run folder;
an abandoned lock is recovered with a note. Output links that resolve outside that folder are refused.
If verification fails, preserve that folder for inspection and start a new output folder.

**4. Check the audit trail.** Save the manifest hash printed at the end of the run somewhere outside its output
folder, then compare with that copy:

    python3 -m second_scribe.cli verify OUT_DIR/logs/run.jsonl --expect-manifest-hash SAVED_HASH

Without this option, `verify` checks consistency of the supplied log and manifest. Someone who rewrites both can
make them consistent again. The externally saved hash is what lets you detect that replacement.

The manifest hash differs from run to run, because the log holds timestamps and request ids, so each run verifies only
against its own saved hash. The report, the aggregates and the trap table of the demo are byte-identical between runs
on the same day; across days, only the date of the declaration check in the report and the aggregates changes.

**5. Read the automated auditors.** Automated checks run at the end of every stage group (Q, J,
M) and write `OUT_DIR/audits/<group>.json` and `OUT_DIR/aggregates/audits.json` (counts only; the report embeds
them). A failed blocking auditor stops the run (exit 4): saved maps that break the schema or whose quotes an
independent matcher cannot find, judge labels that are missing, outside the vocabulary, or in disagreement for the
same cache key, the synthetic-corpus canary, and the leak scanner. Re-sent T=0 requests (`corpus.audit_determinism_k`,
default 20, 0 switches it off) and the agreement of the second judge are reported, not blocking. Failures on a real
corpus name positions (`n#3`, `#2`), never ids a model wrote.

A blocking audit prints the path to its evidence. Exit 4 marks an earlier report with a neighbouring
`report.STALE` file; finish the run before using that report. A successful report rebuild clears its marker.

For a real corpus the canary cannot be switched off (`corpus.canary` may only name another synthetic config). Before
the opening real model call of every session, a small preflight runs the same clients on two synthetic transcripts and
four planted wished/negated items. After the opening model stage group, the whole synthetic corpus runs through the same
stages. At the end of the judge group, the planted items go through the judge once more. If any of them fails, the
run stops. When it is the second one, every work item the session wrote is marked `quarantined`; when it is the
last one, the session's judge items only, and the judge caches are restored. The next session redoes them and no
later stage reads them.

**6. Optional checks with no model.** On a finished judge phase (the run's out_dir is only read; aggregates only,
written to a new folder with its own hash-chained log; see `second_scribe/nomodel.py`):

    python3 -m second_scribe.cli validate-nomodel --config CFG --out DIR [--checks keyword,robustness,null,diffs]

- `keyword`: the no-AI cue-word labels next to the judge's (agreement and kappa per class).
- `robustness`: agreement per interview and per role, and leave-one-interview-out of the headline numbers.
- `null`: the run's quotes located in another participant's transcript under the current quote rule.
- `diffs`: what differs in the approximate quotes, counted by type, never the words.

**7. Scan before anything leaves the machine** (appendix, draft, figures, the public repository):

    python3 -m second_scribe.cli audit-leaks --real-config CFG --paths FILE_OR_FOLDER ... [--repo DIR]

Exit 0 is clean, 1 is hits, 2 means the scan could not run (any error: never read an error as "hits"). `CFG` must name
`corpus.participant_id_map` (a JSON file with a `map` of initials to entries) for a real corpus. `--repo` scans what
`git push` would publish: every blob of the index and of HEAD and every blob reachable from HEAD (merge commits
included), every path, every commit message and every annotated tag message; `--since-upstream` limits the history to
`@{upstream}..HEAD`. A git-LFS pointer is unscannable, and a working tree that differs from HEAD is itself a failure.

A hit is any 5-word run (runs with 4 or more stopwords excepted) found in a real transcript (headers, footers and
comments included), endorsed-map label or work file; a participant id, initials or interviewer code; a string over 40
characters in a JSON aggregate; an email, phone number, Canadian postal code or IPv4 address (loopback excepted).
Texts are compared after folding accents, invisible characters, look-alike Cyrillic and Greek letters and hyphenated
line breaks. Initials match only in capitals (also as `X.Y.` or `XY2`); ids match in capitals with any separator,
space or zero padding (`X07`, `X-7`, `X 7`; never `x7`); a file name gives only id-shaped parts, never words or
acronyms. The names of the synthetic demo config (`I1`-`I3`, `pt`, `iv`, `SYN-*`) and generic role words
(Interviewer, Participant...) are public: they are exempt as speaker labels and as file-name stems, and interviewer
codes of the form `I<digits>` are exempt as public pseudonyms; all are reported as `names_public_exempt`. A name from
the id map, `corpus.roles` or `corpus.pairs` is never dropped: if it collides with a public name (`PT`, `I3`, `PT01`,
`MAP`), the index records the count-only problem `public_name_collision` and the scan fails closed (rename the
speaker tag or the id). Every file is read in its decoded views (HTML entities, JSON strings, markup without tags,
RTF and TeX without control words, archives and compressed files opened, PNG text chunks, UTF-16/32, cp1252). A
missing path, a PDF, an encrypted or binary file, or an SVG without text elements (unless its data file is scanned
too) fails the scan. The output holds counts and `kind:path@byte offset` (offset 0: found only in a decoded view),
never the matched text, and the forbidden sets exist only in memory.

Accepted limits of the scan: the names of people who are not participants (staff, relatives) and of places or sites are
not looked for, so text that names them can pass; a copied fragment of 4 words or fewer, and deliberate obfuscation (base64, URL percent
encoding, ROT13, synonyms, paraphrase), are not caught; text drawn as pixels or outlines cannot be read. If a write
is refused or an output scan fails, the earlier outputs are moved to `OUT_DIR/stale/`.

**8. Judge benchmark on the synthetic planted traps** (no real data):
`python3 -m second_scribe.cli bench --mode generative|logprob --out DIR` (`bench --help` lists the options;
`--client oracle` runs it offline). The pipeline judges in generative mode, so the bench that selects the judge is
`--mode generative`; its output starts with the floors, the `in_transcript` (off-record) rows on top.
`--label-order both` also judges the same items with the labels listed backwards and reports the agreement between
the two orders (label-position bias).

**9. Export a profile.** `make export-profile CONFIG=run-config.json PROFILE=profile.json [OUTDIR=out] [WITHHOLD=1]`,
which runs `python3 -m second_scribe.cli export-profile --config CONFIG --profile FILE [--out-dir DIR] [--B N]
[--seed N] [--model-info FILE] [--reference-type CODE] [--withhold-chain-heads]`. `--withhold-chain-heads` writes
`run.run_chain_heads` as null. The exporter recomputes every cell
with the profile estimator (a pooled ratio of sums with a percentile cluster bootstrap over maps; B 2000 and seed
20261017 by default, both recorded in the file). It never copies kit sub-trees and never writes per-map rows. In a lab
export `state` is `"synthetic"` or `"pending"`; only this study's gate-cleared file carries `"reported"`.

## What the study published

The study behind this kit read 43 of 46 journeys (autism service pathways, Ontario; REB-approved) three ways. The
interviews never leave the authors' machines. The archive contains a reading copy of the aggregate profile and
its schema; the underlying cleared records, gate chain and full declaration log are not included:

- **`analysis/public/profile.json`**: the study's gate-cleared distance profile (schema `orgsci-profile/v2`,
  `state: "reported"`). It holds numbers and codes only; every number is copied from a gate-cleared record or is
  arithmetic on such records and marked `derived`. The source-entry identifiers report the authors' provenance;
  the records they name stay with the study team. The run block below gives the chain heads: a head lets anyone who
  later receives the records check them, but the head alone does not show what they hold or their order.
- **`analysis/public/PROFILE-SCHEMA.md`**: every field, band, qualifier and `rests_on` code, the pooling rule, and the
  fields a lab export adds.
- **The run block** inside the profile: the head of the gate chain (links and file hashes checked by the authors), the
  head of the declaration log, the prompt-set hashes, the model families and the declared settings. The
  declaration log is a record of the analysis rules, not a lock. Per-record hashes of the real run are not
  published; the heads of the real run's own chains are available from the authors.

**The publish gate.** Only gate-cleared pooled numbers are public:

- no per-map rows;
- no per-interviewer or per-role splits;
- no cell under 10 people;
- no per-record hashes;
- no second-judge headline differencing.

Small counts of elements (for example, ideals that were flattened) are allowed by the gate's policy when they are
counts of elements, not of people, and are printed with their bounds.

**How to read it.** The profile has a lead row (listening added, with the sizes of the two maps beside it, and
arithmetic on those sizes marked "not a test"), a ledger row and nine rows. Where a number depends on the matcher or
the labeller, the file carries a band over the eight match settings or the three labellers, and the band is the
number to quote. These sensitivity ranges are distinct from sampling confidence intervals. Anyone can
inspect the aggregate profile and its source identifiers, and can redo the whole method on the public synthetic corpus:
every measure, Tables 15.1 and 15.2, and the fingerprint check (see "Reproduce Table 15.1" above). The synthetic values
show how each measure is computed; they are not the study's findings. The claims about autism services are tested when
other teams run the kit on their own interviews: use `export-profile` to write a gate-checked profile in the same
format as ours; the kit's pool command places such profiles side by side, and a team can read its own profile against
the nine rows of ours. The interviews stay with the study
team, and a re-run on real data may not match to the last digit.

The kit's synthetic output has the same shape. A lab export marks what it could not compute with `state: "not_run"`
and the model steps it still needs.

## Privacy model

- **Local endpoints.** Interviews go only to the endpoints you configure. The no-interview calls carry no interview
  text at all: only the domain, the role and the phases. For a `"kind": "real"` corpus this is enforced before any
  request: every endpoint (reconstructor, judge, second judge) must be loopback (`127.0.0.0/8`, `::1`), a private
  range (RFC 1918: `10/8`, `172.16/12`, `192.168/16`) or Tailscale (`100.64/10`). The host is read from the URL; a
  name is resolved and every address it resolves to must be in those ranges. Anything else, or a name that does not
  resolve, is refused with exit 2. Synthetic corpora may use any endpoint. The check runs when the run starts, so a
  name whose DNS answer changes mid-run is not re-checked; use IP addresses or a fixed `/etc/hosts` entry.
- **Aggregates only.** `corpus.kind` must be exactly `"synthetic"` or `"real"`. For every corpus that is not
  synthetic, everything the kit writes outside `work/` is counts, rates, confidence intervals, n and exclusions. Before
  writing, a guard walks the output and refuses any string longer than 40 characters (exact-match exceptions: whole
  sha256 digests and the kit's own fixed notes, listed by value) and any string equal to a node label or quote; the
  labels and quotes are collected in memory and never written. `work/` and `logs/` hold participant text: keep them on
  the machine that ran the study.
- **Audit log.** Every pipeline model call is written to a hash-chained, append-only log before it is sent and after it
  returns (`logs/run.jsonl`), closed by a manifest. Against a manifest hash saved elsewhere, `verify
  --expect-manifest-hash` detects edits, deletions, reordering and truncation; it also flags requests with no recorded
  answer. Without that external anchor, a consistent rewrite of both log and manifest can pass. Every command that opens an existing log checks it, before anything else, against its manifest and
  refuses (exit 2) if records were cut or rewritten; each manifest records the hash of the one it replaces. Each run
  records the config hash, the prompt-set hash (`prompts/MANIFEST.json`) and the model ids; each call records the
  endpoint host it went to (never the URL, credentials or key). Determinism re-asks are recorded in the audit summary,
  rather than as individual request records.
- **Thinking off.** A reply that carries thinking output (a `reasoning_content` or `reasoning` field, reasoning
  tokens, or a `<think>` block) stops the run (exit 4). The call is logged as an error with the kind and size of what
  was seen, and no item is written, so it can be redone once the server is fixed.
- **No de-identification by a model.** The kit never asks a model to remove or rewrite participant text.
- **Sync folders.** CLI runs on real corpora refuse known sync folders for inputs and output. Move them to a local
  folder, or use `--allow-synced-folder` only if you intend those files to be copied by that service. Folder-name
  detection is best effort; the researcher still chooses where the files live.
- **What a profile export leaves out.** Per-map rows, per-interviewer and per-role splits, per-record hashes, quotes
  and labels. A cell computed on fewer than 10 people stays in the file with no value, marked not run
  (`cell_under_10`). A lab with fewer than 10 people analysed cannot export.
- **Before you publish a repository of your own,** run `audit-leaks --repo` (step 7 above) on it.

## Limits

- **Endorsement shows agreement, not completeness.** A participant who endorses a map agrees with it and with a
  selection of what was said. That is why the yardstick is only the endorsed steps that appear in the transcript, and
  why the ledger counts what the map carries beyond the transcript as well as what the transcript supports that the map
  does not hold. The 304 quote-attested unmatched AI boxes are candidate additions within the generated AI set.
  They bound true additions from above only if accepted matches and attestation are correct: missed true matches
  inflate the count, false accepted matches can hide additions, and content omitted by both maps is invisible.
  The 365 boxes not attested by their supplied quote are not thereby absent everywhere in the transcript.
- **The no-interview map starts from the domain.** It is given the domain, the role and the phases, so its score
  measures what the model brings to that domain and template, not what it brings to an empty page. It is one draw per
  role, so the interval on its score carries no draw-to-draw variance. The lead holds for this prompt, and it moves with
  model, prompt and domain.
- **Map size matters.** The AI's map and the no-interview map differ in size, and recall rises with size. The profile
  prints both sizes beside the lead, and two box-for-box cells. One is the expected recall of the AI map cut at random
  to the no-interview map's size (.217 [.199, .238] in the study). The other is that recall minus the no-interview
  recall (+.065 [.043, .086]). They answer a narrower question, whether listening helps with equally many boxes,
  and they lean against the AI map, because a model asked for fewer boxes would keep the important steps rather than
  a random subset. The profile also prints arithmetic on the sizes under a constant-yield assumption, labelled as not
  used in the study's paper.
- **The judge is a model.** It is from another family than the reader and is checked on a synthetic bench of planted
  traps, which tests the judge only. A second family re-labels modality elements, and numbers that depend on the modality
  label are reported as bands. Re-sent its own requests, a judge changes some labels: re-runnable, not reproducible to
  the digit. Agreement between model families is not independence.
- **The known-distance tests** (`make phantom`, `make calibrate`) check the gauge's arithmetic on planted doses of
  distance in synthetic text. They do not check matching on real data.
- **The synthetic demo** tests the code path. It is not evidence about real interviews.
- **Scope of the study.** One domain, 43 maps, one model family at two sizes. Which model you choose changes the
  map, and the instrument shows it. Nothing here supports a structural claim about organizations.
- **Pooling is a table.** Profiles are pooled side by side, within one reference type and one prompt set. There is no
  combined estimate, no common interval, no meta-analysis, and no ranking of labs or models. A lab that ran only some
  prompt variants is not flagged; labs should state which variants ran.
- **Leak scan limits** are listed in step 7. The scan is a safety net, not a substitute for your ethics board's rules.
- **Audit trail limits.** A consistent rewrite of both log and manifest passes a check that uses only the folder's own files. Compare with
  a manifest hash saved outside the folder, using `verify --expect-manifest-hash`.
- **English only.** The word splitter used by the quote check and the word counts keeps only ASCII letters, so non-English transcripts are a known
  limit.
- **Ethics.** New uses of your own data are a question for your own ethics board.

## Layout, citation, licence

- `second_scribe/pipeline.py`: stages, resume, aggregates. `cli.py`: the commands. `report.py`: the aggregate-only
  outputs and the guard. `demo_oracle.py`: the oracle client for the synthetic demo.
- `second_scribe/audits.py`: the independent auditors. `leakscan.py`: the leak scanner's engine.
- `second_scribe/reconstruct.py`, `judge.py`, `match.py`, `transcripts.py`, `provenance.py`, `metrics.py`, `stats.py`,
  `graphs.py`, `keywords.py`: the measurement. `audit/`: the log and its verifier. `maps/`: .vsdx reading. `synth/`:
  the synthetic corpus builder.
- `prompts/`: every prompt, frozen and hashed in `MANIFEST.json` (`python3 -m second_scribe.prompt_manifest`).
- `configs/`: the demo config, the real-corpus template and the smoke-test configs.
- `demo/`: the synthetic corpus and the recorded fixtures. `demo/recorded/run-2026-10-18.jsonl` is the log of the
  18 Oct 2026 recording, in which the study's two models answered the synthetic corpus live. The app's time estimates
  (`second_scribe/app/timing.py`) pool the response times in this log with those in `demo/recorded/run.jsonl`.
- `analysis/public/`: the study's distance profile and its schema.
- `tests/`: the unit tests (`make test`).

**Cite as:** *What did listening add? Human and AI readings of confidential interviews*. 2026.
Anonymous Stage 1 submission; proposal and technical appendix accompany this archive.

**Repository:** this archive (Stage 1, anonymous for review); the kit is also kept in a public repository, withheld here for blind review.

MIT licence.
