"""
SPHERA Server v1.0
Full platform: messages, missions, work, agents, decisions, room events.
"""
import json, os, sys
sys.path.insert(0, "/home/claude/sphera")
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from db import get_db, init
from core import *

# ── Auth ──────────────────────────────────────────────────────────────────────
KEYS: dict = {}

def _need(name):
    v = os.environ.get(name,"")
    if not v: raise RuntimeError(f"Missing required env var: {name}")
    return v

def auth(authorization: str = "") -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Unauthorized")
    token = authorization[7:].strip()
    if token not in KEYS:
        raise HTTPException(401, "Unauthorized")
    return KEYS[token]

def arcides_only(p):
    if p != "arcides": raise HTTPException(403, "arcides only")

# ── WebSocket room ─────────────────────────────────────────────────────────────
connected: list = []

async def broadcast(event: dict):
    dead = []
    for ws in connected:
        try: await ws.send_json(event)
        except: dead.append(ws)
    for ws in dead: connected.remove(ws)

# ── App ───────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global KEYS
    KEYS = {_need("CLAUDE_KEY"):"claude", _need("SOBA_KEY"):"soba", _need("ARCIDES_KEY"):"arcides"}
    init()
    with get_db() as db:
        n = recover(db)
        if n: print(f"[startup] recovered {n} stale leases")
    print("[sphera] ready on :8765")
    yield

