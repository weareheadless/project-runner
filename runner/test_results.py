"""Durable test-result ingestion and deduplicated GitHub issue reporting.

No models or test executions here. The supervisor drains test-results/*.json,
including during provider cooldowns. Raw transcripts, URLs and logs stay local.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import time
from pathlib import Path

from tick import ROOT, Ctx


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2))
    os.replace(temporary, path)


def normalize(value):
    """Accept the browser evidence contract or a runner command receipt."""
    if not isinstance(value, dict):
        raise ValueError('result must be an object')
    if value.get('schema') not in ('site-agent.qa.evidence/1', 'project-runner.test-result/1'):
        raise ValueError('unsupported result schema')
    test = (value.get('flow') or {}).get('id') or value.get('test_id')
    run = value.get('run_id')
    if not isinstance(test, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,100}', test):
        raise ValueError('invalid test id')
    if not isinstance(run, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,200}', run):
        raise ValueError('invalid run id')
    status = value.get('status')
    if status not in ('passed', 'failed', 'error', 'unavailable', 'interrupted'):
        raise ValueError('invalid result status')
    rubric = value.get('rubric') or {}
    if value['schema'] == 'site-agent.qa.evidence/1' and not isinstance(rubric.get('passed'), bool):
        raise ValueError('browser result needs a deterministic rubric verdict')
    category = value.get('failure_kind') or ('execution' if status in ('error', 'unavailable', 'interrupted') else 'observed')
    if category not in ('execution', 'observed', 'harness', 'infrastructure'):
        category = 'unclassified'
    checks = sorted(str(item)[:100] for item in rubric.get('failed_checks', []) if isinstance(item, str))
    step = str(value.get('failed_step') or 'not-recorded')[:100]
    # Never infer root cause from arbitrary page text or model-generated errors.
    material = json.dumps([test, category, step, checks, value.get('failure_code')], sort_keys=True)
    fingerprint = hashlib.sha256(material.encode()).hexdigest()[:24]
    return {
        'test': test, 'run': run, 'status': status, 'category': category,
        'step': step, 'checks': checks, 'fingerprint': fingerprint,
        'code': value.get('failure_code'),
        'failed': status != 'passed' or rubric.get('passed') is False,
        'sha': (value.get('target') or {}).get('sha') or value.get('source_sha'),
        'deployed_sha': (value.get('target') or {}).get('deployed_sha'),
    }


def _safe(value):
    """Only opaque identifiers are published; evidence content stays private."""
    text = str(value or 'unknown')
    return text if re.fullmatch(r'[a-zA-Z0-9_.:-]{1,200}', text) else '[see private evidence]'


def _write(ctx, url, method, body):
    # Ctx normally retries transport failures. Retrying an ambiguous issue POST
    # can create duplicates; defer instead and recover by its durable marker.
    previous = ctx.cfg.get('github_attempts')
    ctx.cfg['github_attempts'] = 1
    try:
        return ctx.api(url, method, body)
    finally:
        if previous is None:
            ctx.cfg.pop('github_attempts', None)
        else:
            ctx.cfg['github_attempts'] = previous


def publish(ctx, result, receipt_path, receipt_hash, ledger):
    key = result['fingerprint']
    marker = f'<!-- project-runner-test:{key} -->'
    base = f'https://api.github.com/repos/{ctx.repo}'
    record = ledger.get(key)
    issue = None
    if record:
        issue = ctx.api(f'{base}/issues/{record["number"]}')
    else:
        # Recover a POST acknowledged by GitHub before a local ledger write.
        # List directly instead of depending on eventual search indexing.
        page = 1
        while True:
            batch = ctx.api(f'{base}/issues?state=all&per_page=100&page={page}')
            issue = next((item for item in batch if marker in (item.get('body') or '')), None)
            if issue or len(batch) < 100:
                break
            page += 1
    if issue and issue.get('state') == 'closed':
        issue = _write(ctx, f'{base}/issues/{issue["number"]}', 'PATCH', {'state': 'open'})
    if not issue:
        body = (
            f'{marker}\n\nAutomated test result: **{_safe(result["test"])}**.\n\n'
            f'- Outcome: `{_safe(result["status"])}`; classification: `{_safe(result["category"])}`.\n'
            f'- Failure code: `{_safe(result["code"])}`.\n'
            f'- First recorded boundary: `{_safe(result["step"])}`.\n'
            f'- Failed checks: `{", ".join(_safe(x) for x in result["checks"]) or "not recorded"}`.\n'
            f'- Source SHA: `{_safe(result["sha"])}`; serving SHA: `{_safe(result["deployed_sha"])}`.\n'
            f'- Run: `{_safe(result["run"])}`. Evidence SHA256: `{receipt_hash}`.\n\n'
            'Private evidence is retained in the project runner test-results directory; '
            'screenshots, transcripts, credentials and raw logs are not posted here.\n\n'
            '## Required follow-up\n'
            'Inspect the retained evidence, reproduce the first broken boundary, and determine '
            'whether the defect is in the product, the harness, or infrastructure. '
            'Do not infer cause from a truncated warning, weaken the assertion, or mark an '
            'unavailable run passed. Check existing product tasks before opening another fix. '
            'Attach the fix and rerun the same test with exact revision evidence. '
            'This issue is a report, not approval to spend, deploy, delete a tenant, or close a parent request.'
        )
        issue = _write(ctx, base + '/issues', 'POST', {
            'title': f'[UI test] {result["test"]}: {result["category"]} failure at {_safe(result["step"])}'[:240],
            'body': body,
        })
    if ctx.cfg.get('project'):
        project = ctx.cfg['project']
        data = ctx.gql('query($owner:String!,$number:Int!){user(login:$owner){projectV2(number:$number){id}}}',
                       {'owner': project['owner'], 'number': project['number']})
        project_id = data['user']['projectV2']['id']
        # GitHub returns the same item when it already belongs to this project.
        ctx.gql('mutation($project:ID!,$issue:ID!){addProjectV2ItemById(input:{projectId:$project,contentId:$issue}){item{id}}}',
                {'project': project_id, 'issue': issue['node_id']})
    ledger[key] = {
        'number': issue['number'], 'url': issue['html_url'], 'last_run': result['run'],
        'last_receipt': str(receipt_path), 'sha256': receipt_hash,
    }
    return issue['html_url']


def process_pending(ctx, now=None):
    """Run before model/quota gates. Failed publication remains queued with backoff."""
    now = time.time() if now is None else now
    directory = ctx.state / 'test-results'
    if not directory.exists():
        return {'processed': 0, 'issues': [], 'errors': []}
    summary = {'processed': 0, 'issues': [], 'errors': []}
    with (ctx.state / 'test-results.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ledger_path = ctx.state / 'test-issues.json'
        ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
        for path in sorted(directory.glob('*.json')):
            if path.is_symlink():
                continue
            delivered = path.with_suffix('.reported')
            retry = path.with_suffix('.retry')
            if delivered.exists():
                continue
            if retry.exists() and json.loads(retry.read_text()).get('after', 0) > now:
                continue
            try:
                raw = path.read_bytes()
                receipt_hash = hashlib.sha256(raw).hexdigest()
                try:
                    result = normalize(json.loads(raw))
                except (ValueError, TypeError, AttributeError):
                    result = normalize({
                        'schema': 'project-runner.test-result/1', 'test_id': 'result-ingestion',
                        'run_id': receipt_hash, 'status': 'error', 'failure_kind': 'harness',
                        'failure_code': 'malformed-evidence',
                    })
                issue_url = None
                if result['failed']:
                    issue_url = publish(ctx, result, path, receipt_hash, ledger)
                    save(ledger_path, ledger)
                    summary['issues'].append(issue_url)
                save(delivered, {'run_id': result['run'], 'issue': issue_url, 'sha256': receipt_hash})
                summary['processed'] += 1
            except Exception as exc:
                # No provider responses or credential-bearing transport diagnostics.
                save(retry, {'after': now + 60, 'error': type(exc).__name__})
                summary['errors'].append({'receipt': path.name, 'error': type(exc).__name__})
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', default='site-agent')
    parser.add_argument('--ingest', type=Path)
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[a-z0-9_-]+', args.project):
        parser.error('invalid project name')
    ctx = Ctx(json.loads((ROOT / 'projects' / f'{args.project}.json').read_text()))
    if args.ingest:
        value = json.loads(args.ingest.read_text())
        result = normalize(value)
        destination = ctx.state / 'test-results' / (result['run'] + '.json')
        if destination.exists() and json.loads(destination.read_text()) != value:
            parser.error('run id already exists with different evidence')
        save(destination, value)
    result = process_pending(ctx)
    print(json.dumps(result))
    return 1 if result['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
