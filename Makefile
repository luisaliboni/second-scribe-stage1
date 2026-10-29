PY ?= python3

.PHONY: test test-fast test-js e2e demo demo-oracle demo-live export-profile pool phantom calibrate app

# the whole suite (about 45 min on an idle Mac); eight optional parity tests need SS_PARITY_DIR (the authors' analysis pack)
# two additional opt-in checks use SS_ANALYSIS_PUBLIC and SS_WRITE_STUDY_DEV_PAGE
test:
	$(PY) -m unittest discover -s tests -t .

# the same discovery minus the slow suites listed in tests/fast.py (pipeline end-to-end, leak-scan, replay, export, ...); about 30 min
test-fast:
	$(PY) -m tests.fast

# the app's JavaScript tests (needs Node.js; not part of `make test`). The glob skips tests/js/harness.js, which is not a test.
test-js:
	node --test tests/js/*.test.js

# the app end to end on the real path, against mock model servers on 127.0.0.1 (slow; `make test` runs it too)
e2e:
	$(PY) -m unittest tests.test_e2e_app -v

# offline: replays demo/fixtures.json (no model calls)
demo:
	$(PY) -m second_scribe.cli demo --out out/demo --client replay --fresh

# offline: the oracle client answers from the synthetic gold (a test of the code path)
demo-oracle:
	$(PY) -m second_scribe.cli demo --out out/demo-oracle --client oracle --fresh

# live: endpoints from SS_RECON_* and SS_JUDGE_* environment variables; records the responses to an
# untracked file. To ship them, review and copy it to demo/fixtures.json by hand.
demo-live:
	$(PY) -m second_scribe.cli demo --out out/demo-live --client http --record out/demo-live-fixtures.json

# offline: a finished run's distance profile (orgsci-profile/v2); refused (exit 2, nothing written) under 10 analysed
# people or on a failed privacy scan. OUTDIR is the run's out_dir when it is not the config's own; WITHHOLD=1 writes
# run.run_chain_heads as null (available from the authors).
#   make export-profile CONFIG=run-config.json PROFILE=profile.json [OUTDIR=out] [WITHHOLD=1]
export-profile:
	@test -n "$(CONFIG)" -a -n "$(PROFILE)" || { echo "usage: make export-profile CONFIG=<run config> PROFILE=<profile.json> [OUTDIR=<run out_dir>] [WITHHOLD=1]"; exit 2; }
	$(PY) -m second_scribe.cli export-profile --config "$(CONFIG)" --profile "$(PROFILE)" $(if $(OUTDIR),--out-dir "$(OUTDIR)") $(if $(WITHHOLD),--withhold-chain-heads)

# offline, oracle client only: three synthetic labs -> a profile per lab -> labA and labB side by side (never combined).
# lab9 has 9 analysed people, so its export is refused (printed, expected, not a failure of the target).
pool:
	rm -rf out/pool
	$(PY) -m second_scribe.synth.labs out/pool
	for lab in labA labB lab9; do \
	  $(PY) -m second_scribe.cli demo --out out/pool/$$lab/out --config out/pool/$$lab/config.json --client oracle > out/pool/$$lab/demo.log 2>&1 || { cat out/pool/$$lab/demo.log >&2; exit 1; }; \
	done
	for lab in labA labB; do \
	  $(PY) -m second_scribe.cli export-profile --config out/pool/$$lab/config.json --out-dir out/pool/$$lab/out --profile out/pool/$$lab.json || exit 1; \
	done
	$(PY) -m second_scribe.cli export-profile --config out/pool/lab9/config.json --out-dir out/pool/lab9/out --profile out/pool/lab9.json 2> out/pool/lab9.err; \
	rc=$$?; cat out/pool/lab9.err >&2; \
	if [ $$rc -ne 2 ] || ! grep -q "k floor" out/pool/lab9.err || [ -e out/pool/lab9.json ]; then \
	  echo "lab9 export exited $$rc, not the expected k-floor refusal" >&2; exit 1; fi; \
	echo "lab9 refused (expected: fewer than 10 people analysed)"
	$(PY) -m second_scribe.cli pool --profiles out/pool/labA.json out/pool/labB.json --out out/pool/pool.json
	$(PY) -c "import json,sys; p=json.load(open('out/pool/pool.json')); sys.exit(0 if len(p['groups'])==1 and not p['separate'] else 'labA and labB did not land in one group')"

# offline: the known-distance check. Plants doses in the synthetic demo (built by the oracle into a temp dir) and writes
# out/phantom/phantom.json and phantom.md. 20 replicates per dose (about 8 s in all; see the Task 8 timing).
phantom:
	$(PY) -m second_scribe.cli phantom --reps 20 --out out/phantom

# offline: the same check with a lab's B, seed and kit commit (read from its profile), on the public synthetic demo; adds a
# detection_limits block to the profile, in place. OUTDIR is the run's out_dir when it is not the config's own.
#   make calibrate CONFIG=run-config.json PROFILE=profile.json [OUTDIR=out]
calibrate:
	@test -n "$(CONFIG)" -a -n "$(PROFILE)" || { echo "usage: make calibrate CONFIG=<run config> PROFILE=<profile.json> [OUTDIR=<run out_dir>]"; exit 2; }
	$(PY) -m second_scribe.cli calibrate --config "$(CONFIG)" --profile "$(PROFILE)" --reps 20 $(if $(OUTDIR),--out-dir "$(OUTDIR)")

# offline site (app T28): Explain · The study · Try it · Install, built into site/ (a build output, not committed).
# Try it shows the recorded run: demo/recorded/ is replayed with the replay client (no model call) into
# build/tryit-run/, exported to build/tryit.js, and build_site --recorded runs replay-check on it; the build stops if
# the check fails. Without demo/recorded/ it ships the labelled test pattern (oracle, no drift), never the dev-only
# static/dev/data/tryit.js. With demo/recorded the Try-it profile is exported from the replayed run the lanes show
# (build_site tryit-profile -> build/tryit-profile.json, the kit's export-profile on build/tryit-run). TRYIT_PROFILE,
# a byte copy of analysis/public/parity-synth/profile-synth.json, is only the fallback of the test-pattern path
# (no demo/recorded). STUDY (optional): the study's public profile, e.g.
#   make app STUDY=analysis/public/profile.json      (without it, The study page shows "not yet run")
RECORDED ?= demo/recorded
TRYIT_PROFILE ?= tests/fixtures/profiles/profile-synth.json
STUDY ?=

app:
	mkdir -p build
	if [ -f $(RECORDED)/demo-live-fixtures.v2.json ]; then \
	  $(PY) -m second_scribe.cli demo --out build/tryit-run --client replay --fresh \
	    --fixtures $(RECORDED)/demo-live-fixtures.v2.json > build/tryit-run.txt && \
	  $(PY) -m second_scribe.app.build_site tryit-js --run build/tryit-run --out build/tryit.js && \
	  $(PY) -m second_scribe.app.build_site tryit-profile --run build/tryit-run --out build/tryit-profile.json && \
	  $(PY) -m second_scribe.app.build_site --out site --tryit build/tryit.js --tryit-profile build/tryit-profile.json \
	    --recorded $(RECORDED) $(if $(STUDY),--study $(STUDY)); \
	else \
	  $(PY) -m second_scribe.app.testpattern --out build/tryit.js && \
	  $(PY) -m second_scribe.app.build_site --out site --tryit build/tryit.js --tryit-profile $(TRYIT_PROFILE) \
	    $(if $(STUDY),--study $(STUDY)); \
	fi