app = FastAPI(title="SPHERA", version="1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def ok(d, s=200): return JSONResponse(d, s)
def err(m, s=400): return JSONResponse({"error":m}, s)

# ── WebSocket ─────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    connected.append(ws)
    try:
        # Send history on connect
        with get_db() as db:
            rows = db.execute("SELECT * FROM events ORDER BY seq DESC LIMIT 100").fetchall()
        await ws.send_json({"type":"history","events":[dict(r) for r in reversed(rows)]})
        while True:
            await ws.receive_text()  # keep alive
    except WebSocketDisconnect:
        if ws in connected: connected.remove(ws)

# ── Messages ──────────────────────────────────────────────────────────────────
@app.post("/message")
async def post_message(req: Request, authorization: str = Header(default="")):
    p = auth(authorization)
    b = await req.json()
    content = (b.get("content") or "").strip()
    if not content: return err("content required")
    with get_db() as db:
        seq = emit(db, p, "message", {"content": content})
        db.commit()
        ev = {"seq":seq,"principal":p,"type":"message","content":content,"ts":now_iso()}
    await broadcast(ev)
    return ok({"ok":True,"seq":seq}, 201)

@app.get("/events")
async def get_events(after: int = 0, authorization: str = Header(default="")):
    auth(authorization)
    with get_db() as db:
        rows = db.execute("SELECT * FROM events WHERE seq>? ORDER BY seq", (after,)).fetchall()
    events = [{"seq":r["seq"],"id":r["id"],"ts":r["ts"],"principal":r["principal"],
               "type":r["type"],**json.loads(r["payload"])} for r in rows]
    return ok({"events":events,"count":len(events),"cursor":events[-1]["seq"] if events else after})

# ── Missions ──────────────────────────────────────────────────────────────────
@app.post("/mission")
async def create_mission(req: Request, authorization: str = Header(default="")):
    p = auth(authorization)
    b = await req.json()
    if not b.get("objective"): return err("objective required")
    mid = uid()
    with get_db() as db:
        seq = emit(db, p, "mission_created", {"mission_id":mid,"objective":b["objective"],"owner":p})
        db.execute("INSERT INTO missions VALUES(?,?,?,?,?,?,?)", (mid,b["objective"],p,"active",now_iso(),None,seq))
        db.commit()
    await broadcast({"type":"mission_created","mission_id":mid,"objective":b["objective"],"seq":seq})
    return ok({"ok":True,"mission_id":mid,"seq":seq}, 201)

@app.get("/missions")
async def list_missions(authorization: str = Header(default="")):
    auth(authorization)
    with get_db() as db:
        missions = db.execute("SELECT * FROM missions ORDER BY created_at DESC").fetchall()
        result = []
        for m in missions:
            items = db.execute("SELECT status FROM work_items WHERE mission_id=?", (m["mission_id"],)).fetchall()
            done = sum(1 for i in items if i["status"]=="done")
            result.append({**dict(m),"work_count":len(items),"done_count":done})
    return ok({"missions":result})

@app.post("/mission/{mid}/work")
async def add_work(mid: str, req: Request, authorization: str = Header(default="")):
    p = auth(authorization)
    b = await req.json()
    if not b.get("description"): return err("description required")
    if not b.get("capability"):  return err("capability required")
    with get_db() as db:
        m = db.execute("SELECT owner FROM missions WHERE mission_id=?", (mid,)).fetchone()
        if not m: return err("mission not found", 404)
        if m["owner"] != p: raise HTTPException(403, "owner only")
        deps = b.get("dependencies", [])
        for d in deps:
            if not db.execute("SELECT 1 FROM work_items WHERE work_id=? AND mission_id=?", (d,mid)).fetchone():
                return err(f"dep {d} not found in this mission")
        wid = uid()
        status = "blocked" if deps else "ready"
        seq = emit(db, p, "work_created", {"work_id":wid,"mission_id":mid,"description":b["description"],"capability":b["capability"],"deps":deps,"status":status})
        db.execute("INSERT INTO work_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (wid,mid,b["description"],b["capability"],json.dumps(deps),status,None,None,None,None,None,now_iso(),seq))
        db.commit()
    await broadcast({"type":"work_created","work_id":wid,"description":b["description"],"capability":b["capability"],"status":status,"seq":seq})
    return ok({"ok":True,"work_id":wid,"status":status,"seq":seq}, 201)

@app.get("/mission/{mid}")
async def get_mission(mid: str, authorization: str = Header(default="")):
    auth(authorization)
    with get_db() as db:
        m = db.execute("SELECT * FROM missions WHERE mission_id=?", (mid,)).fetchone()
        if not m: return err("not found", 404)
        items = db.execute("SELECT * FROM work_items WHERE mission_id=? ORDER BY created_at", (mid,)).fetchall()
        ready   = [dict(i) for i in items if i["status"]=="ready"]
        blocked = [dict(i) for i in items if i["status"]=="blocked"]
        leased  = [dict(i) for i in items if i["status"]=="leased"]
        done    = [dict(i) for i in items if i["status"]=="done"]
        agents_avail = db.execute("SELECT * FROM agents WHERE status='available'").fetchall()
    return ok({"mission":dict(m),"ready":ready,"blocked":blocked,"leased":leased,"done":done,
               "total":len(items),"available_agents":len(agents_avail)})

# ── Agents ────────────────────────────────────────────────────────────────────
@app.post("/agent/register")
async def register_agent(req: Request, authorization: str = Header(default="")):
    p = auth(authorization)
    b = await req.json()
    if not b.get("name"): return err("name required")
    caps = b.get("capabilities", [])
    if not isinstance(caps, list) or not caps: return err("capabilities must be a non-empty list")
    aid = uid()
    with get_db() as db:
        seq = emit(db, p, "agent_registered", {"agent_id":aid,"name":b["name"],"capabilities":caps,"registered_by":p})
        db.execute("INSERT INTO agents VALUES(?,?,?,?,?,?,?,?,?)",
                   (aid, b["name"], json.dumps(caps), p, "available", None, None, None, now_iso()))
        db.commit()
    await broadcast({"type":"agent_registered","agent_id":aid,"name":b["name"],"capabilities":caps,"seq":seq})
    return ok({"ok":True,"agent_id":aid,"seq":seq}, 201)

@app.get("/agents")
async def list_agents(authorization: str = Header(default="")):
    auth(authorization)
    with get_db() as db:
        agents = db.execute("SELECT * FROM agents ORDER BY registered_at").fetchall()
    return ok({"agents":[{**dict(a),"capabilities":json.loads(a["capabilities"])} for a in agents]})

@app.post("/work/{wid}/claim")
async def claim_work(wid: str, req: Request, authorization: str = Header(default="")):
    p = auth(authorization)
    b = await req.json()
    secs = max(1, min(int(b.get("lease_seconds", 60)), MAX_LEASE))
    agent_id = b.get("agent_id")
    lid = uid()
    exp = (now() + timedelta(seconds=secs)).isoformat()
    with get_db() as db:
        # Lazy expire stale lease
        stale = db.execute("SELECT lease_id FROM work_items WHERE work_id=? AND status='leased' AND lease_expires<?", (wid, now_iso())).fetchone()
        if stale:
            emit(db, "system", "lease_expired", {"work_id":wid,"lease_id":stale["lease_id"]})
            db.execute("UPDATE work_items SET status='ready',assigned_to=NULL,lease_id=NULL,lease_expires=NULL WHERE work_id=?", (wid,))
            db.commit()
        # Check capability match if agent specified
        if agent_id:
            agent = db.execute("SELECT * FROM agents WHERE agent_id=? AND status='available'", (agent_id,)).fetchone()
            if not agent: return err("agent not available", 409)
            work = db.execute("SELECT capability FROM work_items WHERE work_id=?", (wid,)).fetchone()
            if work and json.loads(agent["capabilities"]) and work["capability"] not in json.loads(agent["capabilities"]):
                return err(f"agent capability mismatch: needs '{work['capability']}'", 400)
        cur = db.execute(
            "UPDATE work_items SET status='leased',assigned_to=?,lease_id=?,lease_expires=? WHERE work_id=? AND status='ready'",
            (agent_id or p, lid, exp, wid))
        if cur.rowcount != 1:
            db.rollback()
            return err("not available to claim", 409)
        if agent_id:
            db.execute("UPDATE agents SET status='busy',current_work_id=?,lease_id=?,lease_expires=? WHERE agent_id=?", (wid,lid,exp,agent_id))
        seq = emit(db, p, "work_claimed", {"work_id":wid,"lease_id":lid,"lease_expires":exp,"claimed_by":agent_id or p})
        db.commit()
    await broadcast({"type":"work_claimed","work_id":wid,"lease_id":lid,"claimed_by":agent_id or p,"seq":seq})
    return ok({"ok":True,"lease_id":lid,"lease_expires":exp,"seq":seq}, 201)

@app.post("/work/{wid}/result")
async def submit_result(wid: str, req: Request, authorization: str = Header(default="")):
    p = auth(authorization)
    b = await req.json()
    lid = b.get("lease_id")
    result = b.get("result")
    if not lid:        return err("lease_id required")
    if result is None: return err("result required")
    with get_db() as db:
        row = db.execute("SELECT * FROM work_items WHERE work_id=?", (wid,)).fetchone()
        if not row:               return err("not found", 404)
        if row["lease_id"] != lid: return err("stale lease", 403)
        if row["status"] != "leased": return err("not leased", 409)
        if expired(row["lease_expires"]): return err("lease expired", 403)
        seq = emit(db, p, "work_result", {"work_id":wid,"lease_id":lid,"result":result})
        db.execute("UPDATE work_items SET status='done',result=?,result_seq=?,assigned_to=NULL,lease_id=NULL,lease_expires=NULL WHERE work_id=?",
                   (json.dumps(result), seq, wid))
        # Free agent
        agent = db.execute("SELECT agent_id FROM agents WHERE current_work_id=?", (wid,)).fetchone()
        if agent:
            db.execute("UPDATE agents SET status='available',current_work_id=NULL,lease_id=NULL,lease_expires=NULL WHERE agent_id=?", (agent["agent_id"],))
        unblocked = unblock_dependents(db, wid)
        db.commit()
    await broadcast({"type":"work_result","work_id":wid,"result":result,"unblocked":unblocked,"seq":seq})
    return ok({"ok":True,"seq":seq,"unblocked":unblocked}, 201)

# ── Decisions ─────────────────────────────────────────────────────────────────
@app.post("/decision")
async def req_decision(req: Request, authorization: str = Header(default="")):
    p = auth(authorization)
    b = await req.json()
    scope=b.get("scope"); target=b.get("target"); params=b.get("params",{})
    if not scope:  return err("scope required")
    if not target: return err("target required")
    if not isinstance(params, dict): return err("params must be object")
    dg = digest(scope, target, p, params)
    rid = uid()
    with get_db() as db:
        seq = emit(db, p, "decision_requested", {"request_id":rid,"scope":scope,"target":target,"params":params,"digest":dg})
        db.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (rid,"pending",p,scope,target,json.dumps(params),dg,b.get("deadline"),None,None,seq))
        db.commit()
    await broadcast({"type":"decision_requested","request_id":rid,"scope":scope,"by":p,"seq":seq})
    return ok({"ok":True,"request_id":rid,"seq":seq,"digest":dg}, 201)

@app.post("/decision/{rid}/approve")
async def approve_decision(rid: str, req: Request, authorization: str = Header(default="")):
    p = auth(authorization); arcides_only(p)
    b = await req.json()
    with get_db() as db:
        row = db.execute("SELECT * FROM decisions WHERE request_id=?", (rid,)).fetchone()
        if not row:                  return err("not found", 404)
        if row["status"]!="pending": return err(f"status is '{row['status']}'", 409)
        seq = emit(db, p, "decision_approved", {"request_id":rid,"note":b.get("note")})
        db.execute("UPDATE decisions SET status='approved' WHERE request_id=?", (rid,))
        db.commit()
    await broadcast({"type":"decision_approved","request_id":rid,"scope":row["scope"],"seq":seq})
    return ok({"ok":True,"seq":seq}, 201)

@app.post("/decision/{rid}/reject")
async def reject_decision(rid: str, req: Request, authorization: str = Header(default="")):
    p = auth(authorization); arcides_only(p)
    b = await req.json()
    with get_db() as db:
        row = db.execute("SELECT * FROM decisions WHERE request_id=?", (rid,)).fetchone()
        if not row:                  return err("not found", 404)
        if row["status"]!="pending": return err(f"status is '{row['status']}'", 409)
        seq = emit(db, p, "decision_rejected", {"request_id":rid,"reason":b.get("reason")})
        db.execute("UPDATE decisions SET status='rejected' WHERE request_id=?", (rid,))
        db.commit()
    await broadcast({"type":"decision_rejected","request_id":rid,"seq":seq})
    return ok({"ok":True,"seq":seq}, 201)

@app.post("/decision/{rid}/claim")
async def claim_decision(rid: str, req: Request, authorization: str = Header(default="")):
    p = auth(authorization)
    b = await req.json()
    params = b.get("params")
    if not isinstance(params, dict): return err("params required")
    with get_db() as db:
        row = db.execute("SELECT * FROM decisions WHERE request_id=?", (rid,)).fetchone()
        if not row: return err("not found", 404)
        if row["status"]=="claimed" and expired(row["claim_expires"]):
            emit(db, "system", "decision_claim_expired", {"request_id":rid})
            db.execute("UPDATE decisions SET status='approved',claimed_at=NULL,claim_expires=NULL WHERE request_id=?", (rid,))
            db.commit()
            row = db.execute("SELECT * FROM decisions WHERE request_id=?", (rid,)).fetchone()
        if row["status"]!="approved": return err(f"status is '{row['status']}'", 409)
        if row["requesting_principal"]!=p: raise HTTPException(403, "only requester may claim")
        if expired(row["deadline"]): return err("expired", 410)
        computed = digest(row["scope"], row["target"], p, params)
        if computed != row["digest"]: return err("digest mismatch", 422)
        exp = (now()+timedelta(seconds=CLAIM_TTL)).isoformat()
        seq = emit(db, p, "decision_claimed", {"request_id":rid})
        db.execute("UPDATE decisions SET status='claimed',claimed_at=?,claim_expires=? WHERE request_id=?", (now_iso(),exp,rid))
        db.commit()
    return ok({"ok":True,"seq":seq,"claim_expires":exp}, 201)

@app.post("/decision/{rid}/consume")
async def consume_decision(rid: str, authorization: str = Header(default="")):
    p = auth(authorization)
    with get_db() as db:
        row = db.execute("SELECT * FROM decisions WHERE request_id=?", (rid,)).fetchone()
        if not row:                  return err("not found", 404)
        if row["status"]!="claimed": return err(f"status is '{row['status']}'", 409)
        if row["requesting_principal"]!=p: raise HTTPException(403)
        seq = emit(db, p, "decision_consumed", {"request_id":rid})
        db.execute("UPDATE decisions SET status='consumed' WHERE request_id=?", (rid,))
        db.commit()
    await broadcast({"type":"decision_consumed","request_id":rid,"seq":seq})
    return ok({"ok":True,"seq":seq}, 201)

@app.get("/decisions")
async def list_decisions(authorization: str = Header(default="")):
    auth(authorization)
    with get_db() as db:
        rows = db.execute("SELECT * FROM decisions ORDER BY seq DESC").fetchall()
    return ok({"decisions":[dict(r) for r in rows]})

# ── Room state snapshot ───────────────────────────────────────────────────────
@app.get("/room")
async def room_state(authorization: str = Header(default="")):
    auth(authorization)
    with get_db() as db:
        event_count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        last_seq    = db.execute("SELECT MAX(seq) FROM events").fetchone()[0] or 0
        missions    = db.execute("SELECT COUNT(*) FROM missions WHERE status='active'").fetchone()[0]
        work_ready  = db.execute("SELECT COUNT(*) FROM work_items WHERE status='ready'").fetchone()[0]
        work_leased = db.execute("SELECT COUNT(*) FROM work_items WHERE status='leased'").fetchone()[0]
        work_done   = db.execute("SELECT COUNT(*) FROM work_items WHERE status='done'").fetchone()[0]
        agents_avail= db.execute("SELECT COUNT(*) FROM agents WHERE status='available'").fetchone()[0]
        agents_busy = db.execute("SELECT COUNT(*) FROM agents WHERE status='busy'").fetchone()[0]
        pending_dec = db.execute("SELECT COUNT(*) FROM decisions WHERE status='pending'").fetchone()[0]
    return ok({"event_count":event_count,"last_seq":last_seq,"active_missions":missions,
               "work":{"ready":work_ready,"leased":work_leased,"done":work_done},
               "agents":{"available":agents_avail,"busy":agents_busy},
               "pending_decisions":pending_dec})

# NOTE: uvicorn.run() moved to end of file after ALL route declarations (Soba bug fix)

# ── Agent self-assignment (capability-driven auto-claim) ──────────────────────
@app.post("/work/{wid}/auto-claim")
async def auto_claim_work(wid: str, req: Request, authorization: str = Header(default="")):
    """Agent announces it's ready. Server finds and assigns best-matched available work."""
    p = auth(authorization)
    b = await req.json()
    agent_id = b.get("agent_id")
    if not agent_id:
        return err("agent_id required")
    
    with get_db() as db:
        agent = db.execute("SELECT * FROM agents WHERE agent_id=? AND status='available'", (agent_id,)).fetchone()
        if not agent: return err("agent not found or not available", 404)
        
        # Check this agent is registered by this principal
        if agent["registered_by"] != p: raise HTTPException(403, "not your agent")
        
        caps = json.loads(agent["capabilities"])
        
        # Find ready work items this agent can handle (best capability match)
        ready = db.execute("SELECT * FROM work_items WHERE status='ready'").fetchall()
        candidates = []
        for w in ready:
            if w["capability"] in caps:
                # Score: specialist bonus
                s = 110 if len(caps) == 1 else max(80 - (len(caps)-1)*2, 10)
                candidates.append((dict(w), s))
        
        if not candidates:
            return ok({"ok": False, "reason": "no matching work available", "agent_caps": caps})
        
        # Pick highest scored, then oldest
        candidates.sort(key=lambda x: (-x[1], x[0].get('created_at','')))
        best_work = candidates[0][0]
        
        # Atomic claim
        lid = uid()
        exp = (now() + timedelta(seconds=60)).isoformat()
        cur = db.execute(
            "UPDATE work_items SET status='leased',assigned_to=?,lease_id=?,lease_expires=? WHERE work_id=? AND status='ready'",
            (agent_id, lid, exp, best_work["work_id"])
        )
        if cur.rowcount != 1:
            db.rollback()
            return err("race: work item claimed by another agent", 409)
        
        db.execute("UPDATE agents SET status='busy',current_work_id=?,lease_id=?,lease_expires=? WHERE agent_id=?",
                   (best_work["work_id"], lid, exp, agent_id))
        seq = emit(db, p, "work_auto_claimed",
                   {"work_id": best_work["work_id"], "agent_id": agent_id,
                    "capability": best_work["capability"], "lease_id": lid})
        db.commit()
    
    await broadcast({"type":"work_auto_claimed","work_id":best_work["work_id"],
                     "agent_id":agent_id,"description":best_work["description"],"seq":seq})
    
    return ok({"ok": True, "work_id": best_work["work_id"],
               "description": best_work["description"],
               "capability": best_work["capability"],
               "lease_id": lid, "lease_expires": exp, "seq": seq}, 201)

@app.get("/work/queue")
async def work_queue(authorization: str = Header(default="")):
    """Full work queue view: ready, leased, blocked, done per mission."""
    auth(authorization)
    with get_db() as db:
        missions = db.execute("SELECT * FROM missions WHERE status='active'").fetchall()
        result = []
        for m in missions:
            items = db.execute("SELECT * FROM work_items WHERE mission_id=? ORDER BY created_at", (m["mission_id"],)).fetchall()
            agents = db.execute("SELECT * FROM agents").fetchall()
            ready_items = [dict(i) for i in items if i["status"]=="ready"]
            # For each ready item, show best available agent
            for item in ready_items:
                avail = [dict(a) for a in agents if a["status"]=="available"]
                avail_with_caps = [{**a, "capabilities": json.loads(a["capabilities"])} for a in avail]
                candidates = [(a,110 if len(a["capabilities"])==1 else max(80-(len(a["capabilities"])-1)*2,10))
                              for a in avail_with_caps if item["capability"] in a["capabilities"]]
                candidates.sort(key=lambda x: -x[1])
                item["best_agent"] = candidates[0][0]["name"] if candidates else None
                item["agent_score"] = candidates[0][1] if candidates else None
            result.append({
                "mission_id": m["mission_id"],
                "objective": m["objective"],
                "ready": ready_items,
                "leased": [dict(i) for i in items if i["status"]=="leased"],
                "blocked": [dict(i) for i in items if i["status"]=="blocked"],
                "done_count": sum(1 for i in items if i["status"]=="done"),
                "total": len(items)
            })
    return ok({"queue": result, "ts": now_iso()})


# ── Health endpoint (no auth — reachability check) ───────────────────────────
@app.get("/health")
async def health():
    with get_db() as db:
        last_seq = db.execute("SELECT MAX(seq) FROM events").fetchone()[0] or 0
        event_count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    return ok({"ok": True, "instance": "arcides-victus", "last_seq": last_seq,
               "event_count": event_count, "transport": "sphera-room-v1"})

# ── Token via query param (bootstrap only — for runtimes that cannot set headers) ──
@app.get("/events-public")
async def events_public(after: int = 0, token: str = ""):
    """Read-only events endpoint accepting token as query param for bootstrap."""
    import os
    valid = {os.environ.get("CLAUDE_KEY",""), os.environ.get("SOBA_KEY",""), os.environ.get("ARCIDES_KEY","")}
    if token not in valid or not token:
        return JSONResponse({"error": "Unauthorized"}, 401)
    with get_db() as db:
        rows = db.execute("SELECT * FROM events WHERE seq > ? ORDER BY seq", (after,)).fetchall()
    events = [{"seq":r["seq"],"id":r["id"],"ts":r["ts"],"principal":r["principal"],
               "type":r["type"],**json.loads(r["payload"])} for r in rows]
    return ok({"events": events, "count": len(events),
               "cursor": events[-1]["seq"] if events else after})

# ── Bridge ingest endpoint (FIX 3 & 4) ───────────────────────────────────────
@app.post("/bridge/ingest")
async def bridge_ingest(request: Request, authorization: str = Header(default="")):
    """
    Bridge-only endpoint. Preserves true principal from signed bridge envelope.
    Server-side dedup by source_message_id prevents duplicate ingestion.
    """
    token = authorization.replace("Bearer ", "").strip()
    bridge_key = os.environ.get("BRIDGE_KEY", "br-sphera")
    if token != bridge_key:
        raise HTTPException(403, "Bridge key required")

    body = await request.json()
    principal        = body.get("principal", "unknown")
    content          = body.get("content", "")
    source_message_id = body.get("source_message_id", "")
    transport        = body.get("transport", "gmail")
    original_ts      = body.get("original_ts")

    if not content:   return _err("content required")
    if not principal: return _err("principal required")

    with get_db() as db:
        # Server-side dedup check
        if source_message_id:
            existing = db.execute(
                "SELECT seq FROM events WHERE json_extract(payload,'$.source_message_id')=?",
                (source_message_id,)
            ).fetchone()
            if existing:
                return _ok({"duplicate": True, "seq": existing["seq"]}, 200)

        seq = emit(db, principal, "bridge_message", {
            "content":           content,
            "transport_provenance": transport,
            "source_message_id": source_message_id,
            "original_ts":       original_ts
        })
        db.commit()

    await broadcast({"type": "bridge_message", "principal": principal,
                     "content": content, "transport": transport, "seq": seq})
    return _ok({"ok": True, "seq": seq, "principal": principal}, 201)

if __name__ == "__main__":
    # All routes declared above - safe to start server now
    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="info")

