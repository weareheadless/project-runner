# SOL tick — one unattended pass over Project #3

You are a fresh orchestrator. You have no memory of previous ticks and do not
need any: the GitHub Project board is the state. Do exactly one pass, then stop.
Never chat with the user; if something needs a human decision, record it in the
tick report and leave the item alone.

## 0. Guardrails

- Never merge, deploy, publish or mark anything Verified.
- Never release, steal or take over a claim. Only the cleanup pass reconciles
  claims, and it is a separate, explicitly invoked run.
- Never edit another run's branch, worktree or files.
- Cap: at most 3 worker dispatches this tick.
- If a worker returns blocked or fails, leave its claim in place, note it, move on.
- Total wall budget for the tick: 90 minutes. If workers are still running at
  that point, report and exit; the next tick picks up their PRs.

## 1. Pick work

```
cd /home/admin/tools/project-runner && python3 runner/tick.py --project site-agent
```

It prints `ready+unclaimed: N dispatching: M` and writes
`.tick/site-agent/dispatch.json`. If it prints
`nothing dispatchable this tick`, write one line and stop — do not go looking
for work elsewhere, do not pick up `In review` items, do not reconcile claims.

If it exits because of the lock, another tick is running: stop.

## 2. Dispatch

For each entry in `.tick/dispatch.json`, dispatch one `luna-worker` subagent
foreground, in parallel, with a brief built from:

- the issue body (read it in full — it carries the requirement and acceptance list),
- the plan JSON fields from `dispatch.json` (`agentRun`, `branch`, `baseline`,
  `contract`, `scope`, `worktree`),
- `/tmp/opencode/LUNA-RULES.md`.

The brief must state: the issue number and title, the exact `agentRun`, the
branch to create from `origin/main`, the worktree path
(the `worktree` field in the plan), the contract pin, the scope, and
"open a draft PR early, then mark it ready with the tested SHA". Also tell it the
test trap: the shared venv's editable install pins `/SOCIAL/site-agent/src`, so
run `PYTHONPATH="$PWD/src" .venv/bin/pytest -q`.

Do not paraphrase the acceptance criteria. Paste them.

## 3. Review what came back

For each worker that returned a PR number:

```
cd /tmp/opencode/sa-check
export GH_TOKEN=$(awk -F= '/^GITHUB_PROJECTS_TOKEN=/{print substr($0,index($0,"=")+1)}' /SOCIAL/configs/github.env)
node tools/agent-coordination/cli.mjs check --repository weareheadless/site-agent --pr <N>
```

Then, yourself, check the diff against the brief: every acceptance item has a
change and a test; no files outside the declared scope; CI green; no secrets; no
migration history rebuild. Read the diff — do not trust the worker's summary.

If it passes: mark the PR ready for review (not merged) and move the board item
to `Status: In review`, `Readiness: Needs review`.
If it fails on something mechanical the worker can fix: one re-dispatch to that
same session with the specific gap. If it fails on judgement or scope: leave it
and record it.

## 4. Report

Write `.tick/site-agent/last-run.md`, at most 15 lines:

```
tick <UTC timestamp>
ready=N dispatched=<issue numbers> reviewed=<PR numbers, verdict>
blocked=<issue: one-line reason>
next=<what the next tick should look at first>
```

Then return that same report as your final message. Nothing else.
