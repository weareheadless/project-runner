"""Detect and queue safe automatic site-agent API release gaps."""
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

REPO = 'weareheadless/site-agent'
RELEASES = Path('/SOCIAL/payload-releases')


def running_api():
    """Return the deployed API receipt identity, or fail closed with a reason."""
    try:
        current = RELEASES / 'api' / 'current'
        target = current.resolve(strict=True)
        if not target.is_dir():
            raise ValueError('current API target is not a directory')
        release_id = target.name
        receipt_path = RELEASES / 'runs' / 'api' / release_id / 'receipt.json'
        receipt = json.loads(receipt_path.read_text())
        sha = receipt.get('sourceSha') or receipt.get('source_sha')
        status = receipt.get('status')
        if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}', sha):
            raise ValueError('receipt has no valid full source SHA')
        if not isinstance(status, str) or not status:
            raise ValueError('receipt has no status')
        return {'releaseId': release_id, 'sourceSha': sha, 'status': status}
    except Exception as exc:
        return None, str(exc)


def _api(ctx, path):
    return ctx.api(f'https://api.github.com/repos/{REPO}/{path}')


def inspect(ctx, state, now):
    running = running_api()
    if isinstance(running, tuple):
        return {'main_sha': None, 'running_sha': None, 'status': 'unknown',
                'blocked_reason': 'running API unavailable: ' + running[1], 'commits': []}
    running_sha = running['sourceSha']
    try:
        main_sha = _api(ctx, 'commits/main')['sha']
        commits = _api(ctx, f'compare/{running_sha}...{main_sha}').get('commits', [])
        checks = _api(ctx, f'commits/{main_sha}/check-runs').get('check_runs', [])
        meaningful = [c for c in checks if c.get('name') != 'task-format']
        reason = None
        if running_sha == main_sha:
            reason = 'already current'
        elif not meaningful or any(c.get('status') != 'completed' or c.get('conclusion') != 'success'
                                   for c in meaningful):
            reason = 'main-head checks are not all completed successfully'
        elif any(re.search(r'(^|/)migrations(/|$)', p if isinstance(p, str) else p.get('filename', ''))
                 for c in commits for p in c.get('files', [])):
            # Queue files are ordinary metadata: blocking on them would stall
            # forever, because every queued release is itself such a commit.
            reason = 'gap includes a schema migration'
        # GitHub compare may elide files from commits; fetch each commit's paths.
        if not reason and not any(c.get('files') for c in commits):
            for c in commits:
                files = _api(ctx, f'commits/{c["sha"]}').get('files', [])
                c['files'] = files
                if any(re.search(r'(^|/)migrations(/|$)', f['filename']) for f in files):
                    reason = 'gap includes a schema migration'
                    break
        return {'main_sha': main_sha, 'running_sha': running_sha, 'status': running['status'],
                'blocked_reason': reason, 'commits': commits}
    except Exception as exc:
        return {'main_sha': None, 'running_sha': running_sha, 'status': running['status'],
                'blocked_reason': f'GitHub inspection failed: {exc}', 'commits': []}


def _receipts():
    root = RELEASES / 'runs' / 'api'
    found = []
    try:
        for p in root.glob('*/receipt.json'):
            try:
                r = json.loads(p.read_text())
                found.append((p.stat().st_mtime, r))
            except Exception:
                continue
    except OSError:
        pass
    return found


def queue_release(ctx, state, gap):
    sha = gap.get('main_sha')
    if (not ctx.cfg.get('auto_release_api') or not sha or gap.get('blocked_reason') or
            sha in state.setdefault('auto_release_queued', {})):
        return False
    try:
        current_mtime = (RELEASES / 'api' / 'current').resolve(strict=True).stat().st_mtime
    except OSError:
        return False
    if any(mtime > current_mtime and r.get('status') in {'running', 'building', 'uploaded'}
           for mtime, r in _receipts()):
        return False
    release_id = time.strftime('%Y%m%d%H%M%S', time.gmtime()) + '-api-auto-' + secrets.token_hex(4)
    repo_url = 'git@github.com:weareheadless/site-agent.git'
    with tempfile.TemporaryDirectory(prefix='runner-api-release-') as tmp:
        checkout = Path(tmp) / 'site-agent'
        env = dict(os.environ)
        identity = os.environ.get('RUNNER_GIT_SSH_IDENTITY', '/home/admin/.ssh/github_weareheadless_ed25519')
        if Path(identity).is_file():
            env['GIT_SSH_COMMAND'] = f'ssh -i {identity} -o IdentitiesOnly=yes -o BatchMode=yes'
        subprocess.run(['git', 'clone', '--no-checkout', repo_url, str(checkout)],
                       check=True, capture_output=True, text=True, env=env)
        subprocess.run(['git', '-C', str(checkout), 'checkout', '--detach', sha],
                       check=True, capture_output=True, text=True)
        # Exact canonical queue contract: {"schema":1,"release":{"id","sha"}}.
        manifest = {'schema': 1, 'release': {'id': release_id, 'sha': sha}}
        (checkout / 'deploy' / 'desired-api-release.json').write_text(
            json.dumps(manifest, indent=2) + '\n')
        subprocess.run(['git', '-C', str(checkout), 'add', 'deploy/desired-api-release.json'],
                       check=True, capture_output=True, text=True)
        subprocess.run(['git', '-C', str(checkout), '-c', 'user.name=Project Runner',
                        '-c', 'user.email=project-runner@users.noreply.github.com', 'commit',
                        '-m', f'Queue API release {release_id}'], check=True,
                       capture_output=True, text=True)
        subprocess.run(['git', '-C', str(checkout), 'push', 'origin', 'HEAD:main'],
                       check=True, capture_output=True, text=True, env=env)
    state['auto_release_queued'][sha] = {'releaseId': release_id, 'queued_at': time.time()}
    return True


def step(ctx, state, now):
    gap = inspect(ctx, state, now)
    if not gap.get('main_sha') or not gap.get('running_sha') or gap['main_sha'] == gap['running_sha']:
        return
    key = f"{gap['main_sha']}:{gap['running_sha']}"
    # The event is deduplicated, the queue attempt is not: a gap that was
    # reported while blocked must still be released once the block clears.
    already_reported = key in state.setdefault('release_gap_events', {})
    queued = gap['main_sha'] in state.get('auto_release_queued', {})
    if not queued:
        try:
            queued = queue_release(ctx, state, gap)
        except Exception as exc:
            gap['blocked_reason'] = f'release queue failed: {exc}'
    event = {'id': 'delivery-gap-' + key, 'kind': 'delivery-gap',
             'message': f"API release gap: main {gap['main_sha']}, running {gap['running_sha']}; "
                        f"release {'queued' if queued else 'not queued'}. "
                        f"{gap.get('blocked_reason') or ''}"}
    state['release_gap_events'][key] = {'queued': queued, 'at': now}
    if already_reported:
        return
    outbox = ctx.state / 'outbox'
    outbox.mkdir(exist_ok=True)
    from supervisor import save
    save(outbox / (event['id'] + '.json'), event)
