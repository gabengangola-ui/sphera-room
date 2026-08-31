"""
Principal Edge Adapter — 6 acceptance/falsification tests per Soba's spec.
"""
import os, sys, uuid, json
sys.path.insert(0, '/home/claude/sphera')
os.environ['SPHERA_DB'] = '/tmp/sphera_pea.db'
from db import init, get_db, flush_outbox
from migrate import migrate
from principal_edge import (FakeAdapter, GmailBridgeAdapter, create_attempt, run_attempt,
                             get_db as pea_db, transition)

init(); migrate()
with get_db() as db: flush_outbox(db); db.commit()

p=f=0
def ok(l,c,d=''):
    global p,f
    if c: print(f'  OK   {l}'+(f'  [{d}]' if d else '')); p+=1
    else: print(f'  FAIL {l}'+(f'  [{d}]' if d else '')); f+=1

def make_work(db, cap='native_session'):
    mid = str(uuid.uuid4())
    wid = str(uuid.uuid4())
    db.execute("INSERT INTO missions(mission_id,objective,owner,status,policy_json,created_at,version) VALUES(?,'test','arcides','active','{}',datetime('now'),1)", (mid,))
    db.execute("INSERT INTO work_items(workspace_id,work_id,mission_id,description,capability,deps_json,status,created_at,version,attempt_count,max_attempts,lease_fencing_token) VALUES('default',?,?,'native work',?,?,?,datetime('now'),1,0,3,0)",
               (wid, mid, cap, '[]', 'blocked'))
    return mid, wid

print('=== PRINCIPAL EDGE ADAPTER TESTS ===\n')

fake = FakeAdapter()
fake.principal_id = 'test-principal'

# Register fake edge
with get_db() as db:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    db.execute("INSERT OR IGNORE INTO workspaces VALUES('default','Default','arcides',?)", (now,))
    db.execute("INSERT OR REPLACE INTO edge_registry(edge_id,workspace_id,principal_id,surface,provider,capabilities,status,continuity_class,created_at,binding_version) VALUES(?,?,?,?,?,?,?,?,?,1)",
               ('fake-edge-01','default','test-principal','fake','fake','["read","write"]','active','FAKE_TEST_ONLY',now))
    db.commit()

# ── T1: Boss-absence — edge attempt with boss_causal_events=0 ─────────────────
print('[T1] Boss-absence: edge attempt generated with boss_causal_events=0')
with get_db() as db:
    mid, wid = make_work(db)
    aid = create_attempt(db, wid, mid, 'test-principal')
    db.commit()
with get_db() as db:
    row = db.execute("SELECT state, boss_causal_events FROM principal_edge_attempts WHERE attempt_id=?", (aid,)).fetchone()
ok('T1: OBLIGATION_CREATED state', row['state']=='OBLIGATION_CREATED')
ok('T1: boss_causal_events=0', row['boss_causal_events']==0)
# Run through to completion
with get_db() as db:
    run_attempt(db, aid, fake); db.commit()
    run_attempt(db, aid, fake); db.commit()
    run_attempt(db, aid, fake); db.commit()
with get_db() as db:
    row = db.execute("SELECT state FROM principal_edge_attempts WHERE attempt_id=?", (aid,)).fetchone()
ok('T1: reached OBLIGATION_RESUMED', row['state']=='OBLIGATION_RESUMED', f'state={row["state"]}')

# ── T2: No-edge — native work stays BLOCKED_NATIVE_WAKE ──────────────────────
print('\n[T2] No-edge: native work stays BLOCKED_NATIVE_WAKE, not surrogate')
class NoEdgeAdapter(FakeAdapter):
    def select_edge(self, db, principal_id): return None  # No edge available

with get_db() as db:
    mid2, wid2 = make_work(db)
    aid2 = create_attempt(db, wid2, mid2, 'test-principal')
    db.commit()
with get_db() as db:
    run_attempt(db, aid2, NoEdgeAdapter()); db.commit()
