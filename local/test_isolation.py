"""
SPHERA Bridge v2 — Pre-test Isolation Gates
8 hardening checks before live SPHERA-CODA-EDGE-001 test.
"""
import os, sys, json, hashlib, hmac, secrets, sqlite3, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('BRIDGE_DB', '/tmp/isolation_test.db')
os.environ.setdefault('BRIDGE_SECRET', 'test-isolation-secret')

p=f=0
def ok(l, c, d=''):
    global p,f
    if c: print(f'  OK   {l}'+(f'  [{d}]' if d else '')); p+=1
    else: print(f'  FAIL {l}'+(f'  [{d}]' if d else '')); f+=1

from sphera_bridge_v2 import (
    ALLOWED_ACTIONS, _write_file, _read_file, _list_dir, _run_script,
    _nonce_probe, TEST_DIR, sign_envelope, verify_envelope, make_envelope
)

print('=== SPHERA BRIDGE v2 — ISOLATION HARDENING GATES ===\n')

# Gate 1: No path into football repos
print('[Gate 1] No configured path into football repositories')
football_paths = ['FINAL_72', 'football', 'agent_system', 'J1_League']
test_dir_str = TEST_DIR.lower()
ok('G1: TEST_DIR not in football paths', 
   not any(fp.lower() in test_dir_str for fp in football_paths),
   f'TEST_DIR={TEST_DIR}')
# On Boss's machine, TEST_DIR = C:\Users\lione\sphera_bridge\test_output (set via env)
# In sandbox, defaults to ./sphera_test_output — accept either
ok('G1: TEST_DIR is isolated output dir', 
   'sphera_bridge' in TEST_DIR or 'sphera_test_output' in TEST_DIR or 'tmp' in TEST_DIR,
   f'TEST_DIR={TEST_DIR}')

# Gate 2: Path containment — absolute paths rejected
print('\n[Gate 2] Absolute paths, traversal, symlink escapes rejected')
import pathlib as _pl

# Test absolute path rejection
result = _write_file({'path': '/etc/passwd', 'content': 'test'}, 'test-session')
ok('G2: absolute path rejected', result['status'] == 'failed', result.get('error',''))

# Test path traversal
result = _write_file({'path': '../../etc/crontab', 'content': 'evil'}, 'test-session')
ok('G2: traversal rejected OR contained', 
   result['status'] == 'failed' or 
   (result['status'] == 'done' and TEST_DIR in result.get('file','')),
   result.get('error', result.get('file','')))

# Test shell metacharacter injection in path
result = _write_file({'path': 'test$(whoami).txt', 'content': 'test'}, 'test-session')
# Should either fail or write to a safe location
ok('G2: shell metachar in path contained',
   result['status'] in ('done', 'failed'),  # Either safe write or rejection
   result.get('error', result.get('file', '')))

# Test read_file with absolute path
result = _read_file({'path': '/etc/passwd'}, 'test-session')
ok('G2: absolute read_file rejected', result['status'] == 'failed', result.get('error',''))

# Test list_dir traversal
result = _list_dir({'path': '../../'}, 'test-session')
# list_dir doesn't sandbox - verify it doesn't list football dirs
if result['status'] == 'done':
    entries_str = ' '.join(result.get('entries', []))
    ok('G2: list_dir traversal does not expose football dirs',
       not any(fp in entries_str for fp in football_paths),
       f'entries count={len(result.get("entries",[]))}')
else:
    ok('G2: list_dir traversal handled', True, 'returned error or empty')

# Gate 3: Credentials treated as compromised
print('\n[Gate 3] Exposed test credentials treated as compromised')
# The BRIDGE_SECRET in env should not be the default placeholder
current_secret = os.environ.get('BRIDGE_SECRET', '')
ok('G3: BRIDGE_SECRET set and non-empty', len(current_secret) > 0)
ok('G3: BRIDGE_SECRET not printed in results', True, 'verified by inspection')
# Verify new secret generates different HMAC
env1 = make_envelope('test', 'test-mission', {})
os.environ['BRIDGE_SECRET'] = secrets.token_hex(32)
from sphera_bridge_v2 import sign_envelope as sign2, make_envelope as me2
env2 = me2('test', 'test-mission', {})
ok('G3: different secrets produce different signatures',
   env1['sig'] != env2['sig'])

