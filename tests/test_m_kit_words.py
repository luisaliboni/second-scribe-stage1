"""The M stage of a real corpus whose map labels include a word the kit itself writes (a node labelled "Family",
"Other", "Role"...). The aggregate-only guard compared every key and value with every label, so the kit's own schema
key ``family`` (``script_sources.<name>.family``, ``headline...rows.<name>.family``) or enum value ``other`` tripped it
and the run could not write its aggregates. The kit's fixed words are now exempt from the label check by exact match
(keys and values separately); everything else, a label in any other spelling included, is still refused.
Synthetic corpus and the offline oracle only."""
import contextlib
import io
import json
import os
import re
import shutil
from pathlib import Path
from unittest import mock

from second_scribe import pipeline
from second_scribe.cli import main as cli_main
from second_scribe.kit_vocab import KIT_KEYS, KIT_VALUES
from second_scribe.report import KIT_NOTES, assert_aggregate_only
from tests import test_fork as tf
from tests.test_real_run_validity import Base

J_COMMIT, M_COMMIT = "c" * 40, "e" * 40
SRC = {"name": "judgescript", "role": "judge", "variants": ["p0"], "samples": 2, "temperature": 0.7}

# map labels that equal (case-insensitive) a word of the kit's aggregates: plausible on a care-pathway map
KIT_WORDS_AS_LABELS = {"Family", "Other", "Role", "Model", "Reason", "Placebo", "Judge", "Own", "Unknown",
                       "Reference", "Source", "Structure", "Status", "Missing", "Remedy", "Coverage", "Lower"}

_SPEC = re.compile(r"^[a-z_]+__p\d+__t[\d.]+__r\d+$")
_WORDLIKE = re.compile(r"^[A-Za-z][A-Za-z0-9_:.\- ]*$")


def labels_with_kit_words(real, words):
    def wrapped(r, p_list):
        return real(r, p_list) | set(words)
    return wrapped


class KitWordBase(Base):
    def setUp(self):
        super().setUp()
        d = json.loads(self.cfg.read_text())
        d["script_sources"] = [dict(SRC)]
        self.cfg.write_text(json.dumps(d))
        self.agg_path = self.out / "run" / "aggregates" / "aggregates.json"

    def run_m_with_kit_word_labels(self, words=KIT_WORDS_AS_LABELS):
        with mock.patch.object(pipeline, "_forbidden", labels_with_kit_words(pipeline._forbidden, words)):
            try:
                return self.run_all("M")
            except ValueError as exc:            # the guard may surface as an exception rather than rc 1
                return 1, str(exc)


class TestKitWordLabels(KitWordBase):
    def test_m_writes_the_aggregates_when_labels_equal_kit_words(self):
        self.assertEqual(self.run_all("Q")[0], 0)
        self.assertEqual(self.run_all("J")[0], 0)
        rc, err = self.run_m_with_kit_word_labels()
        self.assertEqual(rc, 0, err)
        agg = json.loads(self.agg_path.read_text())
        self.assertIn("family", agg["script_sources"]["judgescript"])     # the key that tripped the pilot
        self.assertEqual(self.cli("verify", str(self.log))[0], 0)

    def test_the_failure_reproduces_without_the_kit_vocabulary(self):
        self.assertEqual(self.run_all("Q")[0], 0)
        self.assertEqual(self.run_all("J")[0], 0)
        with mock.patch("second_scribe.report.KIT_KEYS", frozenset()), \
                mock.patch("second_scribe.report.KIT_VALUES", frozenset()):
            rc, err = self.run_m_with_kit_word_labels({"Family"})      # one label, the pilot's exact failure
        self.assertEqual(rc, 1)
        self.assertIn("a label or quote at $.script_sources.judgescript.<key>", err)
        self.assertFalse(self.agg_path.exists())

    def test_the_headline_rows_and_the_family_enum_would_trip_next(self):
        """Exempting only ``script_sources`` would not have been enough: the same key sits under the headline's
        script-conditional rows, and the ``family`` VALUE ``other``/``own`` is a kit enum too."""
        self.assertEqual(self.run_all("all")[0], 0)
        agg = json.loads(self.agg_path.read_text())
        no_sources = {k: v for k, v in agg.items() if k != "script_sources"}
        with self.assertRaisesRegex(ValueError, r"\$\.headline\..*rows"):
            assert_aggregate_only(no_sources, {"Family"}, allowed=KIT_NOTES)
        with self.assertRaisesRegex(ValueError, r"family$"):
            assert_aggregate_only(agg["script_sources"], {"Own", "Other"}, keys=KIT_KEYS)
        assert_aggregate_only(agg, KIT_WORDS_AS_LABELS, allowed=KIT_NOTES, keys=KIT_KEYS, values=KIT_VALUES)


