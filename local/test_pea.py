"""
Principal Edge Adapter Tests — 6 original + 8 response-binding tests
All use correct API from committed principal_edge.py
"""
import os, sys, uuid, json, secrets
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('SPHERA_TEST_PEA', '1')

from db import init, get_db, flush_outbox
from migrate import migrate
from principal_edge import (
    FakeAdapter, GmailBridgeAdapter,
    create_attempt_atomic, run_attempt,
    cas_transition, reconcile_nonterminal_attempts
)
from datetime import datetime, timezone, timedelta

DB_PATH = os.environ.get('SPHERA_DB', '/tmp/sphera_pea_test.db')
os.environ['SPHERA_DB'] = DB_PATH

init(); migrate()
with get_db() as db: flush_outbox(db); db.commit()

p=f=0
def ok(l,c,d=''):
    global p,f
    if c: print(f'  OK   {l}'+(f'  [{d}]' if d else '')); p+=1
    else: print(f'  FAIL {l}'+(f'  [{d}]' if d else '')); f+=1

now = datetime.now(timezone.utc).isoformat()
with get_db() as db:
    db.execute("INSERT OR IGNORE INTO workspaces VALUES('default','D','arcides',?)", (now,))
    db.execute("""INSERT OR REPLACE INTO edge_registry
        (edge_id,workspace_id,principal_id,surface,provider,capabilities,status,continuity_class,created_at,binding_version)
        VALUES('fake-edge-01','default','test-principal','fake','fake','[]','active','FAKE_TEST_ONLY',?,1)""", (now,))
    db.execute("""INSERT OR REPLACE INTO edge_registry
        (edge_id,workspace_id,principal_id,surface,provider,capabilities,status,continuity_class,created_at,binding_version)
        VALUES('gmail-claude-01','default','claude','gmail','gmail','[]','active','surrogate_transport',?,1)""", (now,))
    db.commit()

def make_work(db, cap='native_session', status='ready'):
    mid=str(uuid.uuid4()); wid=str(uuid.uuid4())
    db.execute("INSERT INTO missions(mission_id,objective,owner,status,policy_json,created_at,version) VALUES(?,'t','arcides','active','{}',datetime('now'),1)", (mid,))
    db.execute("""INSERT INTO work_items(workspace_id,work_id,mission_id,description,capability,
           deps_json,status,created_at,version,attempt_count,max_attempts,
           lease_fencing_token,work_generation,waiting_reason,updated_at)
           VALUES('default',?,?,'native work',?,'[]',?,datetime('now'),1,0,3,0,1,NULL,datetime('now'))""",
        (wid, mid, cap, status))
    return mid, wid

def run_n(aid, adapter, n=6):
    for _ in range(n):
        with get_db() as db:
            run_attempt(db, aid, adapter); db.commit()

def get_state(aid):
    with get_db() as db:
        row = db.execute("SELECT state,failure_reason FROM principal_edge_attempts WHERE attempt_id=?", (aid,)).fetchone()
        return (row['state'], row['failure_reason']) if row else (None, None)

def get_work_status(wid):
    with get_db() as db:
        row = db.execute("SELECT status FROM work_items WHERE work_id=?", (wid,)).fetchone()
        return row['status'] if row else None

print('=== PRINCIPAL EDGE ADAPTER — FULL TEST SUITE ===\n')

# ── Original T1-T6 ────────────────────────────────────────────────────────────
print('[T1] Boss-absence: attempt created with boss_causal_events=0')
with get_db() as db:
    mid,wid = make_work(db); db.commit()
with get_db() as db:
    aid = create_attempt_atomic(db, wid, mid, 'test-principal', 1); db.commit()
ok('T1: attempt created', bool(aid))
with get_db() as db:
    row=db.execute("SELECT state,boss_causal_events FROM principal_edge_attempts WHERE attempt_id=?",(aid,)).fetchone()
ok('T1: OBLIGATION_CREATED', row['state']=='OBLIGATION_CREATED')
ok('T1: boss_causal_events=0', row['boss_causal_events']==0)
fake = FakeAdapter()  # Uses class defaults: principal_id='test-principal', edge_id='fake-edge-01'
run_n(aid, fake)
state, _ = get_state(aid)
ok('T1: reached OBLIGATION_RESUMED', state=='OBLIGATION_RESUMED', f'state={state}')
ok('T1: response_artifact created', bool(get_db().execute("SELECT 1 FROM response_artifacts WHERE attempt_id=? AND response_verified=1",(aid,)).fetchone()))

print('\n[T2] No-edge: work stays blocked, not surrogate')
class NoEdgeAdapter(FakeAdapter):
    def select_edge(self, db, pid, gen=1): return None
