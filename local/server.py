"""
SPHERA Server v2.0 - Clean rewrite. All endpoints in correct order.
"""
import hashlib, json, os, sqlite3, uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

DB_PATH = os.environ.get("SPHERA_DB", "./sphera.db")
MAX_LEASE = 300
CLAIM_TTL = 120
KEYS: dict = {}
connected: list = []

def utcnow(): return datetime.now(timezone.utc)
def utcnow_iso(): return utcnow().isoformat()
def uid(): return str(uuid.uuid4())

def parse_dt(s):
    if not s: raise ValueError("empty")
    dt = datetime.fromisoformat(s.replace("Z","+00:00"))
    if dt.tzinfo is None: raise ValueError(f"naive: {s}")
    return dt.astimezone(timezone.utc)

def expired(s):
    if not s: return False
    return utcnow() > parse_dt(s)

def canonical(obj):
    if isinstance(obj, dict): return "{"+",".join(f"{json.dumps(k)}:{canonical(obj[k])}" for k in sorted(obj))+"}"
    if isinstance(obj, list): return "["+",".join(canonical(i) for i in obj)+"]"
    return json.dumps(obj)

def sha256(s): return hashlib.sha256(s.encode()).hexdigest()
def digest(scope,target,principal,params): return sha256(canonical({"params":params,"principal":principal,"scope":scope,"target":target}))

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    """Single entry point for schema."""
    import os, sqlite3
    db_path = os.environ.get("SPHERA_DB", "./sphera.db")

    # Step 1: Migrate legacy schema if DB exists
    try:
        from migrate import migrate
        if os.path.exists(db_path):
            migrate(db_path)
    except Exception as e:
        print(f"[sphera] migrate warning: {e}")

    # Step 2: Apply canonical schema + ADD new columns via ALTER TABLE
    from db import init as db_init, flush_outbox, get_db as _get_db
    db_init()

    # Step 3: Flush outbox
    try:
        with _get_db() as _db:
            flush_outbox(_db)
            _db.commit()
    except Exception as e:
        print(f"[sphera] flush_outbox warning: {e}")

def emit(db, principal, type_, payload):
    from db import append_event
    seq, _ = append_event(db, uid(), principal, type_, payload)
    return seq

def recover(db):
    n = utcnow_iso()
    for r in db.execute("SELECT work_id,lease_id FROM work_items WHERE status='leased' AND lease_expires<?", (n,)).fetchall():
        emit(db,"system","lease_expired",{"work_id":r["work_id"]})
        db.execute("UPDATE work_items SET status='ready',lease_holder=NULL,lease_id=NULL,lease_expires=NULL WHERE work_id=?", (r["work_id"],))
    for r in db.execute("SELECT request_id FROM decisions WHERE status='claimed' AND claim_expires<?", (n,)).fetchall():
        emit(db,"system","decision_claim_expired",{"request_id":r["request_id"]})
        # BUG FIX (Soba): expired claim returns to pending, NOT approved.
        # approved would silently manufacture owner authority after a crash.
        db.execute("UPDATE decisions SET status='pending',claimed_at=NULL,claim_expires=NULL WHERE request_id=?", (r["request_id"],))

def unblock(db, done_id):
    unblocked = []
    for b in db.execute("SELECT work_id,deps_json FROM work_items WHERE status='blocked'").fetchall():
        try:
            deps = json.loads(b["deps_json"] or "[]")
        except: continue
        if done_id not in deps: continue
        if all(db.execute("SELECT status FROM work_items WHERE work_id=?", (d,)).fetchone()["status"]=="done" for d in deps if db.execute("SELECT status FROM work_items WHERE work_id=?", (d,)).fetchone()):
            db.execute("UPDATE work_items SET status='ready' WHERE work_id=?", (b["work_id"],))
            emit(db,"system","work_unblocked",{"work_id":b["work_id"]})
            unblocked.append(b["work_id"])
    return unblocked

def _require_env(name):
    v = os.environ.get(name,"")
    if not v: raise RuntimeError(f"Missing required env var: {name}")
    return v

