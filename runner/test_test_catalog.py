import json
import unittest

from test_catalog import ROOT, select


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = json.loads((ROOT / 'projects/site-agent-tests.json').read_text())

    def test_issue_selects_relevant_tests(self):
        tests = select(self.catalog, issue=458)
        self.assertIn('intake-to-preview', [test['id'] for test in tests])

    def test_paths_select_both_surface_and_live_coverage(self):
        ids = [test['id'] for test in select(self.catalog, paths=['src/site_agent/web/intake_lab.py'])]
        self.assertIn('intake-surface', ids)
        self.assertIn('intake-to-preview', ids)

    def test_unrelated_change_does_not_require_paid_run(self):
        self.assertEqual(select(self.catalog, paths=['docs/unrelated.md']), [])

    def test_live_test_is_not_advertised_as_a_qualified_gate(self):
        live = next(test for test in self.catalog['tests'] if test['tier'] == 'live')
        self.assertFalse(live['gate_ready'])
        self.assertTrue(live['limitations'])
        self.assertEqual(self.catalog['default_test_owner']['model'], 'openai/gpt-6-luna')


if __name__ == '__main__':
    unittest.main()