with get_db() as db:
    mid,wid=make_work(db); db.commit()
with get_db() as db:
    aid=create_attempt_atomic(db,wid,mid,'test-principal',1); db.commit()
run_n(aid, NoEdgeAdapter())
state,_ = get_state(aid)
ok('T2: NO_EDGE', state=='NO_EDGE', f'state={state}')
ok('T2: work stays blocked', get_work_status(wid)!='ready', f'status={get_work_status(wid)}')

print('\n[T3] Replay: reused response_event_id rejected')
class ReplayAdapter(FakeAdapter):
    shared = None
    def observe_response(self, db, aid, nonce, edge_id=None):
        eid = str(uuid.uuid4())
        if not ReplayAdapter.shared: ReplayAdapter.shared = eid
        else: eid = ReplayAdapter.shared  # reuse
        return {"nonce_echo": nonce, "response_event_id": eid, "boss_causal_events": 0, "task_answer": {"done":True}}
with get_db() as db:
    mid1,wid1=make_work(db); mid2,wid2=make_work(db); db.commit()
with get_db() as db:
    aid1=create_attempt_atomic(db,wid1,mid1,'test-principal',1); db.commit()
with get_db() as db:
    aid2=create_attempt_atomic(db,wid2,mid2,'test-principal',1); db.commit()
run_n(aid1, ReplayAdapter())
run_n(aid2, ReplayAdapter())
s1,_ = get_state(aid1); s2,_ = get_state(aid2)
ok('T3: first accepted', s1=='OBLIGATION_RESUMED', f's1={s1}')
ok('T3: reused response_event_id blocked', s2 not in ('OBLIGATION_RESUMED','RESPONSE_ACCEPTED'), f's2={s2}')

print('\n[T4] Wrong-principal: cross-edge BINDING_FAILED')
class WrongEdgeAdapter(FakeAdapter):
    def select_edge(self, db, pid, gen=1):
        db.execute("INSERT OR REPLACE INTO edge_registry(edge_id,workspace_id,principal_id,surface,provider,capabilities,status,continuity_class,created_at,binding_version) VALUES('wrong-01','default','wrong-p','fake','fake','[]','active','FAKE_TEST_ONLY',datetime('now'),1)")
        return 'wrong-01'
with get_db() as db:
    mid,wid=make_work(db); db.commit()
with get_db() as db:
    aid=create_attempt_atomic(db,wid,mid,'test-principal',1); db.commit()
run_n(aid, WrongEdgeAdapter())
state,_ = get_state(aid)
ok('T4: BINDING_FAILED for wrong-principal', state=='BINDING_FAILED', f'state={state}')

print('\n[T5] E3_N unproven (Gmail): BLOCKED_NATIVE_WAKE, not surrogate')
class GmailDeliverFakeObserveAdapter(GmailBridgeAdapter):
    """Gmail that fakes delivery+observation but still fails E3_N (surrogate_transport)"""
    def emit_challenge(self, db, attempt_id, nonce, edge_id=None):
        return True  # Fake delivery
    def observe_response(self, db, aid, nonce, edge_id=None):
        return {"nonce_echo": nonce, "response_event_id": str(uuid.uuid4()), "boss_causal_events": 0, "task_answer": {"done":True}}
with get_db() as db:
    mid,wid=make_work(db); db.commit()
with get_db() as db:
    aid=create_attempt_atomic(db,wid,mid,'claude',1); db.commit()
ga = GmailDeliverFakeObserveAdapter(); ga.principal_id='claude'; ga.edge_id='gmail-claude-01'
run_n(aid, ga)
state,_ = get_state(aid)
ok('T5: BLOCKED_NATIVE_WAKE (E3_N unproven)', state=='BLOCKED_NATIVE_WAKE', f'state={state}')
ok('T5: work stays blocked', get_work_status(wid)=='blocked', f'status={get_work_status(wid)}')

print('\n[T6] Boss contamination: BOSS_CAUSALITY_PRESENT')
class BossAdapter(FakeAdapter):
    def observe_response(self, db, aid, nonce, edge_id=None):
        return {"nonce_echo": nonce, "response_event_id": str(uuid.uuid4()), "boss_causal_events": 1, "task_answer": {"done":True}}
with get_db() as db:
    mid,wid=make_work(db); db.commit()
with get_db() as db:
    aid=create_attempt_atomic(db,wid,mid,'test-principal',1); db.commit()
run_n(aid, BossAdapter())
state,_ = get_state(aid)
ok('T6: BOSS_CAUSALITY_PRESENT', state=='BOSS_CAUSALITY_PRESENT', f'state={state}')