@asynccontextmanager
async def lifespan(app: FastAPI):
    global KEYS
    KEYS = {
        _require_env("CLAUDE_KEY"):  "claude",
        _require_env("SOBA_KEY"):    "soba",
        _require_env("ARCIDES_KEY"): "arcides",
        os.environ.get("BRIDGE_KEY","br-sphera"): "bridge",
    }
    init_db()
    with get_db() as db:
        recover(db); db.commit()
    print("[sphera] ready on :8765")
    # Flush any events stuck in outbox from previous crash
    try:
        from db import flush_outbox
        with get_db() as db:
            flushed = flush_outbox(db)
            if flushed:
                print(f"[sphera] flushed {flushed} events from outbox on startup")
    except Exception as e:
        print(f"[sphera] outbox flush warning: {e}")

    # Start orchestrator as background thread
    try:
        import threading as _threading
        from orchestrator import Orchestrator as _Orch
        _orch = _Orch()
        _orch_thread = _threading.Thread(target=_orch.run, daemon=True)
        _orch_thread.start()
        print("[sphera] orchestrator running")
    except Exception as e:
        print(f"[sphera] orchestrator failed to start: {e}")
    yield

app = FastAPI(title="SPHERA", version="2.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def ok(d,s=200): return JSONResponse(d,s)
def err(m,s=400): return JSONResponse({"error":m},s)
def auth(authorization=""):
    if not authorization.startswith("Bearer "): raise HTTPException(401,"Unauthorized")
    t = authorization[7:].strip()
    if t not in KEYS: raise HTTPException(401,"Unauthorized")
    return KEYS[t]

async def broadcast(ev):
    dead=[]
    for ws in connected:
        try: await ws.send_json(ev)
        except: dead.append(ws)
    for ws in dead: connected.remove(ws)

# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    with get_db() as db:
        seq = db.execute("SELECT MAX(seq) FROM events").fetchone()[0] or 0
        cnt = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    return ok({"ok":True,"instance":"arcides-victus","last_seq":seq,"event_count":cnt,"transport":"sphera-room-v2"})

# ── WebSocket — correct cursor-replay transport ───────────────────────────────
# Design (Soba review):
# 1. Auth required: ?token=KEY
# 2. Per-subscriber bounded asyncio.Queue + dedicated sender task
# 3. Register queue BEFORE capturing replay head H → closes replay/live race
# 4. Replay (cursor, H] from DB in chunks → drain queued seq > H with dedup
# 5. Slow consumer: queue overflow → explicit close (1013), never silent loss
# 6. DB is truth — reconnect replays from DB, no in-memory dependency
# 7. Identical envelope to REST /events: {seq,id,ts,principal,type,...payload}

import asyncio
SUBSCRIBER_QUEUE_SIZE = 200  # bounded — overflow = resync close

class Subscriber:
    def __init__(self, cursor: int):
        self.cursor    = cursor
        self.queue     = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self.last_sent = cursor

def _make_envelope(row) -> dict:
    """Normalise a DB row to canonical event envelope — same schema as REST /events."""
    payload = {}
    try:
        raw = row["payload_json"] if "payload_json" in row.keys() else row.get("payload","{}") 
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:
        payload = {"_parse_error": str(raw)[:100]}
    eid = row["event_id"] if "event_id" in row.keys() else row.get("id","")
    return {"seq": row["seq"], "id": eid, "ts": row["ts"],
            "principal": row["principal"], "type": row["type"], **payload}

subscribers: list  # list[Subscriber] — populated at startup
subscribers = []



@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    # 1. Auth
    token = ws.query_params.get("token", "")
    if token not in KEYS:
        await ws.close(code=1008, reason="Unauthorized")
        return

    # 2. Cursor
    cursor_str = ws.query_params.get("cursor", "0")
    try:
        cursor = int(cursor_str)
        if cursor < 0: raise ValueError("negative")
    except (ValueError, TypeError):
        await ws.close(code=1008, reason="cursor must be non-negative integer")
        return

    await ws.accept()

    # 3. Register subscriber BEFORE reading head → closes replay/live race
    sub = Subscriber(cursor)
    subscribers.append(sub)

    async def sender():
        """Dedicated sender task — owns ws.send_json, enforces monotonic delivery."""
        try:
            while True:
                envelope = await sub.queue.get()
                if envelope.get("_resync_required"):
                    await ws.send_json({"type": "resync_required",
                                        "head_seq": envelope.get("head_seq"),
                                        "reason": "queue_overflow"})
                    await ws.close(code=4008, reason="queue overflow — reconnect")
                    return
                seq = envelope.get("seq", 0)
                if seq <= sub.last_sent:
                    continue  # dedup: already sent during replay drain
                sub.last_sent = seq
                await ws.send_json(envelope)
        except Exception:
            pass

    # 4. Capture replay head AFTER subscriber is registered
    with get_db() as db:
        head_row = db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()
        H = head_row[0]

    # Sanity: cursor > head → resync
    if cursor > H:
        await ws.send_json({"type": "resync_required", "head_seq": H,
                            "reason": "cursor_ahead_of_ledger"})
        await ws.close(code=4008, reason="future cursor")
        subscribers.remove(sub)
        return

    # Start sender task
    send_task = asyncio.create_task(sender())

    try:
        # 5. Replay (cursor, H] from DB in bounded chunks
        CHUNK = 200
        pos = cursor
        while pos < H:
            with get_db() as db:
                rows = db.execute(
                    "SELECT * FROM events WHERE seq > ? AND seq <= ? ORDER BY seq LIMIT ?",
                    (pos, H, CHUNK)
                ).fetchall()
            for row in rows:
                envelope = _make_envelope(row)
                if envelope["seq"] <= sub.last_sent:
                    continue
                sub.last_sent = envelope["seq"]
                await ws.send_json(envelope)
            if rows:
                pos = rows[-1]["seq"]
            else:
                break

        # 6. Drain live queue, dedup seq <= H (already replayed above)
        # Items queued between registration and now with seq > H will flow through
        # sender task naturally; sender dedupes via last_sent.

        # 7. Keep alive — drain queue via sender task
        while True:
            # Just wait for messages from client (keep-alive ping etc)
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                pass  # no-op keep-alive
            except Exception:
                break

    except Exception:
        pass
    finally:
        send_task.cancel()
        try: subscribers.remove(sub)
        except ValueError: pass

# ── Events ────────────────────────────────────────────────────────────────────
@app.get("/events")
async def get_events(after:int=0, authorization:str=Header(default="")):
    auth(authorization)
    with get_db() as db:
        rows = db.execute("SELECT * FROM events WHERE seq>? ORDER BY seq",(after,)).fetchall()
    events = [{"seq":r["seq"],"id":r["event_id"],"ts":r["ts"],"principal":r["principal"],"type":r["type"],**json.loads(r["payload_json"] if "payload_json" in r.keys() else r.get("payload","{}") )} for r in rows]
    return ok({"events":events,"count":len(events),"cursor":events[-1]["seq"] if events else after})

@app.get("/events-public")
async def events_public(after:int=0, token:str=""):
    valid = {os.environ.get("CLAUDE_KEY",""),os.environ.get("SOBA_KEY",""),os.environ.get("ARCIDES_KEY","")}
    if not token or token not in valid: return JSONResponse({"error":"Unauthorized"},401)
    with get_db() as db:
        rows = db.execute("SELECT * FROM events WHERE seq>? ORDER BY seq",(after,)).fetchall()
    events = [{"seq":r["seq"],"id":r["event_id"],"ts":r["ts"],"principal":r["principal"],"type":r["type"],**json.loads(r["payload_json"] if "payload_json" in r.keys() else r.get("payload","{}") )} for r in rows]
    return ok({"events":events,"count":len(events),"cursor":events[-1]["seq"] if events else after})

# ── Messages ──────────────────────────────────────────────────────────────────
@app.post("/message")
async def post_message(req:Request, authorization:str=Header(default="")):
    p = auth(authorization)
    b = await req.json()
    content = (b.get("content") or "").strip()
    if not content: return err("content required")
    with get_db() as db:
        seq = emit(db,p,"message",{"content":content}); db.commit()
        # Broadcast canonical ledger row — never hand-build envelope
        row = db.execute("SELECT * FROM events WHERE seq=?", (seq,)).fetchone()
    await broadcast(_make_envelope(row))
    return ok({"ok":True,"seq":seq},201)

# ── Bridge ingest ─────────────────────────────────────────────────────────────
@app.post("/bridge/ingest")
async def bridge_ingest(req:Request, authorization:str=Header(default="")):
    t = authorization.replace("Bearer ","").strip()
    if t != os.environ.get("BRIDGE_KEY","br-sphera"): raise HTTPException(403,"Bridge key required")
    b = await req.json()
    principal = b.get("principal","unknown")
    content   = b.get("content","")
    src_id    = b.get("source_message_id","")
    transport = b.get("transport","gmail")
    orig_ts   = b.get("original_ts")
    if not content: return err("content required")
    with get_db() as db:
        if src_id:
            ex = db.execute("SELECT seq FROM events WHERE json_extract(payload_json,'$.source_message_id')=?",(src_id,)).fetchone()
            if ex: return ok({"duplicate":True,"seq":ex["seq"]},200)
        seq = emit(db,principal,"bridge_message",{"content":content,"transport_provenance":transport,"source_message_id":src_id,"original_ts":orig_ts})
        db.commit()
    await broadcast({"type":"bridge_message","principal":principal,"content":content,"seq":seq})
    return ok({"ok":True,"seq":seq,"principal":principal},201)

# ── Room state ────────────────────────────────────────────────────────────────
@app.get("/room")
async def room_state(authorization:str=Header(default="")):
    auth(authorization)
    with get_db() as db:
        ev_count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        last_seq = db.execute("SELECT MAX(seq) FROM events").fetchone()[0] or 0
        missions = db.execute("SELECT COUNT(*) FROM missions WHERE status='active'").fetchone()[0]
        ready    = db.execute("SELECT COUNT(*) FROM work_items WHERE status='ready'").fetchone()[0]
        leased   = db.execute("SELECT COUNT(*) FROM work_items WHERE status='leased'").fetchone()[0]
        done     = db.execute("SELECT COUNT(*) FROM work_items WHERE status='done'").fetchone()[0]
        avail    = db.execute("SELECT COUNT(*) FROM agents WHERE status='available'").fetchone()[0]
        busy     = db.execute("SELECT COUNT(*) FROM agents WHERE status='busy'").fetchone()[0]
        pending  = db.execute("SELECT COUNT(*) FROM decisions WHERE status='pending'").fetchone()[0]
    return ok({"event_count":ev_count,"last_seq":last_seq,"active_missions":missions,
               "work":{"ready":ready,"leased":leased,"done":done},
               "agents":{"available":avail,"busy":busy},"pending_decisions":pending})

# ── Missions ──────────────────────────────────────────────────────────────────
@app.post("/mission")
async def create_mission(req:Request, authorization:str=Header(default="")):
    p = auth(authorization)
    b = await req.json()
    if not b.get("objective"): return err("objective required")
    mid = uid()
    with get_db() as db:
        seq = emit(db,p,"mission_created",{"mission_id":mid,"objective":b["objective"],"owner":p})
        db.execute("INSERT INTO missions(mission_id,objective,owner,status,policy_json,created_at,version) VALUES(?,?,?,?,?,?,?)",(mid,b["objective"],p,"active","{}",utcnow_iso(),1))
        db.commit()
    return ok({"ok":True,"mission_id":mid,"seq":seq},201)

@app.get("/missions")
async def list_missions(authorization:str=Header(default="")):
    auth(authorization)
    with get_db() as db:
        ms = db.execute("SELECT * FROM missions ORDER BY created_at DESC").fetchall()
        result=[]
        for m in ms:
            items=db.execute("SELECT status FROM work_items WHERE mission_id=?",(m["mission_id"],)).fetchall()
            done=sum(1 for i in items if i["status"]=="done")
            result.append({**dict(m),"work_count":len(items),"done_count":done})
    return ok({"missions":result})

@app.post("/mission/{mid}/work")
async def add_work(mid:str, req:Request, authorization:str=Header(default="")):
    p = auth(authorization)
    b = await req.json()
    if not b.get("description"): return err("description required")
    if not b.get("capability"):  return err("capability required")
    deps = b.get("dependencies",[])
    with get_db() as db:
        m = db.execute("SELECT owner FROM missions WHERE mission_id=?",(mid,)).fetchone()
        if not m: return err("mission not found",404)
        if m["owner"]!=p: raise HTTPException(403,"owner only")
        wid=uid(); status="blocked" if deps else "ready"
        seq=emit(db,p,"work_created",{"work_id":wid,"mission_id":mid,"description":b["description"],"capability":b["capability"],"deps_json":deps,"status":status})
        db.execute("INSERT INTO work_items(work_id,mission_id,description,capability,deps_json,status,created_at,version,attempt_count,max_attempts,lease_fencing_token) VALUES(?,?,?,?,?,?,?,1,0,3,0)",(wid,mid,b["description"],b["capability"],json.dumps(deps),status,utcnow_iso()))
        db.commit()
    return ok({"ok":True,"work_id":wid,"status":status,"seq":seq},201)

@app.get("/mission/{mid}")
async def get_mission(mid:str, authorization:str=Header(default="")):
    auth(authorization)
    with get_db() as db:
        m=db.execute("SELECT * FROM missions WHERE mission_id=?",(mid,)).fetchone()
        if not m: return err("not found",404)
        items=db.execute("SELECT * FROM work_items WHERE mission_id=? ORDER BY created_at",(mid,)).fetchall()
    return ok({"mission":dict(m),"ready":[dict(i) for i in items if i["status"]=="ready"],
               "leased":[dict(i) for i in items if i["status"]=="leased"],
               "blocked":[dict(i) for i in items if i["status"]=="blocked"],
               "done":[dict(i) for i in items if i["status"]=="done"],"total":len(items)})

@app.post("/mission/{mid}/decompose")
async def decompose(mid:str, authorization:str=Header(default="")):
    p = auth(authorization)
    with get_db() as db:
        m=db.execute("SELECT * FROM missions WHERE mission_id=?",(mid,)).fetchone()
        if not m: return err("not found",404)
        if m["owner"]!=p: raise HTTPException(403,"owner only")
        ex=db.execute("SELECT COUNT(*) FROM work_items WHERE mission_id=?",(mid,)).fetchone()[0]
        if ex>0: return err(f"mission already has {ex} work items",409)
        obj=m["objective"].lower()
        caps=[]
        for cap,signals in [("backend",["api","server","backend","database"]),("frontend",["ui","frontend","console","dashboard"]),("testing",["test","verify","qa"]),("security",["security","auth","audit"]),("devops",["deploy","production","ship","release"])]:
            if any(s in obj for s in signals): caps.append(cap)
        if not caps: caps=["backend","testing","devops"]
        if ("backend" in caps or "frontend" in caps) and "testing" not in caps: caps.append("testing")
        if "devops" not in caps: caps.append("devops")
        created=[]; wids=[]
        for cap in caps:
            deps=[wids[-1]] if wids else []
            wid=uid(); status="blocked" if deps else "ready"
            desc=f"{'Implement' if cap not in ('testing','devops') else ('Test' if cap=='testing' else 'Deploy')} {cap}: {m['objective'][:40]}"
            seq=emit(db,p,"work_created",{"work_id":wid,"mission_id":mid,"description":desc,"capability":cap,"deps_json":deps,"status":status})
            db.execute("INSERT INTO work_items(work_id,mission_id,description,capability,deps_json,status,created_at,version,attempt_count,max_attempts,lease_fencing_token) VALUES(?,?,?,?,?,?,?,1,0,3,0)",(wid,mid,desc,cap,json.dumps(deps),status,utcnow_iso()))
            wids.append(wid); created.append({"work_id":wid,"description":desc,"capability":cap,"status":status})
        db.commit()
    return ok({"ok":True,"mission_id":mid,"work_items":created,"count":len(created)},201)

# ── Agents ────────────────────────────────────────────────────────────────────
@app.post("/agent/register")
async def register_agent(req:Request, authorization:str=Header(default="")):
    p=auth(authorization)
    b=await req.json()
    if not b.get("name"): return err("name required")
    caps=b.get("capabilities",[])
    if not isinstance(caps,list) or not caps: return err("capabilities required")
    aid=uid()
    with get_db() as db:
        seq=emit(db,p,"agent_registered",{"agent_id":aid,"name":b["name"],"capabilities":caps})
        db.execute("INSERT INTO agents VALUES(?,?,?,?,?,?,?,?,?)",(aid,b["name"],json.dumps(caps),p,"available",None,None,None,utcnow_iso()))
        db.commit()
    return ok({"ok":True,"agent_id":aid,"seq":seq},201)

@app.get("/agents")
async def list_agents(authorization:str=Header(default="")):
    auth(authorization)
    with get_db() as db:
        agents=db.execute("SELECT * FROM agents ORDER BY registered_at").fetchall()
    return ok({"agents":[{**dict(a),"capabilities":json.loads(a["capabilities"])} for a in agents]})

# ── Work ──────────────────────────────────────────────────────────────────────
@app.post("/work/{wid}/claim")
async def claim_work(wid:str, req:Request, authorization:str=Header(default="")):
    p=auth(authorization)
    b=await req.json()
    secs=max(1,min(int(b.get("lease_seconds",60)),MAX_LEASE))
    lid=uid(); exp=(utcnow()+timedelta(seconds=secs)).isoformat()
    with get_db() as db:
        cur=db.execute("UPDATE work_items SET status='leased',lease_holder=?,lease_id=?,lease_expires=?,attempt_count=attempt_count+1 WHERE work_id=? AND status='ready'",(p,lid,exp,wid))
        if cur.rowcount!=1: return err("not available",409)
        seq=emit(db,p,"work_claimed",{"work_id":wid,"lease_id":lid,"lease_expires":exp}); db.commit()
    return ok({"ok":True,"lease_id":lid,"lease_expires":exp,"seq":seq},201)

@app.post("/work/{wid}/result")
async def submit_result(wid:str, req:Request, authorization:str=Header(default="")):
    p=auth(authorization)
    b=await req.json()
    lid=b.get("lease_id"); result=b.get("result")
    if not lid: return err("lease_id required")
    if result is None: return err("result required")
    with get_db() as db:
        row=db.execute("SELECT * FROM work_items WHERE work_id=?",(wid,)).fetchone()
        if not row: return err("not found",404)
        if row["lease_id"]!=lid: return err("stale lease",403)
        if expired(row["lease_expires"]): return err("lease expired",403)
        seq=emit(db,p,"work_result",{"work_id":wid,"result_summary":result})
        db.execute("UPDATE work_items SET status='done',result_summary=?,result_seq=?,lease_id=NULL,lease_holder=NULL WHERE work_id=?",(json.dumps(result),seq,wid))
        unblocked=unblock(db,wid); db.commit()
    return ok({"ok":True,"seq":seq,"unblocked":unblocked},201)

@app.get("/work/queue")
async def work_queue(authorization:str=Header(default="")):
    auth(authorization)
    with get_db() as db:
        ms=db.execute("SELECT * FROM missions WHERE status='active'").fetchall()
        result=[]
        for m in ms:
            items=db.execute("SELECT * FROM work_items WHERE mission_id=?",(m["mission_id"],)).fetchall()
            result.append({"mission_id":m["mission_id"],"objective":m["objective"],
                           "ready":[dict(i) for i in items if i["status"]=="ready"],
                           "leased":[dict(i) for i in items if i["status"]=="leased"],
                           "done_count":sum(1 for i in items if i["status"]=="done"),"total":len(items)})
    return ok({"queue":result,"ts":utcnow_iso()})

# ── Decisions ─────────────────────────────────────────────────────────────────
@app.post("/decision")
async def req_decision(req:Request, authorization:str=Header(default="")):
    p=auth(authorization)
    b=await req.json()
    scope=b.get("scope"); target=b.get("target"); params=b.get("params",{})
    if not scope: return err("scope required")
    if not target: return err("target required")
    dg=digest(scope,target,p,params); rid=uid()
    with get_db() as db:
        seq=emit(db,p,"decision_requested",{"request_id":rid,"scope":scope,"target":target,"params":params,"digest":dg})
        db.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?,?)",(rid,"pending",p,scope,target,json.dumps(params),dg,b.get("deadline"),None,None,seq))
        db.commit()
    return ok({"ok":True,"request_id":rid,"seq":seq,"digest":dg},201)

