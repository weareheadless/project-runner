import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import merge
import supervisor as s


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
        s.step(self.ctx, {'fingerprint': s.digest(self.snap)})
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
        oc.side_effect = [{'data': {'time': {'idle': 1}, 'outcome': 'succeeded'}}, {},
                          {'data': {'id': 'ses_followup'}}, {}]
        state = s.step(self.ctx, {'active': {'id': 'ses_done', 'started': 1, 'admitted': True},
                                  'fingerprint': 'old-state'})
        self.assertEqual(state['active']['id'], 'ses_followup')


if __name__ == '__main__':
    unittest.main()