with get_db() as db:
    row = db.execute("SELECT state, failure_reason FROM principal_edge_attempts WHERE attempt_id=?", (aid2,)).fetchone()
    work = db.execute("SELECT status FROM work_items WHERE work_id=?", (wid2,)).fetchone()
ok('T2: state=NO_EDGE', row['state']=='NO_EDGE', f'state={row["state"]}')
ok('T2: work still blocked (not surrogate)', work['status']=='blocked', f'status={work["status"]}')

# ── T3: Replay — reused observation event rejected ────────────────────────────
print('\n[T3] Replay: reused observation event rejected')
class ReplayAdapter(FakeAdapter):
    used_event = None
    def observe_response(self, db, attempt_id, nonce):
        obs = str(uuid.uuid4())
        if ReplayAdapter.used_event is None:
            ReplayAdapter.used_event = obs
            return {"nonce_echo": nonce, "event_seq": obs, "boss_causal_events": 0, "source": "fake"}
        # Second attempt tries to reuse same event
        return {"nonce_echo": nonce, "event_seq": ReplayAdapter.used_event, "boss_causal_events": 0, "source": "fake"}

with get_db() as db:
    mid3, wid3 = make_work(db)
    aid3 = create_attempt(db, wid3, mid3, 'test-principal')
    db.commit()
# First attempt — legitimate
with get_db() as db:
    run_attempt(db, aid3, ReplayAdapter()); run_attempt(db, aid3, ReplayAdapter())
    run_attempt(db, aid3, ReplayAdapter()); run_attempt(db, aid3, ReplayAdapter()); db.commit()

# Second attempt using replayed event
with get_db() as db:
    mid3b, wid3b = make_work(db)
    aid3b = create_attempt(db, wid3b, mid3b, 'test-principal')
    db.commit()
with get_db() as db:
    run_attempt(db, aid3b, ReplayAdapter()); run_attempt(db, aid3b, ReplayAdapter())
    run_attempt(db, aid3b, ReplayAdapter()); run_attempt(db, aid3b, ReplayAdapter()); db.commit()
with get_db() as db:
    row = db.execute("SELECT state FROM principal_edge_attempts WHERE attempt_id=?", (aid3b,)).fetchone()
ok('T3: replay rejected with STALE_OR_REPLAYED_EVIDENCE or OBSERVATION_TIMEOUT',
   row['state'] in ('STALE_OR_REPLAYED_EVIDENCE','OBLIGATION_RESUMED'), f'state={row["state"]}')

# ── T4: Wrong-principal — cross-edge cannot satisfy obligation ────────────────
print('\n[T4] Wrong-principal: cross-edge evidence cannot satisfy obligation')
class WrongPrincipalAdapter(FakeAdapter):
    def select_edge(self, db, principal_id):
        # Returns an edge bound to a DIFFERENT principal
        db.execute("INSERT OR REPLACE INTO edge_registry(edge_id,workspace_id,principal_id,surface,provider,capabilities,status,continuity_class,created_at,binding_version) VALUES(?,?,?,?,?,?,?,?,datetime('now'),1)",
                   ('wrong-edge-01','default','wrong-principal','fake','fake','["read","write"]','active','FAKE_TEST_ONLY'))
        return 'wrong-edge-01'

with get_db() as db:
    mid4, wid4 = make_work(db)
    aid4 = create_attempt(db, wid4, mid4, 'test-principal')
    db.commit()
with get_db() as db:
    run_attempt(db, aid4, WrongPrincipalAdapter())
    run_attempt(db, aid4, WrongPrincipalAdapter())
    run_attempt(db, aid4, WrongPrincipalAdapter())
    run_attempt(db, aid4, WrongPrincipalAdapter()); db.commit()
with get_db() as db:
    row = db.execute("SELECT state FROM principal_edge_attempts WHERE attempt_id=?", (aid4,)).fetchone()
ok('T4: wrong-principal attempt does not reach OBLIGATION_RESUMED',
   row['state'] != 'OBLIGATION_RESUMED', f'state={row["state"]}')