# ── Response Binding T7-T14 ───────────────────────────────────────────────────
print('\n[T7] E3_N proven + no task_answer → RESPONSE_MISSING')
class WakeOnlyAdapter(FakeAdapter):
    def observe_response(self, db, aid, nonce, edge_id=None):
        return {"nonce_echo": nonce, "response_event_id": str(uuid.uuid4()), "boss_causal_events": 0}  # No task_answer
with get_db() as db:
    mid,wid=make_work(db); db.commit()
with get_db() as db:
    aid=create_attempt_atomic(db,wid,mid,'test-principal',1); db.commit()
run_n(aid, WakeOnlyAdapter())
state,_ = get_state(aid)
ok('T7: RESPONSE_MISSING', state=='RESPONSE_MISSING', f'state={state}')
ok('T7: work stays blocked', get_work_status(wid)=='blocked', f'status={get_work_status(wid)}')

print('\n[T8] Valid full response → OBLIGATION_RESUMED + artifact')
with get_db() as db:
    mid,wid=make_work(db); db.commit()
with get_db() as db:
    aid=create_attempt_atomic(db,wid,mid,'test-principal',1); db.commit()
run_n(aid, fake)
state,_ = get_state(aid)
ok('T8: OBLIGATION_RESUMED', state=='OBLIGATION_RESUMED', f'state={state}')
art = get_db().execute("SELECT count(*) FROM response_artifacts WHERE attempt_id=? AND response_verified=1",(aid,)).fetchone()[0]
ok('T8: exactly one artifact', art==1, f'count={art}')

print('\n[T9] Duplicate attempt for same work+generation → second returns None')
with get_db() as db:
    mid,wid=make_work(db); db.commit()
with get_db() as db:
    aid1=create_attempt_atomic(db,wid,mid,'test-principal',1); db.commit()
with get_db() as db:
    aid2=create_attempt_atomic(db,wid,mid,'test-principal',1); db.commit()
ok('T9: second attempt returns None (unique constraint)', aid2 is None, f'aid2={aid2}')

print('\n[T10] UNKNOWN boss_causal_events (-1) fails closed')
class UnknownBossAdapter(FakeAdapter):
    def observe_response(self, db, aid, nonce, edge_id=None):
        return {"nonce_echo": nonce, "response_event_id": str(uuid.uuid4()), "boss_causal_events": -1, "task_answer": {"done":True}}
with get_db() as db:
    mid,wid=make_work(db); db.commit()
with get_db() as db:
    aid=create_attempt_atomic(db,wid,mid,'test-principal',1); db.commit()
run_n(aid, UnknownBossAdapter())
state,reason = get_state(aid)
ok('T10: BOSS_CAUSALITY_PRESENT on -1', state=='BOSS_CAUSALITY_PRESENT', f'state={state} reason={reason}')

print('\n[T11] Nonce mismatch → STALE_OR_REPLAYED_EVIDENCE')
class WrongNonceAdapter(FakeAdapter):
    def observe_response(self, db, aid, nonce, edge_id=None):
        return {"nonce_echo": "WRONG_NONCE", "response_event_id": str(uuid.uuid4()), "boss_causal_events": 0, "task_answer": {"done":True}}
with get_db() as db:
    mid,wid=make_work(db); db.commit()
with get_db() as db:
    aid=create_attempt_atomic(db,wid,mid,'test-principal',1); db.commit()
run_n(aid, WrongNonceAdapter())
state,_ = get_state(aid)
ok('T11: nonce mismatch → STALE_OR_REPLAYED_EVIDENCE', state=='STALE_OR_REPLAYED_EVIDENCE', f'state={state}')

print('\n[T12] Reconciler: expired attempt → OBSERVATION_TIMEOUT, work stays blocked')
with get_db() as db:
    mid,wid=make_work(db); db.commit()
with get_db() as db:
    aid=create_attempt_atomic(db,wid,mid,'test-principal',1); db.commit()
    # Manually expire
    db.execute("UPDATE principal_edge_attempts SET state='CHALLENGE_EMITTED',challenge_nonce='abc',challenge_emitted_at=?,expires_at=? WHERE attempt_id=?",
               ('2020-01-01T00:00:00+00:00','2020-01-01T00:00:00+00:00',aid)); db.commit()
reconcile_nonterminal_attempts()
state,_ = get_state(aid)
ok('T12: OBSERVATION_TIMEOUT after reconcile', state=='OBSERVATION_TIMEOUT', f'state={state}')

print(f'\n{"="*50}')
print(f'RESULT: {p} passed, {f} failed')
if f==0: print('ALL GREEN')
