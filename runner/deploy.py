#!/usr/bin/env python3
"""Deploy and inspect the project runner.

The runner has no release pipeline: systemd runs `runner/supervisor.py` from this
checkout, so the checkout *is* the deployment. That is easy to misread, so this
command records what is actually running and refuses to pretend otherwise.

  python3 runner/deploy.py            # install agents/rules, record, restart, verify
  python3 runner/deploy.py --check    # non-zero if the running deployment differs
  python3 runner/deploy.py --status   # what is running, as JSON

A dirty checkout is deployed too, but it is recorded as dirty with its changed
paths, because "merged" and "deployed" are different questions here.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICE = 'project-runner'


def git(*args):
    return subprocess.run(['git', *args], cwd=ROOT, capture_output=True,
                          text=True, timeout=30)


def describe():
    """What this checkout would deploy."""
    head = git('rev-parse', 'HEAD').stdout.strip()
    branch = git('rev-parse', '--abbrev-ref', 'HEAD').stdout.strip()
    dirty_paths = [line[3:] for line in git('status', '--porcelain').stdout.splitlines() if line.strip()]
    return {
        'sha': head or None,
        'branch': branch or None,
        'dirty': bool(dirty_paths),
        'dirty_paths': dirty_paths[:50],
        'dirty_count': len(dirty_paths),
        'checkout': str(ROOT),
    }


def service_state():
    try:
        active = subprocess.run(['systemctl', '--user', 'is-active', SERVICE],
                                capture_output=True, text=True, timeout=15).stdout.strip()
    except OSError:
        return {'active': 'unknown', 'started_at': None, 'pid': None}
    started = subprocess.run(['systemctl', '--user', 'show', SERVICE, '-p', 'ActiveEnterTimestamp'],
                             capture_output=True, text=True, timeout=15).stdout.strip().split('=', 1)[-1]
    return {'active': active, 'started_at': started or None, 'pid': _pid()}


def _pid():
    try:
        out = subprocess.run(['systemctl', '--user', 'show', SERVICE, '-p', 'MainPID'],
                             capture_output=True, text=True, timeout=15).stdout.strip()
        value = int(out.split('=', 1)[-1] or 0)
        return value or None
    except (OSError, ValueError):
        return None


def receipt_path(project='site-agent'):
    return ROOT / '.tick' / project / 'deploy.json'


def read_receipt(project='site-agent'):
    path = receipt_path(project)
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def write_receipt(project='site-agent', **extra):
    path = receipt_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {**describe(), **extra,
             'deployed_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    os.replace(temporary, path)
    return value


def health(project='site-agent'):
    try:
        return json.loads((ROOT / '.tick' / project / 'health.json').read_text())
    except (OSError, ValueError):
        return None


def check(project='site-agent'):
    """Return (ok, problems). Compares what is running with what is checked out."""
    problems = []
    receipt = read_receipt(project)
    current = describe()
    if receipt is None:
        problems.append('no deploy receipt: this checkout has never been deployed')
    elif receipt.get('sha') != current['sha']:
        problems.append(f"checkout moved since deploy: deployed {str(receipt.get('sha'))[:8]}, "
                        f"checked out {str(current['sha'])[:8]}")
    elif receipt.get('dirty_paths') != current['dirty_paths']:
        problems.append('uncommitted changes differ from what was deployed')
    state = service_state()
    if state['active'] != 'active':
        problems.append(f'service is {state["active"]!r}, not active')
    if receipt and state['started_at'] and receipt.get('service_started_at') \
            and state['started_at'] != receipt['service_started_at']:
        problems.append('service was restarted after the last recorded deploy')
    if current['dirty']:
        problems.append(f"checkout is dirty ({current['dirty_count']} path(s)): "
                        'uncommitted work is live')
    return not problems, problems


def deploy(project='site-agent', timeout=120):
    install = subprocess.run(['sh', str(ROOT / 'install.sh')], cwd=ROOT,
                             capture_output=True, text=True, timeout=timeout)
    if install.returncode:
        raise RuntimeError('install.sh failed; nothing was restarted')
    restart = subprocess.run(['systemctl', '--user', 'restart', SERVICE],
                             capture_output=True, text=True, timeout=timeout)
    if restart.returncode:
        raise RuntimeError('service restart failed; run install.sh --check')
    deadline = time.time() + 30
    state = service_state()
    while time.time() < deadline and state['active'] != 'active':
        time.sleep(1)
        state = service_state()
    value = write_receipt(project, service_started_at=state['started_at'], service_pid=state['pid'])
    return {'receipt': value, 'service': state, 'install': install.stdout.strip().splitlines()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--project', default='site-agent')
    parser.add_argument('--check', action='store_true', help='exit non-zero if the deployment is stale')
    parser.add_argument('--status', action='store_true', help='print the deployment as JSON')
    args = parser.parse_args(argv)

    if args.status:
        print(json.dumps({'receipt': read_receipt(args.project), 'checkout': describe(),
                          'service': service_state(), 'health': health(args.project)}, indent=2))
        return 0
    if args.check:
        ok, problems = check(args.project)
        print('deployment is current' if ok else 'deployment is NOT current')
        for problem in problems:
            print('  - ' + problem)
        return 0 if ok else 1

    try:
        result = deploy(args.project)
    except (RuntimeError, OSError) as exc:
        print(f'deploy failed: {exc}', file=sys.stderr)
        return 1
    state = result['service']
    print(f"deployed {str(result['receipt']['sha'])[:8]} ({result['receipt']['branch']})"
          f"{' with uncommitted changes' if result['receipt']['dirty'] else ''}")
    print(f"service {state['active']}, started {state['started_at']}")
    ok, problems = check(args.project)
    if not ok:
        print('follow-up:')
        for problem in problems:
            print('  - ' + problem)
    return 0 if state['active'] == 'active' else 1


if __name__ == '__main__':
    raise SystemExit(main())
