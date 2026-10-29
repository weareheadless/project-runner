"""Run a catalogued deterministic test, retaining evidence and filing failures.

Live browser flows use the harness's --runner-results-dir after target approval;
this wrapper never starts a paid flow or chooses a tenant.
"""
import argparse
import fcntl
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

from test_results import ROOT, Ctx, process_pending, save


def run(ctx, test, checkout, timeout=600):
    if test['tier'] != 'deterministic':
        raise ValueError('live tests require target authorization; use the harness result queue')
    if not 1 <= timeout <= 3600:
        raise ValueError('timeout must be between 1 and 3600 seconds')
    checkout = Path(checkout).resolve()
    if not (checkout / 'src/site_agent').is_dir():
        raise ValueError('checkout must contain src/site_agent')
    if any('<' in part for part in test['argv']):
        raise ValueError('test prerequisites have unresolved placeholders')
    identifier = test['id'] + '-' + uuid.uuid4().hex
    root = ctx.state / 'test-evidence' / identifier
    root.mkdir(parents=True)
    sha = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=checkout,
                         capture_output=True, text=True, timeout=15, check=True).stdout.strip()
    receipt = {
        'schema': 'project-runner.test-result/1', 'test_id': test['id'], 'run_id': identifier,
        'source_sha': sha, 'failed_step': 'test-command', 'command': test['argv'],
        'status': 'error', 'failure_kind': 'execution',
    }
    started = time.monotonic()
    log = root / 'command.log'
    with log.open('w') as output:
        log.chmod(0o600)
        try:
            result = subprocess.run(test['argv'], cwd=checkout,
                                    env={**os.environ, 'PYTHONPATH': str(checkout / 'src')},
                                    stdout=output, stderr=subprocess.STDOUT, timeout=timeout)
            receipt.update(status='passed' if result.returncode == 0 else 'failed',
                           failure_kind='observed', exit_code=result.returncode,
                           failure_code=None if result.returncode == 0 else 'test-command-nonzero')
        except subprocess.TimeoutExpired:
            receipt.update(status='unavailable', failure_code='test-command-timeout')
        except OSError:
            receipt.update(status='unavailable', failure_code='test-command-unavailable')
    receipt.update(duration_seconds=round(time.monotonic() - started, 3), log=str(log))
    save(ctx.state / 'test-results' / (identifier + '.json'), receipt)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', default='site-agent', choices=['site-agent'])
    parser.add_argument('--test', required=True)
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args(argv)
    ctx = Ctx(json.loads((ROOT / 'projects' / (args.project + '.json')).read_text()))
    catalog = json.loads((ROOT / 'projects' / (args.project + '-tests.json')).read_text())
    test = next((entry for entry in catalog['tests'] if entry['id'] == args.test), None)
    if test is None:
        parser.error('unknown test; consult test_catalog.py')
    with (ctx.state / 'test-run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('another owned test is running')
        try:
            receipt = run(ctx, test, args.checkout, args.timeout)
        except ValueError as exc:
            parser.error(str(exc))
        reports = process_pending(ctx)
    print(json.dumps({'result': receipt, 'reporting': reports}))
    return 0 if receipt['status'] == 'passed' and not reports['errors'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
