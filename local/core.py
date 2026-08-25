"""
SPHERA core v1.1 — Soba code review fixes:
1. parse_dt() strict: raises ValueError on naive/invalid (never returns None silently)
2. expired() uses parsed aware datetimes, not string comparison
3. recover() uses Python datetime comparison, not SQL lexical comparison
4. unblock_dependents() guards against missing/corrupted dependency rows
"""
import hashlib, json, uuid
from datetime import datetime, timezone, timedelta
from db import get_db

MAX_LEASE = 300
CLAIM_TTL = 120

def now(): return datetime.now(timezone.utc)
def now_iso(): return now().isoformat()
def uid(): return str(uuid.uuid4())

def parse_dt(s: str) -> datetime:
    """
    Parse ISO timestamp to aware UTC datetime.
    STRICT: raises ValueError on naive timestamps or bad input.
    Never returns None — callers must handle the exception.
    """
    if not s:
        raise ValueError("Empty timestamp string")
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception as e:
        raise ValueError(f"Invalid timestamp '{s}': {e}")
    if dt.tzinfo is None:
        raise ValueError(f"Naive datetime rejected (no timezone): '{s}'. Use UTC with offset.")
    return dt.astimezone(timezone.utc)

def expired(s) -> bool:
    """
    Returns True if timestamp s is in the past.
    Returns False if s is None (no expiry set).
    Raises ValueError on malformed/naive timestamps (fail closed, never silently allow).
    """
    if s is None:
        return False
    return now() > parse_dt(s)  # always uses aware datetime comparison

def canonical(obj):
    if isinstance(obj, dict):
        return "{" + ",".join(f"{json.dumps(k)}:{canonical(obj[k])}" for k in sorted(obj)) + "}"
    if isinstance(obj, list):
        return "[" + ",".join(canonical(i) for i in obj) + "]"
    return json.dumps(obj)

def digest(scope, target, principal, params):
    return hashlib.sha256(
        canonical({"params": params, "principal": principal, "scope": scope, "target": target}).encode()
    ).hexdigest()

def emit(db, principal, type_, payload):
    eid = uid()
    db.execute(
        "INSERT INTO events(id,ts,principal,type,payload) VALUES(?,?,?,?,?)",
        (eid, now_iso(), principal, type_, json.dumps(payload))
    )
    return db.execute("SELECT seq FROM events WHERE id=?", (eid,)).fetchone()["seq"]

def recover(db):
    """
    On startup: expire stale work leases, agent leases, and decision claims.
    Bug fix: uses Python datetime comparison (aware UTC), not lexical SQL comparison.
    """
    recovered = 0
    now_dt = now()

    # Load all leased work items and compare datetimes in Python
    leased_work = db.execute(
        "SELECT work_id, lease_id, lease_expires FROM work_items WHERE status='leased'"
    ).fetchall()
    for r in leased_work:
        try:
            if r["lease_expires"] and now_dt > parse_dt(r["lease_expires"]):
                emit(db, "system", "lease_expired", {"work_id": r["work_id"], "lease_id": r["lease_id"], "reason": "startup_recovery"})
                db.execute("UPDATE work_items SET status='ready',assigned_to=NULL,lease_id=NULL,lease_expires=NULL WHERE work_id=?", (r["work_id"],))
                recovered += 1
        except ValueError as e:
            # Malformed timestamp — expire defensively (fail closed)
            emit(db, "system", "lease_expired", {"work_id": r["work_id"], "reason": f"malformed_timestamp: {e}"})
            db.execute("UPDATE work_items SET status='ready',assigned_to=NULL,lease_id=NULL,lease_expires=NULL WHERE work_id=?", (r["work_id"],))
            recovered += 1

    # Busy agents with expired leases
    busy_agents = db.execute(
        "SELECT agent_id, lease_id, lease_expires FROM agents WHERE status='busy'"
    ).fetchall()
    for r in busy_agents:
        try:
            if r["lease_expires"] and now_dt > parse_dt(r["lease_expires"]):
                emit(db, "system", "agent_lease_expired", {"agent_id": r["agent_id"], "reason": "startup_recovery"})
                db.execute("UPDATE agents SET status='available',current_work_id=NULL,lease_id=NULL,lease_expires=NULL WHERE agent_id=?", (r["agent_id"],))
                recovered += 1
        except ValueError:
            db.execute("UPDATE agents SET status='available',current_work_id=NULL,lease_id=NULL,lease_expires=NULL WHERE agent_id=?", (r["agent_id"],))
            recovered += 1

    # Stale decision claims
    claimed = db.execute(
        "SELECT request_id, claim_expires FROM decisions WHERE status='claimed'"
    ).fetchall()
    for r in claimed:
        try:
            if r["claim_expires"] and now_dt > parse_dt(r["claim_expires"]):
                emit(db, "system", "decision_claim_expired", {"request_id": r["request_id"], "reason": "startup_recovery"})
                db.execute("UPDATE decisions SET status='approved',claimed_at=NULL,claim_expires=NULL WHERE request_id=?", (r["request_id"],))
                recovered += 1
        except ValueError:
            db.execute("UPDATE decisions SET status='approved',claimed_at=NULL,claim_expires=NULL WHERE request_id=?", (r["request_id"],))
            recovered += 1

    if recovered:
        db.commit()
    return recovered

def unblock_dependents(db, done_work_id):
    """
    After a work item completes, check if any blocked items can now be unblocked.
    Bug fix: guards against missing/corrupted dependency rows instead of crashing.
    """
    blocked = db.execute("SELECT work_id, deps FROM work_items WHERE status='blocked'").fetchall()
    unblocked = []
    for b in blocked:
        try:
            deps = json.loads(b["deps"] or "[]")
        except (json.JSONDecodeError, TypeError):
            # Corrupted deps field — skip, do not crash
            emit(db, "system", "dependency_parse_error", {"work_id": b["work_id"]})
            continue

        if done_work_id not in deps:
            continue

        # Verify all deps exist and are done
        all_done = True
        for dep_id in deps:
            dep_row = db.execute("SELECT status FROM work_items WHERE work_id=?", (dep_id,)).fetchone()
            if dep_row is None:
                # Missing dependency — treat as corrupted, block unblocking
                emit(db, "system", "missing_dependency", {"work_id": b["work_id"], "missing_dep": dep_id})
                all_done = False
                break
            if dep_row["status"] != "done":
                all_done = False
                break

        if all_done:
            db.execute("UPDATE work_items SET status='ready' WHERE work_id=?", (b["work_id"],))
            emit(db, "system", "work_unblocked", {"work_id": b["work_id"], "unblocked_by": done_work_id})
            unblocked.append(b["work_id"])

    return unblocked

def match_agents(db, capability):
    """Find available agents that can handle the required capability."""
    agents = db.execute("SELECT * FROM agents WHERE status='available'").fetchall()
    matched = []
    for a in agents:
        try:
            caps = json.loads(a["capabilities"] or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        if capability in caps:
            score = 110 if len(caps) == 1 else max(80 - (len(caps) - 1) * 2, 10)
            matched.append((dict(a), score))
    matched.sort(key=lambda x: (-x[1], len(json.loads(x[0]["capabilities"] or "[]")), x[0]["agent_id"]))
    return matched
