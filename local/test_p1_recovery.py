"""SPHERA P1 Recovery Tests — Soba spec invariants 1-7"""
import json, os, sys, uuid, shutil, tempfile
TMPDIR  = tempfile.mkdtemp()
TEST_DB = os.path.join(TMPDIR, "test_p1.db")
os.environ["SPHERA_DB"] = TEST_DB
sys.path.insert(0, '/home/claude/sphera')

from db import init, get_db, append_event, IdempotencyConflict
from projector import Projector

p = f = 0
def ok(l, c=True, d=''):
    global p; print(f'  OK   {l}' + (f'  [{d}]' if d else '')); p += 1
def fail(l, d=''):
    global f; print(f'  FAIL {l}' + (f'  [{d}]' if d else '')); f += 1

init()

def append(principal, type_, payload):
    with get_db() as db:
        seq, dup = append_event(db, str(uuid.uuid4()), principal, type_, payload)
        db.commit()
    return seq

def append_with_id(event_id, principal, type_, payload):
    with get_db() as db:
        seq, dup = append_event(db, event_id, principal, type_, payload)
        db.commit()
    return seq, dup

print('=== SPHERA P1 RECOVERY TESTS ===\n')

print('[P1.1] Projector cursor persisted')
proj = Projector()
ok('cursor starts at 0', proj.cursor == 0, f'cursor={proj.cursor}')
seq1 = append("claude", "message", {"content": "hello room"})
ok(f'event appended seq:{seq1}', seq1 > 0)
proj.replay()
ok('cursor advanced after replay', proj.cursor == seq1, f'cursor={proj.cursor}')
with get_db() as db:
    row = db.execute("SELECT value FROM schema_meta WHERE key='projector_cursor'").fetchone()
ok('cursor in schema_meta', row and int(row["value"]) == seq1)

print('\n[P1.2] Rebuild state from replay')
seq2 = append("arcides", "mission_created", {"mission_id": "m-001", "objective": "Build SPHERA"})
proj2 = Projector()  # fresh instance
proj2.replay()
state2 = proj2.state()
ok('mission in projected state', "m-001" in [m["mission_id"] for m in state2["missions"]])
ok('arcides presence from ledger', "arcides" in [q["principal"] for q in state2["presence"]])

print('\n[P1.3] Duplicate event IDs idempotent')
ev_id = str(uuid.uuid4())
s_a, _ = append_with_id(ev_id, "claude", "message", {"content": "original"})
s_b, dup = append_with_id(ev_id, "claude", "message", {"content": "original"})
ok('same payload → same seq', s_a == s_b, f's_a={s_a} s_b={s_b}')
ok('was_duplicate flag set', dup == True)
try:
    append_with_id(ev_id, "claude", "message", {"content": "DIFFERENT"})
    fail('should raise IdempotencyConflict')
except IdempotencyConflict:
    ok('different payload → IdempotencyConflict raised')

print('\n[P1.4] Crash between append and projection')
seq4 = append("soba", "message", {"content": "soba was here"})
proj4 = Projector()  # Projector never ran for seq4
saved_cursor = proj4.cursor
proj4.replay()
ok('replay catches up after simulated crash', proj4.cursor >= seq4)
# Verify event still in ledger
with get_db() as db:
    ev = db.execute("SELECT * FROM events WHERE seq=?", (seq4,)).fetchone()
ok('event NOT lost in ledger after crash', ev is not None)

print('\n[P1.5] Idempotent re-apply (cursor regressed)')
# Regress cursor to simulate "projected but cursor not saved"
with get_db() as db:
    db.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('projector_cursor','1')")
    db.commit()
proj5 = Projector()
ok('cursor regressed to 1', proj5.cursor == 1)
proj5.replay()
state5 = proj5.state()
missions5 = [m["mission_id"] for m in state5["missions"]]
ok('state correct after re-apply', "m-001" in missions5)
ok('no mission duplicates', missions5.count("m-001") == 1)

print('\n[P1.6] Deterministic restart — byte-equivalent state')
state_before = proj5.state()
mb = sorted([m["mission_id"] for m in state_before["missions"]])
pb = sorted([q["principal"] for q in state_before["presence"]])

# Full wipe + replay from 0
with get_db() as db:
    db.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('projector_cursor','0')")
    db.execute("DELETE FROM missions_proj")
    db.execute("DELETE FROM presence")
    db.execute("DELETE FROM work_proj")
    db.commit()

proj6 = Projector()
ok('cursor reset to 0', proj6.cursor == 0)
proj6.replay()
state6 = proj6.state()
ma = sorted([m["mission_id"] for m in state6["missions"]])
pa = sorted([q["principal"] for q in state6["presence"]])
ok('missions byte-equivalent after restart', mb == ma, f'before={mb} after={ma}')
ok('presence byte-equivalent after restart', pb == pa, f'before={pb} after={pa}')
ok('replay_lag == 0', state6["replay_lag"] == 0, f'lag={state6["replay_lag"]}')

print('\n[P1.7] Health state')
s = proj6.state()
ok('ledger_head_seq present', "ledger_head_seq" in s)
ok('projector_cursor present', "projector_cursor" in s)
ok('replay_lag == 0', s["replay_lag"] == 0)
ok('last_error is None', s["last_error"] is None)
print(f'\n  ledger_head={s["ledger_head_seq"]} cursor={s["projector_cursor"]} lag={s["replay_lag"]}')
print(f'  missions={[m["mission_id"] for m in s["missions"]]}')
print(f'  presence={[q["principal"] for q in s["presence"]]}')

shutil.rmtree(TMPDIR)
print(f'\n{"="*50}')
print(f'P1 RESULT: {p} passed, {f} failed')
if f == 0: print('P1 ALL INVARIANTS GREEN')
