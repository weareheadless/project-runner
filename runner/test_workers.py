import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import workers


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = Mock()
        self.ctx.state = Path(self.tmp.name)
        self.ctx.repo = 'o/r'
        self.ctx.cfg = {'max_dispatch': 1, 'repositories': ['o/r']}
        self.plan = {'repository': 'o/r', 'issueNumber': 1, 'agentRun': 'run-1',
                     'branch': 'work', 'baseline': 'abc', 'worktree': '/isolated', 'scope': ['src/x']}

    @patch.object(workers, 'oc')
    def test_creation_is_durable_before_prompt(self, oc):
        oc.side_effect = [{}, {'data': {'id': 'ses_worker'}}, {}]
        result = workers.start(self.ctx, self.plan, 'Implement the spec', 'medium')
        self.assertTrue(result['admitted'])
        self.assertEqual(oc.call_args.args[0:2], ('post', '/api/session/ses_worker/prompt'))
        self.assertEqual(oc.call_args_list[1].args[2]['agent'], 'luna-worker-med')
        self.assertTrue(json.loads((self.ctx.state/'workers.json').read_text())['run-1']['admitted'])

    @patch.object(workers, 'oc')
    def test_capacity_is_global(self, oc):
        (self.ctx.state/'workers.json').write_text(json.dumps({'other': {
            'session': 'ses_busy', 'repository': 'o/r', 'issue': 2, 'terminal': False}}))
        oc.return_value = {'data': {'time': {}}}
        with self.assertRaisesRegex(ValueError, 'capacity'):
            workers.start(self.ctx, self.plan, 'spec', 'low')
        self.assertEqual(oc.call_count, 1)

    @patch.object(workers, 'oc')
    def test_duplicate_issue_rejected(self, oc):
        (self.ctx.state/'workers.json').write_text(json.dumps({'other': {
            'session': 'ses_busy', 'repository': 'o/r', 'issue': 1, 'terminal': False}}))
        oc.return_value = {'data': {'time': {}}}
        with self.assertRaisesRegex(ValueError, 'already dispatched'):
            workers.start(self.ctx, self.plan, 'spec', 'low')

    @patch.object(workers, 'oc')
    def test_finished_worker_frees_slot(self, oc):
        (self.ctx.state/'workers.json').write_text(json.dumps({'other': {
            'session': 'ses_done', 'repository': 'o/r', 'issue': 2, 'terminal': False}}))
        oc.side_effect = [{'data': {'time': {'idle': 1}}}, {}, {'data': {'id': 'ses_new'}}, {}]
        self.assertEqual(workers.start(self.ctx, self.plan, 'spec', 'low')['session'], 'ses_new')

    def test_unapproved_repo(self):
        self.plan['repository'] = 'other/repo'
        with self.assertRaisesRegex(ValueError, 'not authorized'):
            workers.start(self.ctx, self.plan, 'spec', 'low')
