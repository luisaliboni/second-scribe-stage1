"""Route every temp file of the suite (and of its subprocesses) into one per-run dir, removed at exit.

Test temp dirs that leak (or a killed run) used to pile up in the shared $TMPDIR (F7: ~24,000 dirs, ~10 GB).
"""
import atexit
import os
import shutil
import tempfile

_RUN_TMP = tempfile.mkdtemp(prefix="ss-tests-")
tempfile.tempdir = _RUN_TMP
for _var in ("TMPDIR", "TMP", "TEMP"):  # subprocesses (CLI calls, git) inherit these
    os.environ[_var] = _RUN_TMP
atexit.register(shutil.rmtree, _RUN_TMP, ignore_errors=True)
