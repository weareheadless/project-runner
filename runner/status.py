#!/usr/bin/env python3
"""Human-readable runner state: suspension, active run, workers, last events."""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def age(ts):
    if not ts:
        return 'never'
    delta = time.time() - ts
    return f'{delta/60:.0f}m ago' if delta >= 0 else f'in {abs(delta)/60:.0f}m'


def main(project='site-agent'):
    state_dir = ROOT / '.tick' / project
    if not state_dir.exists():
        sys.exit(f'no runner state for {project}')
    state = json.loads((state_dir / 'supervisor.json').read_text()) \
        if (state_dir / 'supervisor.json').exists() else {}
    health = json.loads((state_dir / 'health.json').read_text()) \
        if (state_dir / 'health.json').exists() else {}
    workers = json.loads((state_dir / 'workers.json').read_text()) \
        if (state_dir / 'workers.json').exists() else {}

    print(f'project      {project}')
    print(f'health       {"ok" if health.get("ok") else "ERROR " + str(health.get("error"))}'
          f'  (checked {age(health.get("at"))})')
    if state.get('paused'):
        print('mode         PAUSED — needs a human; nothing is dispatched')
    elif state.get('quota'):
        hold = state['quota']
        print(f'mode         QUOTA SUSPENDED until {time.strftime("%H:%M:%S", time.localtime(hold["until"]))}'
              f' ({age(hold["until"])}, attempt {hold["attempt"]},'
              f' {"provider reset" if hold["explicit_reset"] else "bounded backoff"})')
        print(f'parked       {len(hold["sessions"])} session(s)')
    else:
        print('mode         RUNNING')
    active = state.get('active')
    if active:
        print(f'active       {active["id"]} (started {age(active["started"])})')
    if state.get('quota_recovering'):
        print(f'recovering   {", ".join(state["quota_recovering"])}')
    if workers:
        live = [w for w in workers.values() if not w.get('terminal')]
        print(f'workers      {len(workers)} total, {len(live)} unfinished')
        for run, w in workers.items():
            if not w.get('terminal'):
                print(f'  {run}  issue #{w["issue"]}  {w["effort"]}')
    outbox = state_dir / 'outbox'
    pending = sorted(p.name for p in outbox.glob('*.json')) if outbox.exists() else []
    print(f'outbox       {len(pending)} undelivered' + (f': {", ".join(pending)}' if pending else ''))


if __name__ == '__main__':
    main(*(sys.argv[1:2] or ['site-agent']))
