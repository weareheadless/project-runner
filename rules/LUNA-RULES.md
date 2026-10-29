# Luna worker operational rules (read this before acting)

Transport update: run coordination commands via
`node /home/admin/tools/project-runner/runner/coordination.mjs ...`.
It uses the pinned canonical checker with compressed IPv4 HTTPS transport.
Do not invoke the checker's direct Node fetch entrypoint or create ad-hoc
wrappers: direct fetch has timed out on this host. For other GitHub curl reads,
use `curl -4 --compressed` with bounded connection/request timeouts. A failed
write has ambiguous state: re-read the claim/PR before retrying it.

These rules apply to every dispatch from SOL for Project #3 (Unified Business
Intelligence) and take precedence over stale agent prompts and over generic
repository credential guidance.

## Run identity

SOL assigns your run label in the dispatch prompt (for example
`luna-ui003-20261026t1420z`). Use that exact string as `agentRun` in the claim
plan and in the PR `agent-task` JSON. Do not invent a different ID and do not
use your session ID unless SOL says so. If no label is supplied, stop and ask.

## GitHub access

Use the classic token, never the fine-grained runtime token:

```bash
export GH_TOKEN=$(awk -F= '/^GITHUB_PROJECTS_TOKEN=/{print substr($0,index($0,"=")+1)}' /SOCIAL/configs/github.env)
```

The fine-grained `GITHUB_PERSONAL_ACCESS_TOKEN` has no GitHub Project scope, so
claims, board reads and manager checks fail with 403/404 when it is used.

Each bash tool call runs in a fresh shell, so an `export` from one call is gone
in the next. Put the export and the request in the SAME command, for example:

```bash
export GH_TOKEN=$(awk -F= '/^GITHUB_PROJECTS_TOKEN=/{print substr($0,index($0,"=")+1)}' /SOCIAL/configs/github.env) && curl -sS -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $GH_TOKEN" https://api.github.com/repos/weareheadless/site-agent/issues/336
```

An empty `GH_TOKEN` produces 401; verify the length first if unsure
(`echo ${#GH_TOKEN}` should print 40).

Do not use the `github_*` MCP tools for these repositories. They use another
credential and return 404. Use curl with `Authorization: Bearer $GH_TOKEN`, or
the pinned checker at `/tmp/opencode/sa-check/tools/agent-coordination/cli.mjs`.

Never print, log or commit the token or the env file contents.

## Claims

Only SOL reconciles claims. Never release, steal or take over another run's
claim. If the issue already has an active claim that is not yours, stop and
report it.

Claim and check from the pinned checker checkout:

```bash
cd /tmp/opencode/sa-check
export GH_TOKEN=$(awk -F= '/^GITHUB_PROJECTS_TOKEN=/{print substr($0,index($0,"=")+1)}' /SOCIAL/configs/github.env)
node tools/agent-coordination/cli.mjs claim --plan /path/to/plan.json
node tools/agent-coordination/cli.mjs check --repository owner/repo --pr N
```

## Work

Work only in the checkout and branch SOL names. Keep the declared scope. Do not
merge, deploy, publish customer changes or mark anything Verified. Stop and ask
when scope, dependencies or a consequential assumption changes.

## Dependencies

`/tmp` is mounted `noexec`, so binaries installed under `/tmp/opencode/...`
cannot run (`esbuild` postinstall fails with EACCES). Install `node_modules` in
an exec-enabled location and symlink it into the worktree, for example:

```bash
mkdir -p /home/admin/luna-deps/<task> && cp package.json package-lock.json /home/admin/luna-deps/<task>/
(cd /home/admin/luna-deps/<task> && npm ci --no-audit --no-fund)
ln -sfn /home/admin/luna-deps/<task>/node_modules <worktree>/node_modules
```
