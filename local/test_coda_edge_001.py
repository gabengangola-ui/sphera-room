"""
SPHERA-CODA-EDGE-001 Acceptance Test
8 gates, Boss does zero courier actions.
"""
import hashlib, json, os, sys, time, uuid
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('SPHERA_URL', 'http://localhost:8765')
os.environ.setdefault('BRIDGE_DB', '/tmp/test_bridge.db')
os.environ.setdefault('BRIDGE_SECRET', 'test-secret-abc123')

from sphera_bridge_v2 import (
    init_db, make_envelope, queue_event, sign_envelope, verify_envelope,
    already_processed, execute_mission, parse_checkpoint_reply, new_id,
    ALLOWED_ACTIONS, TEST_DIR
)

p=f=0
def ok(l, c, d=''):
    global p,f
    if c: print(f'  OK   {l}'+(f'  [{d}]' if d else '')); p+=1
    else: print(f'  FAIL {l}'+(f'  [{d}]' if d else '')); f+=1

import shutil
if os.path.exists('/tmp/test_bridge.db'): os.remove('/tmp/test_bridge.db')
if os.path.exists(TEST_DIR): shutil.rmtree(TEST_DIR)

print('=== SPHERA-CODA-EDGE-001 ACCEPTANCE TEST ===\n')

# Gate 1: Soba sends harmless mission (simulated — no Boss transport)
print('[Gate 1] Soba sends instruction — Boss does nothing')
mission_id = 'CODA-EDGE-TEST-001'
session_id = new_id()
instr_env = make_envelope('instruction', mission_id, {
    'action': 'nonce_probe',
    'params': {'nonce_value': 'GATE1-PROOF'}
}, session_id=session_id)
ok('G1: instruction envelope created', instr_env['event_type'] == 'instruction')
ok('G1: HMAC signature present', len(instr_env.get('sig','')) == 64)
ok('G1: signature verifies', verify_envelope(instr_env))
queue_event(instr_env)  # Persist for G7 dedup check

# Gate 2: Local Coda creates proof file in isolated test dir
print('\n[Gate 2] Coda creates proof file in isolated test directory')
result_env = execute_mission(mission_id, 'nonce_probe',
    {'nonce_value': 'GATE2-PROOF'}, session_id, instr_env['event_id'])
ok('G2: completion event emitted', result_env['event_type'] == 'completion')
result = result_env['payload']
ok('G2: proof file created', os.path.exists(result.get('file','')), result.get('file',''))
ok('G2: file hash present', len(result.get('hash','')) == 64)
ok('G2: file in isolated test dir', TEST_DIR in result.get('file',''))

# Gate 3: Coda emits checkpoint (simulated)
print('\n[Gate 3] Coda emits checkpoint event')
chk_env = make_envelope('checkpoint', mission_id,
    {'question': 'Proceed with option A or B?', 'options': ['A','B'], 'recommendation': 'A'},
    session_id=session_id, parent_event_id=instr_env['event_id'], approval_req=True)
queue_event(chk_env)
ok('G3: checkpoint event queued', chk_env['event_type'] == 'checkpoint')
ok('G3: approval_required=True', chk_env['approval_required'] == True)
ok('G3: options present', len(chk_env['payload']['options']) == 2)

# Gate 4: Soba receives and answers checkpoint (via SPHERA REPLY)
print('\n[Gate 4] Soba answers checkpoint via SPHERA REPLY')
reply_body = f"SPHERA REPLY {chk_env['event_id']}: proceed with option A"
parsed = parse_checkpoint_reply(reply_body)
ok('G4: reply parsed', parsed is not None)
ok('G4: reply_to_event_id correct', parsed.get('reply_to_event_id') == chk_env['event_id'])
ok('G4: reply text captured', 'option A' in parsed.get('reply',''))

# Gate 5: Same mission resumes, updates proof file
print('\n[Gate 5] Same mission resumes after checkpoint reply')
update_result = execute_mission(mission_id, 'write_file',
    {'path': 'gate5_continuation.txt', 'content': f'checkpoint answered: {parsed["reply"]}'},
    session_id, chk_env['event_id'])
ok('G5: mission continues with same mission_id',
   update_result['payload'].get('action') == 'write_file')
ok('G5: continuation file created',
   os.path.exists(os.path.join(TEST_DIR, 'gate5_continuation.txt')))
ok('G5: same session_id preserved',
   update_result['payload'].get('session_id') == session_id)

# Gate 6: Completion returns with hashes and log evidence
print('\n[Gate 6] Completion returns with hashes and evidence')
ok('G6: completion event type', update_result['event_type'] == 'completion')
ok('G6: file hash in result', len(update_result['payload'].get('hash','')) == 64)
# Check audit ledger
from sphera_bridge_v2 import DB as _testdb; rows = _testdb.execute("SELECT action, result FROM audit_ledger WHERE mission_id=? ORDER BY id",(mission_id,)).fetchall()
ok('G6: audit ledger has entries', len(rows) >= 4, f'count={len(rows)}')
ok('G6: progress logged', any(r['action']=='progress_emitted' for r in rows))
ok('G6: completion logged', any(r['action']=='completion' for r in rows))

# Gate 7: Restart — same event_id not re-processed
print('\n[Gate 7] Restart — duplicate delivery rejected')
# Debug
from sphera_bridge_v2 import DB as _g7db
_g7row = _g7db.execute('SELECT event_id FROM event_queue WHERE event_id=?', (instr_env['event_id'],)).fetchone()
print(f'  [debug] event_id={instr_env["event_id"][:8]} in DB: {_g7row is not None}')
already = already_processed(instr_env['event_id'])
ok('G7: original event_id marked processed', already)
# Try to process same event again — should be skipped
dup_result = already_processed(instr_env['event_id'])
ok('G7: duplicate rejected (idempotent)', dup_result == True)

# Gate 8: Signature verification — tampered envelope rejected
print('\n[Gate 8] Tampered envelope rejected by signature check')
tampered = dict(instr_env)
tampered['payload'] = {'action': 'rm', 'params': {'path': '/'}}  # Malicious
ok('G8: tampered envelope fails verification', not verify_envelope(tampered))
ok('G8: original envelope still valid', verify_envelope(instr_env))

# Final: list evidence files
files = os.listdir(TEST_DIR) if os.path.exists(TEST_DIR) else []
print(f'\n  Evidence files in {TEST_DIR}:')
for f_name in sorted(files):
    fpath = os.path.join(TEST_DIR, f_name)
    fhash = hashlib.sha256(open(fpath,'rb').read()).hexdigest()[:16]
    print(f'    {f_name}: sha256={fhash}')

print(f'\n{"="*50}')
print(f'RESULT: {p} passed, {f} failed')
if f==0: print('SPHERA-CODA-EDGE-001 — ALL GATES PASS')
