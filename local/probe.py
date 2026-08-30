"""
SPHERA Principal Reachability Probe v1.2 — frozen L0
Fixes all Soba P0 gates before advancing to Principal Edge falsification.

P0 fixes:
1. Bad nonce does NOT consume challenge or insert probe row
2. Atomic single-use: UPDATE consumed=1 WHERE consumed=0, rowcount must be 1
3. No hardcoded default PROBE_KEY — fail closed on external bind
4. X-Forwarded-For only trusted from configured proxy IPs
5. Expired challenge → 410, no probe row
"""
import hashlib, json, os, secrets, sqlite3, uuid
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

DB_PATH     = os.environ.get("PROBE_DB", "./probe.db")
PROBE_KEY   = os.environ.get("PROBE_KEY", "")  # Must be set explicitly — no default
TRUSTED_PROXY = os.environ.get("PROBE_TRUSTED_PROXY", "127.0.0.1")  # ngrok runs locally
CHALLENGE_TTL = int(os.environ.get("PROBE_CHALLENGE_TTL", "300"))  # 5 min

app = FastAPI(title="SPHERA Probe v1.2")

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def utcnow(): return datetime.now(timezone.utc)
def utcnow_iso(): return utcnow().isoformat()

def get_real_ip(req: Request) -> str:
    """Only trust X-Forwarded-For from configured trusted proxy."""
    client_ip = req.client.host if req.client else "unknown"
    if client_ip in TRUSTED_PROXY.split(","):
        forwarded = req.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        return forwarded or client_ip
    return client_ip

def verify_key(key: str):
    if not PROBE_KEY:
        raise HTTPException(503, "PROBE_KEY not configured — probe not accepting requests")
    if key != PROBE_KEY:
        raise HTTPException(403, "Invalid probe key")

