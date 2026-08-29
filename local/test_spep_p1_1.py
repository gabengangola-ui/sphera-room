"""
SPEP Phase 1.1 Acceptance Tests — Soba's T1-T7
"""
import os, sys, uuid, json, threading, time, urllib.request
sys.path.insert(0, '/home/claude/sphera')
os.environ.update({'CLAUDE_KEY':'ck-spep','SOBA_KEY':'sk-spep','ARCIDES_KEY':'ak-spep',
                   'BRIDGE_KEY':'br-spep','SPHERA_DB':'/tmp/spep_test.db'})
from db import init, get_db, flush_outbox
from migrate import migrate
from server import app
import uvicorn

init(); migrate()
with get_db() as db: flush_outbox(db); db.commit()

srv = threading.Thread(target=uvicorn.run,
      kwargs={'app':app,'host':'127.0.0.1','port':8820,'log_level':'error'}, daemon=True)
srv.start(); time.sleep(2)

def call(method, path, body=None, key='ak-spep'):
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(f'http://127.0.0.1:8820{path}', data=data, method=method,
           headers={'Authorization':f'Bearer {key}','Content-Type':'application/json'})
    try:
        r = urllib.request.urlopen(req, timeout=5)
        return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        try: return json.loads(e.read()), e.code
        except: return {}, e.code

p = f = 0
def ok(l, c, d=''):
    global p,f
    if c: print(f'  OK   {l}' + (f'  [{d}]' if d else '')); p+=1
    else: print(f'  FAIL {l}' + (f'  [{d}]' if d else '')); f+=1

print('=== SPEP Phase 1.1 Acceptance Tests ===\n')

# Register test edges
r, s = call('POST', '/edge/register', {
    'edge_id':'gmail-claude-01','principal_id':'claude',
    'surface':'gmail','provider':'gmail','continuity_class':'surrogate_transport'
})
ok('Register gmail-claude-01', s==201, f'r={r}')
ver1 = r.get('binding_version', 0)
ok('binding_version >= 1 on register (bootstrap may pre-exist)', ver1>=1, f'ver={ver1}')

r, s = call('POST', '/edge/register', {
    'edge_id':'gmail-soba-01','principal_id':'soba',
    'surface':'gmail','provider':'gmail','continuity_class':'surrogate_transport'
})
ok('Register gmail-soba-01', s==201)

# T1: Soba bearer -> heartbeat Claude edge = 403
print('\n[T1] Cross-principal heartbeat rejected')
r, s = call('POST', '/edge/gmail-claude-01/heartbeat', {'lease_seconds':60}, key='sk-spep')
ok('T1: Soba bearer → Claude edge = 403', s==403, f'status={s} r={r}')
# Verify no state mutation
with get_db() as db:
    row = db.execute("SELECT trust_state FROM principal_edge_state WHERE edge_id='gmail-claude-01'").fetchone()
ok('T1: state NOT mutated after 403', row is None or row['trust_state']=='BOUND_DORMANT',
   f'state={row["trust_state"] if row else "None"}')

# T2: Revoked edge heartbeat rejected
print('\n[T2] Revoked edge heartbeat rejected')
r, s = call('POST', '/edge/gmail-claude-01/heartbeat', {'lease_seconds':60}, key='ck-spep')
ok('Claude heartbeat while active succeeds', s==200, f's={s}')
r, s = call('POST', '/edge/gmail-claude-01/revoke', key='ak-spep')
ok('Revoke edge succeeds', s==200)
r, s = call('POST', '/edge/gmail-claude-01/heartbeat', {'lease_seconds':60}, key='ck-spep')
ok('T2: Revoked edge heartbeat = 403', s==403, f's={s}')

# T3: Stale binding_version rejected after rebind
print('\n[T3] Stale binding_version rejected')
r, s = call('POST', '/edge/register', {
    'edge_id':'gmail-claude-01','principal_id':'claude',
    'surface':'gmail','continuity_class':'surrogate_transport'
})
ver2 = r.get('binding_version', 0)
ok('T7: Rebind increments version', ver2 == ver1+1, f'ver1={ver1} ver2={ver2}')
# Try heartbeat with old version
r, s = call('POST', '/edge/gmail-claude-01/heartbeat',
            {'lease_seconds':60,'binding_version':ver1}, key='ck-spep')
ok('T3: Stale version heartbeat = 409', s==409, f's={s} r={r}')
# Correct version works
r, s = call('POST', '/edge/gmail-claude-01/heartbeat',
            {'lease_seconds':60,'binding_version':ver2}, key='ck-spep')
ok('Correct version heartbeat succeeds', s==200, f's={s}')

# T4: Heartbeat does NOT promote Principal to REACHABLE
print('\n[T4] Heartbeat = transport UP, principal stays DORMANT')
with get_db() as db:
    row = db.execute(
        "SELECT trust_state, wake_capable FROM principal_edge_state WHERE edge_id='gmail-claude-01'"
    ).fetchone()
ok('T4: trust_state=TRANSPORT_UP not REACHABLE', row and row['trust_state']=='TRANSPORT_UP',
   f'state={row["trust_state"] if row else None}')
ok('T4: wake_capable=0 (UNPROVEN)', row and row['wake_capable']==0,
   f'wake={row["wake_capable"] if row else None}')

# T5: Client-declared wake_capable=1 not persisted
print('\n[T5] wake_capable cannot be self-declared')
r, s = call('POST', '/edge/gmail-claude-01/heartbeat',
            {'lease_seconds':60,'wake_capable':1,'binding_version':ver2}, key='ck-spep')
ok('Heartbeat with wake_capable=1 accepted (but ignored)', s==200)
with get_db() as db:
    row = db.execute(
        "SELECT wake_capable FROM principal_edge_state WHERE edge_id='gmail-claude-01'"
    ).fetchone()
ok('T5: wake_capable remains 0 despite request', row and row['wake_capable']==0,
   f'wake={row["wake_capable"] if row else None}')
ok('T5: response shows wake_capable=UNPROVEN', r.get('wake_capable')=='UNPROVEN', f'r={r}')

# T6: Duplicate event idempotent (bridge/ingest dedup)
print('\n[T6] Duplicate source_message_id idempotent')
src = f'test-src-{uuid.uuid4()}'
r1, s1 = call('POST', '/bridge/ingest', {
    'principal':'claude','content':'test','source_message_id':src,'transport':'test'
}, key='br-spep')
r2, s2 = call('POST', '/bridge/ingest', {
    'principal':'claude','content':'test','source_message_id':src,'transport':'test'
}, key='br-spep')
ok('T6: First ingest creates event', s1==201, f's={s1}')
ok('T6: Second ingest is duplicate', r2.get('duplicate')==True or s2==200, f's={s2} r={r2}')
ok('T6: Same seq returned', r1.get('seq')==r2.get('seq'), f'seq1={r1.get("seq")} seq2={r2.get("seq")}')

# T7: binding_version monotonic (already verified above)
print('\n[T7] binding_version monotonic')
ok('T7: ver2 > ver1 (monotonic increment)', ver2 > ver1, f'ver1={ver1} ver2={ver2}')
r, s = call('POST', '/edge/register', {
    'edge_id':'gmail-claude-01','principal_id':'claude','surface':'gmail','continuity_class':'surrogate_transport'
})
ver3 = r.get('binding_version', 0)
ok('T7: Third registration ver3 > ver2', ver3 > ver2, f'ver2={ver2} ver3={ver3}')

print(f'\n{"="*50}')
print(f'RESULT: {p} passed, {f} failed')
if f==0: print('SPEP PHASE 1.1 — ALL GREEN')
