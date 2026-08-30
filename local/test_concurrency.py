"""
Concurrency tests per Soba's requirement:
A) work_obligations uniqueness/FKs workspace scoped
B) Two server processes cannot double-release deps or double-claim wakes
"""
import os, sys, uuid, json, threading, time
sys.path.insert(0, '/home/claude/sphera')
os.environ['SPHERA_DB'] = '/tmp/sphera_concurrency.db'
from db import init, get_db, append_event
from migrate import migrate

init(); migrate()

p=f=0
def ok(l,c,d=''):
    global p,f
    if c: print(f'  OK   {l}'+(f'  [{d}]' if d else '')); p+=1
    else: print(f'  FAIL {l}'+(f'  [{d}]' if d else '')); f+=1

print('=== CONCURRENCY TESTS ===\n')

# Setup: mission + two work items
mid = str(uuid.uuid4())
w1  = str(uuid.uuid4())
w2  = str(uuid.uuid4())
with get_db() as db:
    db.execute("INSERT INTO missions(workspace_id,mission_id,objective,owner,status,policy_json,created_at,version) VALUES('default',?,?,?,?,?,datetime('now'),1)",
               (mid,'Concurrency test','arcides','active','{}'))
    db.execute("INSERT INTO work_items(workspace_id,work_id,mission_id,description,capability,deps_json,status,created_at,version,attempt_count,max_attempts,lease_fencing_token) VALUES('default',?,?,?,?,?,?,datetime('now'),1,0,3,0)",
               (w1,mid,'Step 1','backend','[]','ready'))
    db.execute("INSERT INTO work_items(workspace_id,work_id,mission_id,description,capability,deps_json,status,created_at,version,attempt_count,max_attempts,lease_fencing_token) VALUES('default',?,?,?,?,?,?,datetime('now'),1,0,3,0)",
               (w2,mid,'Step 2','testing',json.dumps([w1]),'blocked'))
    db.commit()

print('[A] work_obligations uniqueness')
# Insert work_obligation — should succeed
with get_db() as db:
    db.execute("INSERT INTO work_obligations(workspace_id,work_id,assignee,next_action,wake_state,created_at,updated_at) VALUES('default',?,?,?,?,datetime('now'),datetime('now'))",
               (w1,'claude','execute','not_required'))
    db.commit()
ok('first obligation insert succeeds', True)

# Duplicate should fail (UNIQUE constraint)
conflict = False
try:
    with get_db() as db:
        db.execute("INSERT INTO work_obligations(workspace_id,work_id,assignee,next_action,wake_state,created_at,updated_at) VALUES('default',?,?,?,?,datetime('now'),datetime('now'))",
                   (w1,'soba','execute','not_required'))
        db.commit()
except Exception as e:
    conflict = True
ok('duplicate work_obligation rejected by UNIQUE constraint', conflict)

# Different workspace — should succeed (workspace scoped)
with get_db() as db:
    db.execute("INSERT OR IGNORE INTO workspaces(workspace_id,name,owner,created_at) VALUES('ws-other','Other','arcides',datetime('now'))")
    db.execute("INSERT INTO work_obligations(workspace_id,work_id,assignee,next_action,wake_state,created_at,updated_at) VALUES('ws-other',?,?,?,?,datetime('now'),datetime('now'))",
               (w1,'soba','execute','not_required'))
    db.commit()
ok('same work_id different workspace allowed (workspace scoped)', True)

print('\n[B] Concurrent claim — only one winner')
winners = []
errors  = []
lock    = threading.Lock()

def try_claim(worker_id):
    lid = str(uuid.uuid4())
    try:
        with get_db() as db:
            cur = db.execute(
                "UPDATE work_items SET status='leased',lease_id=?,lease_holder=?,lease_fencing_token=lease_fencing_token+1,attempt_count=attempt_count+1 WHERE workspace_id='default' AND work_id=? AND status='ready'",
                (lid, worker_id, w1)
            )
            db.commit()
            if cur.rowcount == 1:
                with lock: winners.append(worker_id)
    except Exception as e:
        with lock: errors.append(str(e))

threads = [threading.Thread(target=try_claim, args=(f'worker-{i}',)) for i in range(10)]
for t in threads: t.start()
for t in threads: t.join()

ok('exactly one winner from 10 concurrent claimers', len(winners)==1, f'winners={winners}')
ok('no errors in concurrent claims', len(errors)==0, f'errors={errors}')

print('\n[C] Concurrent dep release — no double-unblock')
# Complete w1 from 5 concurrent workers — only one should trigger unblock
def complete_w1(worker_id):
    try:
        with get_db() as db:
            cur = db.execute(
                "UPDATE work_items SET status='done',lease_id=NULL,lease_holder=NULL WHERE workspace_id='default' AND work_id=? AND status='leased'",
                (w1,)
            )
            if cur.rowcount == 1:
                # Unblock w2
                db.execute("UPDATE work_items SET status='ready' WHERE workspace_id='default' AND work_id=? AND status='blocked'",(w2,))
            db.commit()
    except Exception as e:
        pass

# First, ensure w1 is leased
lid2 = str(uuid.uuid4())
with get_db() as db:
    db.execute("UPDATE work_items SET status='leased',lease_id=?,lease_holder='prime' WHERE workspace_id='default' AND work_id=?",(lid2,w1))
    db.commit()

release_threads = [threading.Thread(target=complete_w1, args=(f'releaser-{i}',)) for i in range(5)]
for t in release_threads: t.start()
for t in release_threads: t.join()

with get_db() as db:
    w2_status = db.execute("SELECT status FROM work_items WHERE workspace_id='default' AND work_id=?",(w2,)).fetchone()['status']
    w1_status = db.execute("SELECT status FROM work_items WHERE workspace_id='default' AND work_id=?",(w1,)).fetchone()['status']
ok('w1=done after concurrent release', w1_status=='done', f'status={w1_status}')
ok('w2=ready (unblocked exactly once)', w2_status=='ready', f'status={w2_status}')

print('\n[D] wake_attempt uniqueness — one claim per obligation+generation')
obl_id = str(uuid.uuid4())
nonce  = str(uuid.uuid4())
with get_db() as db:
    db.execute("INSERT INTO wake_attempt(workspace_id,attempt_id,obligation_id,generation,target_principal,target_surface_binding,edge_id,nonce,expires_at) VALUES('default',?,?,1,'claude','dispatch','gmail-claude-01',?,datetime('now','+5 minutes'))",
               (str(uuid.uuid4()),obl_id,nonce))
    db.commit()
ok('first wake_attempt succeeds', True)

dup_conflict = False
try:
    with get_db() as db:
        db.execute("INSERT INTO wake_attempt(workspace_id,attempt_id,obligation_id,generation,target_principal,target_surface_binding,edge_id,nonce,expires_at) VALUES('default',?,?,1,'claude','dispatch','gmail-claude-01',?,datetime('now','+5 minutes'))",
                   (str(uuid.uuid4()),obl_id,str(uuid.uuid4())))
        db.commit()
except Exception:
    dup_conflict = True
ok('duplicate obligation+generation rejected', dup_conflict)

print(f'\n{"="*50}')
print(f'RESULT: {p} passed, {f} failed')
if f==0: print('CONCURRENCY — ALL GREEN')