def init_db():
    with get_db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS challenges (
            challenge_id TEXT PRIMARY KEY,
            nonce        TEXT NOT NULL,
            nonce_echo   TEXT NOT NULL,
            issued_at    TEXT NOT NULL,
            expires_at   TEXT NOT NULL,
            consumed     INTEGER NOT NULL DEFAULT 0,
            consumed_at  TEXT
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS probes (
            probe_id         TEXT PRIMARY KEY,
            challenge_id     TEXT NOT NULL,
            principal_claim  TEXT NOT NULL,
            surface_claim    TEXT NOT NULL,
            obligation_id    TEXT,
            classification   TEXT NOT NULL DEFAULT 'L0_TRANSPORT_UNVERIFIED',
            received_at      TEXT NOT NULL,
            source_ip        TEXT,
            echo_valid       INTEGER NOT NULL DEFAULT 0
        )""")
        db.commit()

    # Fail closed: refuse external bind without PROBE_KEY
    bind_host = os.environ.get("PROBE_HOST", "0.0.0.0")
    if bind_host != "127.0.0.1" and not PROBE_KEY:
        raise RuntimeError("PROBE_KEY must be set for non-loopback bind. Set env PROBE_KEY=<secret>.")
    print(f"[probe] v1.2 ready | db:{DB_PATH} | key:{'SET' if PROBE_KEY else 'MISSING'}")

@app.on_event("startup")
async def startup(): init_db()

@app.get("/health")
async def health():
    with get_db() as db:
        probes = db.execute("SELECT COUNT(*) FROM probes").fetchone()[0]
        challenges = db.execute("SELECT COUNT(*) FROM challenges").fetchone()[0]
    return {"ok": True, "probe": "v1.2", "probes": probes, "challenges": challenges}

@app.post("/challenge")
async def create_challenge(req: Request):
    """Server generates nonce. Client cannot supply it."""
    nonce      = secrets.token_hex(16)
    cid        = str(uuid.uuid4())
    nonce_echo = hashlib.sha256(f"{nonce}:sphera".encode()).hexdigest()[:16]
    issued     = utcnow_iso()
    expires    = (utcnow() + timedelta(seconds=CHALLENGE_TTL)).isoformat()

    with get_db() as db:
        db.execute(
            "INSERT INTO challenges(challenge_id,nonce,nonce_echo,issued_at,expires_at) VALUES(?,?,?,?,?)",
            (cid, nonce, nonce_echo, issued, expires)
        )
        db.commit()

    print(f"[probe] challenge issued: {cid[:8]} expires:{expires[:16]}")
    return JSONResponse({"challenge_id": cid, "nonce": nonce, "expires_at": expires}, 201)

@app.post("/probe")
async def submit_probe(req: Request):
    """
    Submit probe response. Atomic single-use. Bad echo = 422, no row inserted.
    """
    b = await req.json()
    challenge_id = b.get("challenge_id", "")
    nonce_echo   = b.get("nonce_echo", "")
    principal    = b.get("principal_claim", "UNKNOWN")
    surface      = b.get("surface_claim", "UNKNOWN")
    obligation   = b.get("obligation_id")
    source_ip    = get_real_ip(req)

    if not challenge_id or not nonce_echo:
        return JSONResponse({"error": "challenge_id and nonce_echo required"}, 400)

    with get_db() as db:
        ch = db.execute("SELECT * FROM challenges WHERE challenge_id=?", (challenge_id,)).fetchone()

        if not ch:
            return JSONResponse({"error": "challenge not found"}, 404)

        # Check expiry — no row inserted
        if utcnow_iso() > ch["expires_at"]:
            return JSONResponse({"error": "challenge expired", "expired_at": ch["expires_at"]}, 410)

        # Validate echo BEFORE consuming — bad echo = 422, no row, challenge still usable
        if nonce_echo != ch["nonce_echo"]:
            return JSONResponse({"error": "invalid nonce_echo — challenge not consumed"}, 422)

        # Atomic single-use: consume only if not already consumed
        cur = db.execute(
            "UPDATE challenges SET consumed=1, consumed_at=? WHERE challenge_id=? AND consumed=0",
            (utcnow_iso(), challenge_id)
        )
        if cur.rowcount != 1:
            return JSONResponse({"error": "challenge already consumed"}, 409)

        # Only now insert probe row
        probe_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO probes(probe_id,challenge_id,principal_claim,surface_claim,obligation_id,received_at,source_ip,echo_valid) VALUES(?,?,?,?,?,?,?,1)",
            (probe_id, challenge_id, principal, surface, obligation, utcnow_iso(), source_ip)
        )
        db.commit()

    print(f"[probe] PASS {principal} from {surface} via {source_ip} — probe:{probe_id[:8]}")
    return JSONResponse({
        "probe_id":        probe_id,
        "received_at":     utcnow_iso(),
        "principal_claim": principal,
        "surface_claim":   surface,
        "classification":  "L0_TRANSPORT_UNVERIFIED",
        "note":            "L0 only — transport proven, identity not bound. Evidence slot unfilled."
    }, 201)

@app.get("/probe/{probe_id}")
async def get_probe(probe_id: str, x_probe_key: str = Header(default="")):
    verify_key(x_probe_key)
    with get_db() as db:
        row = db.execute("SELECT * FROM probes WHERE probe_id=?", (probe_id,)).fetchone()
    if not row: return JSONResponse({"error": "not found"}, 404)
    return JSONResponse(dict(row))

@app.get("/probes")
async def list_probes(x_probe_key: str = Header(default="")):
    verify_key(x_probe_key)
    with get_db() as db:
        rows = db.execute("SELECT * FROM probes ORDER BY received_at DESC LIMIT 50").fetchall()
    return JSONResponse({"probes": [dict(r) for r in rows], "count": len(rows)})

if __name__ == "__main__":
    host = os.environ.get("PROBE_HOST", "0.0.0.0")
    if host != "127.0.0.1" and not PROBE_KEY:
        print("[probe] ERROR: Set PROBE_KEY env var for external binding. Exiting.")
        exit(1)
    uvicorn.run(app, host=host, port=8766, log_level="info")
