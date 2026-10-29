import json
import tempfile
import unittest
from pathlib import Path

from test_results import normalize, process_pending, save


def evidence(status='failed', run='run-1', kind=None):
    return {
        'schema': 'site-agent.qa.evidence/1', 'run_id': run,
        'flow': {'id': 'intake-to-preview'}, 'status': status,
        'failure_kind': kind, 'failed_step': 'candidate-in-review',
        'target': {'sha': 'a' * 40},
        'error': 'SECRET token from a private transcript',
        'rubric': {'passed': status == 'passed', 'failed_checks': [] if status == 'passed' else ['flow.steps']},
    }


class FakeContext:
    def __init__(self, state):
        self.state, self.repo, self.cfg = state, 'weareheadless/site-agent', {}
        self.issues, self.created, self.patched = [], [], []
        self.offline = False
        self.graph_calls = []

    def api(self, url, method='GET', body=None):
        if self.offline:
            raise OSError('credential-bearing diagnostics must not be logged')
        if method == 'POST':
            issue = {**body, 'number': len(self.issues) + 1, 'node_id': 'issue-node',
                     'html_url': 'https://github.com/weareheadless/site-agent/issues/1', 'state': 'open'}
            self.issues.append(issue)
            self.created.append(body)
            return issue
        if method == 'PATCH':
            self.issues[0].update(body)
            self.patched.append(body)
            return self.issues[0]
        if '/issues?' in url:
            return self.issues
        return self.issues[0]

    def gql(self, query, variables):
        self.graph_calls.append(variables)
        return {'user': {'projectV2': {'id': 'project-node'}}}


class TestResultsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = FakeContext(Path(self.tmp.name))

    def enqueue(self, value, name=None):
        path = self.ctx.state / 'test-results' / (name or value['run_id'] + '.json')
        save(path, value)
        return path

    def test_pass_never_files_issue(self):
        self.enqueue(evidence('passed'))
        self.assertEqual(process_pending(self.ctx)['processed'], 1)
        self.assertEqual(self.ctx.created, [])

    def test_failure_creates_actionable_issue_without_private_text(self):
        self.enqueue(evidence())
        result = process_pending(self.ctx)
        self.assertEqual(len(result['issues']), 1)
        body = self.ctx.created[0]['body']
        self.assertIn('candidate-in-review', body)
        self.assertIn('Required follow-up', body)
        self.assertNotIn('SECRET', body)
        self.assertTrue((self.ctx.state / 'test-results/run-1.reported').exists())

    def test_repeated_failure_uses_one_issue(self):
        self.enqueue(evidence())
        process_pending(self.ctx)
        self.enqueue(evidence(run='run-2'))
        process_pending(self.ctx)
        self.assertEqual(len(self.ctx.created), 1)
        self.assertEqual(process_pending(self.ctx)['processed'], 0)

    def test_execution_and_product_observations_are_not_conflated(self):
        self.enqueue(evidence())
        self.enqueue(evidence('error', 'run-2'))
        process_pending(self.ctx)
        self.assertEqual(len(self.ctx.created), 2)
        self.assertIn('execution', self.ctx.created[1]['title'])

    def test_recurrence_reopens_closed_issue(self):
        self.enqueue(evidence())
        process_pending(self.ctx)
        self.ctx.issues[0]['state'] = 'closed'
        self.enqueue(evidence(run='run-2'))
        process_pending(self.ctx)
        self.assertEqual(len(self.ctx.created), 1)
        self.assertEqual(self.ctx.patched, [{'state': 'open'}])

    def test_restart_after_post_before_local_save_recovers_existing_issue(self):
        self.enqueue(evidence())
        process_pending(self.ctx)
        (self.ctx.state / 'test-issues.json').unlink()
        (self.ctx.state / 'test-results/run-1.reported').unlink()
        process_pending(self.ctx)
        self.assertEqual(len(self.ctx.created), 1)

    def test_github_outage_leaves_result_pending_and_backs_off(self):
        self.enqueue(evidence())
        self.ctx.offline = True
        first = process_pending(self.ctx, now=100)
        self.assertEqual(len(first['errors']), 1)
        self.assertFalse((self.ctx.state / 'test-results/run-1.reported').exists())
        self.assertEqual(process_pending(self.ctx, now=110)['errors'], [])
        self.ctx.offline = False
        self.assertEqual(process_pending(self.ctx, now=161)['processed'], 1)

    def test_malformed_result_files_harness_issue_not_product_issue(self):
        path = self.ctx.state / 'test-results/bad.json'
        path.parent.mkdir()
        path.write_text('{SECRET invalid json')
        process_pending(self.ctx)
        self.assertIn('result-ingestion: harness', self.ctx.created[0]['title'])
        self.assertNotIn('SECRET', self.ctx.created[0]['body'])

    def test_new_issue_is_added_to_project(self):
        self.ctx.cfg['project'] = {'owner': 'weareheadless', 'number': 3}
        self.enqueue(evidence())
        process_pending(self.ctx)
        self.assertEqual(self.ctx.graph_calls[-1], {'project': 'project-node', 'issue': 'issue-node'})

    def test_missing_verdict_never_becomes_pass(self):
        value = evidence('passed')
        value['rubric'] = {}
        with self.assertRaises(ValueError):
            normalize(value)

    def test_pass_status_with_red_rubric_is_reported(self):
        value = evidence('passed')
        value['rubric'] = {'passed': False, 'failed_checks': ['browser.console']}
        self.enqueue(value)
        process_pending(self.ctx)
        self.assertEqual(len(self.ctx.created), 1)

    def test_empty_directory_has_no_side_effects(self):
        self.assertEqual(process_pending(self.ctx), {'processed': 0, 'issues': [], 'errors': []})

    def test_supervisor_reports_before_provider_cooldown(self):
        from unittest.mock import patch
        import supervisor
        calls = []
        with patch.object(supervisor.test_results, 'process_pending', side_effect=lambda ctx: (
                calls.append('report') or {'processed': 0, 'errors': []})), \
                patch.object(supervisor, 'process_outbox'), \
                patch.object(supervisor, 'quota_check', side_effect=lambda *args: calls.append('quota') or True):
            supervisor.step(self.ctx, {})
        self.assertEqual(calls, ['report', 'quota'])


if __name__ == '__main__':
    unittest.main()
