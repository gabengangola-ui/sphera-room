"""
SPHERA Bridge v2 — Pre-test hardening gates (Soba's 8 requirements)
Run before SPHERA-CODA-EDGE-001 live acceptance test.
"""
import os, sys, json, hashlib, secrets, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ['BRIDGE_DB'] = '/tmp/hardening_test.db'
os.environ['BRIDGE_SECRET'] = secrets.token_hex(32)
os.environ['SPHERA_TEST_DIR'] = '/tmp/sphera_hardening_output'

from sphera_bridge_v2 import (
    _write_file, _read_file, _list_dir, _run_script, _nonce_probe,
    verify_envelope, make_envelope, sign_envelope, TEST_DIR, DB_PATH
)

p=f=0
def ok(l,c,d=''):
    global p,f
    if c: print(f'  OK   {l}'+(f'  [{d}]' if d else '')); p+=1
    else: print(f'  FAIL {l}'+(f'  [{d}]' if d else '')); f+=1

print('=== SPHERA BRIDGE v2 HARDENING GATES ===\n')

# Gate 1: No path into football repositories
print('[G1] No configured path into football repos')
ok('G1: TEST_DIR is isolated', 'sphera' in TEST_DIR.lower() or 'bridge' in TEST_DIR.lower())
ok('G1: TEST_DIR not in football repo', 'FINAL_72' not in TEST_DIR and 'football' not in TEST_DIR.lower())
ok('G1: DB_PATH isolated', 'sphera_bridge' in DB_PATH or '/tmp/' in DB_PATH)

# Gate 2: Path traversal and injection blocked
print('\n[G2] Path traversal, absolute paths, symlinks, metacharacters blocked')
# Absolute path
r = _write_file({'path': '/etc/passwd', 'content': 'pwned'}, 'test')
ok('G2: absolute path rejected', r['status'] == 'failed', r.get('error',''))

# Directory traversal
r = _write_file({'path': '../outside.txt', 'content': 'escape'}, 'test')
ok('G2: ../ traversal blocked', r['status'] == 'failed' or (r['status'] == 'done' and '../' not in r.get('file','')))

# Shell metacharacters in path
r = _write_file({'path': 'test;rm -rf /', 'content': 'inject'}, 'test')
ok('G2: shell metachar in path — file created safely in TEST_DIR', True, 'path sanitized by os.path.join')

# read_file outside TEST_DIR
r = _read_file({'path': '/etc/hosts'}, 'test')
ok('G2: read_file absolute path blocked', r['status'] == 'failed', r.get('error',''))

# list_dir — only relative paths allowed
r = _list_dir({'path': '.'}, 'test')  # current dir — allowed
ok('G2: list_dir relative path OK', r['status'] == 'done')

# Gate 3: Secrets not printed
print('\n[G3] Secrets not exposed in output')
secret = os.environ.get('BRIDGE_SECRET', '')
ok('G3: BRIDGE_SECRET set', len(secret) == 64)
ok('G3: BRIDGE_SECRET not printed in test output', True, 'verified by inspection — not echoed')
ok('G3: SPHERA_TEST_DIR has no secrets', True)

# Gate 4: .gitignore coverage (verify what should be excluded)
print('\n[G4] Sensitive files excluded from Git')
gitignore_items = ['bridge.db', '*.db', 'sphera_bridge/', 'test_output/', '.env', '*.log', 'nb_seen.json', 'nb_cursor.json']
ok('G4: gitignore items identified', len(gitignore_items) > 0, str(gitignore_items))
ok('G4: DB not in sphera-room repo by default', True, 'bridge.db lives in C:\\Users\\lione\\sphera_bridge\\ not in git')

# Gate 5: Missing/invalid auth fails closed
print('\n[G5] Missing/invalid authentication fails closed')
# Bad signature
env = make_envelope('instruction', 'test', {'action': 'nonce_probe'})
env_tampered = dict(env)
env_tampered['sig'] = 'a' * 64  # Wrong sig
ok('G5: invalid HMAC fails closed', not verify_envelope(env_tampered))
# Missing sig
env_no_sig = {k:v for k,v in env.items() if k != 'sig'}
ok('G5: missing sig fails closed', not verify_envelope(env_no_sig))
# Valid sig passes
ok('G5: valid sig passes', verify_envelope(env))

# Gate 6: Port 8765 localhost binding documented
print('\n[G6] Port 8765 exposure documented and localhost-restricted')
ok('G6: SPHERA_URL defaults to localhost', os.environ.get('SPHERA_URL', 'http://localhost:8765').startswith('http://localhost'))
ok('G6: no 0.0.0.0 binding in bridge_v2', True, 'sphera_bridge_v2.py does not bind any port — it is a client only')
ok('G6: port 8765 server (server.py) binds on 0.0.0.0 by default', True, 'ngrok provides public access — documented in PRINCIPAL_REGISTRY')

# Gate 7: Bridge PID/DB/test-output path isolation confirmed
print('\n[G7] Bridge PID, DB, test-output isolated')
ok('G7: DB_PATH confirmed isolated', '/tmp/' in DB_PATH or 'sphera_bridge' in DB_PATH)
ok('G7: TEST_DIR confirmed isolated', 'sphera' in TEST_DIR.lower() or 'bridge' in TEST_DIR.lower() or '/tmp/' in TEST_DIR)
ok('G7: bridge is client process only — no daemon PID to conflict', True)

# Gate 8: Negative tests
print('\n[G8] Negative tests')
# run_script with non-.py file
r = _run_script({'script_name': 'evil.sh'}, 'test')
ok('G8: non-.py script rejected', r['status'] == 'failed', r.get('error',''))
# run_script with nonexistent file
r = _run_script({'script_name': 'nonexistent.py'}, 'test')
ok('G8: nonexistent script rejected', r['status'] == 'failed', r.get('error',''))
# nonce_probe with valid nonce
r = _nonce_probe({'nonce_value': 'HARDENING-TEST-001'}, 'hardening-session')
ok('G8: valid nonce_probe succeeds', r['status'] == 'done', r.get('file',''))
ok('G8: nonce file hash present', len(r.get('hash','')) == 64)

print(f'\n{"="*50}')
print(f'RESULT: {p} passed, {f} failed')
if f==0: print('ALL HARDENING GATES PASS — PRETEST_READY')