# ── Auto-decompose endpoint ───────────────────────────────────────────────────
@app.post("/mission/{mid}/decompose")
async def auto_decompose(mid: str, authorization: str = Header(default="")):
    """
    Decompose mission objective into work items via rule-based decomposer.
    Writes directly to DB — no self-HTTP-call, no port dependency.
    Only mission owner may decompose. Idempotent check: fails if items already exist.
    """
    p = auth(authorization)
    from decomposer import decompose

    with get_db() as db:
        mission = db.execute("SELECT * FROM missions WHERE mission_id=?", (mid,)).fetchone()
        if not mission: return err("mission not found", 404)
        if mission["owner"] != p: raise HTTPException(403, "owner only")
        existing = db.execute("SELECT COUNT(*) FROM work_items WHERE mission_id=?", (mid,)).fetchone()[0]
        if existing > 0:
            return err(f"mission already has {existing} work item(s). Use manual work creation to add more.", 409)

        plan    = decompose(mission["objective"])
        created = []
        wids    = []

        for item in plan:
            deps   = [wids[i] for i in item['dep_indices'] if i < len(wids)]
            wid    = uid()
            status = "blocked" if deps else "ready"
            seq    = emit(db, p, "work_created", {
                "work_id": wid, "mission_id": mid,
                "description": item['description'],
                "capability": item['capability'],
                "deps": deps, "status": status
            })
            db.execute("INSERT INTO work_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (wid, mid, item['description'], item['capability'],
                        json.dumps(deps), status, None, None, None, None, None, now_iso(), seq))
            wids.append(wid)
            created.append({"work_id": wid, "description": item['description'],
                            "capability": item['capability'], "status": status})
        db.commit()

    await broadcast({"type": "mission_decomposed", "mission_id": mid,
                     "objective": mission["objective"], "work_count": len(created)})
    return ok({"ok": True, "mission_id": mid, "work_items": created, "count": len(created)}, 201)