@app.post("/decision/{rid}/approve")
async def approve_decision(rid:str, req:Request, authorization:str=Header(default="")):
    p=auth(authorization)
    if p!="arcides": raise HTTPException(403,"arcides only")
    b=await req.json()
    with get_db() as db:
        row=db.execute("SELECT * FROM decisions WHERE request_id=?",(rid,)).fetchone()
        if not row: return err("not found",404)
        if row["status"]!="pending": return err(f"status is {row['status']}",409)
        seq=emit(db,p,"decision_approved",{"request_id":rid,"note":b.get("note")})
        db.execute("UPDATE decisions SET status='approved' WHERE request_id=?",(rid,)); db.commit()
    return ok({"ok":True,"seq":seq},201)

@app.get("/decisions")
async def list_decisions(authorization:str=Header(default="")):
    auth(authorization)
    with get_db() as db:
        rows=db.execute("SELECT * FROM decisions ORDER BY seq DESC").fetchall()
    return ok({"decisions":[dict(r) for r in rows]})

@app.get("/orch/state")
async def orch_state(authorization: str = Header(default="")):
    """Live orchestrator state — who moves next, pending replies, stalls, wake required."""
    auth(authorization)
    try:
        from orchestrator import Orchestrator
        orch = Orchestrator()
        return ok(orch.state_summary())
    except Exception as e:
        return ok({"error": str(e), "next_principal": None, "wake_required": [], "pending_replies": []})

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="info")

