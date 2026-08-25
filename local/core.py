import hashlib, json, uuid
from datetime import datetime, timezone, timedelta
from db import get_db

MAX_LEASE = 300
CLAIM_TTL = 120

def now(): return datetime.now(timezone.utc)
def now_iso(): return now().isoformat()
def uid(): return str(uuid.uuid4())

def parse_dt(s):
    if not s: return None
    dt = datetime.fromisoformat(s.replace("Z","+00:00"))
    return dt.astimezone(timezone.utc) if dt.tzinfo else None

def expired(s): return bool(s) and now() > parse_dt(s)

def canonical(obj):
    if isinstance(obj, dict): return "{"+",".join(f"{json.dumps(k)}:{canonical(obj[k])}" for k in sorted(obj))+"}";
    if isinstance(obj, list): return "["+",".join(canonical(i) for i in obj)+"]"
    return json.dumps(obj)

def digest(scope, target, principal, params):
    return hashlib.sha256(canonical({"params":params,"principal":principal,"scope":scope,"target":target}).encode()).hexdigest()

def emit(db, principal, type_, payload):
    eid = uid()
    db.execute("INSERT INTO events(id,ts,principal,type,payload) VALUES(?,?,?,?,?)",
               (eid, now_iso(), principal, type_, json.dumps(payload)))
    return db.execute("SELECT seq FROM events WHERE id=?", (eid,)).fetchone()["seq"]

def recover(db):
    n = now_iso()
    stale_work = db.execute("SELECT work_id,lease_id FROM work_items WHERE status='leased' AND lease_expires<?", (n,)).fetchall()
    for r in stale_work:
        emit(db, "system", "lease_expired", {"work_id":r["work_id"],"lease_id":r["lease_id"]})
        db.execute("UPDATE work_items SET status='ready',assigned_to=NULL,lease_id=NULL,lease_expires=NULL WHERE work_id=?", (r["work_id"],))
    stale_agents = db.execute("SELECT agent_id,lease_id FROM agents WHERE status='busy' AND lease_expires<?", (n,)).fetchall()
    for r in stale_agents:
        emit(db, "system", "agent_lease_expired", {"agent_id":r["agent_id"]})
        db.execute("UPDATE agents SET status='available',current_work_id=NULL,lease_id=NULL,lease_expires=NULL WHERE agent_id=?", (r["agent_id"],))
    stale_claims = db.execute("SELECT request_id FROM decisions WHERE status='claimed' AND claim_expires<?", (n,)).fetchall()
    for r in stale_claims:
        emit(db, "system", "decision_claim_expired", {"request_id":r["request_id"]})
        db.execute("UPDATE decisions SET status='approved',claimed_at=NULL,claim_expires=NULL WHERE request_id=?", (r["request_id"],))
    if stale_work or stale_agents or stale_claims:
        db.commit()
    return len(stale_work)+len(stale_agents)+len(stale_claims)

def unblock_dependents(db, done_work_id):
    blocked = db.execute("SELECT work_id,deps FROM work_items WHERE status='blocked'").fetchall()
    unblocked = []
    for b in blocked:
        deps = json.loads(b["deps"])
        if done_work_id in deps:
            if all(db.execute("SELECT status FROM work_items WHERE work_id=?",(d,)).fetchone()["status"]=="done" for d in deps):
                db.execute("UPDATE work_items SET status='ready' WHERE work_id=?", (b["work_id"],))
                emit(db, "system", "work_unblocked", {"work_id":b["work_id"]})
                unblocked.append(b["work_id"])
    return unblocked

def match_agents(db, capability):
    """Find available agents that have the required capability."""
    agents = db.execute("SELECT * FROM agents WHERE status='available'").fetchall()
    matched = []
    for a in agents:
        caps = json.loads(a["capabilities"])
        if capability in caps or capability == "*":
            matched.append(dict(a))
    return sorted(matched, key=lambda a: -len(json.loads(a["capabilities"])))
