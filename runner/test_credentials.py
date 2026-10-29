import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import credentials
import supervisor as s

TOKEN_A = 'h.' + base64.urlsafe_b64encode(json.dumps(
    {'https://api.openai.com/auth': {'chatgpt_account_id': 'account-a'}}).encode()).decode().rstrip('=') + '.s'
TOKEN_B = 'h.' + base64.urlsafe_b64encode(json.dumps(
    {'https://api.openai.com/auth': {'chatgpt_account_id': 'account-b'}}).encode()).decode().rstrip('=') + '.s'


def credential(cred_id, active, token=TOKEN_A):
    return {'id': cred_id, 'integrationID': 'openai', 'label': cred_id, 'active': active,
            'value': {'type': 'oauth', 'access': token, 'refresh': 'secret-refresh'}}


class AccountTests(unittest.TestCase):
    def test_secrets_never_surface(self):
        oc = Mock(return_value={'data': [credential('c1', True), credential('c2', False, TOKEN_B),
                                         {'id': 'x', 'integrationID': 'openrouter',
                                          'value': {'type': 'key', 'key': 'sk-secret'}}]})
        listed = credentials.accounts(oc)
        self.assertEqual([c['id'] for c in listed], ['c1', 'c2'])
        blob = json.dumps(listed)
        for secret in ('secret-refresh', 'sk-secret', TOKEN_A):
            self.assertNotIn(secret, blob)
        self.assertNotEqual(listed[0]['account'], listed[1]['account'])

    def test_prefers_active_when_usable(self):
        listed = [{'id': 'c1', 'active': False}, {'id': 'c2', 'active': True}]
        self.assertEqual(credentials.choose({}, listed, 100)['id'], 'c2')

    def test_switches_when_active_exhausted(self):
        listed = [{'id': 'c1', 'active': False}, {'id': 'c2', 'active': True}]
        state = {'credentials': {'c2': {'exhausted_until': 200}}}
        self.assertEqual(credentials.choose(state, listed, 100)['id'], 'c1')

    def test_none_when_all_exhausted(self):
        listed = [{'id': 'c1', 'active': False}, {'id': 'c2', 'active': True}]
        state = {'credentials': {'c1': {'exhausted_until': 200}, 'c2': {'exhausted_until': 300}}}
        self.assertIsNone(credentials.choose(state, listed, 100))

    def test_reset_makes_usable_again(self):
        listed = [{'id': 'c1', 'active': True}]
        state = {'credentials': {'c1': {'exhausted_until': 200}}}
        self.assertIsNone(credentials.choose(state, listed, 150))
        self.assertIsNotNone(credentials.choose(state, listed, 200))

    def test_earliest_reset_and_max_marking(self):
        state = {}
        credentials.mark_exhausted(state, 'c1', 300)
        credentials.mark_exhausted(state, 'c1', 200)   # never shortens
        credentials.mark_exhausted(state, 'c2', 250)
        listed = [{'id': 'c1'}, {'id': 'c2'}]
        self.assertEqual(state['credentials']['c1']['exhausted_until'], 300)
        self.assertEqual(credentials.earliest_reset(state, listed, 100), 250)


def make_oc(creds=None, infos=None, messages=None):
    infos, messages = infos or {}, messages or {}

    def fake(method, path, body=None):
        parts = path.split('/')
        if path == '/api/credential':
            return {'data': creds or []}
        if path.endswith('/message?limit=6&order=desc'):
            return {'data': messages.get(parts[3], [])}
        if path.startswith('/api/session/'):
            return {'data': infos.get(parts[3], {'time': {'idle': 1}})}
        return {}
    return fake


QUOTA_ERROR = {'type': 'provider.quota', 'message': 'The usage limit has been reached',
               'response': {'body': '{"error":{"resets_at":9000}}'}}


class FailoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = Mock()
        self.ctx.state = Path(self.tmp.name)
        self.ctx.cfg = {'credential_failover': True, 'credential_switch_settle': 15}

    def run_check(self, state, creds, messages=None, infos=None):
        oc = Mock(side_effect=make_oc(creds, infos, messages))
        with patch.object(s, 'oc', oc), patch.object(s, 'ui_notify'), \
             patch.object(s, 'notify'), patch.object(s.time, 'time', return_value=1000):
            result = s.quota_check(self.ctx, state)
        return result, oc

    def two_accounts(self):
        return [credential('cred_a', False), credential('cred_b', True, TOKEN_B)]

    def test_switches_to_spare_account_and_resumes_promptly(self):
        state = {'active': {'id': 'ses_work', 'started': 900}}
        messages = {'ses_work': [{'id': 'm1', 'type': 'idle', 'error': QUOTA_ERROR}]}
        ok, oc = self.run_check(state, self.two_accounts(), messages)
        self.assertTrue(ok)
        self.assertEqual(state['credentials']['cred_b']['exhausted_until'], 9015)
        self.assertEqual(state['quota']['switched'], 'cred_a')
        self.assertEqual(state['quota']['until'], 1015)   # switch now, not 9015
        self.assertTrue(any(c.args[:2] == ('post', '/api/credential/cred_a/activate')
                            for c in oc.call_args_list))
        self.assertEqual(len(list((self.ctx.state / 'outbox').glob('quota-switch-*.json'))), 1)

    def test_both_dry_waits_for_earliest_reset(self):
        state = {'active': {'id': 'ses_work', 'started': 900},
                 'credentials': {'cred_a': {'exhausted_until': 5000}}}
        messages = {'ses_work': [{'id': 'm1', 'type': 'idle', 'error': QUOTA_ERROR}]}
        ok, oc = self.run_check(state, self.two_accounts(), messages)
        self.assertTrue(ok)
        self.assertEqual(state['quota']['until'], 5000)   # earlier of 9015 and 5000
        self.assertNotIn('switched', state['quota'])
        self.assertFalse(any('/activate' in c.args[1] for c in oc.call_args_list))

    def test_single_account_never_switches(self):
        state = {'active': {'id': 'ses_work', 'started': 900}}
        messages = {'ses_work': [{'id': 'm1', 'type': 'idle', 'error': QUOTA_ERROR}]}
        ok, oc = self.run_check(state, [credential('cred_b', True, TOKEN_B)], messages)
        self.assertTrue(ok)
        self.assertEqual(state['quota']['until'], 9015)
        self.assertFalse(any('/activate' in c.args[1] for c in oc.call_args_list))

    def test_resume_activates_reset_account_when_needed(self):
        state = {'quota': {'until': 900, 'attempt': 1, 'sessions': ['ses_work'],
                           'markers': ['ses_work:m1'], 'exhausted_credential': 'cred_b'},
                 'credentials': {'cred_b': {'exhausted_until': 5000}}}
        ok, oc = self.run_check(state, self.two_accounts(),
                                infos={'ses_work': {'time': {'idle': 1}, 'outcome': 'failed'}})
        self.assertTrue(ok)
        paths = [c.args[1] for c in oc.call_args_list]
        self.assertIn('/api/credential/cred_a/activate', paths)
        self.assertLess(paths.index('/api/credential/cred_a/activate'),
                        next(i for i, p in enumerate(paths) if p.endswith('/prompt')))

    def test_failover_disabled_keeps_waiting(self):
        self.ctx.cfg['credential_failover'] = False
        state = {'active': {'id': 'ses_work', 'started': 900}}
        messages = {'ses_work': [{'id': 'm1', 'type': 'idle', 'error': QUOTA_ERROR}]}
        ok, oc = self.run_check(state, self.two_accounts(), messages)
        self.assertTrue(ok)
        self.assertEqual(state['quota']['until'], 9015)
        self.assertNotIn('credentials', state)


if __name__ == '__main__':
    unittest.main()


class AuthFailoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = Mock()
        self.ctx.state = Path(self.tmp.name)
        self.ctx.cfg = {'credential_failover': True, 'credential_switch_settle': 15,
                        'credential_broken_seconds': 3600}

    def test_auth_error_is_not_quota(self):
        error = {'type': 'provider.auth', 'status': 401,
                 'response': {'body': '{"error":{"code":"no_matching_rule"}}'}}
        self.assertTrue(credentials.auth_failure(error))
        self.assertFalse(credentials.auth_failure(QUOTA_ERROR))
        self.assertFalse(credentials.auth_failure({'type': 'provider.error', 'message': 'socket closed'}))

    @patch.object(s, 'ui_notify')
    def test_refused_account_rotates_instead_of_pausing(self, ui):
        creds = [credential('cred_a', False), credential('cred_b', True, TOKEN_B)]
        messages = {'ses_work': [{'id': 'm401', 'type': 'idle',
                                  'error': {'type': 'provider.auth', 'status': 401,
                                            'response': {'body': '{"error":{"code":"no_matching_rule"}}'}}}]}
        oc = Mock(side_effect=make_oc(creds, {}, messages))
        state = {'active': {'id': 'ses_work', 'started': 900}}
        with patch.object(s, 'oc', oc), patch.object(s.time, 'time', return_value=1000):
            self.assertTrue(s.quota_check(self.ctx, state))
        self.assertFalse(state.get('paused', False))
        self.assertEqual(state['credentials']['cred_b']['broken_until'], 4600)
        self.assertEqual(state['quota']['switched'], 'cred_a')
        self.assertEqual(state['quota']['until'], 1015)
        self.assertIn('/api/credential/cred_a/activate', [c.args[1] for c in oc.call_args_list])

    @patch.object(s, 'ui_notify')
    def test_all_accounts_refused_pauses_for_human(self, ui):
        creds = [credential('cred_b', True, TOKEN_B)]
        messages = {'ses_work': [{'id': 'm401', 'type': 'idle',
                                  'error': {'type': 'provider.auth', 'status': 401}}]}
        oc = Mock(side_effect=make_oc(creds, {}, messages))
        state = {'active': {'id': 'ses_work', 'started': 900}}
        with patch.object(s, 'oc', oc), patch.object(s.time, 'time', return_value=1000):
            self.assertTrue(s.quota_check(self.ctx, state))
        self.assertTrue(state['paused'])
        self.assertEqual(len(list((self.ctx.state / 'outbox').glob('auth-blocked-*.json'))), 1)
        self.assertFalse(any('/activate' in c.args[1] for c in oc.call_args_list))