# ── Autonomous Mission Reconciliation ─────────────────────────────────────────
@app.post("/mission/{mid}/reconcile")
async def reconcile_mission(mid: str, authorization: str = Header(default="")):
    """
    Deterministic reconciliation pass for a single mission.
    Run on startup and every orchestrator poll.
    Order:
    1. Expire/reclaim stale leases + increment fencing token
    2. Promote dependency-satisfied blocked items to ready
    3. Retry eligible failed/expired work (attempt_count < max_attempts)
    4. If no autonomous path remains → create owner_required interrupt
    """
    p = auth(authorization)
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc).isoformat()

    with get_db() as db:
        mission = db.execute("SELECT * FROM missions WHERE mission_id=?", (mid,)).fetchone()
        if not mission: return err("not found", 404)

        actions = []

        # 1. Expire stale leases + increment fencing
        stale = db.execute(
            "SELECT work_id, lease_fencing_token FROM work_items WHERE mission_id=? AND status='leased' AND lease_expires<?",
            (mid, now)
        ).fetchall()
        for s in stale:
            new_fence = s["lease_fencing_token"] + 1
            db.execute(
                "UPDATE work_items SET status='ready', lease_id=NULL, lease_holder=NULL, lease_expires=NULL, lease_fencing_token=? WHERE work_id=?",
                (new_fence, s["work_id"])
            )
            emit(db, "system", "lease_expired_reclaimed", {"work_id": s["work_id"], "new_fencing_token": new_fence})
            actions.append(f"reclaimed:{s['work_id'][:8]}")

        # 2. Promote blocked → ready when all deps done
        blocked = db.execute("SELECT * FROM work_items WHERE mission_id=? AND status='blocked'", (mid,)).fetchall()
        for b in blocked:
            try:
                deps = json.loads(b["deps_json"] or "[]")
            except Exception:
                continue
            if all(db.execute("SELECT status FROM work_items WHERE work_id=?", (d,)).fetchone()["status"] == "done"
                   for d in deps if db.execute("SELECT status FROM work_items WHERE work_id=?", (d,)).fetchone()):
                db.execute("UPDATE work_items SET status='ready' WHERE work_id=?", (b["work_id"],))
                emit(db, "system", "work_unblocked", {"work_id": b["work_id"]})
                actions.append(f"unblocked:{b['work_id'][:8]}")

        # 3. Retry eligible failed/expired work
        now_dt = datetime.now(timezone.utc)
        retryable = db.execute(
            "SELECT * FROM work_items WHERE mission_id=? AND status IN ('failed','ready') AND attempt_count>0 AND retry_at IS NOT NULL",
            (mid,)
        ).fetchall()
        for r in retryable:
            try:
                retry_dt = datetime.fromisoformat(r["retry_at"].replace("Z", "+00:00"))
                if now_dt >= retry_dt:
                    db.execute("UPDATE work_items SET status='ready', retry_at=NULL WHERE work_id=?", (r["work_id"],))
                    emit(db, "system", "work_retry_due", {"work_id": r["work_id"], "attempt": r["attempt_count"]})
                    actions.append(f"retry:{r['work_id'][:8]}")
            except Exception:
                pass

        # 4. Silent-zero invariant: active mission must have at least one runnable item
        items = db.execute("SELECT status FROM work_items WHERE mission_id=?", (mid,)).fetchall()
        statuses = [i["status"] for i in items]
        has_runnable = any(s in ("ready", "leased") for s in statuses)
        all_done = items and all(s == "done" for s in statuses)

        if not has_runnable and not all_done and items:
            # Create owner_required interrupt — idempotent
            interrupt_key = f"owner_required:{mid}:no_runnable_path"
            existing = db.execute(
                "SELECT 1 FROM events WHERE json_extract(payload_json,'$.interrupt_key')=?",
                (interrupt_key,)
            ).fetchone()
            if not existing:
                emit(db, "system", "owner_required", {
                    "mission_id": mid,
                    "interrupt_key": interrupt_key,
                    "reason": "no_autonomous_path",
                    "statuses": statuses
                })
                actions.append("owner_required_interrupt_created")

        db.commit()

    return ok({"ok": True, "mission_id": mid, "actions": actions, "action_count": len(actions)}, 200)
