"""
SPHERA Local Server v0.2
Fixes from Soba's NOT PASS review:
1. Atomic claim via UPDATE WHERE status='ready' + rowcount check
2. Heartbeat rejects expired leases (no resurrection)
3. Lease duration clamped: 1 <= seconds <= MAX_LEASE
4. Dependency integrity: exists, same mission, no self-dep, cycle detection
5. Mission authority: only owner can add work items
6. Decision claim expiry/recovery: claimed_at + claim_expires, lazy recovery
7. Time comparison: aware datetime objects, not ISO string comparison
8. Auth: startup fails if required env vars absent
+ Mission acceptance gate
"""

import hashlib, json, os, sqlite3, uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

MAX_LEASE        = 300   # seconds
CLAIM_LEASE_SECS = 120   # decision claim expires after this if not consumed/failed

# 8. Auth: fail on startup if keys absent
def _require_env(name):
    v = os.environ.get(name, "")
    if not v:
        raise RuntimeError(f"Required env var {name} is not set. Refusing to start with predictable credentials.")
    return v

KEYS: dict = {}  # populated in lifespan

DB_PATH = os.environ.get("SPHERA_DB", "/home/claude/sphera-local/sphera.db")

# ── Time helpers ──────────────────────────────────────────────────────────────
def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def utcnow_iso() -> str:
    return utcnow().isoformat()

def parse_dt(s: Optional[str]) -> Optional[datetime]:
    """Parse ISO timestamp to aware UTC datetime. Returns None if s is None."""
    if s is None:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"Naive datetime rejected: {s}. Supply UTC offset.")
    return dt.astimezone(timezone.utc)

def is_expired(expires_iso: Optional[str]) -> bool:
    if expires_iso is None:
        return False
    return utcnow() > parse_dt(expires_iso)

