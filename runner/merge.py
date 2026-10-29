#!/usr/bin/env python3
"""Fail-closed exact-SHA merge gate. No model determines CI success here."""
import argparse
import json
import os
import subprocess
from tick import ROOT, Ctx


def validate(review, pr, checks, status):
    for key in ('repository', 'pr', 'head_sha', 'acceptance_evidence', 'tests', 'reviewer_session'):
        if not review.get(key):
            raise ValueError(f'missing review {key}')
    if review.get('accepted') is not True:
        raise ValueError('review did not accept this head')
    if pr['head']['sha'] != review['head_sha']:
        raise ValueError('reviewed SHA is stale')
    if pr['state'] != 'open' or pr['draft'] or pr.get('mergeable') is not True:
        raise ValueError('PR is draft, closed, conflicting or mergeability unknown')
    if pr.get('mergeable_state') not in ('clean', 'unstable'):
        raise ValueError('branch protection or base update blocks merge')
    if not checks and not status.get('statuses'):
        raise ValueError('no CI evidence')
    if any(c['status'] != 'completed' or c['conclusion'] != 'success' for c in checks):
        raise ValueError('check run pending, failed, skipped or neutral')
    if status.get('statuses') and status['state'] != 'success':
        raise ValueError('commit statuses not successful')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--project', required=True)
    ap.add_argument('--review', required=True)
    args = ap.parse_args()
    cfg = json.loads((ROOT / 'projects' / f'{args.project}.json').read_text())
    if not cfg.get('auto_merge'):
        raise ValueError('auto_merge disabled')
    ctx = Ctx(cfg)
    review = json.load(open(args.review))
    if review['repository'] not in cfg.get('repositories', [ctx.repo]):
        raise ValueError('repository not authorized')
    ctx.repo = review['repository']
    base = f'https://api.github.com/repos/{ctx.repo}'
    pr = ctx.api(f'{base}/pulls/{int(review["pr"])}')
    if pr['base']['ref'] != cfg.get('baseline_branch', 'main'):
        raise ValueError('unauthorized target branch')
    sha = pr['head']['sha']
    checks, page = [], 1
    while True:
        part = ctx.api(f'{base}/commits/{sha}/check-runs?per_page=100&page={page}')['check_runs']
        checks.extend(part)
        if len(part) < 100:
            break
        page += 1
    status = ctx.api(f'{base}/commits/{sha}/status')
    validate(review, pr, checks, status)
    checker = cfg.get('coordination_checker')
    if not checker:
        raise ValueError('coordination checker not configured')
    checker_dir = os.path.dirname(checker)
    pin = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=checker_dir,
                         capture_output=True, text=True, check=True).stdout.strip()
    if pin != cfg.get('coordination_checker_sha'):
        raise ValueError('coordination checker pin changed')
    result = subprocess.run(['node', checker, 'check', '--repository', ctx.repo,
                             '--pr', str(pr['number'])],
                            cwd=checker_dir,
                            env={**os.environ, 'GH_TOKEN': ctx.tok},
                            capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise ValueError('coordination checker refused merge; inspect its sanitized output separately')
    result = ctx.api(f'{base}/pulls/{pr["number"]}/merge', 'PUT',
                     {'sha': sha, 'merge_method': 'squash'})
    if not result.get('merged'):
        raise ValueError('GitHub refused merge')
    print(json.dumps({'pr': pr['number'], 'reviewed_sha': sha, 'merge_sha': result['sha']}))


if __name__ == '__main__':
    main()
