"""
SPHERA Local Server v0.1
SQLite-backed event ledger. FastAPI. Runs on localhost:8765.
Implements: messages, decisions (fixed sentinel), missions, work items, leases.
Recovery: on startup, materialises projections and expires stale leases.
"""

import hashlib, json, os, sqlite3, time, uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

DB_PATH   = os.environ.get("SPHERA_DB", "/home/claude/sphera-local/sphera.db")
MAX_LEASE = 300  # seconds — max lease duration

# ── Auth ──────────────────────────────────────────────────────────────────────
KEYS = {
    os.environ.get("CLAUDE_KEY",   "claude-local-key"):   "claude",
    os.environ.get("SOBA_KEY",     "soba-local-key"):     "soba",
    os.environ.get("ARCIDES_KEY",  "arcides-local-key"):  "arcides",
}

def get_principal(authorization: str = Header(default="")) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Unauthorized")
    token = authorization[7:].strip()
    if token not in KEYS:
        raise HTTPException(401, "Unauthorized")
    return KEYS[token]

# ── Database ──────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            seq       INTEGER PRIMARY KEY AUTOINCREMENT,
            id        TEXT UNIQUE NOT NULL,
            ts        TEXT NOT NULL,
            principal TEXT NOT NULL,
            type      TEXT NOT NULL,
            payload   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS decision_state (
            request_id      TEXT PRIMARY KEY,
            status          TEXT NOT NULL,
            requesting_principal TEXT NOT NULL,
            scope           TEXT NOT NULL,
            target          TEXT NOT NULL,
            params_json     TEXT NOT NULL,
            bound_digest    TEXT NOT NULL,
            deadline        TEXT,
            approval_seq    INTEGER,
            claim_seq       INTEGER
        );

        CREATE TABLE IF NOT EXISTS missions (
            mission_id  TEXT PRIMARY KEY,
            objective   TEXT NOT NULL,
            owner       TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'active',
            policy_json TEXT,
            created_at  TEXT NOT NULL,
            seq         INTEGER
        );

        CREATE TABLE IF NOT EXISTS work_items (
            work_id      TEXT PRIMARY KEY,
            mission_id   TEXT NOT NULL,
            description  TEXT NOT NULL,
            capability   TEXT NOT NULL,
            deps_json    TEXT NOT NULL DEFAULT '[]',
            status       TEXT NOT NULL DEFAULT 'ready',
            lease_id     TEXT,
            lease_holder TEXT,
            lease_expires TEXT,
            result_seq   INTEGER,
            seq          INTEGER
        );
        """)
    recover_stale_leases()

def recover_stale_leases():
    """On startup, expire any leases that timed out while server was down."""
    now = utcnow()
    with get_db() as db:
        stale = db.execute(
            "SELECT work_id, lease_id FROM work_items WHERE status='leased' AND lease_expires < ?", (now,)
        ).fetchall()
        for row in stale:
            ev = new_event("system", "work_lease_expired", {"work_id": row["work_id"], "lease_id": row["lease_id"], "reason": "server_restart"})
            append_event(db, ev)
            db.execute("UPDATE work_items SET status='ready', lease_id=NULL, lease_holder=NULL, lease_expires=NULL WHERE work_id=?", (row["work_id"],))
        if stale:
            db.commit()
            print(f"[recovery] expired {len(stale)} stale leases on startup")

# ── Event helpers ─────────────────────────────────────────────────────────────
def utcnow():
    return datetime.now(timezone.utc).isoformat()

def new_event(principal, type_, payload):
    return {"id": str(uuid.uuid4()), "ts": utcnow(), "principal": principal, "type": type_, "payload": payload}

def append_event(db, ev):
    db.execute(
        "INSERT INTO events (id, ts, principal, type, payload) VALUES (?,?,?,?,?)",
        (ev["id"], ev["ts"], ev["principal"], ev["type"], json.dumps(ev["payload"]))
    )
    return ev

def canonical_json(obj):
    if isinstance(obj, dict):
        return "{" + ",".join(f"{json.dumps(k)}:{canonical_json(obj[k])}" for k in sorted(obj.keys())) + "}"
    if isinstance(obj, list):
        return "[" + ",".join(canonical_json(i) for i in obj) + "]"
    return json.dumps(obj)

def sha256(s):
    return hashlib.sha256(s.encode()).hexdigest()

def compute_digest(scope, target, principal, params):
    action = {"params": params, "principal": principal, "scope": scope, "target": target}
    return sha256(canonical_json(action))

# ── App startup/shutdown ──────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="SPHERA Local", lifespan=lifespan)

def ok(data, status=200):
    return JSONResponse(content=data, status_code=status)

def err(msg, status=400):
    return JSONResponse(content={"error": msg}, status_code=status)

# ── Messages ──────────────────────────────────────────────────────────────────
@app.post("/message")
async def post_message(request: Request, authorization: str = Header(default="")):
    principal = get_principal(authorization)
    body = await request.json()
    content = body.get("content", "").strip()
    if not content:
        return err("content required")
    # Block decision events through /message
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict) and str(parsed.get("type","")).startswith("decision_"):
            return err("Decision events must use /decision endpoints", 400)
    except Exception:
        pass
    ev = new_event(principal, "message", {"content": content})
    with get_db() as db:
        append_event(db, ev)
        db.commit()
        seq = db.execute("SELECT seq FROM events WHERE id=?", (ev["id"],)).fetchone()["seq"]
    return ok({"ok": True, "event_id": ev["id"], "seq": seq}, 201)

# ── Events ────────────────────────────────────────────────────────────────────
@app.get("/events")
async def read_events(after: int = 0, authorization: str = Header(default="")):
    get_principal(authorization)
    with get_db() as db:
        rows = db.execute("SELECT seq, id, ts, principal, type, payload FROM events WHERE seq > ? ORDER BY seq", (after,)).fetchall()
    events = [{"seq": r["seq"], "id": r["id"], "timestamp": r["ts"], "principal": r["principal"],
               "type": r["type"], **json.loads(r["payload"])} for r in rows]
    cursor = events[-1]["seq"] if events else after
    return ok({"events": events, "count": len(events), "cursor": cursor})

# ── Decisions ─────────────────────────────────────────────────────────────────
@app.post("/decision")
async def decision_request(request: Request, authorization: str = Header(default="")):
    principal = get_principal(authorization)
    body = await request.json()
    scope  = body.get("scope")
    target = body.get("target")
    params = body.get("params")
    if not scope or not isinstance(scope, str):   return err('"scope" required')
    if not target or not isinstance(target, str): return err('"target" required')
    if not isinstance(params, dict):              return err('"params" must be an object')
    deadline = body.get("deadline")
    if deadline:
        try: datetime.fromisoformat(deadline.replace("Z","+00:00"))
        except Exception: return err('"deadline" must be valid ISO timestamp')

    digest     = compute_digest(scope, target, principal, params)
    request_id = str(uuid.uuid4())
    ev = new_event(principal, "decision_requested", {"request_id": request_id, "scope": scope, "target": target, "params": params, "payload_digest": digest, "deadline": deadline})

    with get_db() as db:
        append_event(db, ev)
        seq = db.execute("SELECT seq FROM events WHERE id=?", (ev["id"],)).fetchone()["seq"]
        db.execute(
            "INSERT INTO decision_state (request_id,status,requesting_principal,scope,target,params_json,bound_digest,deadline) VALUES (?,?,?,?,?,?,?,?)",
            (request_id, "pending", principal, scope, target, json.dumps(params), digest, deadline)
        )
        db.commit()
    return ok({"ok": True, "request_id": request_id, "seq": seq, "payload_digest": digest}, 201)

@app.post("/decision/{request_id}/approve")
async def decision_approve(request_id: str, request: Request, authorization: str = Header(default="")):
    principal = get_principal(authorization)
    if principal != "arcides":
        raise HTTPException(403, "Only arcides may approve decisions")
    body = await request.json()
    with get_db() as db:
        row = db.execute("SELECT * FROM decision_state WHERE request_id=?", (request_id,)).fetchone()
        if not row:            return err("Decision not found", 404)
        if row["status"] != "pending": return err(f"Cannot approve: status is '{row['status']}'", 409)
        if row["deadline"] and utcnow() > row["deadline"]:
            db.execute("UPDATE decision_state SET status='expired' WHERE request_id=?", (request_id,))
            ev = new_event("system", "decision_expired", {"request_id": request_id})
            append_event(db, ev)
            db.commit()
            return err("Decision has already expired", 410)
        ev = new_event(principal, "decision_approved", {"request_id": request_id, "bound_digest": row["bound_digest"], "note": body.get("note")})
        append_event(db, ev)
        seq = db.execute("SELECT seq FROM events WHERE id=?", (ev["id"],)).fetchone()["seq"]
        db.execute("UPDATE decision_state SET status='approved', approval_seq=? WHERE request_id=?", (seq, request_id))
        db.commit()
    return ok({"ok": True, "seq": seq, "request_id": request_id}, 201)

@app.post("/decision/{request_id}/reject")
async def decision_reject(request_id: str, request: Request, authorization: str = Header(default="")):
    principal = get_principal(authorization)
    if principal != "arcides":
        raise HTTPException(403, "Only arcides may reject decisions")
    body = await request.json()
    with get_db() as db:
        row = db.execute("SELECT * FROM decision_state WHERE request_id=?", (request_id,)).fetchone()
        if not row:            return err("Decision not found", 404)
        if row["status"] != "pending": return err(f"Cannot reject: status is '{row['status']}'", 409)
        ev = new_event(principal, "decision_rejected", {"request_id": request_id, "reason": body.get("reason")})
        append_event(db, ev)
        seq = db.execute("SELECT seq FROM events WHERE id=?", (ev["id"],)).fetchone()["seq"]
        db.execute("UPDATE decision_state SET status='rejected' WHERE request_id=?", (request_id,))
        db.commit()
    return ok({"ok": True, "seq": seq, "request_id": request_id}, 201)

@app.post("/decision/{request_id}/claim")
async def decision_claim(request_id: str, request: Request, authorization: str = Header(default="")):
    principal = get_principal(authorization)
    body      = await request.json()
    params    = body.get("params")
    if not isinstance(params, dict): return err('"params" required to verify digest')

    with get_db() as db:
        row = db.execute("SELECT * FROM decision_state WHERE request_id=?", (request_id,)).fetchone()
        if not row:             return err("Decision not found", 404)
        if row["status"] != "approved": return err(f"Cannot claim: status is '{row['status']}'", 409)
        if row["requesting_principal"] != principal:
            raise HTTPException(403, "Only the requesting principal may claim")
        if row["deadline"] and utcnow() > row["deadline"]:
            db.execute("UPDATE decision_state SET status='expired' WHERE request_id=?", (request_id,))
            ev = new_event("system", "decision_expired", {"request_id": request_id})
            append_event(db, ev)
            db.commit()
            return err("Decision has expired", 410)
        # Digest verification — separate from params, no sentinel pollution
        computed_digest = compute_digest(row["scope"], row["target"], principal, params)
        if computed_digest != row["bound_digest"]:
            return err("Payload digest mismatch: params do not match approved action", 422)
        ev = new_event(principal, "decision_claimed", {"request_id": request_id})
        append_event(db, ev)
        seq = db.execute("SELECT seq FROM events WHERE id=?", (ev["id"],)).fetchone()["seq"]
        db.execute("UPDATE decision_state SET status='claimed', claim_seq=? WHERE request_id=?", (seq, request_id))
        db.commit()
    return ok({"ok": True, "seq": seq, "request_id": request_id}, 201)

@app.post("/decision/{request_id}/consume")
async def decision_consume(request_id: str, authorization: str = Header(default="")):
    principal = get_principal(authorization)
    with get_db() as db:
        row = db.execute("SELECT * FROM decision_state WHERE request_id=?", (request_id,)).fetchone()
        if not row:             return err("Decision not found", 404)
        if row["status"] != "claimed": return err(f"Cannot consume: status is '{row['status']}'", 409)
        if row["requesting_principal"] != principal: raise HTTPException(403, "Only requester may consume")
        ev = new_event(principal, "decision_consumed", {"request_id": request_id})
        append_event(db, ev)
        seq = db.execute("SELECT seq FROM events WHERE id=?", (ev["id"],)).fetchone()["seq"]
        db.execute("UPDATE decision_state SET status='consumed' WHERE request_id=?", (request_id,))
        db.commit()
    return ok({"ok": True, "seq": seq}, 201)

@app.post("/decision/{request_id}/fail")
async def decision_fail(request_id: str, request: Request, authorization: str = Header(default="")):
    principal = get_principal(authorization)
    body = await request.json()
    with get_db() as db:
        row = db.execute("SELECT * FROM decision_state WHERE request_id=?", (request_id,)).fetchone()
        if not row:             return err("Decision not found", 404)
        if row["status"] != "claimed": return err(f"Cannot fail: status is '{row['status']}'", 409)
        if row["requesting_principal"] != principal: raise HTTPException(403, "Only requester may report failure")
        ev = new_event(principal, "execution_failed", {"request_id": request_id, "reason": body.get("error","unknown")})
        append_event(db, ev)
        seq = db.execute("SELECT seq FROM events WHERE id=?", (ev["id"],)).fetchone()["seq"]
        db.execute("UPDATE decision_state SET status='approved' WHERE request_id=?", (request_id,))
        db.commit()
    return ok({"ok": True, "seq": seq, "status": "approved"}, 201)

@app.get("/decision/{request_id}")
async def get_decision(request_id: str, authorization: str = Header(default="")):
    get_principal(authorization)
    with get_db() as db:
        row = db.execute("SELECT * FROM decision_state WHERE request_id=?", (request_id,)).fetchone()
        if not row: return err("Not found", 404)
        return ok({"found": True, "request_id": request_id, "status": row["status"],
                   "scope": row["scope"], "target": row["target"],
                   "requesting_principal": row["requesting_principal"],
                   "bound_digest": row["bound_digest"], "deadline": row["deadline"]})

# ── Missions ──────────────────────────────────────────────────────────────────
@app.post("/mission")
async def create_mission(request: Request, authorization: str = Header(default="")):
    principal = get_principal(authorization)
    body      = await request.json()
    objective = body.get("objective")
    if not objective: return err('"objective" required')
    policy    = body.get("acceptance_policy", {})
    mid       = str(uuid.uuid4())
    ev = new_event(principal, "mission_created", {"mission_id": mid, "objective": objective,
                    "owner": principal, "policy": policy})
    with get_db() as db:
        append_event(db, ev)
        seq = db.execute("SELECT seq FROM events WHERE id=?", (ev["id"],)).fetchone()["seq"]
        db.execute("INSERT INTO missions (mission_id,objective,owner,status,policy_json,created_at,seq) VALUES (?,?,?,?,?,?,?)",
                   (mid, objective, principal, "active", json.dumps(policy), utcnow(), seq))
        db.commit()
    return ok({"ok": True, "mission_id": mid, "seq": seq}, 201)

@app.post("/mission/{mission_id}/work")
async def create_work_item(mission_id: str, request: Request, authorization: str = Header(default="")):
    principal = get_principal(authorization)
    body = await request.json()
    desc = body.get("description"); cap = body.get("capability"); deps = body.get("dependencies", [])
    if not desc: return err('"description" required')
    if not cap:  return err('"capability" required')
    with get_db() as db:
        if not db.execute("SELECT 1 FROM missions WHERE mission_id=?", (mission_id,)).fetchone():
            return err("Mission not found", 404)
    wid = str(uuid.uuid4())
    status = "blocked" if deps else "ready"
    ev = new_event(principal, "work_item_created", {"work_id": wid, "mission_id": mission_id,
                    "description": desc, "capability": cap, "dependencies": deps, "status": status})
    with get_db() as db:
        append_event(db, ev)
        seq = db.execute("SELECT seq FROM events WHERE id=?", (ev["id"],)).fetchone()["seq"]
        db.execute("INSERT INTO work_items (work_id,mission_id,description,capability,deps_json,status,seq) VALUES (?,?,?,?,?,?,?)",
                   (wid, mission_id, desc, cap, json.dumps(deps), status, seq))
        db.commit()
    return ok({"ok": True, "work_id": wid, "status": status, "seq": seq}, 201)

@app.post("/work/{work_id}/claim")
async def claim_work(work_id: str, request: Request, authorization: str = Header(default="")):
    principal = get_principal(authorization)
    body      = await request.json()
    duration  = min(int(body.get("lease_seconds", 60)), MAX_LEASE)
    with get_db() as db:
        row = db.execute("SELECT * FROM work_items WHERE work_id=?", (work_id,)).fetchone()
        if not row: return err("Work item not found", 404)
        if row["status"] == "leased":
            # Check if lease expired
            if row["lease_expires"] and utcnow() < row["lease_expires"]:
                return err(f"Already leased by {row['lease_holder']}", 409)
            # Lease expired — expire it first
            ev_exp = new_event("system", "work_lease_expired", {"work_id": work_id, "lease_id": row["lease_id"]})
            append_event(db, ev_exp)
            db.execute("UPDATE work_items SET status='ready', lease_id=NULL, lease_holder=NULL, lease_expires=NULL WHERE work_id=?", (work_id,))
            db.commit()
            row = db.execute("SELECT * FROM work_items WHERE work_id=?", (work_id,)).fetchone()
        if row["status"] == "done":   return err("Work item is already done", 409)
        if row["status"] == "failed": return err("Work item has failed", 409)
        if row["status"] == "blocked":
            # Check if deps are done
            deps = json.loads(row["deps_json"])
            for dep in deps:
                dep_row = db.execute("SELECT status FROM work_items WHERE work_id=?", (dep,)).fetchone()
                if not dep_row or dep_row["status"] != "done":
                    return err(f"Dependency {dep} is not done yet", 409)
            # All deps done — unblock
            db.execute("UPDATE work_items SET status='ready' WHERE work_id=?", (work_id,))
            db.commit()
        lid        = str(uuid.uuid4())
        expires_ts = datetime.fromtimestamp(time.time() + duration, tz=timezone.utc).isoformat()
        ev = new_event(principal, "work_claimed", {"work_id": work_id, "lease_id": lid, "lease_expires": expires_ts})
        append_event(db, ev)
        seq = db.execute("SELECT seq FROM events WHERE id=?", (ev["id"],)).fetchone()["seq"]
        db.execute("UPDATE work_items SET status='leased', lease_id=?, lease_holder=?, lease_expires=? WHERE work_id=?",
                   (lid, principal, expires_ts, work_id))
        db.commit()
    return ok({"ok": True, "lease_id": lid, "lease_expires": expires_ts, "seq": seq}, 201)

@app.post("/work/{work_id}/heartbeat")
async def heartbeat(work_id: str, request: Request, authorization: str = Header(default="")):
    principal = get_principal(authorization)
    body      = await request.json()
    lease_id  = body.get("lease_id")
    extension = min(int(body.get("extend_seconds", 60)), MAX_LEASE)
    with get_db() as db:
        row = db.execute("SELECT * FROM work_items WHERE work_id=?", (work_id,)).fetchone()
        if not row:                    return err("Work item not found", 404)
        if row["status"] != "leased":  return err("Work item is not leased", 409)
        if row["lease_id"] != lease_id: return err("Lease ID mismatch", 403)
        if row["lease_holder"] != principal: return err("Not your lease", 403)
        new_exp = datetime.fromtimestamp(time.time() + extension, tz=timezone.utc).isoformat()
        ev = new_event(principal, "work_heartbeat", {"work_id": work_id, "lease_id": lease_id, "new_expiry": new_exp})
        append_event(db, ev)
        db.execute("UPDATE work_items SET lease_expires=? WHERE work_id=?", (new_exp, work_id))
        db.commit()
    return ok({"ok": True, "new_expiry": new_exp}, 200)

@app.post("/work/{work_id}/result")
async def submit_result(work_id: str, request: Request, authorization: str = Header(default="")):
    principal = get_principal(authorization)
    body      = await request.json()
    lease_id  = body.get("lease_id")
    result    = body.get("result")
    if not lease_id: return err('"lease_id" required')
    if result is None: return err('"result" required')
    with get_db() as db:
        row = db.execute("SELECT * FROM work_items WHERE work_id=?", (work_id,)).fetchone()
        if not row:                     return err("Not found", 404)
        if row["lease_id"] != lease_id:  return err("Stale lease — lease ID mismatch", 403)
        if row["lease_holder"] != principal: return err("Not your lease", 403)
        if row["status"] != "leased":    return err("Work item is not leased", 409)
        if row["lease_expires"] and utcnow() > row["lease_expires"]:
            return err("Lease has expired", 403)
        ev = new_event(principal, "work_result", {"work_id": work_id, "lease_id": lease_id, "result": result})
        append_event(db, ev)
        seq = db.execute("SELECT seq FROM events WHERE id=?", (ev["id"],)).fetchone()["seq"]
        db.execute("UPDATE work_items SET status='done', result_seq=?, lease_id=NULL, lease_holder=NULL WHERE work_id=?", (seq, work_id))
        db.commit()
        # Unblock dependents
        deps_on_this = db.execute("SELECT work_id, deps_json FROM work_items WHERE status='blocked'").fetchall()
        for dep in deps_on_this:
            deps = json.loads(dep["deps_json"])
            if work_id in deps:
                all_done = all(
                    db.execute("SELECT status FROM work_items WHERE work_id=?", (d,)).fetchone()["status"] == "done"
                    for d in deps
                )
                if all_done:
                    db.execute("UPDATE work_items SET status='ready' WHERE work_id=?", (dep["work_id"],))
                    ev2 = new_event("system", "work_item_unblocked", {"work_id": dep["work_id"]})
                    append_event(db, ev2)
        db.commit()
    return ok({"ok": True, "seq": seq}, 201)

@app.get("/missions/{mission_id}/next")
async def next_action(mission_id: str, authorization: str = Header(default="")):
    get_principal(authorization)
    with get_db() as db:
        mission = db.execute("SELECT * FROM missions WHERE mission_id=?", (mission_id,)).fetchone()
        if not mission: return err("Mission not found", 404)
        items   = db.execute("SELECT * FROM work_items WHERE mission_id=?", (mission_id,)).fetchall()
        ready   = [{"work_id": i["work_id"], "description": i["description"], "capability": i["capability"]} for i in items if i["status"] == "ready"]
        blocked = [{"work_id": i["work_id"], "description": i["description"], "waiting_on": json.loads(i["deps_json"])} for i in items if i["status"] == "blocked"]
        leased  = [{"work_id": i["work_id"], "holder": i["lease_holder"], "expires": i["lease_expires"]} for i in items if i["status"] == "leased"]
        done    = [i["work_id"] for i in items if i["status"] == "done"]
        all_done = len(done) == len(items) and len(items) > 0
        # Check completion policy
        status = "complete" if all_done else "in_progress"
        return ok({"mission_id": mission_id, "objective": mission["objective"], "status": status,
                   "ready": ready, "blocked": blocked, "leased": leased, "done_count": len(done),
                   "total": len(items)})

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
