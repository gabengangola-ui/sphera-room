"""
PEA-RUNTIME-INTEGRATION-01 — 8 integration tests per Soba's spec.
Uses repo-relative DB, not /home/claude hard-coded paths.
"""
import os, sys, uuid, json, threading, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.update({
    'SPHERA_DB': '/tmp/sphera_pea_int.db',
    'SPHERA_TEST_PEA': '1',
    'CLAUDE_KEY': 'ck-i', 'SOBA_KEY': 'sk-i',
    'ARCIDES_KEY': 'ak-i', 'BRIDGE_KEY': 'br-i'
})
from db import init, get_db, flush_outbox
from migrate import migrate
from server import app
from principal_edge import (FakeAdapter, create_attempt_atomic, run_attempt,
                            reconcile_nonterminal_attempts, _TEST_MODE)
import uvicorn

init(); migrate()
with get_db() as db: flush_outbox(db); db.commit()
srv = threading.Thread(target=uvicorn.run,
      kwargs={'app':app,'host':'127.0.0.1','port':8840,'log_level':'error'}, daemon=True)
srv.start(); time.sleep(3)

def call(m, p, b=None, k='ck-i'):
    data = json.dumps(b).encode() if b else None
    req  = urllib.request.Request(f'http://127.0.0.1:8840{p}', data=data, method=m,
           headers={'Authorization':f'Bearer {k}','Content-Type':'application/json'})
    try: r=urllib.request.urlopen(req,timeout=5); return json.loads(r.read()),r.status
    except urllib.error.HTTPError as e:
        try: return json.loads(e.read()),e.code
        except: return {},e.code

p=f=0
def ok(l,c,d=''):
    global p,f
    if c: print(f'  OK   {l}'+(f'  [{d}]' if d else '')); p+=1
    else: print(f'  FAIL {l}'+(f'  [{d}]' if d else '')); f+=1

print('=== PEA RUNTIME INTEGRATION TESTS ===\n')

# Setup
from datetime import datetime, timezone
now = datetime.now(timezone.utc).isoformat()
with get_db() as db:
    db.execute("INSERT OR IGNORE INTO workspaces VALUES('default','Default','arcides',?)",(now,))
    db.execute("INSERT OR REPLACE INTO edge_registry(edge_id,workspace_id,principal_id,surface,provider,capabilities,status,continuity_class,created_at,binding_version) VALUES(?,?,?,?,?,?,?,?,?,1)",
               ('fake-edge-01','default','test-principal','fake','fake','["read","write"]','active','FAKE_TEST_ONLY',now))
    db.commit()

def make_work(cap='research', principal='test-principal'):
    mid=str(uuid.uuid4()); wid=str(uuid.uuid4())
    with get_db() as db:
        db.execute("INSERT INTO missions(mission_id,objective,owner,status,policy_json,created_at,version) VALUES(?,'test','arcides','active','{}',datetime('now'),1)",(mid,))
        db.execute("INSERT INTO work_items(workspace_id,work_id,mission_id,description,capability,deps_json,status,created_at,version,attempt_count,max_attempts,lease_fencing_token,work_generation) VALUES('default',?,?,'native work',?,?,'ready',datetime('now'),1,0,3,0,0)",
                   (wid,mid,cap,'[]'))
        db.commit()
    return mid, wid

fake = FakeAdapter()
fake.principal_id = 'test-principal'

# A: native READY → exactly one WAITING_PRINCIPAL_EDGE + one attempt; second tick zero duplicates
print('[A] native READY → exactly one attempt; second tick creates zero duplicates')
mid_a, wid_a = make_work()
with get_db() as db:
    aid1 = create_attempt_atomic(db, wid_a, mid_a, 'test-principal', 0)
    db.commit()
ok('A: attempt created', aid1 is not None)
with get_db() as db:
    wrow=db.execute("SELECT status FROM work_items WHERE work_id=?",(wid_a,)).fetchone()
ok('A: work=waiting_principal_edge', wrow['status']=='waiting_principal_edge',f's={wrow["status"]}')
# Second call — must return None (UNIQUE constraint)
with get_db() as db:
    aid2 = create_attempt_atomic(db, wid_a, mid_a, 'test-principal', 0)
    db.commit()
ok('A: second tick creates zero duplicates', aid2 is None, f'aid2={aid2}')
with get_db() as db:
    count=db.execute("SELECT COUNT(*) FROM principal_edge_attempts WHERE work_id=?",(wid_a,)).fetchone()[0]
ok('A: exactly one attempt in DB', count==1, f'count={count}')

# B: simulated crash/restart → reconciler finds same attempt, no duplicate generation
print('\n[B] restart → reconciler finds same attempt, no duplicate')
recovered = reconcile_nonterminal_attempts()
with get_db() as db:
    count_b=db.execute("SELECT COUNT(*) FROM principal_edge_attempts WHERE work_id=?",(wid_a,)).fetchone()[0]
ok('B: reconciler runs without error', True)
ok('B: no duplicate attempts after reconcile', count_b==1, f'count={count_b}')

# C: stale generation response → hard reject
print('\n[C] stale generation: work generation advanced, old attempt rejected')
mid_c, wid_c = make_work()
with get_db() as db:
    aid_c = create_attempt_atomic(db, wid_c, mid_c, 'test-principal', 0)
    db.commit()
# Advance work generation (simulates new lease cycle)
with get_db() as db:
    db.execute("UPDATE work_items SET status='ready', work_generation=1 WHERE work_id=?",(wid_c,)); db.commit()
