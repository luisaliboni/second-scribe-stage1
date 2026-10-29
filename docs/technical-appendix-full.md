<!-- profile.json + PROFILE-SCHEMA.md -->

# Technical appendix, complete reference: What did listening add? Human and AI readings of confidential interviews

The appendix has three parts. Part A shows the method on synthetic interviews from the public corpus, so that each step can be shown without exposing any participant. Part B reports the study itself, the 43 <!-- 002 --> interviews on autism services, with its data, readings, measures, results, controls and instrument error. Part C covers the checks on synthetic data, the record keeping, the kit, the limitations of the study and the steps a reader can follow to reproduce our measures on the synthetic corpus.

# Part A. The method, shown on synthetic interviews

## How the study works, in brief

We read each interview three ways and ask what each reading reaches that the others miss.

1. **Three maps per interview.** Reading 1 is the interviewer-drawn, participant-endorsed map, drawn live by the interviewer and endorsed by the participant (the endorsed map below). Reading 2 is a local AI model's map of the transcript. Reading 3, the no-interview map, is the same model's map of the official pathway for the same role and phases, with no interview (one per role). Readings 2 and 3, each scored against reading 1, show what listening added, a descriptive contrast between two configurations (Sections 2, 7).
2. **Quotes that code can find.** The prompt asks for a verbatim quote for every AI element, and code, not a model, looks for it in the transcript, exactly or by a close match; this keeps the map tied to what was said, and an unfound quote leaves its element unanchored (Section 2).
3. **A judge labels each quote.** A second model, the judge, labels what each quote reports: happened, wished for, denied, hypothetical, or someone else's. An element is attested only when that label backs how it was drawn (Sections 2, 4).
4. **Matching and recall.** A matcher pairs AI boxes with endorsed steps by word overlap or the judge's reading. Recall is the share of endorsed steps said in the transcript that a map recovers, reported with its band across eight matcher settings (Sections 4 to 7). Attested AI boxes that the endorsed map lacks are counted as in the transcript, not on the map; they are candidate additions, and we read that count as a ceiling only under the conditions in Section 6.
5. **Bounding our own error.** Controls remove one ingredient at a time (the interview, the right person's transcript, the judge) and measure what is left. A second judge family relabels the quotes' modality, and repeat calls show how often the judge's labels change (Sections 8 to 11).
6. **A tamper-evident record.** Every pipeline model call goes to a hash-chained log before it is sent and after it returns. Later edits can be checked against a hash saved outside the run folder; rule changes go to a separate declaration log (Section 12).
7. **Only cleared numbers leave.** Transcripts, maps and model outputs stay on our machines. Only numbers the publish gate clears leave, with no per-map rows and no cell under k = 10 people; the distance profile is built from them alone (Sections 1, 5).

**One planted trap, step by step.** In the public synthetic food-truck corpus, where the answers are known (Section 13), one planted trap is a service the owner only wished for. If the AI draws it, code must find its quote (step 2) and the judge should label it wished for (step 3). Drawn as received, the box is not attested by its own quote; drawn as wished, it is attested and can match the endorsed step (step 4). The demo's trap table lists what should happen and what did, and every call is logged (step 6). This tests the pipeline, never real interviews.

## Glossary

| Term | In plain words | Detail in |
|---|---|---|
| Endorsed map | The interviewer-drawn, participant-endorsed map: drawn live on a phase template during the interview and later endorsed by the participant. Endorsement indicates agreement, not completeness. | Sections 1, 2 |
| No-interview map | The map the same model draws of the official pathway for one role and the template's phases, with no interview; one map per role, from a single draw. | Section 2 |
| Attested | Said of an AI element whose quote code finds in the transcript and whose judge label backs how it was drawn (drawn as received needs a quote labeled as happening; drawn as wished, a quote labeled as wished). | Section 2 |
| Recall | The share of endorsed steps said in the transcript that a map recovers, pooled over all maps. Attested recall counts only attested AI boxes. | Sections 4, 7 |
| Kept, coarsened, omitted | The three outcomes for an endorsed step said in the transcript: matched at the primary setting (kept), matched only at the lenient setting (coarsened), or not matched (omitted). | Section 4 |
| Judge | The local model that makes the logged labeling calls: whether a step is said in the transcript, what a quote reports, and whether two boxes are the same. | Sections 3, 4 |
| Second family | A judge from a different model family that labels the same quotes, so we can see how much a number depends on one judge. Agreement across families is not independence. | Sections 4, 10 |
| Matcher setting | One of eight stored rules for when two boxes match: word overlap alone at four thresholds, the judge alone (two ways), or either one (primary and lenient). | Sections 4, 6 |
| Ledger | The table that sets the two readings of one transcript side by side and counts what each holds that the other lacks. It is not an inventory of everything the transcript holds. | Section 6 |
| Profile | The distance profile: the one file the gauge exports, nine rows, each with a value, a band and the model steps it rests on. | Section 5 |
| Gate-cleared | Passed by the publish gate, which refuses per-map rows, small cells and the other outputs listed in Section 1. Only gate-cleared numbers leave our machines. | Section 1 |
| Chain head | The hash of the last entry in a hash chain. A change to any earlier entry would change it, so a published head lets whoever holds the records detect later edits; a head alone does not show what the records contain. | Sections 12, 15 |
| Declaration log | A separate hash-chained log of our rules and every dated change to them, which shows which rule came before which result. It is a record, not a lock. The package carries its head and our summary (Section 12), not the log itself. | Section 12 |
| k floor | The smallest group a released number may describe: no cell under k = 10 people. | Sections 1, 13 |

# Part B. The study: 43 interviews on autism services

## 1. Data and governance

**In plain terms:** we say which interviews and maps every number rests on, and the rules that keep them on our machines. Only numbers the publish gate clears ever leave.

**The material.** The study is a project on autism-service journeys in Ontario, approved by the authors' institutional research ethics board (protocol number withheld for blind review). It holds 46 <!-- 002 --> transcript–map pairs. In each mapping interview the interviewer drew the participant's journey live on a phase template while the participant talked, and the participant later endorsed the map by email. We call the result the interviewer-drawn, participant-endorsed map, a form of patient journey mapping, whose methods and reporting vary widely (Davies et al. 2023). The maps were drawn in Visio, with a fixed legend of element types and one lane per phase. A step added while drawing or at the email verification may be absent from the transcript, which is why the yardstick keeps only the endorsed steps said in it. The template has five phases: Early Concerns, Diagnosis, Services, Funding and School.

**Which maps enter.** We analyze 43 of 46 <!-- 002 --> pairs. Two transcripts lack speaker labels and one run failed <!-- 002 -->. These 43 (P0) are the one population for every number below, including the ladder, the second judge and the 27B. No map was cut short by the output cap <!-- 002 -->. The endorsed maps hold 2,085 <!-- 009 --> steps, 1,977 <!-- 003 --> of them in the transcript.

**Where data live.** Transcripts, maps and model outputs stay on the authors' machines; every model runs locally and is open-weight, in line with the case against unexplained proprietary models in research (Palmer et al. 2024), and a study configuration refuses any endpoint off the machine or private network, with no override (Section 13 describes the one declared exception the app allows a researcher on their own data). Scripts are written outside the data and run by one local runner; their authors read only gate-cleared output.

**Use of AI in this work.** AI systems (Anthropic's Claude and OpenAI's Codex) revised the code under the authors' direction, ran automated checks, and reviewed the text of this submission. The study's own measurements come from the local open-weight models named in Section 3. The authors directed the study, made every design decision and reviewed the text.

**The publish gate.** A number leaves only after the gate clears it, and each cleared file is hash-chained (Section 12). The gate refuses per-map rows, per-interviewer or per-role splits, cells under k = 10 people, per-record hashes, and differencing a second-judge headline. A statistic over a subset of maps is released only if the maps it leaves out number zero or between 10 and n − 10, so no subset can be differenced to expose fewer than ten people (G7; G5 for map-level shares). Counts of AI-read map elements, such as 4 <!-- 013 --> flattened ideals, may fall below ten because they describe no person. A separate internal check matched 148 of 149 cells to the printed results or to the cleared records they cite; the remaining cell, yield per AI box, was reconciled to the cleared counts (778/1,268 = .61). Checks against the cleared records and arithmetic found no discrepancy.

When we present the endorsed maps as one of the readings, we use the same data under the same gate, as pooled counts only. New uses of the data are a question for our REB.

## 2. The three readings

**In plain terms:** each interview is read three ways: the endorsed map, an AI's map drawn from the transcript, and the same model's map drawn with no interview, one per role. The AI is asked to back every element with a quote, and code checks whether it can find that quote in the transcript.

The endorsed map and the AI's map are two readings of one transcript, each measured against it. The no-interview map is a comparison map: what the same model produces for the same role and phases with no interview.

**Reading 1: the interviewer-drawn, participant-endorsed map** (procedure in Section 1). Boxes (steps, services, actors, sources of information) sit in the template's phases, joined by arrows. Boxes drawn as wished for, something that should have existed and did not, are ideals; 90 <!-- 013 --> in-transcript endorsed steps are ideals. The maps predate this project and are read from their original files, never recoded.

**Reading 2: the AI's map.** A local model (Section 3) reads each transcript with the interviewer's mapping talk removed, so it works from the conversation, not the drawing session (Crilly et al. 2006), and draws one map per person in the same phases, at temperature 0 <!-- profile.json run.settings -->, thinking off.

The prompt asks for a verbatim quote for every element, and code, not a model, locates it: exact, or at least .85 of its words in order inside one same-speaker turn, within 1.5 times its length, with negations, modal words and words of wanting or condition identical. An accepted quote is therefore not always word for word. The rule is a guard against generated content that the source does not support (Maynez et al. 2020). An unfound quote leaves its element unanchored; a map is rejected only if more than half are unfound. A quote counts only if it is the participant's, outside the removed turns, and at least eight words or a whole turn.

A located element is *attested* when the judge's modality label for its quote backs the status drawn (a box drawn as received needs a quote labeled as happening; an ideal, one labeled as wished); otherwise it is *not attested by its own quote*, which does not mean its content is absent from the rest of the transcript.

**Reading 3: the no-interview map.** The same model maps the official pathway with no interview, given the domain ("autism assessment, diagnosis and support services in Ontario"), a role (a parent or caregiver, or a service provider) and the five phases. Measuring what a model produces from its prior alone follows work that sets a model's prior against supplied evidence (Wu et al. 2024); language models carry script knowledge of typical event sequences, unevenly (Sancheti & Rudinger 2022). There is one map per role, two in all, each shared by everyone in that role, drawn at temperature 0 <!-- 021; profile.json run.settings -->, the same setting as the AI maps. The map we analyze for each role comes from one run, so we did not choose among several maps, and a second run at the same setting gave identical maps for both roles <!-- 021 -->, which shows that the maps are stable for this prompt but says nothing about other prompts. Its score's interval carries no draw-to-draw variance, and the lead holds only for this prompt. We ran no domain-unnamed arm.

The prompt as run is the file at kit commit `b0827ab`, where the no-interview maps were drawn (the file is the same at the reconstruction commit `431da7d`), sha256 `9b48f8639447dfb74da3fd24876cb5c69d3adff8878fd4b50dcdceffd84302c0` <!-- profile.json run.reading3_prompt -->. We quote it once, verbatim; `{role}`, `{domain}`, `{phases}` and `{schema}` are filled by the kit:

```text
You are helping a research team write down the official version of a process.
Describe the typical pathway of a {role} in {domain}, as official guidance would describe it. You have no interview; use only what is generally known about the process.
Draw it as a map:
- Make one node for each distinct step, service, actor (person or organization) or source of information in the pathway; keep different things in different nodes.
- Put each node in one of these phases: {phases}.
- Add a directed edge from one node to another when the official pathway leads from one to the other.
- Every node and edge is part of the official pathway, so mark every node and edge "experienced".
- There is no transcript, so every "quote" must be null.
- Give one word for how the journey typically feels, or null.
Return only JSON in exactly this format. Begin your answer with {, and write no text before or after the JSON:
{schema}
A node's type must be one of the listed types and is never a phase name.
REMINDER: Mark every node and edge "experienced". Every quote is null, because there is no transcript. Use only the phases listed. Reply with only the JSON. Begin your answer with {
```

The released kit ships this file as run <!-- profile.json run.reading3_prompt -->; an earlier development copy lacked the penultimate sentence, as the declaration log records.

**Its size.** The no-interview maps average 15.465 <!-- 023 --> boxes and 15.488 <!-- 023 --> links, against 29.488 <!-- 020 --> and 20.488 <!-- 020 --> for the AI maps (ratio .524 <!-- 023 -->), as points only: with two maps, an interval would reveal each map's size and the role split. Section 7 prints both sizes beside the lead, together with a comparison at equal size.

## 3. Models and settings

**In plain terms:** this section names each model, what it does, where it ran and the settings each run declared, so a reader can rebuild any run's setup.

All models are open-weight and local; no interview text went to any outside service. The kit refuses a real run whose judge is Qwen-family or the same model as the reconstructor <!-- kit README -->, because LLM judges favor their own outputs and those of related models (Panickssery et al. 2024; Li et al. 2026), and any reply carrying thinking output stops the run <!-- kit README -->.

Entries marked "not in the public record" have no non-private record.

| Role | Model | Served id | Weights / quantization | Machine |
|---|---|---|---|---|
| Reconstructor (readings 2 and 3) | Qwen3.8-Flash-Next, thinking off | `mtplx-qwen38-flashnext` | `Youssofal/Qwen3.8-Flash-Next-MTPLX-Optimized-Speed`; quantization not in the public record | Mac Studio |
| Secondary reconstructor (primary setting only) | Qwen3.8-27B; thinking setting not in the public record | `qwen38-27b` | AWQ | DGX |
| Judge | DeepSeek V4 Flash, thinking off | `deepseek-v4-flash-0731` | UD-Q8_K_XL | Mac Studio |
| Second judge family | Gemma 4 31B | not in the public record | Q8_0 GGUF | Mac Studio |

**Declared settings** (configuration, not results):

- primary spec = mapping talk removed, prompt variant p0, temperature 0 <!-- profile.json -->;
- no-interview map, one draw per role at temperature 0, as the AI map <!-- profile.json run.settings -->;
- primary matcher, lexical .6 <!-- profile.json run.settings --> or judge "same";
- lenient, lexical .4 <!-- profile.json run.settings --> or "same or close";
- quote cutoff 8 <!-- 003 --> words (5 and 12 <!-- 003 --> as sensitivity);
- percentile cluster bootstrap over maps, B = 2000 <!-- 013; profile.json run.settings -->, paired by map;
- gate k = 10.

**What ran when.** Reconstruction ran from kit commit `431da7d`, judging from `b0827ab`, metrics from `bcf1034` <!-- profile.json run.kit.run_commits -->. The no-interview maps were drawn, and scored by the judge, at kit commit `b0827ab`; the no-interview prompt file is the same at both commits (sha256 `9b48f863…`). The prompt sets, which differ in the judge prompts (v3), are `4d03af43…` (reconstruction) and `fcb27f0e…` (judging, metrics) <!-- profile.json run.prompt_set -->; the pooling rule's "one prompt set" means this pair. The judge's runs on the AI maps and the metrics runs passed our verifier on 17 Oct 2026 <!-- profile.json run.verify -->, and the judge's run that scored the no-interview maps passed it on 22 Oct 2026 <!-- profile.json run.verify -->. No analysis here made a new model call.

## 4. Matching and labeling

**In plain terms:** before counting anything we must decide when an AI box and an endorsed step are the same thing, and what each quote reports. A matcher that combines word overlap with the judge settles the matching, and we test it on reworded labels; three labelers read the quotes.

Every recall and precision number depends on how boxes are matched and how quotes are labeled; we set out both and calibrate the matcher.

**Judge calls.** Five logged calls: *in transcript* (IT), whether each endorsed step is said in the transcript (the 108 <!-- 001 --> it places off the record leave the denominator); *modality* (MO), whether a quote reports something that happened, was wished for, denied, hypothetical, or happened to someone else (negation: García-Ferrero et al. 2023; hypothetical and modal statements: Basmov et al. 2024); *canonical names* (CN) for the pull measure; *alignment* with the endorsed map (AL), "same" or "close" (coarser or finer); and alignment with a no-interview map (AS). Code locates quotes (Q) and computes the lexical channel (LX).

**The matcher.** Two boxes match if the Jaccard overlap of their content words reaches a threshold or the judge's label is accepted; matching is many-to-many, and the judge only adds pairs. Scoring a generated map by the share of a reference map's elements it recovers follows text-to-map work in system dynamics (Hosseinichimeh et al. 2024); node- and path-level alignment of generated diagrams is treated elsewhere (Liang & You 2025). Eight settings are stored: lexical only at .4, .5, .6, .7 <!-- PROFILE-SCHEMA §3 -->; judge only, "same" or "same or close"; primary; lenient. The four judge-using settings are the two judge-only ones, primary and lenient. Each in-transcript endorsed step is *kept* (matched at primary), *coarsened* (matched only at lenient) or *omitted*, using any AI box; attested recall counts only attested boxes.

**Matcher calibration (A2).** We reworded endorsed labels and asked whether the matcher found each one's source <!-- 017 -->; the second family wrote the paraphrases, so the main judge never grades its own (Panickssery et al. 2024; Li et al. 2026). The table gives, for each kind of rewording, how often the primary setting found the source.

| Rewording | Matched to its source (primary setting) |
|---|---|
| Word order changed | 97 of 97 <!-- 017 --> |
| One word deleted | 50 of 50 <!-- 017 --> |
| Typo | .814 [.726, .879] <!-- 017 --> |
| Paraphrase | .70 [.604, .781] <!-- 017 --> |
| Paraphrase, lexical channel only | .03 <!-- 017 --> |

The judge channel carries the paraphrases. R\*, the highest recall the calibration allows, is the upper end of attested recall over the lower end of the paraphrase catch rate: .331 / .604 = .548 <!-- 017 -->. Because R\* ≥ .50, attested recall is always a band, [.302, .363] <!-- 003 --> (primary, lenient).

The limit: R\* bounds same-grain recall only if real AI wording is no harder to match than a paraphrase. On the same logged requests the second family finds 560 <!-- 017 --> "same" pairs to the main judge's 614 <!-- 017 --> (476 <!-- 017 --> shared; ratio ρ = .912 <!-- 017 -->; it recovers .775 [.741, .806] <!-- 017 --> of them), so on real pairs it finds slightly fewer. That is agreement between families, not accuracy.

Given endorsed labels from other people's maps, the off-record check called .030 [.016, .054] <!-- 017 --> of 335 <!-- 017 --> absent labels present, under the .20 <!-- 017 --> threshold for a corrected denominator. We run this check because LLM judges are much weaker at confirming absence than presence (Fox et al. 2026).

**Labelers.** Modality has three labelers: the main judge, Gemma (covering every element) and a no-model keyword labeler <!-- 018 -->. Bands are the range across them (Section 10). Labelers from disjoint model families reduce the bias of any one judge (Verga et al. 2024); agreement among them is still not independence (Jiang et al. 2025). Numbers that read no modality, such as the absence counts and the pull, are identical across labelers by construction and never cited as robust on that ground.

## 5. The distance profile

**In plain terms:** the profile is the one result the gauge exports, a table of nine rows. Each row gives one measurement, how far it moves across settings or labelers, and which model steps it depends on.

The gauge exports one file, the distance profile (`analysis/public/profile.json`, schema `orgsci-profile/v2`), built only from gate-cleared records. The same file fills this table and the public "The study" panels, and it shares its measurements with the schema a lab would export in Stage 2 <!-- PROFILE-SCHEMA §1 -->. It is our study's profile, for reading; a lab's comparison file is the one `export-profile` writes, with the provenance fields `pool` requires (Section 13).

Each row of the table gives a primary value, a band (the range over the eight match settings, primary against lenient, or the three labelers; else the 95% interval) and the steps it rests on. Bracketed values are 95% bootstrap intervals over maps, which hold the generated maps and judgments fixed; bands across settings or labelers show sensitivity and are not confidence intervals. The steps, traced in the kit's code, are R, the AI's map; P, the model's no-interview output (the no-interview maps in row 7, the model's no-interview script in row 6); JS, the other family's no-interview script; IT, AL, AS, CN, MO, the judge calls (Section 4); LD, alignment re-asked in the ladder; J2, the second family; Q, quote location; LX, the lexical matcher. "=" marks arithmetic on cleared counts. Every row rests on 43 <!-- 002 --> people.