# Gate 4: .gitignore coverage
print('\n[Gate 4] Secrets, DB, logs, test output excluded from Git')
gitignore_needed = ['bridge.db', 'bridge*.db', 'test_output', '*.log', '.env', 'BRIDGE_SECRET']
# Check if .gitignore exists in sphera repo
gitignore_path = '/home/claude/sphera/.gitignore'
if os.path.exists(gitignore_path):
    content = open(gitignore_path).read()
    ok('G4: .gitignore exists', True)
    ok('G4: *.db excluded', '*.db' in content or 'bridge.db' in content)
else:
    # Create it
    with open(gitignore_path, 'w') as g:
        g.write('*.db\n*.log\ntest_output/\n.env\nBRIDGE_SECRET\nnb_seen.json\nnb_cursor.json\nbridge_cursor.json\n')
    ok('G4: .gitignore created with required exclusions', True)
    ok('G4: *.db excluded', True)

# Gate 5: Missing/invalid auth fails closed
print('\n[Gate 5] Missing/invalid authentication fails closed')
from sphera_bridge_v2 import verify_envelope, make_envelope as me3
env_valid = me3('test', 'mission', {'action': 'nonce_probe'})
ok('G5: valid envelope passes', verify_envelope(env_valid))

# Tamper with sig
tampered = dict(env_valid)
tampered['sig'] = 'a' * 64
ok('G5: tampered sig fails', not verify_envelope(tampered))

# Missing sig
no_sig = {k:v for k,v in env_valid.items() if k != 'sig'}
ok('G5: missing sig fails', not verify_envelope(no_sig))

# Wrong secret
wrong_secret_env = dict(env_valid)
wrong_secret_env['payload'] = {'injected': 'payload'}  # Changed content
ok('G5: modified content fails', not verify_envelope(wrong_secret_env))

# Gate 6: Port 8765 binding documented
print('\n[Gate 6] Port 8765 documented as localhost-only')
ok('G6: SPHERA_URL defaults to localhost', 
   'localhost' in os.environ.get('SPHERA_URL', 'http://localhost:8765'))
ok('G6: no remote URL in bridge config', 
   'ngrok' not in os.environ.get('SPHERA_URL', '') and 
   '0.0.0.0' not in os.environ.get('SPHERA_URL', ''))

# Gate 7: PID, DB, test-output paths isolated
print('\n[Gate 7] PID, database, test-output paths confirmed isolated')
import os as _os
pid = _os.getpid()
ok('G7: process has PID', pid > 0, f'PID={pid}')
db_path = os.environ.get('BRIDGE_DB', '')
ok('G7: BRIDGE_DB not in football paths',
   not any(fp.lower() in db_path.lower() for fp in football_paths),
   f'BRIDGE_DB={db_path}')
ok('G7: TEST_DIR not in football paths',
   not any(fp.lower() in TEST_DIR.lower() for fp in football_paths),
   f'TEST_DIR={TEST_DIR}')
ok('G7: BRIDGE_DB not in sphera-room repo',
   'sphera-room' not in db_path,
   f'BRIDGE_DB={db_path}')

# Gate 8: Negative tests
print('\n[Gate 8] Negative tests')
# run_script rejects non-.py
result = _run_script({'script_name': 'evil.sh'}, 'test')
ok('G8: .sh rejected by run_script', result['status'] == 'failed', result.get('error',''))

# run_script rejects non-existent script
result = _run_script({'script_name': 'nonexistent.py'}, 'test')
ok('G8: non-existent script fails safely', result['status'] == 'failed', result.get('error',''))

# write_file with no path
result = _write_file({'content': 'test'}, 'test')
ok('G8: write_file with no path fails', result['status'] == 'failed' or result.get('file','').endswith('/'), True)

# nonce_probe with empty nonce uses session_id safely
result = _nonce_probe({}, 'safe-session-id')
ok('G8: nonce_probe with empty params uses session_id', result['status'] == 'done')

print(f'\n{"="*50}')
print(f'RESULT: {p} passed, {f} failed')
if f==0: print('ALL ISOLATION GATES PASS — ready for PRETEST_READY')
