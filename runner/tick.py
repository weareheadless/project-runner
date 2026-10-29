#!/usr/bin/env python3
"""One tick of the project runner: board -> dispatchable work.

Config-driven. Reads projects/<name>.json. Read-only against GitHub: it never
changes board state, claims, branches or PRs. The tick session does the writes
that the rules allow; this script only answers "what can be dispatched now".

  python3 tick.py --project site-agent
  python3 tick.py --project site-agent --json
  python3 tick.py --project site-agent --reconcile   # stale in-progress audit
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLAIM_RE = re.compile(r"<!--\s*agent-claim\s*(\{.*?\})\s*-->", re.S)
WORK_ID_RE = re.compile(r"\[([A-Z]+-\d+)\]")


class Ctx:
    def __init__(self, cfg):
        self.cfg = cfg
        self.repo = cfg["repo"]
        self.owner, self.name = cfg["project"]["owner"], cfg["project"]["number"]
        self.state = ROOT / ".tick" / cfg["name"]
        self.state.mkdir(parents=True, exist_ok=True)
        self.tok = self._token()

    def _token(self):
        t = self.cfg["token"]
        env = {}
        for line in open(t["file"]):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"')
        tok = env.get(t["key"], "")
        if len(tok) < 20:
            sys.exit(f"{t['key']} missing from {t['file']}")
        return tok

    def api(self, url, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Authorization": "Bearer " + self.tok,
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json",
                     "User-Agent": "project-runner"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=45) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code in (403, 429, 502, 503) and attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
            except (TimeoutError, urllib.error.URLError, OSError):
                if attempt < 3:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise
        raise RuntimeError("unreachable")

    def gql(self, query, variables=None):
        r = self.api("https://api.github.com/graphql", "POST",
                     {"query": query, "variables": variables or {}})
        if "errors" in r:
            raise RuntimeError(json.dumps(r["errors"])[:400])
        return r["data"]


ITEMS_Q = """
query($owner:String!,$num:Int!,$cursor:String){
  user(login:$owner){ projectV2(number:$num){
    items(first:100, after:$cursor){
      pageInfo{hasNextPage endCursor}
      nodes{
        id
        content{ __typename
          ... on Issue{ number title state updatedAt url
            labels(first:10){nodes{name}} } }
        fieldValues(first:40){nodes{
          ... on ProjectV2ItemFieldSingleSelectValue{ name
            field{ ... on ProjectV2FieldCommon{ name } } } } }
      } } } } }
