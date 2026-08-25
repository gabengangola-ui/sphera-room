"""
SPHERA Mission Runner — autonomous agent loop.

Agents register themselves and continuously pull+execute work from the room.
This is what makes SPHERA autonomous: agents run, complete work, unblock dependencies,
and the mission advances without human coordination.
"""
import os, sys, time, json, uuid, random, urllib.request
sys.path.insert(0, '/home/claude/sphera')

BASE     = os.environ.get('SPHERA_URL', 'http://127.0.0.1:8765')
POLL_SEC = float(os.environ.get('SPHERA_POLL', '2'))

def call(method, path, body=None, key=''):
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(f'{BASE}{path}', data=data, method=method,
           headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try: return {'_err': e.code, **json.loads(e.read())}
        except: return {'_err': e.code}
    except Exception as ex:
        return {'_exc': str(ex)}

class Agent:
    def __init__(self, name: str, capabilities: list, key: str, work_fn=None):
        self.name         = name
        self.capabilities = capabilities
        self.key          = key
        self.work_fn      = work_fn or self._default_work
        self.agent_id     = None
        self.lease_id     = None
        self.current_work = None

    def register(self):
        r = call('POST', '/agent/register', {'name': self.name, 'capabilities': self.capabilities}, key=self.key)
        if r.get('ok'):
            self.agent_id = r['agent_id']
            print(f'[{self.name}] registered {self.agent_id[:8]}... caps:{self.capabilities}')
        else:
            raise RuntimeError(f'Registration failed: {r}')

    def _default_work(self, work_item: dict) -> dict:
        """Simulate doing work — real agents replace this."""
        time.sleep(random.uniform(0.5, 1.5))  # simulate work duration
        return {
            'status': 'completed',
            'agent': self.name,
            'work_id': work_item['work_id'],
            'description': work_item['description'],
            'output': f'{self.name} completed: {work_item["description"]}'
        }

    def try_claim(self) -> bool:
        """Try to auto-claim work matching this agent's capabilities."""
        r = call('POST', f'/work/{self.agent_id}/auto-claim', {'agent_id': self.agent_id}, key=self.key)
        if r.get('ok'):
            self.lease_id    = r['lease_id']
            self.current_work = r
            print(f'[{self.name}] claimed: "{r["description"]}" [{r["capability"]}]')
            return True
        return False

    def heartbeat(self):
        if not self.current_work or not self.lease_id: return
        wid = self.current_work['work_id']
        call('POST', f'/work/{wid}/heartbeat', {'lease_id': self.lease_id, 'extend_seconds': 60}, key=self.key)

    def submit_result(self, result: dict):
        wid = self.current_work['work_id']
        r = call('POST', f'/work/{wid}/result', {'lease_id': self.lease_id, 'result': result}, key=self.key)
        if r.get('ok'):
            print(f'[{self.name}] result submitted seq:{r["seq"]} unblocked:{r.get("unblocked",[])}')
        else:
            print(f'[{self.name}] result error: {r}')
        self.lease_id    = None
        self.current_work = None

    def run_once(self):
        """Single poll cycle."""
        if self.current_work:
            # Execute current work
            result = self.work_fn(self.current_work)
            self.submit_result(result)
        else:
            self.try_claim()

    def run(self, max_cycles: int = 0):
        """Continuous agent loop. Set max_cycles>0 for finite test runs."""
        print(f'[{self.name}] starting loop (poll:{POLL_SEC}s)')
        cycles = 0
        while True:
            self.run_once()
            cycles += 1
            if max_cycles and cycles >= max_cycles:
                break
            time.sleep(POLL_SEC)


# ── Demo: run a full mission autonomously ─────────────────────────────────────
if __name__ == '__main__':
    import threading

    CLAUDE_KEY  = os.environ['CLAUDE_KEY']
    SOBA_KEY    = os.environ['SOBA_KEY']
    ARCIDES_KEY = os.environ['ARCIDES_KEY']

    print('=== SPHERA AUTONOMOUS MISSION RUNNER ===\n')

    # Create mission
    r = call('POST', '/mission', {'objective': 'Autonomous SPHERA build sprint'}, key=ARCIDES_KEY)
    mid = r['mission_id']
    print(f'Mission: {mid[:8]}...\n')

    # Decompose into work items
    sprint = [
        ('Implement event ledger',     'backend'),
        ('Write integration tests',    'testing'),
        ('Build room UI',              'frontend'),
        ('Deploy to production',       'devops'),
    ]
    wids = []
    for i, (desc, cap) in enumerate(sprint):
        deps = [wids[i-1]] if i > 0 else []
        r = call('POST', f'/mission/{mid}/work', {'description': desc, 'capability': cap, 'dependencies': deps}, key=ARCIDES_KEY)
        wids.append(r['work_id'])
        print(f'  [{r["status"]:7s}] {desc} [{cap}]')

    print()

    # Create agents
    agents = [
        Agent('backend-bot',  ['backend','api'],  CLAUDE_KEY),
        Agent('test-bot',     ['testing'],        CLAUDE_KEY),
        Agent('frontend-bot', ['frontend'],       SOBA_KEY),
        Agent('devops-bot',   ['devops'],         SOBA_KEY),
    ]

    for agent in agents:
        agent.register()

    print('\n--- AUTONOMOUS EXECUTION ---')

    # Run all agents concurrently for up to 10 cycles each
    threads = [threading.Thread(target=a.run, kwargs={'max_cycles': 10}, daemon=True) for a in agents]
    for t in threads: t.start()

    # Monitor until mission complete
    for _ in range(30):
        time.sleep(1)
        r = call('GET', '/room', key=CLAUDE_KEY)
        done = r['work']['done']
        total = len(wids)
        print(f'  Progress: {done}/{total} done  leased:{r["work"]["leased"]}  events:{r["event_count"]}')
        if done >= total:
            break

    # Final state
    r = call('GET', f'/mission/{mid}', key=CLAUDE_KEY)
    print(f'\n=== MISSION COMPLETE ===')
    print(f'  Objective: {r["mission"]["objective"]}')
    print(f'  Done: {len(r["done"])}/{r["total"]}')
    for w in r['done']:
        result = json.loads(w['result']) if w['result'] else {}
        print(f'  [done] {w["description"]} → {result.get("output","")[:50]}')
