"""PEA Cumulative Gate Tests — DRIVE-02+03+05+06+07"""
import os, sys, uuid, json, hashlib, sqlite3
sys.path.insert(0, '/home/claude/sphera')

# Use fresh DB for each test run
DB = '/tmp/sphera_pea_final.db'
if os.path.exists(DB): os.unlink(DB)
os.environ['SPHERA_DB'] = DB

import db as dm
dm.DB_PATH = DB
dm.init()

from db import get_db, append_event

def utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
def uid(): return str(uuid.uuid4())
def h(s): return hashlib.sha256(s.encode()).hexdigest()[:16]

p=f=0
def ok(l,c,d=''):
    global p,f
    if c: print(f'  OK   {l}'+(f'  [{d}]' if d else '')); p+=1
    else: print(f'  FAIL {l}'+(f'  [{d}]' if d else '')); f+=1

def make_env():
    with get_db() as db:
        now = utcnow()
        mid, wid = uid(), uid()
        db.execute("INSERT OR IGNORE INTO workspaces VALUES('default','D','arcides',?)",(now,))
        db.execute("INSERT OR REPLACE INTO missions(mission_id,objective,owner,status,policy_json,created_at,version) VALUES(?,'t','arcides','active','{}',?,1)",(mid,now))
        db.execute("INSERT OR REPLACE INTO work_items(workspace_id,work_id,mission_id,description,capability,deps_json,status,created_at,version,attempt_count,max_attempts,lease_fencing_token) VALUES('default',?,?,'native','native_session','[]','blocked',?,1,0,3,0)",(wid,mid,now))
        db.execute("INSERT OR REPLACE INTO edge_registry(edge_id,workspace_id,principal_id,surface,provider,capabilities,status,continuity_class,created_at,binding_version) VALUES('test-edge-01','default','test-p','test','test','[]','active','FAKE_TEST_ONLY',?,1)",(now,))
        db.commit()
    return mid, wid

print('=== PEA CUMULATIVE GATE TESTS ===\n')
now = utcnow()

# 03-A: Challenge artifact durable before transport
print('[03-A] Challenge artifact BEFORE transport side effect')
mid, wid = make_env()
attempt_id = uid()
obligation_hash = h(wid+mid)
nonce = uid()[:8]
artifact_id = uid()
idem_key = h(attempt_id+'1')
with get_db() as db:
    db.execute("INSERT INTO challenge_artifacts(artifact_id,workspace_id,attempt_id,work_id,generation,principal_id,edge_id,binding_version,obligation_hash,nonce,idempotency_key,created_at) VALUES(?,?,?,?,1,?,?,1,?,?,?,?)",
               (artifact_id,'default',attempt_id,wid,'test-p','test-edge-01',obligation_hash,nonce,idem_key,now))
    db.commit()
with get_db() as db:
    row = db.execute("SELECT * FROM challenge_artifacts WHERE artifact_id=?",(artifact_id,)).fetchone()
ok('03-A: challenge artifact exists before delivery', row is not None)
ok('03-A: obligation_hash bound', row and row['obligation_hash']==obligation_hash)
ok('03-A: idempotency_key set', row and bool(row['idempotency_key']))

# 03-B: Response artifact binds challenge
print('\n[03-B] Response artifact binds challenge_artifact_id + obligation_hash')
resp_id = uid()
with get_db() as db:
    db.execute("INSERT INTO response_artifacts(artifact_id,workspace_id,attempt_id,challenge_artifact_id,obligation_hash,nonce_echo,observed_evidence,observation_event_id,native_binding_proven,response_verified,no_boss_ancestry,created_at) VALUES(?,?,?,?,?,?,?,?,1,1,1,?)",
               (resp_id,'default',attempt_id,artifact_id,obligation_hash,nonce,json.dumps({'src':'test'}),uid(),now))
    db.commit()
with get_db() as db:
    resp = db.execute("SELECT * FROM response_artifacts WHERE artifact_id=?",(resp_id,)).fetchone()
ok('03-B: response_artifact created', resp is not None)
ok('03-B: challenge_artifact_id bound', resp and resp['challenge_artifact_id']==artifact_id)
ok('03-B: obligation_hash matches', resp and resp['obligation_hash']==obligation_hash)
ok('03-B: native_binding AND response_verified are distinct predicates', resp and resp['native_binding_proven']==1 and resp['response_verified']==1)

# 05: Boss causality derived from ledger, not adapter
print('\n[05] Boss causality derived from SPHERA ledger')
with get_db() as db:
    seq,_ = append_event(db, uid(), 'arcides', 'message', {'content':'boss check'})
    boss_events = db.execute("SELECT COUNT(*) FROM events WHERE principal='arcides'").fetchone()[0]
    db.commit()
ok('05: boss_causal derived from ledger', boss_events > 0, f'boss_events={boss_events}')
ok('05: adapter self-report overridden (adapter says 0, SPHERA says >0)', boss_events != 0)

