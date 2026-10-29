# Project runner

Quota availability: during a cooldown, the runner now performs bounded fresh,
tool-disabled availability probes, at most once per credential per five minutes.
Successful requests override stale local exhaustion records immediately. Only
failed fresh quota responses extend an account's cooldown; network errors do not.
Session credential IDs are recorded at admission so old failures cannot poison
whichever account happens to be active later. Unknown historical identity is not
guessed. Account rotation remains host-global; probes defer while owned work runs.

An unattended GitHub Project supervisor. No model runs while the project is idle.
GitHub remains the work/evidence ledger; each SOL reconciliation gets a new
OpenCode session, and Luna workers have separate sessions and worktrees.

## Run

```sh
./install.sh
python3 runner/supervisor.py --project site-agent --once
python3 runner/supervisor.py --project site-agent
python3 -m unittest discover -s runner -p 'test_*.py' -v
```

The supervisor reconciles GitHub every 20 seconds (plus API time), using ETags
for REST reads. It is change-driven polling, not an inbound webhook server:
board edits, PR heads/checks, main workflows, release receipts and owner replies
wake a fresh SOL pass. One active coordinator per project, an OS-held lock,
durable session IDs, bounded runtime, and pause-on-failure prevent duplicate
dispatches and retry storms. No public endpoint or webhook secret is required.
Workers are durable independent sessions started with runner/workers.py. Each
completion wakes a fresh SOL pass, which reviews that result and refills the
free slot without waiting for the slowest worker in a batch. Concurrency is
enforced in code, not just in the prompt.

The old 30-minute OpenChamber schedule must remain disabled. Do not run both.
`runner/project-runner.service` is a user-systemd service for this host. Install
under `~/.config/systemd/user/` and enable it with `systemctl --user enable --now
project-runner`. The service survives logout if user lingering is enabled.

## Flow

1. Read full project state and continuation; process existing PRs before new work.
2. Check dependencies and claims, select low/medium/high Luna, run up to three
   independent workers in separate worktrees. Never assume Ready means unclaimed.
3. SOL independently reads diffs, runs coordination checks and records acceptance
   evidence. `runner/merge.py` rechecks exact head SHA, CI and mergeability before
   merging an authorized PR. No CI evidence means no merge.
4. Queue exact merged source in the existing canonical release pipeline. This
   repository does not create an alternative deployer or restart product services.
5. Record exact-SHA receipts and functional live proof. Merged, deployed, verified
   and awaiting owner tests are different states. Failed releases stay unfinished.

Product-specific judgement/release queue operations are executed by SOL under
`rules/TICK.md`, not secretly inferred by the Python watcher. These require live
end-to-end validation; passing supervisor tests does not prove a product release.

## Project management

`projects/site-agent-goal.md` is SOL's stable mandate: integrated outcome,
authority, evidence expectations and escalation boundaries. The board and a
concise strategy/decision handoff supply current context. SOL chooses priorities
and recovery; discovery scripts do not impose a global merge-before-dispatch
order. Blocked unrelated work must not starve independent high-priority tasks.
Execution code retains capacity, ownership, quota and merge safety checks.

## Outcome acceptance

`runner/delivery.py` keeps original requests owned across implementation, merge,
publication, deployment and verification. A published artifact is explicitly not
a completed request. SOL records next actions and links dependent delivery work.
Verified or blocked proposals start a fresh `delivery-reviewer` session; applying
them requires that session to finish successfully with acceptance and evidence
bound to the proposal hash. Rejected proposals stay pending and return actionable
gaps. Blockers require recorded searches, results, evidence, alternatives and the
specific owner action that cannot be automated. The supervisor observes review
completion so SOL can continue without owner prompting.

This is a workflow acceptance gate, not a GitHub security boundary: the shared
GitHub credential can still edit the board outside this tool. Reviewer semantic
quality and live delivery remain subject to end-to-end validation; unit tests
prove rejection rules, not that an owner-visible feature works.

## Notifications

SOL writes durable events under `.tick/site-agent/outbox/`. The supervisor
deduplicates by event ID and posts to an inbox issue in `project-runner`, mentions
the owner, and emits an OpenChamber in-app/native notification. Human-test events
are rejected unless they include live URL, source SHA, steps and expected result.
Owner replies (include test ID and pass/fail) wake the next reconciliation.

OpenChamber requires `nativeNotificationsEnabled` and browser/OS permission.
The optional protected `~/.config/project-runner/notifications.env` supplies
OPENCHAMBER_AGENT_TOOL_URL and OPENCHAMBER_AGENT_TOOL_TOKEN from the current
OpenChamber runtime; refresh these if that runtime rotates its token. No secrets
are stored in this repo. An emit receipt confirms server acceptance, not that
an OS banner was seen. Plugin emits do not provide a guaranteed mobile push.
GitHub email/mobile preferences must be configured by the owner; self-authored
GitHub activity may not generate a notification. The issue is always a durable
inbox even when push delivery is unavailable.

## Operations

```sh
systemctl --user status project-runner
journalctl --user -u project-runner -n 40 --no-pager
systemctl --user stop project-runner
```

`.tick/site-agent/health.json` records last completed reconciliation health.
`supervisor.json` records active session and pause state. Investigate the failed
session before clearing `paused`; never remove a live lock or a worker claim.
If admission was interrupted, inspect the saved session inbox before recovery.
Don't re-prompt a worker whose execution may already be running.

## Provider quota exhaustion

The supervisor recognizes provider usage/quota errors in coordinator and worker
sessions (including internal provider-retry messages). It preserves sessions,
claims and branches, interrupts an affected retry loop, blocks new dispatches,
and emits one quota alert per exhaustion episode. No model probes run while
suspended. An explicit provider reset/Retry-After is respected with a 15-second
margin. Without a usable reset timestamp, retries back off from 5 minutes to a
maximum of one hour. Resume prompts target the same sessions, not fresh duplicate
workers. A successful recovery clears the episode and resets backoff.

Cooldown and resume-admission intent are durable across service restarts. If a
restart occurs during an ambiguous resume acknowledgement, automation pauses for
inspection instead of double-submitting work. Other failures and ordinary 429
request throttles are not silently treated as subscription resets. Unsupported
provider reset formats fall back to bounded backoff; no reset time is invented.

Agent Markdown is the versioned source; install generates native V2 definitions
inside this runner's `.opencode/` and copies global agent files for compatibility.
This avoids modifying product checkouts or relying on broken global discovery.
Private machine state, generated config and notification credentials are ignored.