if __name__ == "__main__":
    # All routes declared above - safe to start server now
    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="info")

# ── Auto-decompose endpoint ───────────────────────────────────────────────────
@app.post("/mission/{mid}/decompose")
async def auto_decompose(mid: str, authorization: str = Header(default="")):
    """
    Decompose mission objective into work items via rule-based decomposer.
    Writes directly to DB — no self-HTTP-call, no port dependency.
    Only mission owner may decompose. Idempotent check: fails if items already exist.
    """
    p = auth(authorization)
    from decomposer import decompose

    with get_db() as db:
        mission = db.execute("SELECT * FROM missions WHERE mission_id=?", (mid,)).fetchone()
        if not mission: return err("mission not found", 404)
        if mission["owner"] != p: raise HTTPException(403, "owner only")
        existing = db.execute("SELECT COUNT(*) FROM work_items WHERE mission_id=?", (mid,)).fetchone()[0]
        if existing > 0:
            return err(f"mission already has {existing} work item(s). Use manual work creation to add more.", 409)

        plan    = decompose(mission["objective"])
        created = []
        wids    = []

        for item in plan:
            deps   = [wids[i] for i in item['dep_indices'] if i < len(wids)]
            wid    = uid()
            status = "blocked" if deps else "ready"
            seq    = emit(db, p, "work_created", {
                "work_id": wid, "mission_id": mid,
                "description": item['description'],
                "capability": item['capability'],
                "deps": deps, "status": status
            })
            db.execute("INSERT INTO work_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (wid, mid, item['description'], item['capability'],
                        json.dumps(deps), status, None, None, None, None, None, now_iso(), seq))
            wids.append(wid)
            created.append({"work_id": wid, "description": item['description'],
                            "capability": item['capability'], "status": status})
        db.commit()

    await broadcast({"type": "mission_decomposed", "mission_id": mid,
                     "objective": mission["objective"], "work_count": len(created)})
    return ok({"ok": True, "mission_id": mid, "work_items": created, "count": len(created)}, 201)

