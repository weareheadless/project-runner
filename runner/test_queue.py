import unittest
import queue as q


def pr(title='', body=''):
    return {'title': title, 'body': body}


class ManagedTests(unittest.TestCase):
    def test_work_id_title(self):
        self.assertTrue(q.managed(pr('[UI-055] Remove the button')))

    def test_agent_task_body(self):
        self.assertTrue(q.managed(pr('something', 'x <!-- agent-task\n{}')))

    def test_legacy_pr_excluded(self):
        self.assertFalse(q.managed(pr('fix: overlay exact remote Payload context')))
        self.assertFalse(q.managed(pr('WIP: unify maintenance candidates')))

    def test_ops_id_recognised(self):
        self.assertTrue(q.managed(pr('[OPS-019] Gate API runtime extras')))


if __name__ == '__main__':
    unittest.main()
