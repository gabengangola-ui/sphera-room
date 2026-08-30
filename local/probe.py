"""
SPHERA Principal Reachability Probe v1.0
Single HTTPS endpoint. No model calls. No persona copying.
Challenge/response only — proves a principal can reach SPHERA infrastructure.

POST /probe
  {nonce, principal_claim, surface_claim, obligation_id}
  → {nonce_echo, received_at, obligation_id, evidence_slot, probe_id}

GET /probe/{probe_id}
  → full probe record with timestamps

Records stored in SQLite for audit.
"""
import hashlib, json, os, sqlite3, uuid
from datetime import datetime, timezone
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

DB_PATH   = os.environ.get("PROBE_DB",  "./probe.db")
PROBE_KEY = os.environ.get("PROBE_KEY", "probe-sphera-2026")

app = FastAPI(title="SPHERA Probe", version="1.0")

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    with get_db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS probes (
            probe_id         TEXT PRIMARY KEY,
            nonce            TEXT NOT NULL,
            nonce_echo       TEXT NOT NULL,
            principal_claim  TEXT NOT NULL,
            surface_claim    TEXT NOT NULL,
            obligation_id    TEXT,
            received_at      TEXT NOT NULL,
            source_ip        TEXT,
            evidence_slot    TEXT,
            evidence_hash    TEXT
        )""")
        db.commit()
    print(f"[probe] db ready: {DB_PATH}")

def utcnow(): return datetime.now(timezone.utc).isoformat()

@app.on_event("startup")
async def startup(): init_db()

@app.get("/health")
async def health():
    with get_db() as db:
        count = db.execute("SELECT COUNT(*) FROM probes").fetchone()[0]
    return {"ok": True, "probe": "sphera-reachability-v1", "total_probes": count}

@app.post("/probe")
async def submit_probe(req: Request):
    """
    Principal submits probe. Records nonce echo + timestamp.
    No model inference. No identity assertion beyond what client sends.
    Evidence must come from external/provider verification — not self-reported.
    """
    b = await req.json()
    nonce         = b.get("nonce", "")
    principal     = b.get("principal_claim", "UNKNOWN")
    surface       = b.get("surface_claim", "UNKNOWN")
    obligation_id = b.get("obligation_id")

    if not nonce:
        return JSONResponse({"error": "nonce required"}, 400)

    probe_id   = str(uuid.uuid4())
    nonce_echo = hashlib.sha256(f"{nonce}:{probe_id}".encode()).hexdigest()[:16]
    received   = utcnow()
    source_ip  = req.client.host if req.client else "unknown"

    with get_db() as db:
        db.execute(
            "INSERT INTO probes(probe_id,nonce,nonce_echo,principal_claim,surface_claim,obligation_id,received_at,source_ip) VALUES(?,?,?,?,?,?,?,?)",
            (probe_id, nonce, nonce_echo, principal, surface, obligation_id, received, source_ip)
        )
        db.commit()

    print(f"[probe] {principal} from {surface} via {source_ip} — probe:{probe_id[:8]}")

    return JSONResponse({
        "probe_id":      probe_id,
        "nonce_echo":    nonce_echo,
        "received_at":   received,
        "obligation_id": obligation_id,
        "principal_claim": principal,
        "surface_claim":   surface,
        # Evidence slot: filled by external/provider verifier only — not by this service
        "evidence_slot":   None,
        "note": "evidence_slot must be filled by authorised external verifier — self-reported evidence not accepted"
    }, 201)

@app.get("/probe/{probe_id}")
async def get_probe(probe_id: str):
    with get_db() as db:
        row = db.execute("SELECT * FROM probes WHERE probe_id=?", (probe_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "not found"}, 404)
    return JSONResponse(dict(row))

@app.get("/probes")
async def list_probes():
    with get_db() as db:
        rows = db.execute("SELECT * FROM probes ORDER BY received_at DESC LIMIT 50").fetchall()
    return JSONResponse({"probes": [dict(r) for r in rows], "count": len(rows)})

if __name__ == "__main__":
    print("[probe] SPHERA Principal Reachability Probe v1.0")
    print("[probe] POST /probe  {nonce, principal_claim, surface_claim, obligation_id}")
    print("[probe] GET  /probe/{id}")
    uvicorn.run(app, host="0.0.0.0", port=8766, log_level="info")
