"""Offline regressions for audit-log recovery and secret redaction."""
import json
from pathlib import Path
import tempfile
import unittest

from second_scribe.audit.registry import AuditLog, REDACTED
from second_scribe.audit.verify import verify_internal_chain, verify_run


class AuditReviewTests(unittest.TestCase):
    def test_tuple_nested_secret_is_redacted_before_serializing(self):
        with tempfile.TemporaryDirectory(prefix='ss-tests-audit-review-') as tmp:
            path = Path(tmp) / 'run.jsonl'
            log = AuditLog(path, 'synthetic')
            payload = {'nested': ({'api_key': 'synthetic-secret'},)}
            log.append('config', payload)
            self.assertNotIn('synthetic-secret', path.read_text())
            self.assertEqual(json.loads(path.read_text())['payload']['nested'][0]['api_key'], REDACTED)
            self.assertEqual(payload['nested'][0]['api_key'], 'synthetic-secret')
            self.assertTrue(verify_internal_chain(path)[0])

    def test_resume_valid_jsonl_without_final_newline(self):
        with tempfile.TemporaryDirectory(prefix='ss-tests-audit-review-') as tmp:
            path = Path(tmp) / 'run.jsonl'
            log = AuditLog(path, 'synthetic')
            log.append('config', {'value': 1})
            path.write_bytes(path.read_bytes().rstrip(b'\n'))
            self.assertTrue(verify_internal_chain(path)[0])
            resumed = AuditLog(path, 'synthetic')
            resumed.append('result', {'value': 2})
            resumed.close()
            ok, detail = verify_run(path)
            self.assertTrue(ok, detail)
            self.assertEqual(detail['records'], 2)

    def test_invalid_request_ids_are_unpairable_not_a_crash(self):
        for rid in ([], {}, True, 7, ''):
            with self.subTest(rid=rid), tempfile.TemporaryDirectory(prefix='ss-tests-audit-review-') as tmp:
                path = Path(tmp) / 'run.jsonl'
                log = AuditLog(path, 'synthetic')
                log.append('call_request', {'request_id': rid})
                log.append('call_response', {'request_id': rid})
                log.close()
                ok, detail = verify_run(path)
                self.assertFalse(ok)
                self.assertEqual(detail['orphan_requests'], [0])
                resumed = AuditLog(path, 'synthetic')
                self.assertEqual(resumed.head()[0], 2)
