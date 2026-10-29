#!/usr/bin/env python3
"""Advisory inventory of PRs, branches and candidate tasks.

Read-only. Categories are discovery hints, not required ordering or proof of
ownership, review acceptance, mergeability or root cause. SOL decides priorities.
"""
import argparse
import json
import re
import sys

from tick import ROOT, Ctx, branches, collect, claim_state, open_prs


MANAGED = re.compile(r'\[(?:UI|OPS)-\d{3}\]')


def managed(pr):
    """Only PRs this initiative owns; legacy branches are not ours to merge."""
    return bool(MANAGED.search(pr.get('title') or '')) or '<!-- agent-task' in (pr.get('body') or '')


def checks_for(ctx, repo, sha):
    runs = ctx.api(f'https://api.github.com/repos/{repo}/commits/{sha}/check-runs'
                   f'?per_page=100')['check_runs']
    status = ctx.api(f'https://api.github.com/repos/{repo}/commits/{sha}/status')
    names = [(r['name'], r['status'], r['conclusion']) for r in runs]
    pending = [n for n, s, c in names if s != 'completed']
    failed = [n for n, s, c in names if s == 'completed' and c != 'success']
    return {'total': len(names), 'pending': pending, 'failed': failed,
            'combined': status.get('state'), 'statuses': status.get('statuses') or []}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--project', default='site-agent')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()
    ctx = Ctx(json.loads((ROOT / 'projects' / f'{args.project}.json').read_text()))

    in_progress, ready, _ = collect(ctx)
    open_work_ids = {(r['work_id'] or '').lower() for r in in_progress + ready if r['work_id']}
    queue = {'merge': [], 'repair': [], 'reopen': [], 'adopt': [], 'dispatch': []}

    for repo in ctx.cfg.get('repositories', [ctx.repo]):
        for pr in ctx.api(f'https://api.github.com/repos/{repo}/pulls'
                          f'?state=open&per_page=100'):
            if not pr['head']['ref'].startswith(ctx.cfg.get('branch_prefix', 'codex/')) \
                    or not managed(pr):
                continue
            state = checks_for(ctx, repo, pr['head']['sha'])
            item = {'repository': repo, 'pr': pr['number'], 'title': pr['title'],
                    'draft': pr['draft'], 'sha': pr['head']['sha'],
                    'failed': state['failed'], 'pending': state['pending'],
                    'checks': state['total']}
            if 'task-format / Agent task format' in state['failed']:
                queue['repair'].append(item)
            elif state['failed']:
                queue['repair'].append(item)
            elif state['pending'] or not state['total']:
                continue  # CI still deciding; nothing to do yet
            else:
                queue['merge'].append(item)

        closed = ctx.api(f'https://api.github.com/repos/{repo}/pulls'
                         f'?state=closed&per_page=30&sort=updated&direction=desc')
        for pr in closed:
            if pr.get('merged_at') or not pr['head']['ref'].startswith(
                    ctx.cfg.get('branch_prefix', 'codex/')) or not managed(pr):
                continue
            queue['reopen'].append({'repository': repo, 'pr': pr['number'],
                                    'title': pr['title'], 'sha': pr['head']['sha'],
                                    'closed_at': (pr.get('closed_at') or '')[:10],
                                    'closer': (pr.get('closed_by') or {}).get('login')})

        have_pr = {pr['head']['ref'] for pr in
                   ctx.api(f'https://api.github.com/repos/{repo}/pulls'
                           f'?state=open&per_page=100')}
        for name in branches(ctx):
            if not name.startswith(ctx.cfg.get('branch_prefix', 'codex/')) or name in have_pr:
                continue
            lowered = name.lower()
            match = next((wid for wid in open_work_ids if wid and wid in lowered), None)
            if match:
                queue['adopt'].append({'repository': repo, 'branch': name, 'workId': match})

    with_branch = ' '.join(b['branch'].lower() for b in queue['adopt'])
    for row in ready:
        if claim_state(ctx, row['issue'])[0] == 'claim':
            continue
        wid = (row['work_id'] or '').lower()
        if wid and wid in with_branch:
            continue
        queue['dispatch'].append({'issue': row['issue'], 'workId': row['work_id'],
                                  'title': row['title'], 'priority': row['priority']})

    queue['dispatch'].sort(key=lambda r: (r['priority'] or 'P9', r['issue']))
    if args.json:
        print(json.dumps(queue, indent=2))
        return

    def show(label, rows, fmt):
        print(f'\n{label} ({len(rows)})')
        for row in rows[:10]:
            print('  ' + fmt(row))

    show('GREEN CHECKS — still needs ownership and acceptance review', queue['merge'],
         lambda r: f'#{r["pr"]} {r["repository"].split("/")[-1]} {r["sha"][:8]} '
                   f'{"draft" if r["draft"] else "ready"} {r["title"][:50]}')
    show('FAILED CHECKS — inspect diagnostics before deciding repair', queue['repair'],
         lambda r: f'#{r["pr"]} {r["repository"].split("/")[-1]} failed={", ".join(r["failed"])[:60]}')
    show('3. CLOSED UNMERGED — read why it was closed before reopening', queue['reopen'],
         lambda r: f'#{r["pr"]} {r["repository"].split("/")[-1]} {r["closed_at"]} '
                   f'by {r["closer"]} {r["title"][:44]}')
    show('4. ADOPT — branch with no PR', queue['adopt'],
         lambda r: f'{r["repository"].split("/")[-1]} {r["branch"]}')
    show('READY CANDIDATES — assess dependencies and available capacity', queue['dispatch'],
         lambda r: f'#{r["issue"]} {r["workId"]} {r["priority"]} {r["title"][:44]}')


if __name__ == '__main__':
    main()
