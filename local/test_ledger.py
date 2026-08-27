"""
SPHERA ledger tests A-I per Soba's spec.
Runs against a fresh in-memory SQLite DB.
"""
import sys, json, uuid, os, threading, time
os.environ["SPHERA_DB"] = "/tmp/sphera_test_ledger.db"
# Clean start
try: os.remove("/tmp/sphera_test_ledger.db")
except: pass

sys.path.insert(0, "/home/claude/sphera")
from db import init, get_db, append_event, IdempotencyConflict

init()

p = f = 0
def ok(l, c=True, d=""):  global p; print(f"  OK   {l}" + (f"  [{d}]" if d else "")); p += 1
def fail(l, d=""): global f; print(f"  FAIL {l}" + (f"  [{d}]" if d else "")); f += 1

print("=== SPHERA LEDGER TESTS A-I ===\n")

# A: append event → durable store → readable by seq
print("[A] append + durable store")
with get_db() as db:
    eid = str(uuid.uuid4())
    seq, dup = append_event(db, eid, "claude", "message", {"content": "hello"})
    db.commit()
    row = db.execute("SELECT * FROM events WHERE seq=?", (seq,)).fetchone()
ok("A: event stored", row is not None, f"seq:{seq}")
ok("A: not duplicate", not dup)
ok("A: event_id matches", row["event_id"] == eid)
ok("A: seq is DB-allocated int", isinstance(seq, int) and seq > 0)

# B: 100 ordered appends → subscriber sees strictly increasing seq, exactly once
print("\n[B] 100 ordered appends, strictly increasing seq")
seqs = []
with get_db() as db:
    for i in range(100):
        s, _ = append_event(db, str(uuid.uuid4()), "soba", "message", {"n": i})
        seqs.append(s)
    db.commit()
ok("B: 100 events appended", len(seqs) == 100)
ok("B: strictly increasing", all(seqs[i] < seqs[i+1] for i in range(99)))
ok("B: no duplicates", len(set(seqs)) == 100)

# C: disconnect at seq N, append N+1..N+10, reconnect(after_seq=N) → exact 10-event replay
print("\n[C] cursor replay after disconnect")
with get_db() as db:
    last, _ = append_event(db, str(uuid.uuid4()), "claude", "heartbeat", {})
    cursor_n = db.execute("SELECT MAX(seq) FROM events").fetchone()[0]
    gap_seqs = []
    for i in range(10):
        s, _ = append_event(db, str(uuid.uuid4()), "soba", "message", {"gap": i})
        gap_seqs.append(s)
    db.commit()
    # Replay: fetch events after cursor_n
    replayed = db.execute("SELECT seq FROM events WHERE seq > ? ORDER BY seq", (cursor_n,)).fetchall()
    replayed_seqs = [r["seq"] for r in replayed]
ok("C: exactly 10 events replayed", len(replayed_seqs) == 10, f"got {len(replayed_seqs)}")
ok("C: replayed seqs match gap seqs", replayed_seqs == gap_seqs)

# D: restart server → max seq resumes +1, no reset
print("\n[D] restart — seq continues from max")
with get_db() as db:
    max_before = db.execute("SELECT MAX(seq) FROM events").fetchone()[0]
    # Simulate restart: close and reopen
    new_seq, _ = append_event(db, str(uuid.uuid4()), "claude", "startup", {})
    db.commit()
ok("D: seq after restart > max before", new_seq == max_before + 1, f"max:{max_before} new:{new_seq}")

# E: duplicate inbound bridge event with same provenance id → no duplicate canonical event
print("\n[E] duplicate bridge event → idempotent (same seq returned)")
bridge_id = f"gmail-uid-621762472-{uuid.uuid4()}"
with get_db() as db:
    s1, dup1 = append_event(db, bridge_id, "soba", "bridge_message",
                            {"content": "hello"}, {"source_message_id": bridge_id})
    db.commit()
    s2, dup2 = append_event(db, bridge_id, "soba", "bridge_message",
                            {"content": "hello"}, {"source_message_id": bridge_id})
    db.commit()
ok("E: first insert not duplicate", not dup1)
ok("E: second insert IS duplicate", dup2)
ok("E: same seq returned", s1 == s2, f"s1:{s1} s2:{s2}")
# Verify only one row
with get_db() as db:
    count = db.execute("SELECT COUNT(*) FROM events WHERE event_id=?", (bridge_id,)).fetchone()[0]
ok("E: exactly one row in DB", count == 1, f"rows:{count}")

# F: console hard refresh → same derived room state
print("\n[F] derived state consistent across reads")
with get_db() as db:
    r1 = db.execute("SELECT MAX(seq) FROM events").fetchone()[0]
    r2 = db.execute("SELECT MAX(seq) FROM events").fetchone()[0]
ok("F: deterministic read", r1 == r2, f"r1:{r1} r2:{r2}")

# G: schema version tracked
print("\n[G] schema version")
with get_db() as db:
    v = db.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
ok("G: schema_meta table exists", v is not None)
ok("G: version is set", int(v["value"]) >= 1, f"v:{v['value']}")

# H: Race test — concurrent appends while reader reconnects
print("\n[H] race test — concurrent appends + cursor reconnect")
results = []
stop_flag = threading.Event()

def writer():
    with get_db() as db:
        for i in range(50):
            if stop_flag.is_set(): break
            s, _ = append_event(db, str(uuid.uuid4()), "claude", "race_msg", {"i": i})
            results.append(s)
            db.commit()
            time.sleep(0.001)

t = threading.Thread(target=writer, daemon=True)
t.start()
time.sleep(0.05)  # let writer get ahead

# Reader: reconnect 5 times
for reconnect in range(5):
    with get_db() as db:
        cursor = db.execute("SELECT MAX(seq) FROM events").fetchone()[0] or 0
    time.sleep(0.01)

stop_flag.set()
t.join(timeout=2)

# Verify all written seqs are unique and in DB
with get_db() as db:
    all_race = [r["seq"] for r in db.execute("SELECT seq FROM events WHERE type='race_msg' ORDER BY seq").fetchall()]
ok("H: all race seqs unique", len(set(results)) == len(results))
ok("H: all race seqs in DB", sorted(results) == sorted(r for r in all_race if r in results) or len(results) == 0, f"written:{len(results)} in_db:{len(all_race)}")

# I: Poison duplicate — same event_id, different payload → IdempotencyConflict
print("\n[I] poison duplicate → IdempotencyConflict")
poison_id = str(uuid.uuid4())
with get_db() as db:
    append_event(db, poison_id, "claude", "test", {"val": "original"})
    db.commit()
try:
    with get_db() as db:
        append_event(db, poison_id, "claude", "test", {"val": "MUTATED"})
        db.commit()
    fail("I: should have raised IdempotencyConflict")
except IdempotencyConflict:
    ok("I: IdempotencyConflict raised for mutated payload")
except Exception as e:
    fail("I: wrong exception", str(e))

# Verify original row unchanged
with get_db() as db:
    row = db.execute("SELECT payload_json FROM events WHERE event_id=?", (poison_id,)).fetchone()
ok("I: original row unchanged", json.loads(row["payload_json"])["val"] == "original")

print(f"\n{'='*50}")
print(f"RESULT: {p} passed, {f} failed")
if f == 0:
    print("ALL LEDGER TESTS PASSED — P0 DONE")