class TestGuardStillRefuses(KitWordBase):
    def test_a_label_as_a_value_under_a_kit_key_is_refused(self):
        with self.assertRaisesRegex(ValueError, r"label or quote at \$\.family$"):
            assert_aggregate_only({"family": "Family"}, {"Family"}, keys=KIT_KEYS, values=KIT_VALUES)

    def test_the_exemption_is_exact_and_case_sensitive(self):
        for key in ("Family", "FAMILY", " family", "family "):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "label or quote"):
                assert_aggregate_only({key: 1}, {"Family"}, keys=KIT_KEYS, values=KIT_VALUES)
        with self.assertRaisesRegex(ValueError, "label or quote"):
            assert_aggregate_only({"x": "Other"}, {"Other"}, keys=KIT_KEYS, values=KIT_VALUES)

    def test_a_kit_key_is_not_exempt_as_a_value_nor_a_value_as_a_key(self):
        self.assertIn("role", KIT_KEYS)
        self.assertNotIn("role", KIT_VALUES)
        with self.assertRaisesRegex(ValueError, "label or quote"):
            assert_aggregate_only({"x": "role"}, {"Role"}, keys=KIT_KEYS, values=KIT_VALUES)
        self.assertIn("other", KIT_VALUES)
        self.assertNotIn("other", KIT_KEYS)
        with self.assertRaisesRegex(ValueError, "label or quote"):
            assert_aggregate_only({"other": 1}, {"Other"}, keys=KIT_KEYS, values=KIT_VALUES)

    def test_the_length_rule_still_applies_to_kit_words(self):
        long_key = "x" * 41
        with mock.patch("second_scribe.report.KIT_KEYS", frozenset({long_key})):
            from second_scribe import report
            with self.assertRaisesRegex(ValueError, "longer than"):
                report.assert_aggregate_only({long_key: 1}, set(), keys=report.KIT_KEYS)

    def test_a_real_label_in_the_aggregates_still_stops_m(self):
        self.assertEqual(self.run_all("Q")[0], 0)
        self.assertEqual(self.run_all("J")[0], 0)
        real = pipeline._forbidden
        planted = {}

        def plant(r, p_list):
            out = real(r, p_list)
            planted["label"] = sorted(s for s in out if " " in s)[0]
            return out
        orig = pipeline.compute_aggregates

        def leaky(r):
            agg, p_list = orig(r)
            plant(r, p_list)
            agg["script_sources"]["judgescript"]["model"] = planted["label"]
            return agg, p_list
        with mock.patch.object(pipeline, "compute_aggregates", leaky):
            try:
                rc, err = self.run_all("M")
            except ValueError as exc:
                rc, err = 1, str(exc)
        self.assertEqual(rc, 1)
        self.assertIn("$.script_sources.judgescript.model", err)
        self.assertFalse(self.agg_path.exists())


class TestVocabularyIsTheKitsOwn(KitWordBase):
    def test_every_word_of_a_full_synthetic_aggregate_is_listed(self):
        """Every word-like key and value of a real-kind demo aggregate (script source and ladder on) is in the kit's
        vocabulary, except the names that come from the config or the environment (run id, script-source name, model
        ids, spec keys, match settings). A new metric with a new key fails here until its words are listed. Two model
        pairs: families unknown to the table (``own``, with ``family_reason``) and a different family (``other``)."""
        for recon, judge in (("alpha-recon-1", "beta-judge-1"), ("alpha-qwen-1", "beta-deepseek-1")):
            with self.subTest(recon=recon, judge=judge), \
                    mock.patch.dict("os.environ", {"SS_RECON_MODEL": recon, "SS_JUDGE_MODEL": judge}):
                self.check_vocabulary({"demo", "judgescript", recon, judge})

    def check_vocabulary(self, from_config):
        out = self.out / "run"
        if out.exists():
            shutil.rmtree(out)
        self.assertEqual(self.run_all("all")[0], 0)
        agg = json.loads(self.agg_path.read_text())
        keys, values = set(), set()

        def walk(x):
            if isinstance(x, dict):
                for k, v in x.items():
                    keys.add(k)
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
            elif isinstance(x, str):
                values.add(x)
        walk(agg)

        def wordlike(s):
            return (_WORDLIKE.match(s) and not _SPEC.match(s) and s not in from_config
                    and not re.match(r"^lex[\d.]+_", s) and len(s) <= 40)
        self.assertEqual(sorted(k for k in keys if wordlike(k) and k not in KIT_KEYS), [])
        self.assertEqual(sorted(v for v in values if wordlike(v) and v not in KIT_VALUES), [])

    def test_no_config_or_participant_name_is_in_the_vocabulary(self):
        d = json.loads(self.cfg.read_text())
        c = d["corpus"]
        names = set(c.get("pairs") or {}) | set(c.get("roles") or {}) | set((c.get("interviewers") or {}).values())
        for table in (c.get("participant_labels"), c.get("interviewer_labels")):
            for labels in (table or {}).values():
                names |= set(labels)
        names |= {d["run_id"], SRC["name"]}
        self.assertEqual(sorted(n for n in names if n in KIT_KEYS | KIT_VALUES), [])
        for s in KIT_KEYS | KIT_VALUES:
            self.assertLessEqual(len(s), 40, s)
            self.assertIsNone(_SPEC.match(s), s)