"""


def board(ctx):
    out, cursor = [], None
    while True:
        page = ctx.gql(ITEMS_Q, {"owner": ctx.owner, "num": ctx.name,
                                 "cursor": cursor})["user"]["projectV2"]["items"]
        out.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            return out
        cursor = page["pageInfo"]["endCursor"]


def fields(node):
    return {v["field"]["name"]: v["name"]
            for v in node["fieldValues"]["nodes"] if v.get("field")}


def claim_state(ctx, issue):
    comments = ctx.api(f"https://api.github.com/repos/{ctx.repo}/issues/{issue}"
                       f"/comments?per_page=50&sort=created&direction=desc")
    active = None
    for c in sorted(comments, key=lambda c: c["id"]):
        for m in CLAIM_RE.finditer(c.get("body") or ""):
            try:
                ev = json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
            if ev.get("event") == "claim":
                active = ev.get("agentRun")
            elif ev.get("event") == "release" and ev.get("agentRun") == active:
                active = None
    return ("claim", active) if active else ("none", None)


def open_prs(ctx):
    prs = ctx.api(f"https://api.github.com/repos/{ctx.repo}/pulls"
                  f"?state=open&per_page=100")
    return [{"number": p["number"], "head": p["head"]["ref"],
             "draft": p["draft"], "title": p["title"]} for p in prs]


def branches(ctx):
    try:
        return [b["name"] for b in
                ctx.api(f"https://api.github.com/repos/{ctx.repo}"
                        f"/branches?per_page=100")]
    except Exception:
        return []


def head_sha(ctx, repo, branch="main"):
    o, n = repo.split("/")
    try:
        d = ctx.gql('query{repository(owner:"%s",name:"%s"){'
                    'defaultBranchRef{target{... on Commit{oid}}}}}' % (o, n))
        return d["repository"]["defaultBranchRef"]["target"]["oid"]
    except Exception:
        return None


def slug(title, work_id, prefix):
    tail = WORK_ID_RE.sub("", title).lower()
    tail = re.sub(r"[^a-z0-9]+", "-", tail).strip("-")[:48]
    return f"{prefix}{work_id.lower()}-{tail}" if work_id else f"{prefix}{tail}"


def collect(ctx):
    items, prs = board(ctx), open_prs(ctx)
    in_progress, ready = [], []
    for n in items:
        c = n.get("content") or {}
        if c.get("__typename") != "Issue" or c.get("state") != "OPEN":
            continue
        f = fields(n)
        m = WORK_ID_RE.search(c["title"])
        row = {"issue": c["number"], "title": c["title"],
               "work_id": f.get("Work ID") or (m.group(1) if m else None),
               "status": f.get("Status"), "readiness": f.get("Readiness"),
               "phase": f.get("Phase"), "area": f.get("Area"),
               "priority": f.get("Priority"),
               "updated": (c.get("updatedAt") or "")[:10]}
        if row["status"] == "In progress":
            in_progress.append(row)
        elif row["status"] in ("Ready", "Backlog") and row["readiness"] == "Ready":
            ready.append(row)
    return in_progress, ready, prs


def reconcile(ctx):
    in_progress, _, prs = collect(ctx)
    heads = {p["head"] for p in prs}
    brs = branches(ctx)
    stale, no_pr = [], []
    for r in in_progress:
        st, run = claim_state(ctx, r["issue"])
        wid = (r["work_id"] or "").lower()
        has_pr = any(wid and wid in h.lower() for h in heads)
        has_br = any(wid and wid in b.lower() for b in brs)
        r.update(claim=st, agent_run=run, open_pr=has_pr, branch=has_br)
        if not has_pr:
            no_pr.append(r)
            if not has_br:
                stale.append(r)
    print(f"in progress: {len(in_progress)}  no open PR: {len(no_pr)}  "
          f"no branch either: {len(stale)}\n")
    for r in sorted(no_pr, key=lambda x: x["updated"]):
        print(f"#{r['issue']:4} {r['updated']} {(r['work_id'] or '-'):8} "
              f"claim={r['claim']:8} branch={'yes' if r['branch'] else 'no ':3} "
              f"{r['title'][:50]}")
    (ctx.state / "reconcile.json").write_text(json.dumps(no_pr, indent=2))
    print(f"\nwritten {ctx.state / 'reconcile.json'}")
    print("advisory only: this script never releases a claim")


def dispatch(ctx, as_json):
    _, ready, _ = collect(ctx)
    picks = [r for r in ready if claim_state(ctx, r["issue"])[0] != "claim"]
    picks.sort(key=lambda r: (r["priority"] or "P9", r["issue"]))
    picks = picks[:ctx.cfg["max_dispatch"]]

    base = head_sha(ctx, ctx.repo, ctx.cfg.get("baseline_branch", "main")) if picks else None
    contract = head_sha(ctx, ctx.cfg["contract"]["repository"]) if picks else None
    stamp = time.strftime("%Y%m%dt%H%Mz", time.gmtime())
    plan = [{
        "workId": r["work_id"],
        "repository": ctx.repo,
        "issueNumber": r["issue"],
        "agentRun": f"luna-{(r['work_id'] or r['issue']).__str__().lower()}-{stamp}",
        "branch": slug(r["title"], r["work_id"], ctx.cfg.get("branch_prefix", "codex/")),
        "baseline": base,
        "contract": {"repository": ctx.cfg["contract"]["repository"], "sha": contract},
        "scope": ctx.cfg["scope"],
        "worktree": f"{ctx.cfg['worktree_root']}/{ctx.cfg['name']}-"
                    f"{(r['work_id'] or r['issue']).__str__().lower()}",
    } for r in picks]

    payload = {"project": ctx.cfg["name"],
               "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "ready_total": len(ready), "dispatch": plan}
    (ctx.state / "dispatch.json").write_text(json.dumps(payload, indent=2))

    if as_json:
        print(json.dumps(payload, indent=2))
        return
    print(f"ready+unclaimed: {len(ready)}  dispatching: {len(plan)}\n")
    for p, r in zip(plan, picks):
        print(f"#{p['issueNumber']:4} {p['workId']:8} {p['branch'][:44]:46} "
              f"{r['title'][:40]}")
    if not plan:
        print("nothing dispatchable this tick")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="site-agent")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--reconcile", action="store_true")
    a = ap.parse_args()

    cfg_path = ROOT / "projects" / f"{a.project}.json"
    if not cfg_path.exists():
        sys.exit(f"no config at {cfg_path}")
    ctx = Ctx(json.loads(cfg_path.read_text()))

    lock = ctx.state / "lock"
    now = time.time()
    if lock.exists() and now - lock.stat().st_mtime < ctx.cfg.get("max_tick_seconds", 5400):
        sys.exit("another tick is running for this project")
    lock.write_text(str(now))
    try:
        reconcile(ctx) if a.reconcile else dispatch(ctx, a.json)
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
