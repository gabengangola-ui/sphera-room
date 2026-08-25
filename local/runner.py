"""
SPHERA Mission Runner v1.1
- Heartbeat thread runs during work execution (prevents lease expiry mid-task)
- Stops heartbeat only after result is submitted
- Agent.run_once() is non-blocking; heartbeat runs in parallel
"""
import os, sys, time, json, threading, urllib.request, random
sys.path.insert(0, '/home/claude/sphera')

BASE     = os.environ.get('SPHERA_URL', 'http://127.0.0.1:8765')
POLL_SEC = float(os.environ.get('SPHERA_POLL', '2'))

def _call(method, path, body=None, key=''):
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
    def __init__(self, name, capabilities, key, work_fn=None):
        self.name         = name
        self.capabilities = capabilities
        self.key          = key
        self.work_fn      = work_fn or self._simulate
        self.agent_id     = None
        self.lease_id     = None
        self.current_work = None
        self._hb_stop     = threading.Event()

    def call(self, method, path, body=None):
        return _call(method, path, body, self.key)

    def register(self):
        r = self.call('POST', '/agent/register', {'name': self.name, 'capabilities': self.capabilities})
        if not r.get('ok'):
            raise RuntimeError(f'[{self.name}] registration failed: {r}')
        self.agent_id = r['agent_id']
        print(f'[{self.name}] registered {self.agent_id[:8]} caps:{self.capabilities}')

    def _simulate(self, work_item):
        """Default: simulate work. Real agents replace this with actual execution."""
        time.sleep(random.uniform(0.5, 1.2))
        return {'agent': self.name, 'output': f'completed: {work_item["description"]}', 'status': 'done'}

    def _heartbeat_loop(self, work_id, interval=20):
        """Runs in a thread. Sends heartbeat every `interval` seconds until stopped."""
        while not self._hb_stop.wait(timeout=interval):
            if not self.lease_id:
                break
            r = self.call('POST', f'/work/{work_id}/heartbeat',
                         {'lease_id': self.lease_id, 'extend_seconds': 60})
            if '_err' in r or '_exc' in r:
                print(f'[{self.name}] heartbeat failed: {r}')
                break

    def try_claim(self):
        if not self.agent_id:
            return False
        r = self.call('POST', f'/work/{self.agent_id}/auto-claim', {'agent_id': self.agent_id})
        if r.get('ok'):
            self.lease_id    = r['lease_id']
            self.current_work = r
            print(f'[{self.name}] claimed "{r["description"]}" [{r["capability"]}]')
            return True
        return False

    def execute_and_submit(self):
        """Execute work with heartbeat running in parallel, then submit result."""
        work = self.current_work
        work_id = work['work_id']

        # Start heartbeat thread before executing
        self._hb_stop.clear()
        hb = threading.Thread(target=self._heartbeat_loop, args=(work_id,), daemon=True)
        hb.start()

        try:
            result = self.work_fn(work)
        except Exception as e:
            result = {'error': str(e), 'status': 'failed'}
        finally:
            # Stop heartbeat before submitting result
            self._hb_stop.set()
            hb.join(timeout=2)

        r = self.call('POST', f'/work/{work_id}/result',
                     {'lease_id': self.lease_id, 'result': result})
        if r.get('ok'):
            print(f'[{self.name}] result seq:{r["seq"]} unblocked:{r.get("unblocked",[])}')
        else:
            print(f'[{self.name}] result error: {r}')

        self.lease_id    = None
        self.current_work = None

    def run_once(self):
        if self.current_work:
            self.execute_and_submit()
        else:
            self.try_claim()

    def run(self, max_cycles=0):
        print(f'[{self.name}] loop started (poll:{POLL_SEC}s)')
        cycles = 0
        while True:
            self.run_once()
            cycles += 1
            if max_cycles and cycles >= max_cycles:
                break
            time.sleep(POLL_SEC)


if __name__ == '__main__':
    CLAUDE_KEY  = os.environ['CLAUDE_KEY']
    SOBA_KEY    = os.environ['SOBA_KEY']
    ARCIDES_KEY = os.environ['ARCIDES_KEY']

    # Arcides gives one outcome once — mission decomposition is his job
    r = _call('POST', '/mission', {'objective': 'Ship SPHERA alpha'}, key=ARCIDES_KEY)
    mid = r['mission_id']
    print(f'Mission: {mid[:8]}')

    tasks = [
        ('Event ledger',  'backend'),
        ('Test suite',    'testing'),
        ('Deploy',        'devops'),
    ]
    wids = []
    for i, (desc, cap) in enumerate(tasks):
        deps = [wids[-1]] if wids else []
        r = _call('POST', f'/mission/{mid}/work',
                  {'description': desc, 'capability': cap, 'dependencies': deps}, key=ARCIDES_KEY)
        wids.append(r['work_id'])
        print(f'  [{r["status"]:7s}] {desc} [{cap}]')

    agents = [
        Agent('backend-bot', ['backend'],      CLAUDE_KEY),
        Agent('test-bot',    ['testing'],      SOBA_KEY),
        Agent('devops-bot',  ['devops'],       CLAUDE_KEY),
    ]
    print()
    for a in agents:
        a.register()

    print('\n--- autonomous execution ---')
    threads = [threading.Thread(target=a.run, kwargs={'max_cycles': 8}, daemon=True) for a in agents]
    for t in threads: t.start()

    for _ in range(20):
        time.sleep(1.5)
        r = _call('GET', '/room', key=CLAUDE_KEY)
        done = r['work']['done']
        total = len(wids)
        sys.stdout.write(f'\r  {done}/{total} done  events:{r["event_count"]}  ')
        sys.stdout.flush()
        if done >= total:
            break

    print('\n--- done ---')
    r = _call('GET', f'/mission/{mid}', key=CLAUDE_KEY)
    for w in r.get('done', []):
        res = json.loads(w['result']) if w.get('result') else {}
        print(f'  [done] {w["description"]} → {res.get("output","")[:50]}')
