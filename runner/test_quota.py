import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from quota import quota_error, reset_time, suspension
import supervisor as s


class QuotaParsingTests(unittest.TestCase):
    def test_codex_usage_limit(self):
        self.assertTrue(quota_error({'type': 'provider.rate-limit',
                                     'message': "You've hit your usage limit."}))

    def test_body_code(self):
        self.assertTrue(quota_error({'type': 'provider.error', 'message': '429',
            'response': {'body': json.dumps({'error': {'code': 'usage_limit_reached'}})}}))

    def test_generic_429_not_quota(self):
        self.assertFalse(quota_error({'type': 'rate_limit_exceeded', 'status': 429,
                                      'message': 'Too many requests per minute'}))

    def test_product_budget_gate_not_provider_quota(self):
        self.assertFalse(quota_error({'type': 'tool.error', 'message': 'Tenant LLM budget exceeded'}))

    def test_epoch_nested_reset(self):
        error = {'response': {'body': json.dumps({'error': {'resets_at': 2000}})}}
        self.assertEqual(reset_time(error, 1000), 2000)
        self.assertEqual(suspension(error, 1000, 1)['until'], 2015)

    def test_millisecond_reset(self):
        self.assertEqual(reset_time({'reset_at': 1_900_000_000_000}, 1000), 1_900_000_000)

    def test_retry_after(self):
        self.assertEqual(reset_time({'retryAfterMs': 30000}, 1000), 1030)

    def test_iso_timestamp(self):
        self.assertEqual(reset_time({'reset_at': '2030-03-17T17:46:40Z'}, 1000), 1900000000)

    def test_backoff_bounded(self):
        self.assertEqual(suspension({}, 1000, 1)['until'], 1300)
        self.assertEqual(suspension({}, 1000, 2)['until'], 1600)
        self.assertEqual(suspension({}, 1000, 100)['until'], 4600)

    def test_past_reset_uses_backoff(self):
        self.assertFalse(suspension({'reset_at': 900}, 1000, 1)['explicit_reset'])


class QuotaLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = Mock()
        self.ctx.state = Path(self.tmp.name)
        self.ctx.cfg = {}

    @patch.object(s, 'ui_notify')
    @patch.object(s, 'oc')
    @patch.object(s.time, 'time', return_value=1000)
    def test_suspends_and_notifies_once(self, clock, oc, ui):
        oc.side_effect = [{'data': [{'id': 'msg_failed', 'type': 'idle',
            'error': {'type': 'usage_limit_reached', 'message': 'quota', 'retryAfterMs': 60000}}]},
            {'data': {'time': {'idle': 1000}}}]
        state = {'active': {'id': 'ses_quota', 'started': 900}}
        self.assertTrue(s.quota_check(self.ctx, state))
        self.assertEqual(state['quota']['until'], 1075)
        self.assertFalse(state.get('paused', False))
        self.assertEqual(len(list((self.ctx.state/'outbox').glob('*.json'))), 1)
        count = oc.call_count
        self.assertTrue(s.quota_check(self.ctx, state))
        self.assertEqual(oc.call_count, count)
        ui.assert_called_once()

    @patch.object(s, 'oc')
    @patch.object(s.time, 'time', return_value=1100)
    def test_reset_resumes_same_session_not_new_worker(self, clock, oc):
        state = {'quota': {'until': 1075, 'sessions': ['ses_worker'], 'markers': ['ses_worker:msg']}}
        oc.side_effect = [{'data': {'time': {'idle': 1000}, 'outcome': 'failed'}}, {}]
        self.assertTrue(s.quota_check(self.ctx, state))
        self.assertNotIn('quota', state)
        self.assertEqual(oc.call_args.args[:2], ('post', '/api/session/ses_worker/prompt'))
        self.assertIn('ses_worker:msg', state['quota_seen'])

    @patch.object(s, 'oc')
    @patch.object(s.time, 'time', return_value=1100)
    def test_ambiguous_resume_pauses_instead_of_double_prompt(self, clock, oc):
        state = {'quota': {'until': 1075, 'resuming': True, 'sessions': ['ses_worker']}}
        self.assertTrue(s.quota_check(self.ctx, state))
        self.assertTrue(state['paused'])
        oc.assert_not_called()

    @patch.object(s, 'oc')
    def test_unrelated_pause_is_not_cleared(self, oc):
        state = {'paused': True}
        self.assertFalse(s.quota_check(self.ctx, state))
        self.assertTrue(state['paused'])
        oc.assert_not_called()

    @patch.object(s, 'oc')
    def test_old_error_before_new_user_input_ignored(self, oc):
        state = {'active': {'id': 'ses_work'}}
        oc.return_value = {'data': [{'type': 'user', 'id': 'new'},
            {'id': 'old', 'type': 'idle', 'error': {'type': 'usage_limit_reached', 'message': 'quota'}}]}
        self.assertFalse(s.quota_check(self.ctx, state))

    @patch.object(s, 'oc')
    def test_worker_failure_is_detected_without_coordinator(self, oc):
        (self.ctx.state/'workers.json').write_text(json.dumps({'run': {'session': 'ses_worker'}}))
        oc.return_value = {'data': []}
        self.assertFalse(s.quota_check(self.ctx, {}))
        self.assertIn('ses_worker/message', oc.call_args.args[1])
