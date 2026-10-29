# Maintenance changes

This archive is a later state of the same kit that produced the study. The study ran at kit
commits 431da7d (reconstruction), b0827ab (judging) and bcf1034 (metrics). Later fixes govern
future runs; they do not recompute the historical study, and the public profile was not
recomputed.

## Fixes

- Project isolation: switching or reopening a project in the local app no longer carries state,
  handles or saved keys across projects, and a run that has just ended no longer counts as busy.
- Task coordination: jobs and model checks in the local app are coordinated per project.
- Secret redaction in request logs.
- JSONL resume: appending to an existing log resumes correctly.
- Malformed records: verification handles them instead of failing.
- A failed comparison export keeps the prior file.
- Invalid response indices and invalid spreadsheet shared-string indices are rejected.
- Determinism auditor: repeats a recorded request with its recorded parameters, and refuses a
  repeat when the original effective parameters cannot be recovered safely.
- Explain page HTML: the nested main landmark was removed.
- Pool lookup: a repeated profile lookup was moved outside a loop.
- Busy check: refreshed (polled) before a project switch.

## Unchanged

The fixes above do not touch the study outputs, prompts, configurations, demo recordings, test
fixtures or the public profile (`analysis/public/profile.json`). The prompt files are those of the judging and
metrics runs: this command prints the `j_m_phase_sha256` value stored in
`analysis/public/profile.json`:

    python3 -c "from second_scribe.prompt_manifest import set_hash, file_hashes; print(set_hash(file_hashes('prompts')))"

New regression tests use synthetic inputs and mock clients. Run the tests with `make test`;
JavaScript checks run with `node --test tests/js/*.test.js`.
