"""
CAUSAL-DAG-12 Falsification Tests C1-C8
Tests the causal ancestry walker — NOT proximity heuristic.
"""
import os, sys, uuid, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('SPHERA_DB', '/tmp/sphera_causal.db')

from db import init, get_db, flush_outbox
from migrate import migrate
from causal_dag import walk_ancestry, compute_and_cache, verify_e3n_core
from datetime import datetime, timezone

init(); migrate()
with get_db() as db: flush_outbox(db); db.commit()

p=f=0
def ok(l,c,d=''):
    global p,f
    if c: print(f'  OK   {l}'+(f'  [{d}]' if d else '')); p+=1
    else: print(f'  FAIL {l}'+(f'  [{d}]' if d else '')); f+=1

now = datetime.now(timezone.utc).isoformat()

with get_db() as db:
    db.execute("INSERT OR IGNORE INTO workspaces VALUES('default','D','arcides',?)",(now,))
    db.execute("INSERT OR REPLACE INTO edge_registry(edge_id,workspace_id,principal_id,surface,provider,capabilities,status,continuity_class,created_at,binding_version) VALUES('fake-edge-01','default','test-principal','fake','fake','[]','active','FAKE_TEST_ONLY',?,1)",(now,))
    db.execute("INSERT OR REPLACE INTO edge_registry(edge_id,workspace_id,principal_id,surface,provider,capabilities,status,continuity_class,created_at,binding_version) VALUES('gmail-claude-01','default','claude','gmail','gmail','[]','active','surrogate_transport',?,1)",(now,))
    db.commit()

