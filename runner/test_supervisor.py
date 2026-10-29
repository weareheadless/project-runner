import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import merge
import supervisor as s
from tick import claim_state


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.review = dict(repository='o/r', pr=1, head_sha='abc', accepted=True,
                           tests=['pytest passes'], acceptance_evidence=['requirement test'],
                           reviewer_session='ses_review')
        self.pr = dict(head={'sha': 'abc'}, state='open', draft=False,
                       mergeable=True, mergeable_state='clean')
        self.checks = [dict(status='completed', conclusion='success')]
        self.status = dict(statuses=[], state='pending')

    def test_green(self):
        merge.validate(self.review, self.pr, self.checks, self.status)

    def test_stale(self):
        self.pr['head']['sha'] = 'changed'
        with self.assertRaises(ValueError):
            merge.validate(self.review, self.pr, self.checks, self.status)

    def test_empty_ci(self):
        with self.assertRaises(ValueError):
            merge.validate(self.review, self.pr, [], self.status)

    def test_failed_skipped_pending(self):
        for status, conclusion in [('in_progress', None), ('completed', 'failure'), ('completed', 'skipped')]:
            with self.subTest(conclusion=conclusion), self.assertRaises(ValueError):
                merge.validate(self.review, self.pr, [dict(status=status, conclusion=conclusion)], self.status)

    def test_no_acceptance(self):
        self.review['acceptance_evidence'] = []
        with self.assertRaises(ValueError):
            merge.validate(self.review, self.pr, self.checks, self.status)

    def test_conflict_or_unknown(self):
        for value in (False, None):
            self.pr['mergeable'] = value
            with self.assertRaises(ValueError):
                merge.validate(self.review, self.pr, self.checks, self.status)


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        quota_patch = patch.object(s, 'quota_check', return_value=False)
        quota_patch.start()
        self.addCleanup(quota_patch.stop)
        accounts_patch = patch.object(s.credentials, 'accounts', return_value=[])
        accounts_patch.start()
        self.addCleanup(accounts_patch.stop)
        self.ctx = Mock()
        self.ctx.state = Path(self.tmp.name)
        self.ctx.cfg = dict(name='test', orchestrator_model={'id': 'model', 'providerID': 'provider'},
                            project={'owner': 'owner'}, max_tick_seconds=10)
        self.snap = {'cards': [], 'pulls': [], 'runs': [], 'releases': []}

    @patch.object(s, 'process_outbox')
    @patch.object(s, 'snapshot')
    @patch.object(s, 'oc')
    def test_unchanged_does_not_wake(self, oc, snapshot, outbox):
        snapshot.return_value = self.snap
        s.step(self.ctx, {'paused': True})
        # A matching fingerprint includes management-policy identity.
        import json as json_module
        written = json_module.loads((self.ctx.state / 'snapshot.json').read_text())
        paths = [s.ROOT / 'rules' / 'TICK.md', s.ROOT / 'projects' / 'test.json']
        policy = {str(p.relative_to(s.ROOT)): s.digest(p.read_text()) for p in paths if p.exists()}
        s.step(self.ctx, {'fingerprint': s.digest({'project': written, 'policy': policy})})
        oc.assert_not_called()

    @patch.object(s, 'process_outbox')
    @patch.object(s, 'snapshot')
    @patch.object(s, 'oc')
    def test_changed_creates_one_session(self, oc, snapshot, outbox):
        snapshot.return_value = self.snap
        oc.side_effect = [{}, {'data': {'id': 'ses_new'}}, {}]
        state = s.step(self.ctx, {})
        self.assertTrue(state['active']['admitted'])
        self.assertEqual(oc.call_count, 3)
        # Admission was durably saved before sending the prompt.
        saved = json.loads((self.ctx.state / 'supervisor.json').read_text())
        self.assertFalse(saved['active']['admitted'])

    @patch.object(s, 'process_outbox')
    @patch.object(s, 'snapshot')
    @patch.object(s, 'oc')
    def test_running_does_not_duplicate(self, oc, snapshot, outbox):
        import time
        snapshot.return_value = self.snap
        oc.return_value = {'data': {'time': {}}}
        state = {'active': {'id': 'ses_existing', 'started': time.time(), 'admitted': True}}
        s.step(self.ctx, state)
        self.assertEqual(oc.call_count, 1)

    @patch.object(s, 'notify')
    @patch.object(s, 'process_outbox')
    @patch.object(s, 'snapshot')
    @patch.object(s, 'oc')
    def test_failure_pauses_and_warns(self, oc, snapshot, outbox, notify):
        snapshot.return_value = self.snap
        oc.return_value = {'data': {'time': {'idle': 1}, 'outcome': 'failed'}}
        state = s.step(self.ctx, {'active': {'id': 'ses_failed', 'started': 1, 'admitted': True}})
        self.assertTrue(state['paused'])
        notify.assert_called_once()

    @patch.object(s, 'notify')
    @patch.object(s, 'process_outbox')
    @patch.object(s, 'snapshot')
    @patch.object(s, 'oc')
    def test_replaced_bound_credential_admits_recovery_without_pause(self, oc, snapshot, outbox, notify):
        snapshot.return_value = self.snap
        s.credentials.accounts.return_value = [{'id': 'cred_new', 'active': True}]
        oc.side_effect = [
            {'data': {'time': {'idle': True}, 'outcome': 'failed'}},
            {'data': []}, {}, {'data': {'id': 'ses_recovered'}}, {},
        ]
        state = {'active': {'id': 'ses_failed', 'started': 1},
                 'session_credentials': {'ses_failed': 'cred_old'},
                 'active_credential_id': 'cred_old'}
        result = s.step(self.ctx, state)
        self.assertFalse(result.get('paused', False))
        self.assertEqual(result['active']['id'], 'ses_recovered')
        notify.assert_not_called()

    @patch.object(s, 'notify')
    @patch.object(s, 'process_outbox')
    @patch.object(s, 'snapshot')
    @patch.object(s, 'oc')
    def test_replaced_binding_recovers_after_switch_was_already_observed(self, oc, snapshot, outbox, notify):
        snapshot.return_value = self.snap
        s.credentials.accounts.return_value = [{'id': 'cred_new', 'active': True}]
        oc.side_effect = [
            {'data': {'time': {'idle': True}, 'outcome': 'failed'}},
            {'data': []}, {}, {'data': {'id': 'ses_recovered'}}, {},
        ]
        policy_paths = [s.ROOT / 'rules' / 'TICK.md', s.ROOT / 'projects' / 'test.json']
        policy = {str(p.relative_to(s.ROOT)): s.digest(p.read_text()) for p in policy_paths if p.exists()}
        fingerprint = s.digest({'project': self.snap, 'policy': policy})
        state = {'active': {'id': 'ses_failed', 'started': 1},
                 'session_credentials': {'ses_failed': 'cred_old'},
                 'active_credential_id': 'cred_new', 'fingerprint': fingerprint}
        result = s.step(self.ctx, state)
        self.assertFalse(result.get('paused', False))
        self.assertEqual(result['active']['id'], 'ses_recovered')
        notify.assert_not_called()

    @patch.object(s, 'notify')
    @patch.object(s, 'process_outbox')
    @patch.object(s, 'snapshot')
    @patch.object(s, 'oc')
    def test_failed_session_keeps_pause_when_binding_exists(self, oc, snapshot, outbox, notify):
        snapshot.return_value = self.snap
        s.credentials.accounts.return_value = [{'id': 'cred_old', 'active': True}]
        oc.return_value = {'data': {'time': {'idle': True}, 'outcome': 'failed'}}
        state = {'active': {'id': 'ses_failed', 'started': 1},
                 'session_credentials': {'ses_failed': 'cred_old'}}
        result = s.step(self.ctx, state)
        self.assertTrue(result['paused'])
        notify.assert_called_once()

    @patch.object(s, 'notify')
    @patch.object(s, 'process_outbox')
    @patch.object(s, 'snapshot')
    @patch.object(s, 'oc')
    def test_replaced_binding_without_usable_credential_keeps_pause(self, oc, snapshot, outbox, notify):
        snapshot.return_value = self.snap
        s.credentials.accounts.return_value = [{'id': 'cred_new', 'active': True}]
        with patch.object(s.credentials, 'choose', return_value=None):
            oc.return_value = {'data': {'time': {'idle': True}, 'outcome': 'failed'}}
            result = s.step(self.ctx, {'active': {'id': 'ses_failed', 'started': 1},
                                      'session_credentials': {'ses_failed': 'cred_old'}})
        self.assertTrue(result['paused'])
        notify.assert_called_once()

    @patch.object(s, 'notify')
    @patch.object(s, 'process_outbox')
    @patch.object(s, 'snapshot')
    @patch.object(s, 'oc')
    def test_failed_unbound_session_keeps_pause(self, oc, snapshot, outbox, notify):
        snapshot.return_value = self.snap
        oc.return_value = {'data': {'time': {'idle': True}, 'outcome': 'failed'}}
        result = s.step(self.ctx, {'active': {'id': 'ses_failed', 'started': 1}})
        self.assertTrue(result['paused'])
        notify.assert_called_once()

    def test_human_test_requires_live_instructions(self):
        with self.assertRaises(ValueError):
            s.notify(self.ctx, {'id': 'test', 'kind': 'human-test', 'message': 'test it'})
        self.ctx.api.assert_not_called()

    def test_delivered_notification_deduplicated(self):
        (self.ctx.state / 'notifications.json').write_text(json.dumps({'already': {}}))
        s.notify(self.ctx, {'id': 'already', 'kind': 'milestone', 'message': 'done'})
        self.ctx.api.assert_not_called()

    def test_fingerprint_stable_key_order(self):
        self.assertEqual(s.digest({'a': 1, 'b': 2}), s.digest({'b': 2, 'a': 1}))

    def test_nested_claim_identity_and_release(self):
        claim = {'id': 1, 'body': '<!-- agent-claim\n' + json.dumps(
            {'event': 'claim', 'plan': {'agentRun': 'owned-run'}}) + '\n-->'}
        self.ctx.api.return_value = [claim]
        self.ctx.repo = 'o/r'
        self.assertEqual(claim_state(self.ctx, 1), ('claim', 'owned-run'))
        self.ctx.api.return_value = [claim, {'id': 2, 'body': '<!-- agent-claim\n' +
            json.dumps({'event': 'release', 'agentRun': 'owned-run'}) + '\n-->'}]
        self.assertEqual(claim_state(self.ctx, 1), ('none', None))

    @patch.object(s, 'notify')
    @patch.object(s, 'process_outbox')
    @patch.object(s, 'snapshot')
    @patch.object(s, 'oc')
    def test_timeout_interrupts_and_pauses(self, oc, snapshot, outbox, notify):
        snapshot.return_value = self.snap
        oc.return_value = {'data': {'time': {}}}
        state = s.step(self.ctx, {'active': {'id': 'ses_old', 'started': 1, 'admitted': True}})
        self.assertTrue(state['paused'])
        self.assertEqual(oc.call_args.args[:2], ('post', '/api/session/ses_old/interrupt'))
        notify.assert_called_once()

    @patch.object(s, 'process_outbox')
    @patch.object(s, 'snapshot')
    @patch.object(s, 'oc')
    def test_paused_never_dispatches(self, oc, snapshot, outbox):
        snapshot.return_value = self.snap
        s.step(self.ctx, {'paused': True})
        oc.assert_not_called()

    @patch.object(s, 'process_outbox')
    @patch.object(s, 'snapshot')
    @patch.object(s, 'oc')
    def test_success_changed_state_reconciles_again(self, oc, snapshot, outbox):
        snapshot.return_value = self.snap
        oc.side_effect = [{'data': {'time': {'idle': 1}, 'outcome': 'succeeded'}},
                          {'data': []}, {}, {'data': {'id': 'ses_followup'}}, {}]
        state = s.step(self.ctx, {'active': {'id': 'ses_done', 'started': 1, 'admitted': True},
                                  'fingerprint': 'old-state'})
        self.assertEqual(state['active']['id'], 'ses_followup')

    @patch.object(s, 'process_outbox')
    @patch.object(s, 'snapshot')
    @patch.object(s, 'oc')
    def test_changed_active_credential_wakes_unchanged_snapshot_once(self, oc, snapshot, outbox):
        snapshot.return_value = self.snap
        s.credentials.accounts.return_value = [{'id': 'cred_new', 'active': True}]
        oc.side_effect = [{}, {'data': {'id': 'ses_recovered'}}, {}]
        policy_paths = [s.ROOT / 'rules' / 'TICK.md', s.ROOT / 'projects' / 'test.json']
        policy = {str(p.relative_to(s.ROOT)): s.digest(p.read_text()) for p in policy_paths if p.exists()}
        fingerprint = s.digest({'project': self.snap, 'policy': policy})
        state = {'active_credential_id': 'cred_old', 'fingerprint': fingerprint}
        result = s.step(self.ctx, state)
        self.assertEqual(result['active']['id'], 'ses_recovered')
        self.assertEqual(result['active_credential_id'], 'cred_new')
        finished = dict(result, active=None, last_outcome='succeeded')
        oc.reset_mock()
        oc.side_effect = [{'data': {'time': {'idle': True}, 'outcome': 'succeeded'}},
                          {'data': []}]
        s.step(self.ctx, finished)
        self.assertFalse(any(call.args[0] == 'post' and call.args[1] == '/api/session'
                             for call in oc.call_args_list))

    @patch.object(s, 'process_outbox')
    @patch.object(s, 'snapshot')
    @patch.object(s, 'oc')
    def test_same_active_credential_does_not_wake(self, oc, snapshot, outbox):
        snapshot.return_value = self.snap
        policy_paths = [s.ROOT / 'rules' / 'TICK.md', s.ROOT / 'projects' / 'test.json']
        policy = {str(p.relative_to(s.ROOT)): s.digest(p.read_text()) for p in policy_paths if p.exists()}
        fingerprint = s.digest({'project': self.snap, 'policy': policy})
        s.credentials.accounts.return_value = [{'id': 'cred_same', 'active': True}]
        s.step(self.ctx, {'active_credential_id': 'cred_same', 'fingerprint': fingerprint})
        self.assertFalse(any(call.args[1] == '/api/session' for call in oc.call_args_list))

    @patch.object(s, 'process_outbox')
    @patch.object(s, 'snapshot')
    @patch.object(s, 'oc')
    def test_credential_change_does_not_duplicate_running_coordinator(self, oc, snapshot, outbox):
        import time
        snapshot.return_value = self.snap
        s.credentials.accounts.return_value = [{'id': 'cred_new', 'active': True}]
        oc.return_value = {'data': {'time': {}}}
        state = {'active_credential_id': 'cred_old', 'active': {
            'id': 'ses_running', 'started': time.time(), 'admitted': True}}
        result = s.step(self.ctx, state)
        self.assertEqual(result['active']['id'], 'ses_running')
        self.assertEqual(oc.call_count, 1)

    @patch.object(s, 'process_outbox')
    @patch.object(s, 'snapshot')
    @patch.object(s, 'oc')
    def test_unrelated_safety_pause_is_not_cleared_by_credential_change(self, oc, snapshot, outbox):
        snapshot.return_value = self.snap
        s.credentials.accounts.return_value = [{'id': 'cred_new', 'active': True}]
        state = {'active_credential_id': 'cred_old', 'paused': True,
                 'paused_reason': 'operator safety hold'}
        result = s.step(self.ctx, state)
        self.assertTrue(result['paused'])
        self.assertEqual(result['paused_reason'], 'operator safety hold')

    @patch.object(s, 'process_outbox')
    @patch.object(s, 'snapshot')
    @patch.object(s, 'oc')
    def test_auth_pause_recovers_only_after_credential_change(self, oc, snapshot, outbox):
        snapshot.return_value = self.snap
        s.credentials.accounts.return_value = [{'id': 'cred_new', 'active': True}]
        oc.side_effect = [{}, {'data': {'id': 'ses_auth_recovery'}}, {}]
        policy_paths = [s.ROOT / 'rules' / 'TICK.md', s.ROOT / 'projects' / 'test.json']
        policy = {str(p.relative_to(s.ROOT)): s.digest(p.read_text()) for p in policy_paths if p.exists()}
        fingerprint = s.digest({'project': self.snap, 'policy': policy})
        state = {'active_credential_id': 'cred_old', 'paused': True,
                 'paused_reason': 'credential refused by provider access policy',
                 'fingerprint': fingerprint}
        result = s.step(self.ctx, state)
        self.assertFalse(result.get('paused', False))
        self.assertEqual(result['active']['id'], 'ses_auth_recovery')

    @patch.object(s, 'process_outbox')
    @patch.object(s, 'snapshot')
    @patch.object(s, 'oc')
    def test_legacy_auth_pause_with_mixed_bindings_stays_paused(self, oc, snapshot, outbox):
        snapshot.return_value = self.snap
        s.credentials.accounts.return_value = [{'id': 'cred_new', 'active': True}]
        state = {'paused': True, 'paused_reason': 'credential refused by provider access policy',
                 'session_credentials': {'ses_old_a': 'cred_a', 'ses_old_b': 'cred_b'}}
        result = s.step(self.ctx, state)
        self.assertTrue(result['paused'])
        self.assertEqual(result['paused_reason'], 'credential refused by provider access policy')
        self.assertFalse(any(call.args[0] == 'post' and call.args[1] == '/api/session'
                             for call in oc.call_args_list))

    @patch.object(s, 'process_outbox')
    @patch.object(s, 'snapshot')
    @patch.object(s, 'oc')
    def test_unusable_new_active_credential_does_not_wake(self, oc, snapshot, outbox):
        snapshot.return_value = self.snap
        s.credentials.accounts.return_value = [
            {'id': 'cred_new', 'active': True}, {'id': 'cred_old', 'active': False}]
        policy_paths = [s.ROOT / 'rules' / 'TICK.md', s.ROOT / 'projects' / 'test.json']
        policy = {str(p.relative_to(s.ROOT)): s.digest(p.read_text()) for p in policy_paths if p.exists()}
        fingerprint = s.digest({'project': self.snap, 'policy': policy})
        state = {'active_credential_id': 'cred_old', 'fingerprint': fingerprint,
                 'credentials': {'cred_new': {'broken_until': 9999999999}}}
        s.step(self.ctx, state)
        self.assertFalse(any(call.args[0] == 'post' and call.args[1] == '/api/session'
                             for call in oc.call_args_list))

    @patch.object(s, 'process_outbox')
    @patch.object(s, 'snapshot')
    @patch.object(s, 'oc')
    def test_legacy_failed_state_gets_one_recovery_wake(self, oc, snapshot, outbox):
        snapshot.return_value = self.snap
        s.credentials.accounts.return_value = [{'id': 'cred_current', 'active': True}]
        oc.side_effect = [{}, {'data': {'id': 'ses_legacy_recovery'}}, {}]
        policy_paths = [s.ROOT / 'rules' / 'TICK.md', s.ROOT / 'projects' / 'test.json']
        policy = {str(p.relative_to(s.ROOT)): s.digest(p.read_text()) for p in policy_paths if p.exists()}
        fingerprint = s.digest({'project': self.snap, 'policy': policy})
        state = {'fingerprint': fingerprint, 'last_outcome': 'failed'}
        result = s.step(self.ctx, state)
        self.assertEqual(result['active']['id'], 'ses_legacy_recovery')


if __name__ == '__main__':
    unittest.main()


class PrClosureTests(unittest.TestCase):
    def setUp(self):
        self.ctx = Mock()
        self.ctx.api.return_value = {'state': 'closed', 'merged_at': None, 'title': 'x'}

    def test_closed_unmerged_is_reported(self):
        state = {'open_prs': [['o/r', 7]]}
        events = s.check_pr_closures(self.ctx, state, {'pulls': []})
        self.assertEqual([e['id'] for e in events], ['pr-closed-o-r-7'])
        self.assertEqual(state['open_prs'], [])

    def test_merged_pr_is_not_reported(self):
        self.ctx.api.return_value = {'state': 'closed', 'merged_at': '2026-01-01', 'title': 'x'}
        state = {'open_prs': [['o/r', 7]]}
        self.assertEqual(s.check_pr_closures(self.ctx, state, {'pulls': []}), [])

    def test_still_open_is_not_reported(self):
        state = {'open_prs': [['o/r', 7]]}
        current = {'pulls': [{'repository': 'o/r', 'number': 7}]}
        self.assertEqual(s.check_pr_closures(self.ctx, state, current), [])
        self.assertEqual(state['open_prs'], [['o/r', 7]])
