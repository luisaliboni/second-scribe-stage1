# Profile fixtures (app T19b)

Copies of public files, except `profile-v2.json` and `profile-v3.json` (see the 22 Oct note below). They hold aggregates only (schema `orgsci-profile/v2`). Do not edit them: tests mutate in-memory copies.

| Fixture | Copied from (repo root `orgsci-ai-frontiers/`) | sha256 | State |
|---|---|---|---|
| `profile-v2.json` | `analysis/public/profile-v2.json`, reading-3 cells at temperature 0 | `1f755871604b5c734039312c8effa219fa14a758a99a7fb27e92e879f511457b` | reported |
| `profile-v3.json` | `analysis/public/profile-v3.json`, reading-3 cells at temperature 0 | `618341e4973da4014a9b20bd1fe7cbc5a69221709eb31408a1b05592060f54dd` | reported |
| `profile-synth.json` | `analysis/public/parity-synth/profile-synth.json` | `5bc4716a994d732dd8858d9f2d1f379f22ea1b87d8700b21b80789714fa1feb1` | synthetic |
| `e2-m1-sample-demo.json` | `E2-M1-sample-profile-demo.json` (the kit exporter's output on the demo, E2 milestone M1 `6b8c398`; 147 cells, 14 not_run) | `33e41001d5e6dd871fc4fa142d4575372c0f60226b8d41d892689ba28bb3e661` | synthetic |

Copied 18 Oct 2026. The coordinator's brief and PROFILE-SCHEMA.md section 12 quote the parity file as `b36504e9…`; the file on disk (modified 18 Oct 2026 18:05) hashes to `47114af5…`, so that is the recorded value. If the analysis session republishes the parity file, re-copy it and update this table; `tests/test_profile_view.py` pins both hashes.

`e2-m1-sample-demo.json` was copied 18 Oct 2026 (app E2-wire). It carries the kit exporter's run block (PROFILE-SCHEMA.md section 14: `run_chain_heads` = `{head, records, manifest_hash, source}`, `kit.run_commits` a list, `pooling.within` = `[reference_type, prompt_set_pair, state]`) and codes, never sentences (section 13). `tests/test_profile_view.py` pins its hash; `tests/js/profile.test.js` renders it.

`profile-v3.json` was copied 19 Oct 2026 (app V3-SWITCH). It has the same 147 cells and values as `profile-v2.json` and keeps the schema string `orgsci-profile/v2`; its run block is in the public form (PROFILE-SCHEMA section 4 and 14): `run_chain_heads` null with `run_chain_heads_source` `available_from_authors`, `kit.run_commits` a list, `kit.release_commit` null and no `release_branch`, the reading-3 prompt by sha256 only, a model field the public files do not record null with that model's source `not_recorded_public`, and cell sources worded "gate chain entry NNN". The study page, the site build and their tests use it. `profile-v2.json` stays for the tests that pin the older per-phase run block (`run_chain_heads` and `kit.run_commits` as `{q, j, m}`, `release_branch`, `working_tree_sha256`), which the study page still draws.

Changed 22 Oct 2026 (council 23 fix 4). `profile-v2.json` and `profile-v3.json` no longer carry the temperature-0.7 reading-3 point. Each is the earlier file with the 20 reading-3 cells of the lead row and row 7 (the no-interview recalls, the three listening lines, the five no-interview sizes, three derived lines) and `reference.map_size.reading3_map_size` replaced by the values of the same cells in the study's public profile (`analysis/public/profile.json`, sha256 `40b1976a62f6f40c218701bb2ac629de4a0176cbc7087e7a5f3c158c3bfb9c25`, reading 3 at temperature 0), with `run.settings.reading3_temperature` set to 0.0 and the row `source` and `band` fields recomputed. Every AI-side cell and every other row is unchanged. The two rarefied cells of the public profile are not added. The sha256 values in the table are the new ones; the earlier files were `e5ef8840...` (v2) and `20ec3796...` (v3).
