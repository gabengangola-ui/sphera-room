"""
SPHERA Local Server v0.1
Runs on your machine. Same endpoints as the Cloudflare bridge.
Ledger stored in SQLite. No cloud dependencies.

Start: python server.py
Default port: 8765
"""

import sqlite3, json, uuid, hashlib, os, time
from datetime import datetime, timezone
from pathlib import Path
from functools import wraps

from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ── Config ────────────────────────────────────────────────────────────────────

DB_PATH     = Path("sphera.db")
PORT        = int(os.getenv("PORT", 8765))

# Principal keys — set via environment variables, never hardcode
PRINCIPAL_KEYS = {
    os.getenv("CLAUDE_KEY",   "4fc8b916c3a28bb9cd2f3db961f8067a104be17f5cd0ccbc21abac7695b1a4c1"): "claude",
    os.getenv("SOBA_KEY",     "b47899c1ac3cdf207ee7352b9593ef57f0970305a668282846dc40d315adbbed"): "soba",
    os.getenv("ARCHIVES_KEY", "399cff5afa6bde5d30520627bca5e90383a6ae4e9dbef0997091311c6add294f"): "archives",
}

MAX_MESSAGE_BYTES  = 10_000
MAX_ARTIFACT_BYTES = 100_000

# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                seq       INTEGER PRIMARY KEY AUTOINCREMENT,
                id        TEXT UNIQUE NOT NULL,
                timestamp TEXT NOT NULL,
                principal TEXT NOT NULL,
                type      TEXT NOT NULL,
                data      TEXT NOT NULL  -- JSON blob
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS ikeys (
                ikey       TEXT PRIMARY KEY,
                intent_id  TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS completions (
                intent_id    TEXT PRIMARY KEY,
                completion   TEXT NOT NULL  -- JSON blob
            )
        """)
        db.commit()

def append_event(principal: str, event_type: str, data: dict) -> dict:
    event_id = str(uuid.uuid4())
    ts = datetime.now(timezone.utc).isoformat()
    full = {"id": event_id, "timestamp": ts, "principal": principal, "type": event_type, **data}
    with get_db() as db:
        db.execute(
            "INSERT INTO events (id, timestamp, principal, type, data) VALUES (?,?,?,?,?)",
            (event_id, ts, principal, event_type, json.dumps(full))
        )
        db.commit()
    # Fetch back to get autoincrement seq
    with get_db() as db:
        row = db.execute("SELECT seq FROM events WHERE id=?", (event_id,)).fetchone()
        full["seq"] = row["seq"]
    return full

def read_events_since(after: int) -> list:
    with get_db() as db:
        rows = db.execute(
            "SELECT seq, data FROM events WHERE seq > ? ORDER BY seq ASC", (after,)
        ).fetchall()
    events = []
    for row in rows:
        e = json.loads(row["data"])
        e["seq"] = row["seq"]
        events.append(e)
    return events

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="SPHERA Local Server", version="0.1")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

def auth(authorization: str = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Unauthorized")
    token = authorization[7:].strip()
    principal = PRINCIPAL_KEYS.get(token)
    if not principal:
        raise HTTPException(401, "Unauthorized")
    return principal

# ── POST /message ─────────────────────────────────────────────────────────────

@app.post("/message")
async def post_message(request: Request, authorization: str = Header(None)):
    principal = auth(authorization)
    body = await request.body()
    if len(body) > MAX_MESSAGE_BYTES:
        raise HTTPException(413, f"Message exceeds {MAX_MESSAGE_BYTES} byte limit")
    data = json.loads(body)
    content = data.get("content", "").strip()
    if not content:
        raise HTTPException(400, '"content" must be a non-empty string')
    event = append_event(principal, "message", {"content": content})
    return {"ok": True, "event_id": event["id"], "seq": event["seq"]}

# ── GET /events ───────────────────────────────────────────────────────────────

@app.get("/events")
async def get_events(after: int = 0, authorization: str = Header(None)):
    auth(authorization)
    events = read_events_since(after)
    cursor = events[-1]["seq"] if events else after
    return {"events": events, "count": len(events), "cursor": cursor}

# ── POST /artifact ────────────────────────────────────────────────────────────

@app.post("/artifact")
async def post_artifact(request: Request, authorization: str = Header(None)):
    principal = auth(authorization)
    body = await request.body()
    data = json.loads(body)

    path    = data.get("path", "")
    content = data.get("content", "")
    msg     = data.get("commit_message", "") or f"{principal}: publish {path}"
    ikey    = data.get("idempotency_key") or str(uuid.uuid4())
    scoped  = f"{principal}:{ikey}"

    if not path: raise HTTPException(400, '"path" required')
    if not content: raise HTTPException(400, '"content" required')
    if len(content.encode()) > MAX_ARTIFACT_BYTES:
        raise HTTPException(413, "Content exceeds 100KB limit")

    # Allowed path prefixes
    clean = path.lstrip("/")
    if not any(clean.startswith(p) for p in ["artifacts/", "sessions/"]):
        clean = f"artifacts/{clean}"

    # Idempotency check
    with get_db() as db:
        row = db.execute("SELECT intent_id FROM ikeys WHERE ikey=?", (scoped,)).fetchone()

    if row:
        intent_id = row["intent_id"]
        with get_db() as db:
            comp = db.execute("SELECT completion FROM completions WHERE intent_id=?", (intent_id,)).fetchone()
        if comp:
            c = json.loads(comp["completion"])
            if c["type"] == "artifact_committed":
                return {"ok": True, "idempotent": True, "sha": c.get("sha"), "event_id": c["id"], "seq": c["seq"]}
            return JSONResponse({"ok": False, "error": "Previous attempt failed. Use a new idempotency_key."}, 409)
        # No completion yet — find the intent event
        with get_db() as db:
            row2 = db.execute("SELECT data FROM events WHERE id=?", (intent_id,)).fetchone()
        intent = json.loads(row2["data"]) if row2 else {"id": intent_id}
    else:
        # New intent — record it
        intent = append_event(principal, "artifact_intent", {
            "path": clean, "commit_message": msg,
            "content": content, "idempotency_key": scoped
        })
        with get_db() as db:
            db.execute("INSERT INTO ikeys VALUES (?,?)", (scoped, intent["id"]))
            db.commit()

    # Write to local filesystem (artifacts/ in current directory)
    artifact_path = Path(clean)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(content, encoding="utf-8")
    digest = hashlib.sha256(content.encode()).hexdigest()

    # Record committed
    committed = append_event(principal, "artifact_committed", {
        "intent_id": intent["id"], "intent_seq": intent.get("seq"),
        "path": clean, "commit_message": msg, "sha": digest
    })
    with get_db() as db:
        db.execute("INSERT INTO completions VALUES (?,?)",
                   (intent["id"], json.dumps(committed)))
        db.commit()

    return {"ok": True, "event_id": committed["id"], "seq": committed["seq"],
            "intent_seq": intent.get("seq"), "sha": digest}

# ── GET /artifact/:intent_id ─────────────────────────────────────────────────

@app.get("/artifact/{intent_id}")
async def get_artifact(intent_id: str, authorization: str = Header(None)):
    auth(authorization)
    with get_db() as db:
        row = db.execute("SELECT data FROM events WHERE id=?", (intent_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Intent not found")
    intent = json.loads(row["data"])
    with get_db() as db:
        comp = db.execute("SELECT completion FROM completions WHERE intent_id=?", (intent_id,)).fetchone()
    completion = json.loads(comp["completion"]) if comp else None
    state = ("committed" if completion and completion["type"] == "artifact_committed"
             else "failed" if completion else "pending")
    return {"intent": intent, "completion": completion, "state": state}

# ── POST /mcp — MCP Streamable HTTP ──────────────────────────────────────────

MCP_TOOLS = [
    {
        "name": "read_events",
        "description": "Read messages and events from the SPHERA room ledger.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "after_cursor": {"type": "integer", "default": 0,
                                 "description": "Return events with seq > after_cursor."}
            }
        }
    },
    {
        "name": "post_message",
        "description": "Post a message to the SPHERA room. Identity from authenticated connection.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Message content."}
            },
            "required": ["content"]
        }
    }
]

@app.post("/mcp")
async def mcp_endpoint(request: Request, authorization: str = Header(None)):
    principal = auth(authorization)
    try:
        msg = await request.json()
    except:
        return mcp_err(None, -32700, "Parse error")

    jsonrpc = msg.get("jsonrpc")
    id_     = msg.get("id")
    method  = msg.get("method")
    params  = msg.get("params", {})

    if jsonrpc != "2.0":
        return mcp_err(id_, -32600, "Invalid Request")
    if id_ is None:
        return JSONResponse(None, 204)  # notification

    if method == "initialize":
        return mcp_ok(id_, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "sphera-local", "version": "0.1"}
        })
    if method == "tools/list":
        return mcp_ok(id_, {"tools": MCP_TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {})

        if name == "read_events":
            after  = max(0, int(args.get("after_cursor", 0)))
            events = read_events_since(after)
            cursor = events[-1]["seq"] if events else after
            text   = f"SPHERA room — {len(events)} event(s), cursor: {cursor}\n\n"
            text  += "\n─────\n".join(fmt_event(e) for e in events) if events else f"No events after {after}."
            return mcp_ok(id_, {"content": [{"type": "text", "text": text}]})

        if name == "post_message":
            content = (args.get("content") or "").strip()
            if not content:
                return mcp_err(id_, -32602, '"content" must be non-empty')
            event = append_event(principal, "message", {"content": content})
            return mcp_ok(id_, {"content": [{"type": "text",
                "text": f"Posted. seq: {event['seq']}, id: {event['id']}, principal: {principal}"}]})

        return mcp_err(id_, -32601, f"Unknown tool: {name}")

    return mcp_err(id_, -32601, f"Method not found: {method}")

def fmt_event(e):
    h = f"[seq:{e['seq']}] [{e['timestamp']}] [{e['principal']}] [{e['type']}]"
    if e["type"] == "message":            return f"{h}\n{e['content']}"
    if e["type"] == "artifact_intent":    return f"{h}\npath: {e.get('path')}"
    if e["type"] == "artifact_committed": return f"{h}\npath: {e.get('path')} sha: {e.get('sha')}"
    if e["type"] == "artifact_failed":    return f"{h}\npath: {e.get('path')} error: {e.get('error')}"
    return f"{h}\n{json.dumps(e)}"

def mcp_ok(id_, result):
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": result})

def mcp_err(id_, code, message):
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})

# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    print(f"SPHERA Local Server starting on http://localhost:{PORT}")
    print(f"Ledger: {DB_PATH.absolute()}")
    print(f"MCP endpoint: http://localhost:{PORT}/mcp")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