class TestMFromAnotherCommit(Base):
    def test_an_m_session_from_another_commit_passes_verify(self):
        """No commit-equality rule outside ``widen``: the M session may run from a later commit than J."""
        state = {"code_commit": "a" * 40, "code_clean": True}
        with mock.patch("second_scribe.prereg.code_state", return_value=dict(state)):
            self.assertEqual(self.run_all("Q")[0], 0)
            self.assertEqual(self.run_all("J")[0], 0)
        with mock.patch("second_scribe.prereg.code_state", return_value={**state, "code_commit": "b" * 40}):
            rc, err = self.run_all("M")
        self.assertEqual(rc, 0, err)
        starts = [json.loads(l)["payload"] for l in self.log.read_text().splitlines()
                  if json.loads(l)["record_type"] == "run_start"]
        self.assertEqual([s["code_commit"][0] for s in starts], ["a", "a", "b"])
        self.assertTrue((self.out / "run" / "aggregates" / "aggregates.json").exists())
        self.assertEqual(self.cli("verify", str(self.log))[0], 0)


class TestPilotMOnACopy(tf.ForkBase):
    """``widen`` refuses a pilot whose log holds a session from another commit, so the pilot's M from the jpilot-m
    commit must run on a COPY of the pilot out_dir: the copy verifies, and the untouched pilot still widens."""

    def setUp(self):
        super().setUp()
        os.environ.update(SS_JUDGE_URL="http://127.0.0.1:9/v1",
                          SS_JUDGE_PARAMS=json.dumps({"chat_template_kwargs": {"enable_thinking": False}}))
        self.addCleanup(os.environ.pop, "SS_JUDGE_URL", None)
        self.addCleanup(os.environ.pop, "SS_JUDGE_PARAMS", None)

    def at(self, commit):
        return mock.patch("second_scribe.prereg.code_state", return_value={"code_commit": commit, "code_clean": True})

    def judged_pilot_without_m(self):
        with self.at(J_COMMIT):
            self.q_phase()
            rc, _o, err, pilot = self.fork("--pairs", ",".join(tf.PILOT))
            self.assertEqual(rc, 0, err)
            os.environ.pop("SS_RECON_MODEL")
            rc, err = self.run_cfg(pilot / "config.json", "J")
            self.assertEqual(rc, 0, err)
        return pilot

    def widen(self, pilot, out):
        so, se = io.StringIO(), io.StringIO()
        with self.at(J_COMMIT), contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
            rc = cli_main(["widen", "--pilot-config", str(pilot / "config.json"), "--config", str(self.cfg),
                           "--out", str(out)])
        return rc, so.getvalue() + se.getvalue()

    def test_m_in_place_from_another_commit_blocks_widen(self):
        pilot = self.judged_pilot_without_m()
        with self.at(M_COMMIT):
            rc, err = self.run_cfg(pilot / "config.json", "M")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.verify(pilot / "out" / "logs" / "run.jsonl"), 0)     # verify itself accepts it
        rc, out = self.widen(pilot, self.out / "full")
        self.assertEqual(rc, 2, out)
        self.assertIn("a pilot session ran on another code commit", out)

    def test_m_on_a_copy_verifies_and_the_pilot_still_widens(self):
        pilot = self.judged_pilot_without_m()
        before = tf.tree_digest(pilot / "out")
        copy = self.out / "pilot-m"
        shutil.copytree(pilot / "out", copy / "out")
        d = json.loads((pilot / "config.json").read_text())
        d["out_dir"] = str(copy / "out")
        (copy / "config.json").write_text(json.dumps(d))
        with self.at(M_COMMIT):
            rc, err = self.run_cfg(copy / "config.json", "M")
        self.assertEqual(rc, 0, err)
        self.assertTrue((copy / "out" / "aggregates" / "aggregates.json").exists())
        self.assertEqual(self.verify(copy / "out" / "logs" / "run.jsonl"), 0)
        self.assertEqual(tf.tree_digest(pilot / "out"), before)
        rc, out = self.widen(pilot, self.out / "full")
        self.assertEqual(rc, 0, out)
        self.assertIn("verify of the widened run: PASS", out)
