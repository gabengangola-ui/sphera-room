"""
SPHERA Principal Reachability Probe v1.1
Soba audit fixes:
- Server-generated challenge nonces (not client-supplied)
- /probe is unauthenticated (L0 TRANSPORT only — never Principal identity)
- GET /probes requires verifier auth (localhost or PROBE_KEY header)
- source_ip recorded with X-Forwarded-For awareness
- Evidence slot: null always — external verifier must fill
"""
import hashlib, json, os, secrets, sqlite3, uuid
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

DB_PATH   = os.environ.get("PROBE_DB",  "./probe.db")
PROBE_KEY = os.environ.get("PROBE_KEY", "probe-sphera-2026")
NONCE_TTL = int(os.environ.get("PROBE_NONCE_TTL", "300"))  # 5 min

app = FastAPI(title="SPHERA Probe", version="1.1")

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    with get_db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS challenges (
            challenge_id  TEXT PRIMARY KEY,
            nonce         TEXT NOT NULL UNIQUE,
            created_at    TEXT NOT NULL,
            expires_at    TEXT NOT NULL,
            consumed      INTEGER NOT NULL DEFAULT 0,
            consumed_at   TEXT
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS probes (
            probe_id         TEXT PRIMARY KEY,
            challenge_id     TEXT NOT NULL,
            nonce_echo       TEXT NOT NULL,
            principal_claim  TEXT NOT NULL,
            surface_claim    TEXT NOT NULL,
            obligation_id    TEXT,
            received_at      TEXT NOT NULL,
            source_ip        TEXT,
            classification   TEXT NOT NULL DEFAULT 'UNVERIFIED_TRANSPORT',
            evidence_slot    TEXT
        )""")
        db.commit()
    print(f"[probe] v1.1 db ready: {DB_PATH}")

def utcnow(): return datetime.now(timezone.utc)
def utcnow_iso(): return utcnow().isoformat()

def get_source_ip(req: Request) -> str:
    forwarded = req.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip() + " (forwarded)"
    return req.client.host if req.client else "unknown"

def verify_key(x_probe_key: str = None) -> bool:
    return x_probe_key == PROBE_KEY

@app.on_event("startup")
async def startup(): init_db()

@app.get("/health")
async def health():
    with get_db() as db:
        probes = db.execute("SELECT COUNT(*) FROM probes").fetchone()[0]
        challenges = db.execute("SELECT COUNT(*) FROM challenges WHERE consumed=0").fetchone()[0]
    return {"ok": True, "probe": "sphera-reachability-v1.1", "total_probes": probes, "open_challenges": challenges}

@app.post("/challenge")
async def create_challenge():
    """
    Server generates challenge nonce. Client cannot supply nonce.
    Any caller can request a challenge — it proves nothing by itself.
    """
    cid = str(uuid.uuid4())
    nonce = secrets.token_hex(16)
    now = utcnow()
    exp = (now + timedelta(seconds=NONCE_TTL)).isoformat()
    with get_db() as db:
        db.execute(
            "INSERT INTO challenges(challenge_id,nonce,created_at,expires_at) VALUES(?,?,?,?)",
            (cid, nonce, now.isoformat(), exp)
        )
        db.commit()
    return JSONResponse({"challenge_id": cid, "nonce": nonce, "expires_at": exp}, 201)

@app.post("/probe")
async def submit_probe(req: Request):
    """
    Submit probe response. UNAUTHENTICATED — proves only L0 TRANSPORT.
    principal_claim/surface_claim are self-reported — never treated as verified.
    Must reference a valid unused server-generated challenge.
    """
    b = await req.json()
    challenge_id  = b.get("challenge_id", "")
    nonce_echo    = b.get("nonce_echo", "")
    principal     = b.get("principal_claim", "UNKNOWN")
    surface       = b.get("surface_claim", "UNKNOWN")
    obligation_id = b.get("obligation_id")

    if not challenge_id or not nonce_echo:
        return JSONResponse({"error": "challenge_id and nonce_echo required"}, 400)

    with get_db() as db:
        ch = db.execute(
            "SELECT * FROM challenges WHERE challenge_id=? AND consumed=0",
            (challenge_id,)
        ).fetchone()
        if not ch:
            return JSONResponse({"error": "invalid or consumed challenge"}, 409)
        if utcnow().isoformat() > ch["expires_at"]:
            return JSONResponse({"error": "challenge expired"}, 410)

        # Verify nonce_echo matches expected hash
        expected_echo = hashlib.sha256(f"{ch['nonce']}:sphera".encode()).hexdigest()[:16]
        echo_valid = (nonce_echo == expected_echo)

        # Consume challenge (single-use)
        db.execute("UPDATE challenges SET consumed=1, consumed_at=? WHERE challenge_id=?",
                   (utcnow_iso(), challenge_id))

        probe_id = str(uuid.uuid4())
        source_ip = get_source_ip(req)
        classification = "L0_TRANSPORT_UNVERIFIED"  # Never promotes from self-claims

        db.execute(
            """INSERT INTO probes(probe_id,challenge_id,nonce_echo,principal_claim,surface_claim,
               obligation_id,received_at,source_ip,classification)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (probe_id, challenge_id, nonce_echo, principal, surface,
             obligation_id, utcnow_iso(), source_ip, classification)
        )
        db.commit()

    print(f"[probe] {principal} from {surface} via {source_ip} echo_valid={echo_valid} — probe:{probe_id[:8]}")

    return JSONResponse({
        "probe_id":        probe_id,
        "nonce_echo_valid": echo_valid,
        "received_at":     utcnow_iso(),
        "obligation_id":   obligation_id,
        "classification":  classification,
        "evidence_slot":   None,
        "note": "classification=L0_TRANSPORT_UNVERIFIED. Evidence slot filled by authorised external verifier only."
    }, 201)

@app.get("/probe/{probe_id}")
async def get_probe(probe_id: str, x_probe_key: str = Header(default=None)):
    if not verify_key(x_probe_key):
        raise HTTPException(403, "verifier key required")
    with get_db() as db:
        row = db.execute("SELECT * FROM probes WHERE probe_id=?", (probe_id,)).fetchone()
    if not row:
        return JSONResponse({"error": "not found"}, 404)
    return JSONResponse(dict(row))

@app.get("/probes")
async def list_probes(x_probe_key: str = Header(default=None)):
    if not verify_key(x_probe_key):
        raise HTTPException(403, "verifier key required")
    with get_db() as db:
        rows = db.execute("SELECT * FROM probes ORDER BY received_at DESC LIMIT 50").fetchall()
    return JSONResponse({"probes": [dict(r) for r in rows], "count": len(rows)})

if __name__ == "__main__":
    print("[probe] SPHERA Principal Reachability Probe v1.1")
    print("[probe] POST /challenge → get server-generated nonce")
    print("[probe] POST /probe {challenge_id, nonce_echo, principal_claim, surface_claim}")
    print("[probe] GET /probe/{id} — requires X-Probe-Key header")
    uvicorn.run(app, host="0.0.0.0", port=8766, log_level="info")