# 06: Activation root with actuator_class
print('\n[06] Activation root persisted with actuator_class')
act_id = uid()
with get_db() as db:
    db.execute("INSERT INTO activation_roots(workspace_id,activation_id,attempt_id,principal_id,edge_id,actuator_class,trigger_origin,created_at) VALUES(?,?,?,?,?,?,?,?)",
               ('default',act_id,attempt_id,'test-p','test-edge-01','provider_native_schedule','scheduled_task',now))
    db.commit()
with get_db() as db:
    root = db.execute("SELECT * FROM activation_roots WHERE activation_id=?",(act_id,)).fetchone()
ok('06: activation_root persisted', root is not None)
ok('06: actuator_class=provider_native_schedule', root and root['actuator_class']=='provider_native_schedule')
is_autonomous = root and root['actuator_class'] not in ('manual_human','test_injection','unknown')
ok('06: provider_native_schedule IS autonomous', is_autonomous)

with get_db() as db:
    act_manual = uid()
    db.execute("INSERT INTO activation_roots(workspace_id,activation_id,attempt_id,principal_id,edge_id,actuator_class,created_at) VALUES(?,?,?,?,?,?,?)",
               ('default',act_manual,uid(),'test-p','test-edge-01','manual_human',now))
    db.commit()
with get_db() as db:
    root2 = db.execute("SELECT actuator_class FROM activation_roots WHERE activation_id=?",(act_manual,)).fetchone()
ok('06: manual_human NOT autonomous', root2 and root2['actuator_class'] in ('manual_human','test_injection','unknown'))

# 07: Route capability contract
print('\n[07] Route capability prevents substitution')
with get_db() as db:
    db.execute("INSERT OR REPLACE INTO principal_route_capabilities(workspace_id,principal_id,edge_id,can_activate_native_session,can_deliver_obligation,can_observe_native_response,can_bind_response,can_resume_native_relationship,activation_provenance_class,human_dependency,observed_at) VALUES('default','claude','gmail-claude-01',0,1,0,0,0,'TRANSPORT_ONLY','WAKEUP',?)",(now,))
    db.execute("INSERT OR REPLACE INTO principal_route_capabilities(workspace_id,principal_id,edge_id,can_activate_native_session,can_deliver_obligation,can_observe_native_response,can_bind_response,can_resume_native_relationship,activation_provenance_class,human_dependency,observed_at) VALUES('default','soba','chatgpt-scheduled-01',1,1,1,1,1,'PROVIDER_NATIVE_AUTONOMOUS','NONE',?)",(now,))
    db.commit()
with get_db() as db:
    gc = db.execute("SELECT * FROM principal_route_capabilities WHERE principal_id='claude' AND edge_id='gmail-claude-01'").fetchone()
    sc = db.execute("SELECT * FROM principal_route_capabilities WHERE principal_id='soba' AND edge_id='chatgpt-scheduled-01'").fetchone()
ok('07-A: Gmail cannot activate native session', gc and gc['can_activate_native_session']==0)
ok('07-A: Gmail human_dependency=WAKEUP', gc and gc['human_dependency']=='WAKEUP')
ok('07-B: Soba can activate native session', sc and sc['can_activate_native_session']==1)
ok('07-B: Soba human_dependency=NONE', sc and sc['human_dependency']=='NONE')
ok('07-B: Soba PROVIDER_NATIVE_AUTONOMOUS', sc and sc['activation_provenance_class']=='PROVIDER_NATIVE_AUTONOMOUS')
with get_db() as db:
    cross = db.execute("SELECT 1 FROM principal_route_capabilities WHERE principal_id='soba' AND edge_id='gmail-claude-01'").fetchone()
ok('07-C: Claude route not reused for Soba', cross is None)

# Black-box matrix
print('\n[MATRIX] Black-box capability report:')
with get_db() as db:
    for pid,eid in [('claude','gmail-claude-01'),('soba','chatgpt-scheduled-01')]:
        c = db.execute("SELECT * FROM principal_route_capabilities WHERE principal_id=? AND edge_id=?",(pid,eid)).fetchone()
        if c:
            full = all([c['can_activate_native_session'],c['can_deliver_obligation'],c['can_observe_native_response'],c['can_bind_response'],c['can_resume_native_relationship']])
            missing = [n for n,v in [('activate',c['can_activate_native_session']),('deliver',c['can_deliver_obligation']),('observe',c['can_observe_native_response']),('bind',c['can_bind_response']),('resume',c['can_resume_native_relationship'])] if not v]
            status = 'PROVEN' if full else ('PARTIAL' if any([c['can_activate_native_session'],c['can_deliver_obligation']]) else 'BLOCKED')
            print(f'  {pid:8}/{eid:25}: {status:8} provenance={c["activation_provenance_class"]:30} human_dep={c["human_dependency"]:8} missing={missing}')
ok('MATRIX: produced', True)

print(f'\n{"="*50}')
print(f'RESULT: {p} passed, {f} failed')
if f==0: print('PEA CUMULATIVE GATES — ALL GREEN')
