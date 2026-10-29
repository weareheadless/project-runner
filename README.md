# project-runner

The fleet that runs a GitHub Project board without a human poking it.

One tick = one fresh orchestrator session: read the board, dispatch bounded
worker agents into their own worktrees, review what came back, update the board,
exit. No context is carried between ticks — the board is the state.

```
agents/     worker agent definitions (opencode agent files)
rules/      worker rules (LUNA-RULES.md) and the tick contract (TICK.md)
runner/     tick.py — board -> dispatchable work, read-only
projects/   one JSON per project: board, repo, contract pin, scope, caps
install.sh  copy agents + rules into the opencode config dir
```

## Why this repo exists separately

The driver must not live in the repository it drives. If the tick code sat in
the product repo, a worker could be dispatched to edit its own dispatcher, and
the tool would change under a running tick.

`tools/agent-coordination` stays in the product repo: it is a validator that
both the worker and the manager run against a PR, so it belongs with the
contract it enforces. This repo is the thing that *runs* that contract.

## Install

```
./install.sh          # copy agents + rules into ~/.config/opencode and /tmp/opencode
./install.sh --check  # show drift between this repo and what is installed
```

Copies, not symlinks: a bad edit in a worktree should not silently become the
live fleet. Commit, then install.

## Run a tick

```
python3 runner/tick.py --project site-agent
python3 runner/tick.py --project site-agent --json
python3 runner/tick.py --project site-agent --reconcile
```

`--reconcile` is advisory only. It lists in-progress items with no open PR and
says whether a branch exists, so you can tell abandoned work from work that just
never got a PR. It never releases a claim.

The script is read-only against GitHub. Writes — claims, PRs, board status — are
done by the tick session under `rules/TICK.md`, which never merges and never
touches another run's claim.

## Adding a project

Copy `projects/site-agent.json`, change the board number, repo, contract pin and
scope. Nothing else is project-specific.