def emit_event(db, eid, principal, etype, trace_id=None, parent=None, root=None, work_gen=None, attempt_id=None):
    """Emit event with causal provenance fields."""
    import uuid as _uuid
    db.execute(
        """INSERT INTO events
           (workspace_id,event_id,ts,principal,type,payload_json,provenance_json,
            prev_hash,this_hash,trace_id,causal_parent_event_id,
            activation_root_event_id,work_generation_tag,attempt_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ('default', eid, now, principal, etype, '{}', '{}', '', '',
         trace_id, parent, root, work_gen, attempt_id)
    )
    return eid

print('=== CAUSAL-DAG-12 FALSIFICATION TESTS ===\n')

# C1: Unrelated Arcides event 1 event before autonomous Claude response → MUST PASS (AUTONOMOUS)
print('[C1] Unrelated Arcides event adjacent (off-chain) → AUTONOMOUS')
trace1 = str(uuid.uuid4())
root1  = str(uuid.uuid4())
aid1   = str(uuid.uuid4())
e_root = str(uuid.uuid4())    # activation root (mission event)
e_pea  = str(uuid.uuid4())    # PEA challenge event
e_boss = str(uuid.uuid4())    # UNRELATED Arcides event (off-chain)
e_resp = str(uuid.uuid4())    # Claude response event
with get_db() as db:
    emit_event(db, e_root, 'system',  'mission_created', trace1, None,   None,   1, aid1)
    emit_event(db, e_pea,  'system',  'pea_challenge',   trace1, e_root, e_root, 1, aid1)
    emit_event(db, e_boss, 'arcides', 'message',         None,   None,   None,   None, None)  # OFF-CHAIN
    emit_event(db, e_resp, 'claude',  'pea_response',    trace1, e_pea,  e_root, 1, aid1)
    db.commit()
with get_db() as db:
    v = walk_ancestry(db, e_resp, e_root, trace1, 1)
ok('C1: AUTONOMOUS (off-chain Arcides irrelevant)', v['result']=='AUTONOMOUS', f'result={v["result"]} reason={v["failure_reason"]}')
ok('C1: no human ancestor', v['human_ancestor_id'] is None)

# C2: Arcides-caused wakeup 50+ events earlier through MissionLoop → MUST FAIL (HUMAN_CAUSAL)
print('\n[C2] Arcides-caused wakeup 50+ events earlier → HUMAN_CAUSAL')
trace2 = str(uuid.uuid4())
aid2   = str(uuid.uuid4())
e_arc  = str(uuid.uuid4())  # Arcides creates mission (human root)
chain  = [str(uuid.uuid4()) for _ in range(52)]  # 52-event chain
e_resp2= str(uuid.uuid4())
with get_db() as db:
    emit_event(db, e_arc, 'arcides', 'mission_created', trace2, None,    e_arc, 1, aid2)
    prev = e_arc
    for i, eid in enumerate(chain):
        emit_event(db, eid, 'system', f'step_{i}', trace2, prev, e_arc, 1, aid2)
        prev = eid
    emit_event(db, e_resp2, 'claude', 'pea_response', trace2, prev, e_arc, 1, aid2)
    db.commit()
with get_db() as db:
    v = walk_ancestry(db, e_resp2, e_arc, trace2, 1)
ok('C2: HUMAN_CAUSAL (Arcides 52 events back)', v['result']=='HUMAN_CAUSAL', f'result={v["result"]}')
ok('C2: human ancestor identified', v['human_ancestor_id'] == e_arc)

# C3: Indirect Arcides→system→PEA→Claude → MUST FAIL (HUMAN_CAUSAL)
print('\n[C3] Indirect Arcides→system→PEA→Claude → HUMAN_CAUSAL')
trace3 = str(uuid.uuid4()); aid3 = str(uuid.uuid4())
e3_arc = str(uuid.uuid4()); e3_sys = str(uuid.uuid4())
e3_pea = str(uuid.uuid4()); e3_resp = str(uuid.uuid4())
with get_db() as db:
    emit_event(db, e3_arc,  'arcides', 'wakeup',       trace3, None,   e3_arc, 1, aid3)
    emit_event(db, e3_sys,  'system',  'pea_dispatch', trace3, e3_arc, e3_arc, 1, aid3)
    emit_event(db, e3_pea,  'system',  'challenge',    trace3, e3_sys, e3_arc, 1, aid3)
    emit_event(db, e3_resp, 'claude',  'response',     trace3, e3_pea, e3_arc, 1, aid3)
    db.commit()
with get_db() as db:
    v = walk_ancestry(db, e3_resp, e3_arc, trace3, 1)
ok('C3: HUMAN_CAUSAL (indirect Arcides)', v['result']=='HUMAN_CAUSAL', f'result={v["result"]}')

# C4: Autonomous mission root→PEA→Claude → MUST PASS (AUTONOMOUS)
print('\n[C4] Autonomous mission root→PEA→Claude → AUTONOMOUS')
trace4 = str(uuid.uuid4()); aid4 = str(uuid.uuid4())
e4_root = str(uuid.uuid4()); e4_pea = str(uuid.uuid4()); e4_resp = str(uuid.uuid4())
with get_db() as db:
    emit_event(db, e4_root, 'system', 'mission_auto',  trace4, None,    e4_root, 1, aid4)
    emit_event(db, e4_pea,  'system', 'pea_challenge', trace4, e4_root, e4_root, 1, aid4)
    emit_event(db, e4_resp, 'claude', 'pea_response',  trace4, e4_pea,  e4_root, 1, aid4)
    db.commit()
with get_db() as db:
    v = walk_ancestry(db, e4_resp, e4_root, trace4, 1)
ok('C4: AUTONOMOUS (system root, no human)', v['result']=='AUTONOMOUS', f'result={v["result"]}')
ok('C4: chain_length=3', v['chain_length']==3, f'len={v["chain_length"]}')

# C5: Missing parent → CHAIN_BROKEN → fail closed
print('\n[C5] Missing causal_parent_event_id → CHAIN_BROKEN')
trace5 = str(uuid.uuid4()); aid5 = str(uuid.uuid4())
e5_root = str(uuid.uuid4()); e5_resp = str(uuid.uuid4())
with get_db() as db:
    emit_event(db, e5_root, 'system', 'root',     trace5, None,          e5_root, 1, aid5)
    emit_event(db, e5_resp, 'claude', 'response', trace5, 'MISSING-PARENT', e5_root, 1, aid5)  # broken parent
    db.commit()
with get_db() as db:
    v = walk_ancestry(db, e5_resp, e5_root, trace5, 1)
ok('C5: CHAIN_BROKEN (missing parent)', v['result']=='CHAIN_BROKEN', f'result={v["result"]}')

# C6: Forged parent pointing to another trace → CHAIN_BROKEN
print('\n[C6] Forged parent (wrong trace_id) → CHAIN_BROKEN')
trace6a = str(uuid.uuid4()); trace6b = str(uuid.uuid4()); aid6 = str(uuid.uuid4())
e6_root = str(uuid.uuid4()); e6_other = str(uuid.uuid4()); e6_resp = str(uuid.uuid4())
with get_db() as db:
    emit_event(db, e6_root,  'system', 'root',      trace6a, None,    e6_root, 1, aid6)
    emit_event(db, e6_other, 'system', 'other_root', trace6b, None,    None,    2, None)  # different trace
    emit_event(db, e6_resp,  'claude', 'response',  trace6a, e6_other, e6_root, 1, aid6)  # parent in wrong trace
    db.commit()
with get_db() as db:
    v = walk_ancestry(db, e6_resp, e6_root, trace6a, 1)
ok('C6: CHAIN_BROKEN (wrong trace)', v['result'] in ('CHAIN_BROKEN','ROOT_MISMATCH'), f'result={v["result"]}')

# C7: Cycle → CYCLE_DETECTED
print('\n[C7] Cyclic parent chain → CYCLE_DETECTED')
trace7 = str(uuid.uuid4()); aid7 = str(uuid.uuid4())
e7_a = str(uuid.uuid4()); e7_b = str(uuid.uuid4()); e7_root = str(uuid.uuid4())
with get_db() as db:
    emit_event(db, e7_root, 'system', 'root',     trace7, None, e7_root, 1, aid7)
    # Manually insert cycle: a→b, b→a
    db.execute("INSERT INTO events(workspace_id,event_id,ts,principal,type,payload_json,provenance_json,prev_hash,this_hash,trace_id,causal_parent_event_id,activation_root_event_id,work_generation_tag) VALUES('default',?,?,?,?,?,?,?,?,?,?,?,1)",
               (e7_a, now, 'system', 'step_a', '{}','{}','','', trace7, e7_b, e7_root))
    db.execute("INSERT INTO events(workspace_id,event_id,ts,principal,type,payload_json,provenance_json,prev_hash,this_hash,trace_id,causal_parent_event_id,activation_root_event_id,work_generation_tag) VALUES('default',?,?,?,?,?,?,?,?,?,?,?,1)",
               (e7_b, now, 'system', 'step_b', '{}','{}','','', trace7, e7_a, e7_root))
    db.commit()
with get_db() as db:
    v = walk_ancestry(db, e7_a, e7_root, trace7, 1)
ok('C7: CYCLE_DETECTED', v['result']=='CYCLE_DETECTED', f'result={v["result"]}')

# C8: Restart/replay → same verdict deterministically from ledger
print('\n[C8] Restart/replay → same ancestry verdict from ledger only')
with get_db() as db:
    v1 = compute_and_cache(db, e4_resp, aid4, e4_root, trace4, 1); db.commit()
with get_db() as db:
    v2 = compute_and_cache(db, e4_resp, aid4, e4_root, trace4, 1); db.commit()
ok('C8: same result on replay', v1['result']==v2['result']=='AUTONOMOUS')
ok('C8: second call from cache', v2['from_cache']==True)
ok('C8: deterministic from ledger only', v1['chain_length']==v2['chain_length'])

# E3_N Core verifier: Gmail cannot produce E3_N
print('\n[E3N] Core verifier: Gmail cannot produce E3_N')
with get_db() as db:
    eid = verify_e3n_core(db, aid1, 'gmail-claude-01', 'claude', {'nonce': 'test'})
    db.commit()
ok('E3N: Gmail → None (cannot produce E3_N)', eid is None, f'eid={eid}')
# Fake (test) surface CAN produce E3_N
with get_db() as db:
    os.environ['SPHERA_TEST_PEA'] = '1'
    import importlib, causal_dag
    importlib.reload(causal_dag)
    from causal_dag import verify_e3n_core as verify2
    eid2 = verify2(db, aid1, 'fake-edge-01', 'test-principal', {'trace_id': trace1})
    db.commit()
ok('E3N: Fake (test) surface → E3_N written', eid2 is not None, f'eid={eid2}')
with get_db() as db:
    row = db.execute("SELECT evidence_level FROM principal_evidence WHERE evidence_id=?",(eid2,)).fetchone()
ok('E3N: Core writes E3_N level', row and row['evidence_level']=='E3_N')

print(f'\n{"="*50}')
print(f'RESULT: {p} passed, {f} failed')
if f==0: print('CAUSAL-DAG-12 — ALL GREEN')