| # | Row | Value (primary) | Band | Rests on |
|---|---|---|---|---|
| 1 | Coverage, of 1,977 <!-- 003 --> in-transcript endorsed steps | kept .394 [.361, .427]; coarsened .068 [.054, .082]; omitted .539 [.507, .569] <!-- 003 -->. Attested recall .302 [.272, .331] <!-- 003 --> | Kept over the 8 settings .073–.461 (judge-using .389–.461; lexical only .073–.200) <!-- 003 -->. Attested recall .050–.363 (judge-using .298–.363) <!-- 003 -->. R\* .548 <!-- 017 --> (a bound only if the AI's wording is no harder to match than a paraphrase), so recall is a band, [.302, .363] <!-- 003 --> | R, LX, AL, IT; attested recall adds Q, MO |
| 2 | Size | = .61 <!-- 003; 009 --> AI boxes per endorsed box (1,268 / 2,085 <!-- 003; 009 -->); per map, 29.488 [26.744, 32.837] AI and 48.488 [45.116, 52.023] endorsed boxes <!-- 020 --> | One draw; .39 <!-- 009; 014 --> under rung 1 | R |
| 3 | Support | precision .472 [.405, .538] <!-- 003 --> of 1,268 <!-- 003 --> boxes; 304 in the transcript, not on the map (conditional ceiling, Section 6); 365 not attested by their own quote <!-- 001; 003 --> | Precision .074–.554 over 8 settings <!-- 003 -->; labellers 265–304 narrow, 365–446 wide <!-- 018 --> | 304: R, LX, AL, Q, MO. 365: R, Q, MO |
| 4 | Links, of 1,481 <!-- 005 --> | endpoint lost .857 [.827, .886]; eligible .138 (204); of these, direct .338 [.27, .402], 7.9× the null .043; path ≤ 2 .392 <!-- 005 --> | Direct, judge same or close (word overlap .6) .312 [.251, .374] <!-- 005 --> | R links, IT, LX, AL, Q, MO |
| 5 | Absences, of 90 <!-- 013 --> ideals (32 <!-- 013 --> matched) | as wished 28; flattened 4; received against own quote 0; missed 55; 3 matched only by boxes without a usable quote <!-- 013 --> | Lenient 35 / 6 / 0 / 45 <!-- 013 -->; zero bounded at .033 / .089 / .070 <!-- 013 -->; matcher-free .126 [.069, .19] <!-- 010 -->, labellers 0–.126 <!-- 018 --> | Counts: R, IT, LX, AL, Q; split adds MO; matcher-free R, Q, MO |
| 6 | Slant | pull .014 [−.047, .072], above .064 ruled out <!-- 007 -->; additions on the model's no-interview script, unattested minus attested +.157 [.103, .221]; other family's script +.004 [−.021, .031] <!-- 009 --> | Pull variants .012–.021 <!-- 007 --> | Pull: R, P, CN, AS, LX. Additions: R, Q, MO, AL, LX with P or JS |
| 7 | Floors | AI without the interview: +.241 [.205, .277] (.394 vs .152) <!-- 021 -->. Wrong transcript: +.073 [.061, .086] (.087 vs .014); own map's mean rank 1.09 of 43 <!-- 013 --> | Lenient +.225 [.178, .27]; attested line +.150 / +.126 <!-- 021 -->. Wrong transcript across labellers .068–.073, narrow <!-- 018 --> | No interview: R, AL, LX, IT against P, AS, LX, IT. Wrong transcript: R, Q, MO, IT, LX. Rank: R, CN |
| 8 | Instruction, one draw per rung | boxes per map 19.0 / 29.488 / 27.14 / 26.442; size-adjusted recall .609 / .540 / .557 / .569 <!-- 014 --> (rung 1, rung 2, remedy, placebo) | 2−1 −.069 [−.127, −.013] <!-- 014 -->; remedy minus placebo leaks −.022 [−.073, .031] <!-- 015 --> | Boxes: R. Recall: R, Q, LD, LX, IT |
| 9 | Instrument error | lexical floor .200 [.182, .219] <!-- 003 -->; re-judging changes 40 of 1,887 lived steps and 4 of 90 ideals <!-- 014 --> | Repeats change .114 (modality), .033 (alignment); ARI .977; label order .052 <!-- 019 --> | Floor: R, IT, LX. Re-judging: AL, LD. Bands: MO, J2. Repeats: MO, AL, CN |

Row 4 licenses no structural claim: the rule writes a link sentence only below .25 direct and drops it at or above .50, and .338 <!-- 005 --> sits between; the cross-phase seam test is withheld under G7. Row 6 compares the maps with the model's *no-interview script*, which differs from the no-interview map of Section 2. The script holds the steps, under the judge's standard names, that appear in at least two of the 15 no-interview drafts the model wrote for a role (three prompt variants, five runs each, at temperature 0.7, so that the drafts differ), and the other family's no-interview script is built in the same way from drafts by the judge's model family. Row 6's pull is the mean over maps of the AI map's share of boxes found in the model's no-interview script minus the endorsed map's share.

Outside the profile, the AI maps are more fragmented than the endorsed maps (branching share .155 against .25 <!-- 011 -->, Holm p .002 <!-- 011 -->; links per box .68 against .78 <!-- 011 -->), a descriptive result, since links carry no structural claim.

## 6. The ledger: two readings of one transcript across the eight match settings

**In plain terms:** the ledger measures both readings against the same transcript. It counts four things: what the AI left out; what it drew that is in the transcript, not on the map; what its own quote does not back; and what is on the map, not in the transcript. Every pair the matcher misses swells the second count and every false pair shrinks it, so we show it under all eight settings and read it as a ceiling only under the conditions below. None of the four counts is an inventory of everything the transcript holds.

The ledger sets two readings of one transcript, each measured against it, side by side, and opens with the AI's own shortfall.

| Row | Unit | Value (primary) | Band |
|---|---|---|---|
| The AI's shortfall | in-transcript endorsed steps (1,977 <!-- 003 -->) | omitted .539 [.507, .569]; kept .394; coarsened .068 <!-- 003 --> | kept .073–.461 over the 8 settings <!-- 003 --> |
| In the transcript, not on the map | AI boxes (of 1,268 <!-- 003 -->) | 304 <!-- 001; 003; 004 -->, a conditional ceiling | 200–809 over the 8 settings; judge-using 200–310 <!-- 003, derived -->; labellers 265–304 <!-- 018 --> |
| Not attested by its own quote | AI boxes | 365 <!-- 001 --> | labellers 365–446, wide <!-- 018 --> |
| On the map, not in the transcript | endorsed steps (of 2,085 <!-- 009 -->) | 108 <!-- 001 --> | same in every setting |

At the primary setting the AI side closes: 599 <!-- 004 --> attested boxes on the map + 304 <!-- 001; 003; 004 --> + 365 <!-- 001 --> = 1,268 <!-- 003 -->, asserted by the build <!-- PROFILE-SCHEMA §3.1 -->. The 304 and the 108 count different units, so we never set them against each other.

The 365 uses no matcher, leaving 903 <!-- 003, derived --> attested boxes. In each setting, "attested, not on the map" is 903 minus attested precision × 1,268 <!-- 003, derived -->, fixed within ±1 by rounding. The table gives that count for each of the eight settings.

| Setting | Attested precision <!-- 003 --> | Attested, not on the map (=) <!-- 003, derived --> |
|---|---|---|
| Lexical only, .4 | .233 | 607–608 |
| Lexical only, .5 | .207 | 640–641 |
| Lexical only, .6 | .132 | 735–736 |
| Lexical only, .7 | .074 | 809 |
| Judge only, "same" | .468 | 309–310 |
| Judge only, "same or close" | .548 | 208 |
| Primary (lexical .6 or "same") | .472 | 304 |
| Lenient (lexical .4 or "same or close") | .554 | 200–201 |

**The ceiling reading.** Every true pair the matcher misses lands in the 304 <!-- 001; 003; 004 -->. If the accepted pairs and the quote labels are right, the count is therefore an upper bound on the additions among the AI's boxes; a false accepted pair or a wrong label breaks that bound, and Stage 1 gives no lower bound. It is not a bound on everything the transcript holds that the endorsed map lacks: what both maps leave out stays unseen. It runs about 200 to 310 <!-- 003, derived --> under the judge-using settings and 607 to 809 <!-- 003, derived --> under word overlap alone (thresholds .4 to .7), a matcher that almost never catches a paraphrase (.03 <!-- 017 -->). We print it beside the paraphrase catch rate, .70 [.604, .781] <!-- 017 -->, and its labeler band, 265–304 <!-- 018 -->.

**The 365** are boxes whose own quote does not back them: missing, too short, not the participant's, mapping talk, or the wrong modality; 365 to 446 <!-- 018 --> across labelers. **The 108** rest on one judge call (IT); since that check called .030 [.016, .054] <!-- 017 --> of absent labels present, they may be an undercount. Endorsement indicates agreement and a selection, not an inventory (Birt et al. 2016; Motulsky 2021); the transcript is the yardstick for both readings.

## 7. Lead and size

**In plain terms:** this is the lead number: how much more of the endorsed steps the AI's map recovers than the no-interview map. Both map sizes sit beside it because recall rises with map size, and we say when the lead's template and its value were fixed.

**The lead.** On the 1,977 <!-- 003 --> in-transcript endorsed steps, the AI's map recovers .394 [.361, .427] <!-- 013 --> and the same model's no-interview map .152 [.135, .173] <!-- 021 -->: listening added .24 [.205, .277] <!-- 021 -->. This is a descriptive contrast between two configurations, not an isolated effect of hearing the interview: besides the interview, the two maps differ in instructions and size. Recalls are pooled ratios of sums over 43 <!-- 002 --> maps, any AI box, primary setting; the difference is paired by map, cluster bootstrap B = 2000 <!-- 013; profile.json run.settings -->. The bootstrap resamples maps with the generated maps and judgments held fixed, and the two no-interview maps, one per role, are not 43 separate draws. The table gives the lead under each matcher and counting rule.

| Line | AI map | No-interview map | Listening added |
|---|---|---|---|
| Primary, any box | .394 [.361, .427] | .152 [.135, .173] | +.241 [.205, .277] <!-- 021 --> |
| Lenient, any box | .461 [.431, .493] | .236 [.206, .27] | +.225 [.178, .27] <!-- 021 --> |
| Attested line, primary | | | +.150 [.113, .184] <!-- 021 --> |
| Attested line, lenient | | | +.126 [.078, .175] <!-- 021 --> |

The attested line counts only attested AI boxes; the no-interview map has no quotes, so the line handicaps only the AI side and is a sensitivity row.

**Sizes beside the lead.** The no-interview maps average 15.465 <!-- 023 --> boxes, the AI maps 29.488 [26.744, 32.837] <!-- 020 -->, a difference of 14.023 [11.349, 17.326] <!-- 023 --> (`prior_map_smaller` true <!-- 023 -->). Recall rises with map size (slope +.082 [.031, .262] <!-- 004 -->), so part of the .24 <!-- 021 --> may be size. We print the sizes wherever we print the lead; the size script measures size and does not remove it from the lead.

**Box for box.** This comparison asks a narrower question than the lead, namely whether listening helps when both maps hold equally many boxes. For each AI map we compute the recall it would reach if it were cut at random down to the size of its role's no-interview map, averaged over every possible cut. An exact formula gives this average, so no random draw is made and the number is the same on every run. If m of the AI map's N boxes match an endorsed step and the map is cut to k boxes, the step is still matched after the cut with probability 1 − C(N − m, k) / C(N, k), where C(a, b) is the number of ways to choose b boxes from a. Every AI box matched to a step counts, since matching is many-to-many, and the recall is pooled over maps like the lead, with the same cluster bootstrap over maps (B = 2000). Cut to the size of the no-interview map, the AI map recovers .217 [.199, .238] <!-- 022 --> of the reference, which is .065 [.043, .086] <!-- 022 --> above the no-interview map. The comparison leans against the AI map, because a model asked for fewer boxes would keep the important steps, while a random cut keeps the important and the minor steps alike. Stage 1 treats size by this calculation, and Stage 2 would test it by experiment, asking the model for a no-interview map with a set number of boxes.

**Order of events.** The template "listening added …" was declared before the floors script ran; its number was not. Computed as a control under the earlier frame, the result was promoted to the lead after every Phase-1 result had been read, with no rule or threshold changed. The size rule was declared before its run, and the box-for-box comparison was declared before its script ran <!-- amendment 12 §3; declarations.jsonl seq 16 -->.

## 8. Controls and floors

**In plain terms:** each control takes away one ingredient (the interview, the right person's transcript, or the judge) and measures what agreement is left. The last column says what each control cannot tell us.

| Control | Result | Cannot show |
|---|---|---|
| No-interview floor | .152 [.135, .173] <!-- 021 -->; the lead is the gap above it | What a human reading without the interview would produce |
| Wrong transcript (is agreement specific to the person?) | own .087 [.074, .101] against another person's .014 [.012, .017]; +.073 [.061, .086] <!-- 013 -->; labellers .068–.073 <!-- 018 --> | Whether the judge is right |
| Own-map rank | mean 1.09 of 43 <!-- 013 --> (null 22 <!-- 013 -->), p < .001 <!-- 013 -->; the top-rank share is withheld (G5) | The same |
| Lexical floor (how much recall needs the judge) | .200 [.182, .219] <!-- 003 --> against .394 <!-- 003 --> | True recall |
| Quote cutoff, 5 / 8 / 12 words | attested recall .329 / .302 / .227 <!-- 003 --> | |

The wrong-transcript floor uses no alignment call (R, Q, MO, IT, LX), so its own-transcript level, .087 <!-- 013 -->, is a different measure from the any-box recall of .394 <!-- 003 -->: the share of in-transcript endorsed steps matched by content-word overlap alone, at the primary lexical threshold, by AI boxes their own quote backs.

The absence measure's sign holds across the grid: leak minus flip is positive in all 32 <!-- 013 --> cells (.014 to .097 <!-- 013 -->). A quote-shuffled re-judging control ran; no locked script releases it yet.

**The instruction ladder.** Only the instruction text changes across four rungs: rung 1, the authors' original draft prompts; rung 2, the main run's good-practice prompts; a remedy (narrator framing plus counterfactual demonstrations, the two prompting strategies of Zhou et al. 2023); and a placebo matched in demonstration count, format and length, with neutral content. Each rung is one draw at the primary setting, since repeated draws at temperature 0 are near copies, not exact ones (see Instrument error), so every contrast is a descriptive package contrast.

Profile row 8 gives sizes and size-adjusted recall. The one licensed sentence is about size: map size moved with the instruction, about 19 against 29.5 <!-- 014 --> boxes, one draw, descriptive. Per box drawn, rung 1 kept slightly more (rung 2 minus rung 1, −.069 [−.127, −.013] <!-- 014 -->, Holm p .072 <!-- 014 -->). Against the placebo, the remedy showed no side effect that the data detect (lived recall +.012 [−.016, .041]; flips +.003 [−.001, .008] <!-- 014 -->). Ideal recall minus lived recall is +.003 [−.094, .110] <!-- 014 --> under rung 2; no rule reads the placebo's +.129 [.016, .242] <!-- 014 -->.

Because rung 2 is the main map re-judged, its lived recall (.352 <!-- 014 -->) differs slightly from the main run's (.349 <!-- 008; 014 -->); Section 10 uses that gap.

## 9. Nulls and what they rule out

**In plain terms:** for each effect we looked for and did not find, we give the smallest effect the data would reliably detect and the largest effect they rule out, so each null says how much it excludes.

For each null of the earlier frame we give MDE₈₀ = 2.8 × SE, with SE = (hi − lo) / 3.92 from the 95% interval, a normal approximation from the observed interval rather than a power guarantee, and the largest effect ruled out: the upper end of the 90% interval where released (G6), else the 95% upper end.

| Null | Estimate | MDE₈₀ | Largest effect ruled out |
|---|---|---|---|
| Pull toward the model's no-interview script (canonical) | .014 [−.047, .072] <!-- 007 --> | .085 <!-- 007, derived --> | .064 <!-- 007 --> |
| Ideals drawn as received against the AI's own quote | 0 of 90 <!-- 013 --> | n/a | .033 over all ideals; .089 over matched ideals (0/32); .070 lenient (0/41), one-sided 95% <!-- 013 --> |
| Additions found on the other family's no-interview script | +.004 [−.021, .031] <!-- 009 --> | .037 <!-- 009, derived --> | .026 <!-- 009 --> |
| Ideals minus lived steps, recovery | +.006 [−.097, .117] <!-- 008 --> | .153; lenient .164 <!-- 008, derived --> | higher recovery of ideals above .117; lenient .179 (95%) <!-- 008 --> |
| AI maps more alike, size-matched | +.007 [−.000, .015] <!-- 011 --> | .011 <!-- 011, derived --> | .015 (95%) <!-- 011 --> |
| Leaks minus flips, pooled | +.029 [−.016, .085] <!-- 015 --> | .072 <!-- 015, derived --> | .074 <!-- 015 --> |
| Remedy minus placebo, leaks | −.022 [−.073, .031] <!-- 015 --> | .074 <!-- 015 --> | a reduction larger than .065 <!-- 015 --> |

The zero-event bounds are exact binomial limits that count ideal steps as the units; they ignore that steps cluster within journeys.

**Licensed readings:**

- *Pull:* no detectable pull toward the model's no-interview script; a lean larger than .064 <!-- 007 --> is ruled out; partly lexical on the AI side.
- *Absences:* none of what participants marked as missing was drawn as received against the AI's own quote. Always printed with four companions: four <!-- 013 --> ideals were flattened, drawn as received without the participant's judgment that the service was not good enough (.044 [.012, .11] <!-- 013 -->); the bounds above; the matcher-free check, under which the AI drew as received .126 [.069, .19] <!-- 010 --> of what its own quotes call wished (95 <!-- 010 --> boxes), 0 to .126 <!-- 018 --> across labelers; and recovery as wished, 28 of 90 <!-- 013 -->.
- *Ideals and lived steps:* we found no detectable difference between how often ideals and lived steps are recovered (recall .356 against .349 <!-- 008 -->), under rung 1's prompt as well. The contrast is ideal minus lived recall, so its upper end, .117 <!-- 008 -->, bounds higher recovery of ideals; the bound that answers whether ideals are lost faster is its lower end: a loss of ideals larger than .097 <!-- 008 --> lies outside the 95% interval.
- *Homogenization:* prior work finds model-written text more alike (Padmakumar & He 2024; Jiang et al. 2025); here the AI maps are not more alike; raw, slightly less (difference −.010 [−.019, −.002] <!-- 011 -->).
- *Leaks conditional on the no-interview script:* not informative, with four <!-- 015 --> events (power about 9% <!-- 015 --> for a doubled rate).
- *Remedy:* did not detectably reduce what was drawn as received (one draw); a reduction above .065 <!-- 015 --> is ruled out, and the placebo leaked .100 <!-- 015 -->, above the approximate MDE₈₀ of .074 <!-- 015 -->.

**The one slant that shows.** When the AI adds boxes its own quotes do not back, it leans toward what it already knows, the pattern NLP research describes as over-reliance on parametric knowledge when it conflicts with the given text (Longpre et al. 2021; Xu et al. 2024); the overlap fits that pattern but does not show where an addition came from. An unattested box may still be said elsewhere in the interview. Of its unattested additions, .266 [.199, .339] <!-- 009 --> appear in the model's no-interview script, against .109 [.078, .141] <!-- 009 --> of attested ones absent from the endorsed map (+.157 [.103, .221] <!-- 009 -->). Limits: the match is partly lexical, there is no sign against the other family's no-interview script (+.004 <!-- 009 -->), and the map-level pull is null.

## 10. Instrument error

**In plain terms:** we ask the judge again, let other labelers read the same quotes and let a second model draw the maps, then report how far each number moves. This is why we call the numbers re-runnable, not reproducible to the digit.

**The judge, asked again**, on its own logged requests <!-- 019 -->:

| Call | Identical replies | Labels changed |
|---|---|---|
| Modality | 13 of 20 <!-- 019 --> | .114 [.085, .153] (39 of 341) <!-- 019 --> |
| Alignment | 3 of 19 <!-- 019 --> | .033 [.022, .050] (23 of 690), all on the judge channel <!-- 019 --> |
| Canonical names | 13 of 20 <!-- 019 --> | adjusted Rand index .977 over 929 labels <!-- 019 --> |
| Modality, label order reversed | | .052 [.042, .064] of 1,547 <!-- 019 --> |

Flips exceed the declared .02 for modality and alignment, although the requests were logged at temperature 0, which does not guarantee identical outputs (Ouyang et al. 2025; Atil et al. 2025). Every number that reads either is therefore re-runnable, not reproducible to the digit, and is printed with .114 <!-- 019 --> and .033 <!-- 019 --> beside it. These rates say how often labels change, not how far each final number would move in a full re-run. Reversing label order, a check on the position bias documented for LLM judges (Wang et al. 2024; Ye et al. 2025), changes labels without exceeding the modality flip rate by more than .05 <!-- 019 -->.

**Judged twice.** Rung 2 is the main map re-judged. Of 1,887 <!-- 014 --> lived steps, 23 <!-- 014 --> gained and 17 <!-- 014 --> lost a located partner (.021 <!-- 014 --> changed); of 90 <!-- 014 --> ideals, 2 <!-- 014 --> and 2 <!-- 014 -->. No claim reads these counts; they explain why the ladder script's earlier run stopped at its rung-2 equality check (Section 12).

**Across labelers** (main judge, Gemma, keyword) <!-- 018 -->, for each number that reads modality:

| Number (reads modality) | Main judge | Gemma | Keyword | Band | Narrow |
|---|---|---|---|---|---|
| Attested recall | .302 | .284 | .281 | .281–.302 | yes |
| Attested, not on the map | 304 | 265 | 277 | 265–304 | yes |
| Not attested by own quote | 365 | 446 | 435 | 365–446 | no |
| Drawn as received against own quote | 0 | 0 | 0 | 0 | yes |
| Flattened | 4 | 4 | 4 | 4 | yes |
| Quote wished, drawn as received | .126 (n 95) | .028 (n 71) | 0 (n 15) | 0–.126 | no |
| Quote actual, drawn as wished | .007 (n 826) | .011 (n 761) | .057 (n 867) | .007–.057 | no |
| Contaminated share | 0 | .001 | .001 | 0–.001 | no |
| Wrong-transcript difference | .073 | .068 | .073 | .068–.073 | yes |

Several bands are wide (`p5_band_narrow` false <!-- 018 -->), so numbers that read modality appear only as bands and never lead; the absence counts and the pull read no modality. Agreement across families is not independence (Jiang et al. 2025).

**A second reconstructor.** Qwen3.8-27B ran at the primary setting only. It confirmed its own mapping-talk turns, so each contrast is "reconstructor and masks" <!-- 016 -->. The declared rule returns a different pattern <!-- 016 -->: the choice of model changes the map, and the instrument shows it; management's LLM-annotation guidance likewise finds that implementation choices move results and asks for sensitivity analysis (Carlson & Burbano 2026). What differs is how much the map holds.

| 27B minus main reconstructor, paired by map | Difference | Levels |
|---|---|---|
| AI boxes per map | −6.58 [−10.14, −3.72] <!-- 016 --> | 22.9 against 29.5 <!-- 016 --> |
| Attested recall | −.025 [−.053, .003] <!-- 016 --> | .277 against .302 <!-- 016 --> |
| Link recall | −.018 [−.030, −.006] <!-- 016 --> | .029 against .047 <!-- 016 --> |
| Size-adjusted recall | +.049 [−.006, .104] <!-- 016 --> | |

The 27B is not deterministic: 16 of 20 <!-- 016 --> repeated requests returned identical replies. The lead is restricted to the main reconstructor.

# Part C. Checks, records and reuse

## 11. The synthetic bench and the known-distance test

**In plain terms:** we plant known answers in synthetic text and check that the judge and the gauge find them. Both are unit tests of the pipeline and say nothing about real interviews. LLM-generated interviews can match real themes while differing in structure and tone (Amirova et al. 2024).

**The synthetic trap bench (judge).** Before the judge phase, the main judge labeled held-out synthetic corpora with planted traps (wished, denied, hypothetical, someone else's, interviewer-voiced; speech and prose styles), in generative mode and both label orders; the second family labeled the same corpora. The pass rule: actual recall ≥ .85, wished precision ≥ .80, off-record mention recall ≥ .85 over at least 100 items. The table gives each judge's results on these corpora.

| Bench (synthetic, held out) | Main judge (DeepSeek V4 Flash) | Second family (Gemma 4 31B) |
|---|---|---|
| Items | 2,708 (2,434 scored) | same corpora |
| Accuracy | .966 [.96, .97]; reversed label order .962; agreement between orders .991 | .984 |
| Recall, unsupported | .857 [.81, .89] (220 sentences) | .934 (its lowest class) |
| Recall, actual | .982 [.96, 1.00] | |
| Off-record recall | .942 [.88, .97] (103 labels); mention recall .974 | |
| Precision, wished | .987 | |
| Prose minus speech gap | .021 | |
| Pass rule | pass | |

Because they come from synthetic text, these values are not gate-cleared. In each real judge session a preflight and an end-of-session recheck ran planted items; the full session's recheck passed, and the ledger does not record the pilot session's. Planted cases from real material, and the rate at which both judges are wrong on them, need a new locked script and the authors' go; Stage 1 reports none.

**The known-distance test (gauge).** We planted known doses of distance in copies of the synthetic oracle's AI maps (the zero-distance point), with zero model calls. Each dose type is detected, reported "at or below"; the first four at the lowest dose tried, the lean one dose above it:

| Dose | Detected at |
|---|---|
| Drop steps | ≤ 10% |
| Drop links | ≤ 25% |
| Merge steps | ≤ 10% |
| Flip wished to received | ≤ 25% |
| Lean toward the no-interview script | 10%; a planted pull of about .07 is detected, in the best case where planted boxes are copied word for word from the script |

The recall drops for merged steps and the lean are upper bounds. Limits printed with it: no dose plants flattening; 7 ideals, so shares move in sevenths; 12 maps; the judge is replayed from the oracle, so the step, link and flip doses test the arithmetic only. It shows that the gauge detects planted distance on synthetic text, not that the matcher is right on real data; the calibration (Section 4) does that in part.

## 12. Record keeping

**In plain terms:** we did not lock the analysis in advance. Instead, pipeline model calls, cleared results and rule changes enter hash-chained records. A later edit can be detected against a hash saved outside the folder holding the record.

We did not pre-register or lock the analysis; the study is exploratory. Rigor comes from three checkable records, automated checks and controls, an approach that separates transparency from replication (Pratt et al. 2020) and does not judge inductive work by a fixed template (Corley et al. 2021).

**The run log.** The kit writes every pipeline model call to an append-only, hash-chained log (the linking scheme of Haber & Stornetta 1991) before the request is sent and after it returns, closed by a manifest. Its verifier checks edits, deletions, reordering and truncation against a manifest hash saved outside the run folder (`verify --expect-manifest-hash`), and flags requests without an answer. Without that external anchor, someone who rewrites both the log and its manifest can make a consistent replacement. Determinism re-asks enter the audit summary rather than individual request records. Each run records the config and prompt-set hashes and model ids; each pipeline call records its host, never a URL or key <!-- kit README -->. Failed and exploratory runs are logged like any other, in line with rules for reproducible computational research (Sandve et al. 2013). The verifier passed on the main run's judge and metrics logs when they closed. These logs hold participant text and stay on our machines. The release carries no per-record hashes; the heads of the real run's Q, J and M chains are available from the authors.

**The gate chain.** Each cleared file (001 to 023, one per analysis script) enters a hash chain of 23 <!-- profile.json run.gate_chain --> entries, head `2469916c…44e0`, which we verified on 22 Oct 2026 <!-- profile.json run.gate_chain -->. The runner's own log has 24 entries; the extra one is a ladder-script run that stopped at a failed check before writing.

**The declaration log.** Rules and every change to them go to a separate hash-chained log, `declarations.jsonl`: 21 <!-- profile.json run.declaration_log --> records, seq 0 to 20, head `b3736796…6bfe` <!-- profile.json run.declaration_log -->. It is a record, not a lock: each change is a dated amendment, so the log shows what was declared before which result was seen. The package carries the log's head and the summary below, not the log itself or the gate chain's records. We ran the checks on them ourselves. A head lets anyone who later receives the records check them, but the head alone does not show what they hold or their order.

Read in order, the log shows the lead's template declared before the floors ran (seq 3, 5), the lead promoted after it was computed (seq 9), and the size rule fixed before its run (seq 11). The box-for-box comparison was declared before its script ran (seq 16, Section 7). The table below gives seq 0 to 20.

| Seq | Date | Head | What it records |
|---|---|---|---|
| 0 | 17 Oct | `0d5bbc5e` | Message map and rules A0–A13, A4b, before any analysis output was read |
| 1 | 18 Oct | `e9c9dfc0` | An author's words reopening the frame, verbatim, before any analysis result |
| 2 | 18 Oct | `59c0f2db` | Clarifications against the kit's code: P0, denominators, the three classes |
| 3 | 18 Oct | `5530c5a6` | Which sentence each outcome licenses, the profile, the floors (the "listening added" template) and the known-distance test, before any Phase-1 output |
| 4 | 18 Oct | `203fa38a` | Rulings from independent script reviews (G1–G6) |
| 5 | 18 Oct | `d54764a9` | The subset rule (G7), floors and absence bounds, known-distance reporting |
| 6 | 18 Oct | `e095211e` | Side analyses (calibration, labellers, repeats, 27B), scripts hashed, before they ran |
| 7 | 18 Oct | `ffd580aa` | Rung 2 is the main map re-judged; declared after the ladder script stopped unwritten |
| 8 | 18 Oct | `2c1ac2dd` | The null table gets its own script, before it ran |
| 9 | 18 Oct | `b3ad3a42` | The frame changes and the listening floor becomes the lead, after cleared 001–019 were read; no rule or number changed |
| 10 | 18 Oct | `697d9151` | Reading 3 described exactly; documents re-hashed |
| 11 | 18 Oct | `5f87c06b` | No-interview map size, before its script ran; prompt hash corrected to the file as run |
| 12 | 22 Oct | `ea5ccb86` | Reading 3 at temperature 0, one map per role, a repeat for stability, and the box-for-box comparison |
| 13 | 22 Oct | `58ad604d` | How the text reports reading 3 |
| 14 | 22 Oct | `c8c2d30c` | Technical settings of the reading-3 run (kit commit, output folders, judge stage) |
| 15 | 22 Oct | `c8e2bdee` | Hash of the script f2 (reading-3 recalls) before its run |
| 16 | 22 Oct | `a5b16790` | Hash of the script r14 (box-for-box comparison) before its run |
| 17 | 22 Oct | `188d5e1b` | Hash of the script r13 (reading-3 map sizes) before its run |
| 18 | 22 Oct | `8f852def` | Vocabulary words for the new outputs, and tests |
| 19 | 22 Oct | `427ea8fb` | Operations plan for the model and judge window |
| 20 | 22 Oct | `b3736796` | Manifests of the reading-3 output folders |

## 13. The kit

**In plain terms:** the kit is the code another lab runs on its own interviews, on its own machines. Its commands replay the synthetic demo, pool profiles from synthetic labs and plant known distances to test the arithmetic.

**What it is.** The kit (`second-scribe`) is local Python using only the standard library, MIT license <!-- kit README -->; the Stage-1 release is the single commit of the repository that carries this file. A team with interviews and compatible human-made maps can run the full comparison locally, with no new round of hand-coding; the automatic checks run without maps. It calls any OpenAI-compatible endpoint the researcher starts, in three resumable stage groups: Q (reconstructor), J (judge) and M (metrics, no model calls) <!-- kit README -->.

**Guards on a real corpus** <!-- kit README -->:

- a study config accepts only endpoints on the machine or a private network, with no override (the app lets a researcher declare a hosted endpoint, https only, after typing the ethics basis, which is printed on the run record, report and methods paragraph);
- thinking must be off;
- no judge may be Qwen-family or the same model as the reconstructor;
- everything written outside the working folder is counts, rates and intervals, and strings over 40 <!-- kit README --> characters or equal to a label or quote are refused;
- code checks close each stage group;
- a synthetic canary runs in every session;
- a leak scanner checks any file before it leaves the machine.

**Tests.** `make test`: 3,953 passed and 11 checks skipped (3,964 collected). Ten skipped checks require local analysis files or explicitly enable writing a development page; one older check for an absent Explain module is superseded by that module's own tests. The JavaScript suite passes 389 tests.

**`make demo` (done).** Offline, on 12 <!-- kit README --> synthetic food-truck licensing interviews, each with a transcript, an endorsed map, a gold graph and planted traps (wished-for, denied, someone else's, hypothetical, interviewer-only, a loop, a dead end, a link or box never said) <!-- kit README -->. It replays recorded responses by request hash, stops at any request it has no recording for, writes the report, aggregates and trap table, and verifies the log. The study's own two models' responses on this corpus were recorded on 18 Oct 2026 and ship as the demo's fixtures. They answered it under the demo's own configuration (`configs/demo.json`), which differs from the study's in the ways listed in Section 15. The no-interview maps and the other family's no-interview script were recorded on 22 and 23 Oct 2026. It tests the pipeline, never real interviews, and shows how the study's models answer interviews whose answers are known in advance. Section 15 shows how to run it and check the result.

**Pooling (M2): a design plus a two-lab synthetic demo.** The pooling rule is fixed <!-- PROFILE-SCHEMA §5 -->: profiles sit side by side within one reference type (participant-endorsed) and one prompt set, never combined into one estimate or meta-analyzed; a lab with a different reference type or prompt-set hash is listed apart, and a lab with fewer than 10 people cannot export. Each profile carries the kit and prompt-set hashes, model ids, settings, the no-interview prompt's hash, the reference type and map size. The kit's `export-profile` writes a lab's profile under these rules, and `cli pool` sets profiles side by side. `make pool` shows it on two synthetic labs and refuses a third with nine people. It tests the arithmetic only.

**`make phantom` and `make calibrate`** package the known-distance test (Section 11). `make phantom` plants known doses of distance in copies of the public synthetic corpus, with no model calls, and records the smallest dose detected for each dose type. `make calibrate` runs the same test with a lab's bootstrap size, seed and kit commit and writes the result into its profile as detection limits. Like that test, they check arithmetic on planted doses in synthetic text and do not show that matching on real data is right.

## 14. Limits

**In plain terms:** these limits travel with every number. They say what the endorsed map, the no-interview map, the judge and the sample can and cannot support.

1. **Endorsement is not completeness.** Maps were drawn live on a template and endorsed by email; endorsement indicates agreement and a selection, so both readings are measured against the transcript. The 304 <!-- 001; 003; 004 --> are candidate additions, a ceiling only under the conditions in Section 6 and not bounded below, and what both maps leave out stays unseen. Members of the research team conducted the interviews and drew the maps with participants, so each endorsed map is itself one interviewer's reading, since the interviewer is part of the instrument (Pezalla et al. 2012).
2. **The no-interview map starts from the domain.** Its .152 <!-- 021 --> measures what the model brings to this domain and template, not to an empty page. It is one draw per role and smaller (15.465 <!-- 023 --> against 29.488 <!-- 020 --> boxes), so part of the .24 <!-- 021 --> may be size; the instructions also differ, so the lead does not isolate the effect of hearing the interview, and it holds only for this prompt. The box-for-box comparison in Section 7 answers the narrower question of what listening adds when both maps hold equally many boxes.
3. **The judge is a model.** The synthetic bench tests it on synthetic text only; repeats change .114 <!-- 019 --> of modality labels and .033 <!-- 019 --> of alignments; several modality bands are wide. Agreement across families is not independence.
4. **Grain.** R\* <!-- 017 --> bounds same-grain recall only if real AI wording is no harder to match than a paraphrase; whether a coarser box should count is not settled.
5. **Small counts.** 90 <!-- 013 --> in-transcript ideals, 32 <!-- 013 --> matched; zeros carry exact one-sided bounds, and four <!-- 013; 015 --> events support no test.
6. **Links.** No structural claim; the seam test is withheld.
7. **Scope.** One domain, one province, 43 <!-- 002 --> maps, one reconstructor family at two sizes; the choice of model changes the map.
8. **The direction changed after the results.** The lead was promoted after every Phase-1 result was read; no rule or threshold changed (Section 12).
9. **Unreleased checks.** Planted real-material cases, the quote-shuffled control, and the second family's agreement and both-wrong rates are not released by any locked script, so Stage 1 reports none.
10. **Single draws.** The ladder rungs are one draw each, and the 27B is not deterministic (16 of 20 <!-- 016 -->), so intervals on the ladder rows carry no draw-to-draw variance. The no-interview map we analyze for each role also comes from one run, and a second run at temperature 0 gave identical maps for both roles <!-- 021 -->, so for these maps the uncertainty that the intervals leave out comes mainly from the choice of prompt (limit 2).

## 15. Reproducibility under restricted data

**In plain terms:** this study yields results of two types, and each type is verified by a different procedure. Results of the first type concern the instrument, that is, how each measure in our profile is computed, whether the readings, the matching and the profile find errors planted in an interview, and how the study's own two models answer interviews whose answers were written in advance. Anyone can reproduce these results on the public synthetic corpus with the commands below, offline, without any model call and in less than a minute, and can check the result against Tables 15.1 and 15.2. Results of the second type concern autism services in Ontario. They rest on interviews that the participants agreed only the study team would read. Each number about them in our public profile names the gate-cleared file it comes from, and any team can ask our question of its own interviews with the same kit and set its profile beside ours, which is how Stage 2 would test whether the pattern holds in other services. Running the commands below therefore lets a reader check how each measure in our profile is computed. It also lets a reader see, case by case, where the study's models succeed and where they fail on synthetic interviews. The size of these measures in autism services is tested when other teams run the same commands on their own interviews.

**What a reader needs.** A reader needs the supplementary file `second-scribe-stage1.zip` and Python 3.10 or newer. The kit uses only Python's standard library, so nothing has to be installed, and every command below runs offline. A reader without `make` can type the `python3 -m second_scribe.cli` form of each command, which `make` echoes as it runs the command and which the kit's README lists.

**Steps.** In the unzipped folder, a reader runs the following commands in this order. <!-- reproduce-15 -->

1. `make demo` replays the recorded answers that the study's two models gave on the 12 synthetic interviews (Section 13, The kit). It prints the trap table, in which 133 of 144 trap checks pass, and it verifies the run log. The demo's report (`report.md`) also shows a FAIL line for `determinism_judge`, because 75% of the repeated judge answers in the recording are identical. This check re-sends logged judge requests and compares the answers, and the recording holds the judge's own differing answers. The line therefore reports how the judge behaves, as Section 10 (Instrument error) describes for the real data, and it does not stop the run. <!-- reproduce-15 -->
2. `make export-profile CONFIG=configs/demo.json PROFILE=demo-profile.json WITHHOLD=1` writes the synthetic profile, the file `demo-profile.json`, with the same rows as our profile. Of its 147 cells, 136 are computed and 11 are marked as not run. One of these cells needs the second judge family and eight need a second judge run, and both kinds need live models, so they are not in the offline demo. The other two cells, in the slant row, compare the boxes the AI added with the other family's no-interview script. That script matches none of the boxes the AI added, so both shares in the comparison are 0%, and the kit's declared rule reports a comparison of 0% against 0% as not run. Table 15.1 lists its values. <!-- reproduce-15 -->
3. The following command prints a fingerprint, a SHA-256 hash, of the profile's rows.

   ```
   python3 -c "import json,hashlib;r=json.load(open('demo-profile.json'))['rows'];print(hashlib.sha256(json.dumps(r,sort_keys=True,separators=(',',':')).encode()).hexdigest())"
   ```

   If the run matches ours, the fingerprint is the following.

   ```
   58fa98534bf6bd6b9ebc6dc143f5e43668df1f75f7bab74b1207f22ecbbc0204
   ```

   We take the fingerprint of the rows because the file also records the kit's version, which changes with every release and is missing from copies made without the repository's history. <!-- reproduce-15 -->
4. `make demo-oracle` runs the same demo with the oracle client, a "perfect AI" that answers from the answer key and passes all 144 trap checks. Table 15.2 sets it beside the recorded models. <!-- reproduce-15 -->
5. `make phantom` regenerates the detection limits of the known-distance test (Section 11, The synthetic bench and the known-distance test). <!-- reproduce-15 -->

On our computer each command took about ten seconds or less. <!-- reproduce-15 -->

**Table 15.1. The synthetic profile from the public food-truck corpus.** The values come from 12 synthetic interviews whose answers we wrote in advance, so they show how each measure is computed and estimate nothing about food trucks or about any service. The kit's profile file has two rows more than the table in Section 5 (The distance profile), so the last column names the section, and where useful the row of that table, that reports the study's value for the same measure. <!-- reproduce-15 -->

| # | Row | Synthetic value | Study value in |
|---|---|---|---|
| 1 | What did listening add? | AI recall 92.4% [89.0, 95.6]; no-interview recall 22.0% [15.7, 28.3]; listening added 70.3 points [64.5, 75.7]; boxes per map 11.25 (AI) and 8.417 (no-interview) <!-- reproduce-15 --> | Section 7 |
| 2 | Two readings of one transcript | Of 135 AI boxes, 105 attested on the map, 8 attested but not on the map, 22 not attested by their own quote; 9 endorsed boxes not in the transcript <!-- reproduce-15 --> | Section 6 |
| 3 | Coverage | kept 92.4%; coarsened 1.7%; omitted 5.9% [3.2, 8.6]; attested recall 87.3% [83.3, 91.7] <!-- reproduce-15 --> | Section 5, row 1 |
| 4 | Size | 1.063 AI boxes per endorsed box (135 / 127) <!-- reproduce-15 --> | Section 5, row 2 |
| 5 | Support in the transcript | precision 77.8% [72.7, 83.2] <!-- reproduce-15 --> | Section 5, row 3 |
| 6 | Links | direct 73.1% [65.9, 80.0] of eligible links, 7.2 times the null 10.1% <!-- reproduce-15 --> | Section 5, row 4 |
| 7 | Absences | 7 of 7 wishes drawn as wished; 0 received against the AI's own quote <!-- reproduce-15 --> | Section 5, row 5; Section 9 |
| 8 | Slant | pull 0.3 points [−7.2, 6.6] <!-- reproduce-15 --> | Section 5, row 6; Section 9 |
| 9 | Floors | wrong transcript 3.6 points [0.0, 10.3]; own map's mean rank 3.25 of 12 <!-- reproduce-15 --> | Section 8 |
| 10 | Instruction | boxes per map 11.833 / 11.25 / 11.167 / 11.167 (rung 1, rung 2, remedy, placebo) <!-- reproduce-15 --> | Section 8 |
| 11 | Instrument error | lexical floor 6.8% [0.0, 16.0]; re-judging changes 0 of 111 lived steps <!-- reproduce-15 --> | Section 10 |

The demo runs the study's pipeline with the study's two models, and its no-interview map is drawn as in the study. The synthetic profile differs from the study's in six ways.

- (a) The model's no-interview script comes from 6 drafts per role, against 15 in the study.
- (b) The cells that need the judge asked again or the second judge family are not in the offline demo, because they need live models.
- (c) The two cells that compare the maps box for box come from a script outside the kit, so the synthetic profile has 147 of the study profile's 149 cells.
- (d) The corpus is 12 synthetic interviews with planted answers, so most values are high by construction. <!-- reproduce-15 -->
- (e) The wrong-transcript control pairs each map with 10 other transcripts in the demo and with 20 in the study. <!-- configs/demo.json:181; pipeline.py:1790 -->
- (f) The demo draws 200 bootstrap samples and 200 permutations, where the study draws 2,000 bootstrap samples and 1,000 or 10,000 permutations depending on the test, so the demo's intervals and p-values are coarser. <!-- configs/demo.json:174-180; real.template.json; profile.json run.settings -->

**Table 15.2. Planted-trap checks by kind of trap, for the oracle client and for the study's recorded models.** <!-- reproduce-15 -->

| Kind of trap | Checks | Oracle client passes | Recorded models pass |
|---|---|---|---|
| Dead end | 16 | 16 | 16 |
| Hypothetical step | 12 | 12 | 12 |
| Step only the interviewer voiced | 12 | 12 | 6 |
| Loop | 14 | 14 | 13 |
| Step said only while mapping | 14 | 14 | 14 |
| Denied service | 12 | 12 | 12 |
| Box drawn but never mentioned | 12 | 12 | 12 |
| Step that happened to someone else | 12 | 12 | 12 |
| Link drawn but never said | 14 | 14 | 14 |
| Wished-for service | 14 | 14 | 14 |
| Workaround | 12 | 12 | 8 |
| All kinds | 144 | 144 | 133 |

The oracle passes every check, so each check that the recorded models miss reflects a model's answer and does not point to a fault in the code. The recorded models miss 11 checks, six on steps that only the interviewer voiced, four on workarounds and one on a loop. They pass every check on wishes, denied services and steps that happened to someone else, which are the traps closest to the absences row of our profile (Section 5, The distance profile). A match with Tables 15.1 and 15.2 therefore shows that the kit computes each measure as this appendix describes it, under the demo's settings, and it shows how the study's two models answer interviews whose answers are known. A reader who runs the two open-weight models locally can also record new answers with `make demo-live` and repeat the steps, and the numbers will then be close to those in Table 15.1 but may differ, because the judge does not always repeat its answers (Section 10, Instrument error) and because `make demo-live` draws the drafts of the model's and the other family's no-interview scripts again at random, at temperature 0.7. The config's `reading3` block keeps the no-interview map itself at temperature 0, as in the study. <!-- reproduce-15 -->

**How the claims about autism services are tested.** For the study's own results, a reader can do the following.

| A reader can | How |
|---|---|
| See which record each number comes from | Each cell of the public profile (`analysis/public/profile.json`) names the gate-chain entry it comes from; the profile build recomputes every chain link and file hash and stops if one breaks <!-- PROFILE-SCHEMA §10 -->. The files checked by the gate stay with the study team, so we ran this check ourselves and report its result in our summary. |
| Hold us to the published chain heads | The gate-chain head (`2469916c…44e0`) and declaration-log head (`b3736796…6bfe`) <!-- profile.json run -->; the real run's chain heads are available from the authors. A head lets anyone who later sees the records check that they were not edited, but on its own it does not show what they contain. |
| See which rule preceded which result | Our summary of the declaration log, seq 0 to 20 <!-- profile.json run.declaration_log --> (Section 12, Record keeping); the log itself stays with the study team |
| Replay the procedure on synthetic data, under the demo's configuration | `make demo` and the steps above, on the public synthetic corpus, offline, replaying the study models' recorded responses (Section 13, The kit); the kit is in the supplementary file `second-scribe-stage1.zip`. The replay makes no new model calls, regenerates the synthetic profile and lets a reader compare it with Tables 15.1 and 15.2. |
| Ask the question of their own interviews | Run the kit or its app locally and use the `export-profile` command to write a gate-cleared profile in the same format as ours; the kit's `pool` command places such profiles side by side (Section 13, The kit), and a team can read its own profile against the nine rows of ours in Section 5 (The distance profile) |

Results on the real data are re-runnable, but a re-run does not always match every number to the last digit, because judge labels change on repetition (.114 <!-- 019 -->, .033 <!-- 019 -->) and the 27B model does not always give the same reply. The instrument reports how often its labels change and how far its numbers move across settings and labelers. Interview studies rarely disclose enough for others to repeat them (Aguinis & Solarino 2019), and transparency need not mean replication (Pratt et al. 2020). To our knowledge, no open dataset pairs interview transcripts with participant-endorsed maps for each interview (search of public data repositories, October 2026) <!-- council/PUBLIC-DATASETS-round24b.md -->, which is why the public test of the instrument uses synthetic interviews and the test of the findings rests on other teams' own interviews. Wider access to the real data, such as an outside researcher who joins the study team and reruns the analysis on our machines, is a question for the research ethics board that approved the study (protocol number withheld for blind review), and any release of interview text would also need new consent from participants.

## References

Aguinis, H., & Solarino, A. M. (2019). Transparency and replicability in qualitative research: The case of interviews with elite informants. *Strategic Management Journal*, 40(8), 1291–1315. https://doi.org/10.1002/smj.3015

Amirova, A., Fteropoulli, T., Ahmed, N., Cowie, M. R., & Leibo, J. Z. (2024). Framework-based qualitative analysis of free responses of large language models: Algorithmic fidelity. *PLOS ONE*, 19(3), Article e0300024. https://doi.org/10.1371/journal.pone.0300024

Atil, B., Aykent, S., Chittams, A., Fu, L., Passonneau, R. J., Radcliffe, E., Rajagopal, G. R., Sloan, A., Tudrej, T., Ture, F., Wu, Z., Xu, L., & Baldwin, B. (2025). Non-determinism of "deterministic" LLM settings. arXiv:2408.04667.

Basmov, V., Goldberg, Y., & Tsarfaty, R. (2024). LLMs' reading comprehension is affected by parametric knowledge and struggles with hypothetical statements. arXiv:2404.06283 (v2 2025).

Birt, L., Scott, S., Cavers, D., Campbell, C., & Walter, F. (2016). Member checking: A tool to enhance trustworthiness or merely a nod to validation? *Qualitative Health Research*, 26(13), 1802–1811. https://doi.org/10.1177/1049732316654870

Carlson, N. A., & Burbano, V. C. (2026). The use of LLMs to annotate data in management research: Foundational guidelines and warnings. *Strategic Management Journal*, 47(3), 699–725. https://doi.org/10.1002/smj.70023

Corley, K. G., Bansal, P., & Yu, H. (2021). An editorial perspective on judging the quality of inductive research when the methodological straightjacket is loosened. *Strategic Organization*, 19(1), 161–175. https://doi.org/10.1177/1476127020968180

Crilly, N., Blackwell, A. F., & Clarkson, P. J. (2006). Graphic elicitation: Using research diagrams as interview stimuli. *Qualitative Research*, 6(3), 341–366. https://doi.org/10.1177/1468794106065007

Davies, E. L., Bulto, L. N., Walsh, A., Pollock, D., Langton, V. M., Laing, R. E., Graham, A., Arnold-Chamney, M., & Kelly, J. (2023). Reporting and conducting patient journey mapping research in healthcare: A scoping review. *Journal of Advanced Nursing*, 79(1), 83–100. https://doi.org/10.1111/jan.15479

Fox, S., Markham, L., Lail, R., & Karotsieris, M. (2026). LLM judges verify presence, not absence: Omission blindness in AI clinical notes and what recovers it. arXiv. https://arxiv.org/abs/2608.31016

García-Ferrero, I., Altuna, B., Alvez, J., Gonzalez-Dios, I., & Rigau, G. (2023). This is not a dataset: A large negation benchmark to challenge large language models. In *Proceedings of the 2023 Conference on Empirical Methods in Natural Language Processing* (pp. 8596–8615). Association for Computational Linguistics. https://doi.org/10.18653/v1/2023.emnlp-main.531

Haber, S., & Stornetta, W. S. (1991). How to time-stamp a digital document. *Journal of Cryptology*, 3(2), 99–111. https://doi.org/10.1007/BF00196791

Hosseinichimeh, N., Majumdar, A., Williams, R., & Ghaffarzadegan, N. (2024). From text to map: A system dynamics bot for constructing causal loop diagrams. *System Dynamics Review*, 40(3), Article e1782. https://doi.org/10.1002/sdr.1782

Jiang, L., Chai, Y., Li, M., Liu, M., Fok, R., Dziri, N., Tsvetkov, Y., Sap, M., Albalak, A., & Choi, Y. (2025). Artificial Hivemind: The open-ended homogeneity of language models (and beyond). NeurIPS 2025. arXiv:2510.22954.

Li, D., Sun, R., Huang, Y., Zhong, M., Jiang, B., Han, J., Zhang, X., Wang, W., & Liu, H. (2026). Preference leakage: A contamination problem in LLM-as-a-judge. In *International Conference on Learning Representations (ICLR 2026)*. arXiv:2502.01534.

Liang, C., & You, J. (2025). Evaluating LLM-generated diagrams as graphs. In *Proceedings of the 2025 Conference on Empirical Methods in Natural Language Processing* (pp. 12689–12701). Association for Computational Linguistics. https://doi.org/10.18653/v1/2025.emnlp-main.640

Longpre, S., Perisetla, K., Chen, A., Ramesh, N., DuBois, C., & Singh, S. (2021). Entity-based knowledge conflicts in question answering. In *Proceedings of the 2021 Conference on Empirical Methods in Natural Language Processing* (pp. 7052–7063). Association for Computational Linguistics. https://doi.org/10.18653/v1/2021.emnlp-main.565

Maynez, J., Narayan, S., Bohnet, B., & McDonald, R. (2020). On faithfulness and factuality in abstractive summarization. In *Proceedings of the 58th Annual Meeting of the Association for Computational Linguistics* (pp. 1906–1919). Association for Computational Linguistics. https://doi.org/10.18653/v1/2020.acl-main.173

Motulsky, S. L. (2021). Is member checking the gold standard of quality in qualitative research? *Qualitative Psychology*, 8(3), 389–406. https://doi.org/10.1037/qup0000215

Ouyang, S., Zhang, J. M., Harman, M., & Wang, M. (2025). An empirical study of the non-determinism of ChatGPT in code generation. *ACM Transactions on Software Engineering and Methodology*, 34(2), 1–28. https://doi.org/10.1145/3697010

Padmakumar, V., & He, H. (2024). Does writing with language models reduce content diversity? In *International Conference on Learning Representations (ICLR 2024)*. arXiv:2309.05196.

Palmer, A., Smith, N. A., & Spirling, A. (2024). Using proprietary language models in academic research requires explicit justification. *Nature Computational Science*, 4(1), 2–3. https://doi.org/10.1038/s43588-023-00585-1

Panickssery, A., Bowman, S. R., & Feng, S. (2024). LLM evaluators recognize and favor their own generations. In *Advances in Neural Information Processing Systems* (Vol. 37, pp. 68772–68802). https://doi.org/10.52202/079017-2197

Pezalla, A. E., Pettigrew, J., & Miller-Day, M. (2012). Researching the researcher-as-instrument: An exercise in interviewer self-reflexivity. *Qualitative Research*, 12(2), 165–185. https://doi.org/10.1177/1468794111422107

Pratt, M. G., Kaplan, S., & Whittington, R. (2020). The tumult over transparency: Decoupling transparency from replication in establishing trustworthy qualitative research. *Administrative Science Quarterly*, 65(1), 1–19. https://doi.org/10.1177/0001839219887663

Sancheti, A., & Rudinger, R. (2022). What do large language models learn about scripts? In *Proceedings of the 11th Joint Conference on Lexical and Computational Semantics* (pp. 1–11). Association for Computational Linguistics. https://doi.org/10.18653/v1/2022.starsem-1.1

Sandve, G. K., Nekrutenko, A., Taylor, J., & Hovig, E. (2013). Ten simple rules for reproducible computational research. *PLoS Computational Biology*, 9(10), Article e1003285. https://doi.org/10.1371/journal.pcbi.1003285

Verga, P., Hofstatter, S., Althammer, S., Su, Y., Piktus, A., Arkhangorodsky, A., Xu, M., White, N., & Lewis, P. (2024). Replacing judges with juries: Evaluating LLM generations with a panel of diverse models. arXiv:2404.18796.

Wang, P., Li, L., Chen, L., Cai, Z., Zhu, D., Lin, B., Cao, Y., Kong, L., Liu, Q., Liu, T., & Sui, Z. (2024). Large language models are not fair evaluators. In *Proceedings of the 62nd Annual Meeting of the Association for Computational Linguistics (Volume 1: Long Papers)* (pp. 9440–9450). Association for Computational Linguistics. https://doi.org/10.18653/v1/2024.acl-long.511

Wu, K., Wu, E., & Zou, J. (2024). ClashEval: Quantifying the tug-of-war between an LLM's internal prior and external evidence. In *Advances in Neural Information Processing Systems* (Vol. 37, pp. 33402–33422). https://doi.org/10.52202/079017-1053

Xu, R., Qi, Z., Guo, Z., Wang, C., Wang, H., Zhang, Y., & Xu, W. (2024). Knowledge conflicts for LLMs: A survey. In *Proceedings of the 2024 Conference on Empirical Methods in Natural Language Processing* (pp. 8541–8565). Association for Computational Linguistics. https://doi.org/10.18653/v1/2024.emnlp-main.486

Ye, J., Wang, Y., Huang, Y., Chen, D., Zhang, Q., Moniz, N., Gao, T., Geyer, W., Huang, C., Chen, P.-Y., Chawla, N. V., & Zhang, X. (2025). Justice or prejudice? Quantifying biases in LLM-as-a-judge. In *International Conference on Learning Representations (ICLR 2025)*. arXiv:2410.02736.

Zhou, W., Zhang, S., Poon, H., & Chen, M. (2023). Context-faithful prompting for large language models. In *Findings of the Association for Computational Linguistics: EMNLP 2023* (pp. 14544–14556). Association for Computational Linguistics. https://doi.org/10.18653/v1/2023.findings-emnlp.968
