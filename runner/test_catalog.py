"""Discover relevant tests; never launch paid work or mutate a test tenant.

    python3 runner/test_catalog.py --project site-agent --issue 458
    python3 runner/test_catalog.py --project site-agent --path src/site_agent/web/intake_lab.py
"""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def select(catalog, *, issue=None, paths=()):
    return [test for test in catalog['tests'] if (
        (issue is None and not paths)
        or (issue is not None and issue in test.get('issues', []))
        or any(path.startswith(prefix) for path in paths for prefix in test.get('paths', []))
    )]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', default='site-agent')
    parser.add_argument('--issue', type=int)
    parser.add_argument('--path', action='append', default=[])
    args = parser.parse_args(argv)
    # A project name, never an arbitrary filesystem path.
    if not args.project or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-_' for c in args.project):
        parser.error('invalid project name')
    catalog = json.loads((ROOT / 'projects' / f'{args.project}-tests.json').read_text())
    print(json.dumps({
        **{key: value for key, value in catalog.items() if key != 'tests'},
        'tests': select(catalog, issue=args.issue, paths=args.path),
    }, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
