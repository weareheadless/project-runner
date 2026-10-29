import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import install_agents


class InstallAgentsTests(unittest.TestCase):
    def test_generate_registers_native_openai_models_and_keeps_agent_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents = root / 'agents'
            agents.mkdir()
            (agents / 'sol.md').write_text(
                '---\ndescription: Coordinator\nmode: primary\n'
                'model: openai/gpt-6.1-sol\nvariant: medium\n---\nSystem prompt')
            (agents / 'luna-worker.md').write_text(
                '---\ndescription: Worker\nmodel: openai/gpt-6-luna\nvariant: low\n---\nWorker prompt')
            with patch.object(install_agents, 'ROOT', root):
                install_agents.generate()
            config = json.loads((root / '.opencode' / 'opencode.json').read_text())

        self.assertEqual(config['providers'], {'openai': {'models': {
            'gpt-6.1-sol': {}, 'gpt-6-luna': {}}}})
        self.assertEqual(config['agents']['sol']['model'], 'openai/gpt-6.1-sol#medium')
        self.assertEqual(config['agents']['sol']['system'], 'System prompt')
        self.assertEqual(config['agents']['luna-worker']['model'], 'openai/gpt-6-luna#low')
        self.assertEqual(config['agents']['luna-worker']['system'], 'Worker prompt')


if __name__ == '__main__':
    unittest.main()
