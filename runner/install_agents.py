#!/usr/bin/env python3
"""Generate native V2 project-scoped definitions without altering global config.

Agent Markdown remains the versioned source. Only this fleet's simple legacy
frontmatter is parsed; unknown header fields fail instead of being ignored.
"""
import json
from tick import ROOT


def generate():
    agents = {}
    for path in (ROOT / 'agents').glob('*.md'):
        _, header, body = path.read_text().split('---', 2)
        data = {'system': body.strip(), 'mode': 'all'}
        variant = None
        for line in header.splitlines():
            if not line or line[0].isspace() or ':' not in line:
                continue
            key, value = line.split(':', 1)
            value = value.strip()
            if key in ('description', 'mode', 'model'):
                data[key] = value
            elif key == 'variant':
                variant = value
            elif key not in ('permission', 'permissions'):
                raise ValueError(f'unsupported frontmatter {key}')
        if variant and '#' not in data.get('model', ''):
            data['model'] += '#' + variant
        worker = path.stem.startswith('luna-')
        data['permissions'] = [
            {'action': 'question', 'resource': '*', 'effect': 'deny'},
            {'action': 'subagent', 'resource': '*', 'effect': 'deny' if worker else 'allow'}]
        agents[path.stem] = data
    out = ROOT / '.opencode' / 'opencode.json'
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({'$schema': 'https://opencode.ai/config.json', 'agents': agents}, indent=2))


if __name__ == '__main__':
    generate()