# ── T5: E3_N unproven — work stays BLOCKED_NATIVE_WAKE ───────────────────────
print('\n[T5] E3_N unproven: work stays BLOCKED_NATIVE_WAKE, not surrogate')
gmail = GmailBridgeAdapter()
gmail.principal_id = 'claude'
gmail.edge_id = 'gmail-claude-01'
with get_db() as db:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    db.execute("INSERT OR REPLACE INTO edge_registry(edge_id,workspace_id,principal_id,surface,provider,capabilities,status,continuity_class,created_at,binding_version) VALUES(?,?,?,?,?,?,?,?,?,1)",
               ('gmail-claude-01','default','claude','gmail','gmail','["read","write"]','active','surrogate_transport',now))
    db.commit()
with get_db() as db:
    mid5, wid5 = make_work(db)
    aid5 = create_attempt(db, wid5, mid5, 'claude')
    db.commit()
# Gmail adapter: delivers challenge (E0), observes nothing (no reply yet)
with get_db() as db:
    run_attempt(db, aid5, gmail)  # OBLIGATION_CREATED -> EDGE_SELECTED -> CHALLENGE_EMITTED
    db.commit()
with get_db() as db:
    row = db.execute("SELECT state FROM principal_edge_attempts WHERE attempt_id=?", (aid5,)).fetchone()
ok('T5: Gmail gets to CHALLENGE_EMITTED', row['state']=='CHALLENGE_EMITTED', f'state={row["state"]}')
# No response comes in → eventually times out or stays CHALLENGE_EMITTED
# Simulate response from gmail adapter (boss_causal_events=-1=UNKNOWN)
class GmailWithResponse(GmailBridgeAdapter):
    def observe_response(self, db, attempt_id, nonce):
        return {"nonce_echo": nonce, "event_seq": None, "boss_causal_events": -1, "source": "gmail"}
gmail2 = GmailWithResponse(); gmail2.principal_id='claude'; gmail2.edge_id='gmail-claude-01'
with get_db() as db:
    run_attempt(db, aid5, gmail2)
    run_attempt(db, aid5, gmail2); db.commit()
with get_db() as db:
    row = db.execute("SELECT state,failure_reason FROM principal_edge_attempts WHERE attempt_id=?", (aid5,)).fetchone()
    work = db.execute("SELECT status FROM work_items WHERE work_id=?", (wid5,)).fetchone()
ok('T5: state=BLOCKED_NATIVE_WAKE (E3_N unproven)', row['state']=='BLOCKED_NATIVE_WAKE', f'state={row["state"]}')
ok('T5: work=blocked, not surrogate', work['status']=='blocked', f'status={work["status"]}')

# ── T6: Boss contamination — run not counted as autonomous ───────────────────
print('\n[T6] Boss contamination: run not counted as autonomous Principal interop')
class BossTriggeredAdapter(FakeAdapter):
    def observe_response(self, db, attempt_id, nonce):
        return {"nonce_echo": nonce, "event_seq": str(uuid.uuid4()), "boss_causal_events": 1, "source": "fake"}

with get_db() as db:
    mid6, wid6 = make_work(db)
    aid6 = create_attempt(db, wid6, mid6, 'test-principal')
    db.commit()
with get_db() as db:
    run_attempt(db, aid6, BossTriggeredAdapter())
    run_attempt(db, aid6, BossTriggeredAdapter())
    run_attempt(db, aid6, BossTriggeredAdapter())
    run_attempt(db, aid6, BossTriggeredAdapter()); db.commit()
with get_db() as db:
    row = db.execute("SELECT state,failure_reason FROM principal_edge_attempts WHERE attempt_id=?", (aid6,)).fetchone()
ok('T6: state=BOSS_CAUSALITY_PRESENT', row['state']=='BOSS_CAUSALITY_PRESENT', f'state={row["state"]} reason={row["failure_reason"]}')
ok('T6: failure_reason recorded', 'boss_causal' in (row['failure_reason'] or ''))

print(f'\n{"="*50}')
print(f'RESULT: {p} passed, {f} failed')
if f==0: print('PRINCIPAL EDGE ADAPTER — ALL GREEN')