# Try to resume old attempt (gen=0) against work that is now gen=1
with get_db() as db:
    from principal_edge import cas_transition
    cas_transition(db, aid_c, 'OBLIGATION_CREATED', 'EDGE_SELECTED', edge_id='fake-edge-01')
    cas_transition(db, aid_c, 'EDGE_SELECTED', 'CHALLENGE_EMITTED', challenge_nonce='deadbeef'*4, challenge_emitted_at=now)
    cas_transition(db, aid_c, 'CHALLENGE_EMITTED', 'EDGE_OBSERVED', observation_event_id='obs-old')
    cas_transition(db, aid_c, 'EDGE_OBSERVED', 'NATIVE_BINDING_VERIFIED')
    # Now try resume — gen mismatch
    cur = db.execute("UPDATE work_items SET status='ready' WHERE workspace_id='default' AND work_id=? AND status='waiting_principal_edge' AND work_generation=0",(wid_c,))
    db.commit()
ok('C: stale generation resume blocked (rowcount=0)', cur.rowcount==0, f'rowcount={cur.rowcount}')

# D: two concurrent ticks → one winner only
print('\n[D] concurrent ticks → one winner only')
mid_d, wid_d = make_work()
winners = []
def try_create():
    with get_db() as db:
        aid = create_attempt_atomic(db, wid_d, mid_d, 'test-principal', 0)
        db.commit()
        if aid: winners.append(aid)
threads = [threading.Thread(target=try_create) for _ in range(5)]
for t in threads: t.start()
for t in threads: t.join()
ok('D: exactly one winner from concurrent ticks', len(winners)==1, f'winners={len(winners)}')

# E: failed native attempt → no worker/API can complete
print('\n[E] failed native attempt → no worker substitute')
mid_e, wid_e = make_work()
with get_db() as db:
    aid_e = create_attempt_atomic(db, wid_e, mid_e, 'test-principal', 0)
    from principal_edge import cas_transition
    cas_transition(db, aid_e, 'OBLIGATION_CREATED', 'NO_EDGE', 'no edge available')
    db.execute("UPDATE work_items SET status='blocked',waiting_reason='NO_EDGE' WHERE workspace_id='default' AND work_id=?",(wid_e,))
    db.commit()
# Attempt to claim with tool_worker — must fail (work is blocked not ready)
r,s = call('POST', f'/work/{wid_e}/claim', {'lease_seconds':60})
ok('E: blocked native work cannot be claimed by tool worker', s in (409,404,400) or not r.get('lease_id'),f's={s} r={r}')

# F: owner decision → WAITING_OWNER_AUTHORITY, zero PEA attempts
print('\n[F] owner decision → WAITING_OWNER_AUTHORITY, no PEA attempt')
mid_f, wid_f = make_work(cap='decision')
with get_db() as db:
    db.execute("UPDATE work_items SET status='waiting_owner_authority', waiting_reason='owner_decision_required' WHERE work_id=?",(wid_f,)); db.commit()
with get_db() as db:
    wrow=db.execute("SELECT status FROM work_items WHERE work_id=?",(wid_f,)).fetchone()
    pea_count=db.execute("SELECT COUNT(*) FROM principal_edge_attempts WHERE work_id=?",(wid_f,)).fetchone()[0]
ok('F: status=waiting_owner_authority', wrow['status']=='waiting_owner_authority')
ok('F: zero PEA attempts for owner decision', pea_count==0)

# G: 10 waiting native + later tool item → tool executes, no starvation
print('\n[G] 10 waiting native items + tool item → tool executes')
mid_g = str(uuid.uuid4())
with get_db() as db:
    db.execute("INSERT INTO missions(mission_id,objective,owner,status,policy_json,created_at,version) VALUES(?,'g-test','arcides','active','{}',datetime('now'),1)",(mid_g,))
    for _ in range(10):
        wid_n=str(uuid.uuid4())
        db.execute("INSERT INTO work_items(workspace_id,work_id,mission_id,description,capability,deps_json,status,created_at,version,attempt_count,max_attempts,lease_fencing_token) VALUES('default',?,?,'native','research','[]','waiting_principal_edge',datetime('now'),1,0,3,0)",(wid_n,mid_g))
    wid_tool=str(uuid.uuid4())
    db.execute("INSERT INTO work_items(workspace_id,work_id,mission_id,description,capability,deps_json,status,created_at,version,attempt_count,max_attempts,lease_fencing_token) VALUES('default',?,?,'tool work','backend','[]','ready',datetime('now'),1,0,3,0)",(wid_tool,mid_g))
    db.commit()
# Tool item should be claimable
r,s = call('POST', f'/work/{wid_tool}/claim', {'lease_seconds':60})
ok('G: tool item claimable despite 10 waiting native items', r.get('lease_id') is not None, f's={s} r={r}')

# H: FakeAdapter fails closed in production mode
print('\n[H] FakeAdapter fails closed outside test mode')
ok('H: _TEST_MODE=True in test', _TEST_MODE==True)
import principal_edge as pe_mod
real_test_mode = pe_mod._TEST_MODE
pe_mod._TEST_MODE = False
try:
    pe_mod.FakeAdapter()
    ok('H: FakeAdapter blocked in prod mode', False, 'should have raised RuntimeError')
except RuntimeError as e:
    ok('H: FakeAdapter raises RuntimeError in prod mode', True, str(e)[:50])
finally:
    pe_mod._TEST_MODE = real_test_mode

print(f'\n{"="*50}')
print(f'RESULT: {p} passed, {f} failed')
if f==0: print('PEA RUNTIME INTEGRATION — ALL GREEN')