# ── Database ──────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
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
            request_id           TEXT PRIMARY KEY,
            status               TEXT NOT NULL,
            requesting_principal TEXT NOT NULL,
            scope                TEXT NOT NULL,
            target               TEXT NOT NULL,
            params_json          TEXT NOT NULL,
            bound_digest         TEXT NOT NULL,
            deadline             TEXT,
            claimed_at           TEXT,
            claim_expires        TEXT
        );
        CREATE TABLE IF NOT EXISTS missions (
            mission_id   TEXT PRIMARY KEY,
            objective    TEXT NOT NULL,
            owner        TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'active',
            policy_json  TEXT,
            created_at   TEXT NOT NULL,
            accepted_at  TEXT,
            acceptance_note TEXT,
            seq          INTEGER
        );
        CREATE TABLE IF NOT EXISTS work_items (
            work_id       TEXT PRIMARY KEY,
            mission_id    TEXT NOT NULL,
            description   TEXT NOT NULL,
            capability    TEXT NOT NULL,
            deps_json     TEXT NOT NULL DEFAULT '[]',
            status        TEXT NOT NULL DEFAULT 'ready',
            lease_id      TEXT,
            lease_holder  TEXT,
            lease_expires TEXT,
            result_seq    INTEGER,
            seq           INTEGER
        );
        """)
    recover_on_startup()

def recover_on_startup():
    now_iso = utcnow_iso()
    with get_db() as db:
        # Recover stale work leases
        stale_work = db.execute(
            "SELECT work_id, lease_id FROM work_items WHERE status='leased' AND lease_expires < ?", (now_iso,)
        ).fetchall()
        for row in stale_work:
            _emit_event(db, "system", "work_lease_expired",
                        {"work_id": row["work_id"], "lease_id": row["lease_id"], "reason": "server_restart"})
            db.execute("UPDATE work_items SET status='ready', lease_id=NULL, lease_holder=NULL, lease_expires=NULL WHERE work_id=?",
                       (row["work_id"],))
        # Recover stale decision claims
        stale_claims = db.execute(
            "SELECT request_id FROM decision_state WHERE status='claimed' AND claim_expires < ?", (now_iso,)
        ).fetchall()
        for row in stale_claims:
            _emit_event(db, "system", "decision_claim_expired",
                        {"request_id": row["request_id"], "reason": "server_restart"})
            db.execute("UPDATE decision_state SET status='approved', claimed_at=NULL, claim_expires=NULL WHERE request_id=?",
                       (row["request_id"],))
        if stale_work or stale_claims:
            db.commit()

# ── Event helpers ─────────────────────────────────────────────────────────────
def _emit_event(db, principal, type_, payload):
    eid = str(uuid.uuid4())
    db.execute("INSERT INTO events (id,ts,principal,type,payload) VALUES (?,?,?,?,?)",
               (eid, utcnow_iso(), principal, type_, json.dumps(payload)))
    return db.execute("SELECT seq FROM events WHERE id=?", (eid,)).fetchone()["seq"]

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

# ── Cycle detection ───────────────────────────────────────────────────────────
def has_cycle(db, mission_id: str, new_work_id: str, new_deps: list) -> bool:
    """DFS from new_work_id following deps — cycle if we reach new_work_id again."""
    all_items = db.execute("SELECT work_id, deps_json FROM work_items WHERE mission_id=?", (mission_id,)).fetchall()
    graph = {r["work_id"]: json.loads(r["deps_json"]) for r in all_items}
    graph[new_work_id] = new_deps

    visited = set()
    def dfs(node):
        if node == new_work_id and node in visited:
            return True
        if node in visited:
            return False
        visited.add(node)
        for dep in graph.get(node, []):
            if dfs(dep):
                return True
        visited.discard(node)
        return False

    visited.add(new_work_id)
    for dep in new_deps:
        if dep == new_work_id:
            return True
        if dfs(dep):
            return True
    return False

# ── Lazy decision claim recovery ──────────────────────────────────────────────
def recover_decision_claim_if_expired(db, request_id: str) -> bool:
    """Returns True if claim was expired and recovered."""
    row = db.execute("SELECT status, claim_expires FROM decision_state WHERE request_id=?", (request_id,)).fetchone()
    if not row or row["status"] != "claimed":
        return False
    if row["claim_expires"] and is_expired(row["claim_expires"]):
        _emit_event(db, "system", "decision_claim_expired", {"request_id": request_id, "reason": "lazy_recovery"})
        db.execute("UPDATE decision_state SET status='approved', claimed_at=NULL, claim_expires=NULL WHERE request_id=?",
                   (request_id,))
        db.commit()
        return True
    return False

# ── App ───────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global KEYS
    KEYS = {
        _require_env("CLAUDE_KEY"):  "claude",
        _require_env("SOBA_KEY"):    "soba",
        _require_env("ARCIDES_KEY"): "arcides",
    }
    init_db()
    yield

app = FastAPI(title="SPHERA Local v0.2", lifespan=lifespan)

def _ok(data, status=200): return JSONResponse(content=data, status_code=status)
def _err(msg, status=400): return JSONResponse(content={"error": msg}, status_code=status)

def _auth(authorization: str) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Unauthorized")
    token = authorization[7:].strip()
    if token not in KEYS:
        raise HTTPException(401, "Unauthorized")
    return KEYS[token]

# ── Messages ──────────────────────────────────────────────────────────────────
@app.post("/message")
async def post_message(request: Request, authorization: str = Header(default="")):
    principal = _auth(authorization)
    body = await request.json()
    content = (body.get("content") or "").strip()
    if not content:
        return _err("content required")
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict) and str(parsed.get("type", "")).startswith("decision_"):
            return _err("Decision events must use /decision endpoints", 400)
    except Exception:
        pass
    with get_db() as db:
        seq = _emit_event(db, principal, "message", {"content": content})
        db.commit()
    return _ok({"ok": True, "seq": seq}, 201)

@app.get("/events")
async def read_events(after: int = 0, authorization: str = Header(default="")):
    _auth(authorization)
    with get_db() as db:
        rows = db.execute("SELECT * FROM events WHERE seq > ? ORDER BY seq", (after,)).fetchall()
    events = [{"seq": r["seq"], "id": r["id"], "timestamp": r["ts"],
               "principal": r["principal"], "type": r["type"], **json.loads(r["payload"])} for r in rows]
    cursor = events[-1]["seq"] if events else after
    return _ok({"events": events, "count": len(events), "cursor": cursor})

# ── Decisions ─────────────────────────────────────────────────────────────────
@app.post("/decision")
async def decision_request(request: Request, authorization: str = Header(default="")):
    principal = _auth(authorization)
    body = await request.json()
    scope  = body.get("scope")
    target = body.get("target")
    params = body.get("params")
    if not scope  or not isinstance(scope, str):  return _err('"scope" required')
    if not target or not isinstance(target, str): return _err('"target" required')
    if not isinstance(params, dict):              return _err('"params" must be an object')
    deadline = body.get("deadline")
    if deadline:
        try:
            parse_dt(deadline)
        except Exception:
            return _err('"deadline" must be a valid UTC ISO timestamp')

    digest     = compute_digest(scope, target, principal, params)
    request_id = str(uuid.uuid4())
    with get_db() as db:
        seq = _emit_event(db, principal, "decision_requested",
                          {"request_id": request_id, "scope": scope, "target": target,
                           "params": params, "payload_digest": digest, "deadline": deadline})
        db.execute("INSERT INTO decision_state VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (request_id, "pending", principal, scope, target, json.dumps(params), digest, deadline, None, None))
        db.commit()
    return _ok({"ok": True, "request_id": request_id, "seq": seq, "payload_digest": digest}, 201)

@app.post("/decision/{request_id}/approve")
async def decision_approve(request_id: str, request: Request, authorization: str = Header(default="")):
    principal = _auth(authorization)
    if principal != "arcides":
        raise HTTPException(403, "Only arcides may approve")
    body = await request.json()
    with get_db() as db:
        row = db.execute("SELECT * FROM decision_state WHERE request_id=?", (request_id,)).fetchone()
        if not row:                   return _err("Not found", 404)
        if row["status"] != "pending": return _err(f"Cannot approve: status is '{row['status']}'", 409)
        if row["deadline"] and is_expired(row["deadline"]):
            _emit_event(db, "system", "decision_expired", {"request_id": request_id})
            db.execute("UPDATE decision_state SET status='expired' WHERE request_id=?", (request_id,))
            db.commit()
            return _err("Decision has expired", 410)
        seq = _emit_event(db, principal, "decision_approved",
                          {"request_id": request_id, "bound_digest": row["bound_digest"], "note": body.get("note")})
        db.execute("UPDATE decision_state SET status='approved' WHERE request_id=?", (request_id,))
        db.commit()
    return _ok({"ok": True, "seq": seq, "request_id": request_id}, 201)

@app.post("/decision/{request_id}/reject")
async def decision_reject(request_id: str, request: Request, authorization: str = Header(default="")):
    principal = _auth(authorization)
    if principal != "arcides":
        raise HTTPException(403, "Only arcides may reject")
    body = await request.json()
    with get_db() as db:
        row = db.execute("SELECT * FROM decision_state WHERE request_id=?", (request_id,)).fetchone()
        if not row:                   return _err("Not found", 404)
        if row["status"] != "pending": return _err(f"Cannot reject: status is '{row['status']}'", 409)
        seq = _emit_event(db, principal, "decision_rejected",
                          {"request_id": request_id, "reason": body.get("reason")})
        db.execute("UPDATE decision_state SET status='rejected' WHERE request_id=?", (request_id,))
        db.commit()
    return _ok({"ok": True, "seq": seq}, 201)

@app.post("/decision/{request_id}/claim")
async def decision_claim(request_id: str, request: Request, authorization: str = Header(default="")):
    principal = _auth(authorization)
    body   = await request.json()
    params = body.get("params")
    if not isinstance(params, dict): return _err('"params" required to verify digest')
    with get_db() as db:
        # Lazy claim recovery
        recover_decision_claim_if_expired(db, request_id)
        row = db.execute("SELECT * FROM decision_state WHERE request_id=?", (request_id,)).fetchone()
        if not row:                    return _err("Not found", 404)
        if row["status"] != "approved": return _err(f"Cannot claim: status is '{row['status']}'", 409)
        if row["requesting_principal"] != principal:
            raise HTTPException(403, "Only the requesting principal may claim")
        if row["deadline"] and is_expired(row["deadline"]):
            _emit_event(db, "system", "decision_expired", {"request_id": request_id})
            db.execute("UPDATE decision_state SET status='expired' WHERE request_id=?", (request_id,))
            db.commit()
            return _err("Decision has expired", 410)
        computed = compute_digest(row["scope"], row["target"], principal, params)
        if computed != row["bound_digest"]:
            return _err("Payload digest mismatch", 422)
        now_iso      = utcnow_iso()
        claim_exp    = (utcnow() + timedelta(seconds=CLAIM_LEASE_SECS)).isoformat()
        seq = _emit_event(db, principal, "decision_claimed", {"request_id": request_id})
        db.execute("UPDATE decision_state SET status='claimed', claimed_at=?, claim_expires=? WHERE request_id=?",
                   (now_iso, claim_exp, request_id))
        db.commit()
    return _ok({"ok": True, "seq": seq, "claim_expires": claim_exp}, 201)

@app.post("/decision/{request_id}/consume")
async def decision_consume(request_id: str, authorization: str = Header(default="")):
    principal = _auth(authorization)
    with get_db() as db:
        recover_decision_claim_if_expired(db, request_id)
        row = db.execute("SELECT * FROM decision_state WHERE request_id=?", (request_id,)).fetchone()
        if not row:                    return _err("Not found", 404)
        if row["status"] != "claimed": return _err(f"Cannot consume: status is '{row['status']}'", 409)
        if row["requesting_principal"] != principal: raise HTTPException(403, "Only requester may consume")
        seq = _emit_event(db, principal, "decision_consumed", {"request_id": request_id})
        db.execute("UPDATE decision_state SET status='consumed' WHERE request_id=?", (request_id,))
        db.commit()
    return _ok({"ok": True, "seq": seq}, 201)

@app.post("/decision/{request_id}/fail")
async def decision_fail(request_id: str, request: Request, authorization: str = Header(default="")):
    principal = _auth(authorization)
    body = await request.json()
    with get_db() as db:
        recover_decision_claim_if_expired(db, request_id)
        row = db.execute("SELECT * FROM decision_state WHERE request_id=?", (request_id,)).fetchone()
        if not row:                    return _err("Not found", 404)
        if row["status"] != "claimed": return _err(f"Cannot fail: status is '{row['status']}'", 409)
        if row["requesting_principal"] != principal: raise HTTPException(403, "Only requester may report failure")
        seq = _emit_event(db, principal, "execution_failed",
                          {"request_id": request_id, "reason": body.get("error", "unknown")})
        db.execute("UPDATE decision_state SET status='approved', claimed_at=NULL, claim_expires=NULL WHERE request_id=?",
                   (request_id,))
        db.commit()
    return _ok({"ok": True, "seq": seq, "status": "approved"}, 201)

@app.get("/decision/{request_id}")
async def get_decision(request_id: str, authorization: str = Header(default="")):
    _auth(authorization)
    with get_db() as db:
        row = db.execute("SELECT * FROM decision_state WHERE request_id=?", (request_id,)).fetchone()
        if not row: return _err("Not found", 404)
        return _ok({"found": True, "request_id": request_id, "status": row["status"],
                    "scope": row["scope"], "target": row["target"],
                    "requesting_principal": row["requesting_principal"],
                    "bound_digest": row["bound_digest"], "deadline": row["deadline"],
                    "claim_expires": row["claim_expires"]})

# ── Missions ──────────────────────────────────────────────────────────────────
@app.post("/mission")
async def create_mission(request: Request, authorization: str = Header(default="")):
    principal = _auth(authorization)
    body      = await request.json()
    objective = body.get("objective")
    if not objective: return _err('"objective" required')
    policy = body.get("acceptance_policy", {})
    mid    = str(uuid.uuid4())
    with get_db() as db:
        seq = _emit_event(db, principal, "mission_created",
                          {"mission_id": mid, "objective": objective, "owner": principal, "policy": policy})
        db.execute("INSERT INTO missions VALUES (?,?,?,?,?,?,?,?,?)",
                   (mid, objective, principal, "active", json.dumps(policy), utcnow_iso(), None, None, seq))
        db.commit()
    return _ok({"ok": True, "mission_id": mid, "seq": seq}, 201)

@app.post("/mission/{mission_id}/work")
async def create_work_item(mission_id: str, request: Request, authorization: str = Header(default="")):
    principal = _auth(authorization)
    body = await request.json()
    desc = body.get("description")
    cap  = body.get("capability")
    deps = body.get("dependencies", [])
    if not desc: return _err('"description" required')
    if not cap:  return _err('"capability" required')
    if not isinstance(deps, list): return _err('"dependencies" must be a list')

    with get_db() as db:
        mission = db.execute("SELECT owner FROM missions WHERE mission_id=?", (mission_id,)).fetchone()
        if not mission: return _err("Mission not found", 404)
        # 5. Mission authority: only owner may add work
        if mission["owner"] != principal:
            raise HTTPException(403, f"Only mission owner '{mission['owner']}' may add work items")

        wid = str(uuid.uuid4())
        # 4. Dependency integrity
        for dep in deps:
            if dep == wid: return _err("Self-dependency not allowed", 400)
            dep_row = db.execute("SELECT mission_id FROM work_items WHERE work_id=?", (dep,)).fetchone()
            if not dep_row:                        return _err(f"Dependency '{dep}' not found", 400)
            if dep_row["mission_id"] != mission_id: return _err(f"Dependency '{dep}' belongs to a different mission", 400)
        if deps and has_cycle(db, mission_id, wid, deps):
            return _err("Dependency cycle detected", 400)

        status = "blocked" if deps else "ready"
        seq    = _emit_event(db, principal, "work_item_created",
                             {"work_id": wid, "mission_id": mission_id, "description": desc,
                              "capability": cap, "dependencies": deps, "status": status})
        db.execute("INSERT INTO work_items VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (wid, mission_id, desc, cap, json.dumps(deps), status, None, None, None, None, seq))
        db.commit()
    return _ok({"ok": True, "work_id": wid, "status": status, "seq": seq}, 201)

@app.post("/work/{work_id}/claim")
async def claim_work(work_id: str, request: Request, authorization: str = Header(default="")):
    principal = _auth(authorization)
    body = await request.json()
    # 3. Clamp lease duration
    try:
        raw_secs = int(body.get("lease_seconds", 60))
    except (TypeError, ValueError):
        return _err('"lease_seconds" must be an integer', 400)
    if raw_secs < 1 or raw_secs > MAX_LEASE:
        return _err(f'"lease_seconds" must be between 1 and {MAX_LEASE}', 400)

    lid     = str(uuid.uuid4())
    expires = (utcnow() + timedelta(seconds=raw_secs)).isoformat()

    with get_db() as db:
        # 1. ATOMIC CLAIM via conditional UPDATE + rowcount check
        # First expire stale lease if any
        stale = db.execute(
            "SELECT lease_id FROM work_items WHERE work_id=? AND status='leased' AND lease_expires < ?",
            (work_id, utcnow_iso())
        ).fetchone()
        if stale:
            _emit_event(db, "system", "work_lease_expired",
                        {"work_id": work_id, "lease_id": stale["lease_id"], "reason": "lazy_expiry"})
            db.execute("UPDATE work_items SET status='ready', lease_id=NULL, lease_holder=NULL, lease_expires=NULL WHERE work_id=?",
                       (work_id,))
            db.commit()

        # Check blocked status and unblock if deps done
        row = db.execute("SELECT * FROM work_items WHERE work_id=?", (work_id,)).fetchone()
        if not row:               return _err("Work item not found", 404)
        if row["status"] == "done":   return _err("Work item is already done", 409)
        if row["status"] == "failed": return _err("Work item has failed", 409)
        if row["status"] == "blocked":
            deps = json.loads(row["deps_json"])
            for dep in deps:
                dep_row = db.execute("SELECT status FROM work_items WHERE work_id=?", (dep,)).fetchone()
                if not dep_row or dep_row["status"] != "done":
                    return _err(f"Dependency '{dep}' is not done", 409)
            db.execute("UPDATE work_items SET status='ready' WHERE work_id=?", (work_id,))
            db.commit()

        # Atomic claim: UPDATE only if status='ready', check rowcount=1
        cursor = db.execute(
            "UPDATE work_items SET status='leased', lease_id=?, lease_holder=?, lease_expires=? WHERE work_id=? AND status='ready'",
            (lid, principal, expires, work_id)
        )
        if cursor.rowcount != 1:
            db.rollback()
            return _err("Work item is not available to claim (concurrent claim or wrong status)", 409)
        seq = _emit_event(db, principal, "work_claimed",
                          {"work_id": work_id, "lease_id": lid, "lease_expires": expires})
        db.commit()
    return _ok({"ok": True, "lease_id": lid, "lease_expires": expires, "seq": seq}, 201)

@app.post("/work/{work_id}/heartbeat")
async def heartbeat(work_id: str, request: Request, authorization: str = Header(default="")):
    principal = _auth(authorization)
    body = await request.json()
    lease_id = body.get("lease_id")
    try:
        extend = int(body.get("extend_seconds", 60))
    except (TypeError, ValueError):
        return _err('"extend_seconds" must be an integer', 400)
    # 3. Clamp extension
    if extend < 1 or extend > MAX_LEASE:
        return _err(f'"extend_seconds" must be between 1 and {MAX_LEASE}', 400)

    with get_db() as db:
        row = db.execute("SELECT * FROM work_items WHERE work_id=?", (work_id,)).fetchone()
        if not row:                    return _err("Not found", 404)
        if row["status"] != "leased":  return _err("Not leased", 409)
        if row["lease_id"] != lease_id: return _err("Lease ID mismatch", 403)
        if row["lease_holder"] != principal: return _err("Not your lease", 403)
        # 2. Heartbeat must not revive expired lease
        if is_expired(row["lease_expires"]):
            _emit_event(db, "system", "work_lease_expired",
                        {"work_id": work_id, "lease_id": lease_id, "reason": "heartbeat_on_expired"})
            db.execute("UPDATE work_items SET status='ready', lease_id=NULL, lease_holder=NULL, lease_expires=NULL WHERE work_id=?",
                       (work_id,))
            db.commit()
            return _err("Lease has already expired — cannot renew", 410)
        new_exp = (utcnow() + timedelta(seconds=extend)).isoformat()
        _emit_event(db, principal, "work_heartbeat",
                    {"work_id": work_id, "lease_id": lease_id, "new_expiry": new_exp})
        db.execute("UPDATE work_items SET lease_expires=? WHERE work_id=?", (new_exp, work_id))
        db.commit()
    return _ok({"ok": True, "new_expiry": new_exp}, 200)

@app.post("/work/{work_id}/result")
async def submit_result(work_id: str, request: Request, authorization: str = Header(default="")):
    principal = _auth(authorization)
    body      = await request.json()
    lease_id  = body.get("lease_id")
    result    = body.get("result")
    if not lease_id:    return _err('"lease_id" required')
    if result is None:  return _err('"result" required')
    with get_db() as db:
        row = db.execute("SELECT * FROM work_items WHERE work_id=?", (work_id,)).fetchone()
        if not row:                      return _err("Not found", 404)
        if row["lease_id"] != lease_id:  return _err("Stale lease", 403)
        if row["lease_holder"] != principal: return _err("Not your lease", 403)
        if row["status"] != "leased":    return _err("Not leased", 409)
        if is_expired(row["lease_expires"]):
            return _err("Lease expired — result rejected", 403)
        seq = _emit_event(db, principal, "work_result",
                          {"work_id": work_id, "lease_id": lease_id, "result": result})
        db.execute("UPDATE work_items SET status='done', result_seq=?, lease_id=NULL, lease_holder=NULL WHERE work_id=?",
                   (seq, work_id))
        # Unblock dependents
        blocked = db.execute("SELECT work_id, deps_json FROM work_items WHERE status='blocked'").fetchall()
        for b in blocked:
            bdeps = json.loads(b["deps_json"])
            if work_id in bdeps:
                if all(db.execute("SELECT status FROM work_items WHERE work_id=?", (d,)).fetchone()["status"] == "done"
                       for d in bdeps):
                    db.execute("UPDATE work_items SET status='ready' WHERE work_id=?", (b["work_id"],))
                    _emit_event(db, "system", "work_item_unblocked", {"work_id": b["work_id"]})
        db.commit()
    return _ok({"ok": True, "seq": seq}, 201)

@app.get("/missions/{mission_id}/next")
async def next_action(mission_id: str, authorization: str = Header(default="")):
    _auth(authorization)
    with get_db() as db:
        mission = db.execute("SELECT * FROM missions WHERE mission_id=?", (mission_id,)).fetchone()
        if not mission: return _err("Not found", 404)
        items = db.execute("SELECT * FROM work_items WHERE mission_id=?", (mission_id,)).fetchall()
        ready   = [{"work_id": i["work_id"], "description": i["description"], "capability": i["capability"]}
                   for i in items if i["status"] == "ready"]
        blocked = [{"work_id": i["work_id"], "description": i["description"], "waiting_on": json.loads(i["deps_json"])}
                   for i in items if i["status"] == "blocked"]
        leased  = [{"work_id": i["work_id"], "holder": i["lease_holder"], "expires": i["lease_expires"]}
                   for i in items if i["status"] == "leased"]
        done_ids = [i["work_id"] for i in items if i["status"] == "done"]
        all_work_done = len(items) > 0 and all(i["status"] == "done" for i in items)
        # Mission acceptance gate
        status = mission["status"]
        needs_acceptance = all_work_done and status == "active"
        return _ok({"mission_id": mission_id, "objective": mission["objective"],
                    "status": status, "ready": ready, "blocked": blocked, "leased": leased,
                    "done_count": len(done_ids), "total": len(items),
                    "needs_owner_acceptance": needs_acceptance,
                    "accepted_at": mission["accepted_at"],
                    "acceptance_note": mission["acceptance_note"]})

@app.post("/mission/{mission_id}/accept")
async def accept_mission(mission_id: str, request: Request, authorization: str = Header(default="")):
    """Owner explicitly accepts completed mission — not self-asserted by principals."""
    principal = _auth(authorization)
    body = await request.json()
    with get_db() as db:
        mission = db.execute("SELECT * FROM missions WHERE mission_id=?", (mission_id,)).fetchone()
        if not mission: return _err("Not found", 404)
        if mission["owner"] != principal:
            raise HTTPException(403, "Only mission owner may accept")
        if mission["status"] != "active": return _err(f"Mission status is '{mission['status']}'", 409)
        items = db.execute("SELECT status FROM work_items WHERE mission_id=?", (mission_id,)).fetchall()
        if not all(i["status"] == "done" for i in items):
            return _err("Cannot accept: not all work items are done", 409)
        note    = body.get("note", "")
        now_iso = utcnow_iso()
        seq = _emit_event(db, principal, "mission_accepted",
                          {"mission_id": mission_id, "note": note})
        db.execute("UPDATE missions SET status='complete', accepted_at=?, acceptance_note=? WHERE mission_id=?",
                   (now_iso, note, mission_id))
        db.commit()
    return _ok({"ok": True, "seq": seq, "mission_id": mission_id, "status": "complete"}, 201)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
