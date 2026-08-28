"""
SPHERA DB v3.0
Based on Soba's migration 001_initial_schema.sql.
Key additions over v2: outbox pattern, hash chain, fencing tokens, version counters.
"""
import hashlib, json, os, sqlite3, uuid
from datetime import datetime, timezone

DB_PATH = os.environ.get("SPHERA_DB", "./sphera.db")
SCHEMA_VERSION = 3

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS schema_meta (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id  TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    owner         TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL DEFAULT 'default',
    event_id    TEXT    NOT NULL UNIQUE,
    ts          TEXT    NOT NULL,
    principal   TEXT    NOT NULL,
    type        TEXT    NOT NULL,
    payload_json TEXT   NOT NULL DEFAULT '{}',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    prev_hash   TEXT    NOT NULL DEFAULT '',
    this_hash   TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS outbox (
    workspace_id TEXT NOT NULL DEFAULT 'default',
    outbox_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT    NOT NULL UNIQUE,
    ts          TEXT    NOT NULL,
    principal   TEXT    NOT NULL,
    type        TEXT    NOT NULL,
    payload_json TEXT   NOT NULL DEFAULT '{}',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    workspace_id         TEXT NOT NULL DEFAULT 'default',
    request_id           TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'pending',
    requesting_principal TEXT NOT NULL,
    scope                TEXT NOT NULL,
    target               TEXT NOT NULL,
    params_json          TEXT NOT NULL DEFAULT '{}',
    bound_digest         TEXT NOT NULL,
    deadline             TEXT,
    version              INTEGER NOT NULL DEFAULT 1,
    claimed_at           TEXT,
    claim_expires        TEXT,
    claim_fencing_token  INTEGER,
    approved_at          TEXT,
    consumed_at          TEXT,
    PRIMARY KEY(workspace_id, request_id)
);

CREATE TABLE IF NOT EXISTS missions (
    workspace_id    TEXT NOT NULL DEFAULT 'default',
    mission_id      TEXT NOT NULL,
    objective       TEXT NOT NULL,
    owner           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    policy_json     TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    accepted_at     TEXT,
    acceptance_note TEXT,
    version         INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(workspace_id, mission_id)
);

CREATE TABLE IF NOT EXISTS work_items (
    workspace_id         TEXT NOT NULL DEFAULT 'default',
    work_id              TEXT NOT NULL,
    mission_id           TEXT NOT NULL,
    description          TEXT NOT NULL,
    capability           TEXT NOT NULL,
    deps_json            TEXT NOT NULL DEFAULT '[]',
    status               TEXT NOT NULL DEFAULT 'ready',
    lease_id             TEXT,
    lease_holder         TEXT,
    lease_expires        TEXT,
    lease_fencing_token  INTEGER NOT NULL DEFAULT 0,
    result_seq           INTEGER,
    result_summary       TEXT,
    created_at           TEXT NOT NULL,
    version              INTEGER NOT NULL DEFAULT 1,
    attempt_count        INTEGER NOT NULL DEFAULT 0,
    max_attempts         INTEGER NOT NULL DEFAULT 3,
    retry_at             TEXT,
    last_error           TEXT,
    PRIMARY KEY(workspace_id, work_id)
);

CREATE TABLE IF NOT EXISTS pending_reply (
    workspace_id        TEXT NOT NULL DEFAULT 'default',
    id                  TEXT NOT NULL,
    principal           TEXT NOT NULL,
    source_seq          INTEGER NOT NULL,
    source_principal    TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
    resolved_at         TEXT,
    resolved_by_seq     INTEGER,
    PRIMARY KEY(workspace_id, id)
);

CREATE TABLE IF NOT EXISTS orch_mission_state (
    mission_id          TEXT PRIMARY KEY,
    last_progress_at    TEXT,
    stalled_since       TEXT,
    stall_count         INTEGER NOT NULL DEFAULT 0,
    next_principal      TEXT,
    status              TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    filename    TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_seq        ON events(seq);
CREATE INDEX IF NOT EXISTS idx_events_workspace   ON events(workspace_id, seq);
CREATE INDEX IF NOT EXISTS idx_work_workspace     ON work_items(workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_missions_workspace ON missions(workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_events_principal  ON events(principal);
CREATE INDEX IF NOT EXISTS idx_events_type       ON events(type);
CREATE INDEX IF NOT EXISTS idx_decisions_status  ON decisions(status);
CREATE INDEX IF NOT EXISTS idx_work_mission      ON work_items(mission_id);
CREATE INDEX IF NOT EXISTS idx_work_status       ON work_items(status);
CREATE INDEX IF NOT EXISTS idx_outbox_created    ON outbox(created_at);
"""

class IdempotencyConflict(Exception):
    pass

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def _hash_event(event_id, principal, type_, payload_str, prev_hash):
    content = f"{event_id}|{principal}|{type_}|{payload_str}|{prev_hash}"
    return hashlib.sha256(content.encode()).hexdigest()

def init():
    with get_db() as db:
        db.executescript(SCHEMA)
        # Record migration
        db.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,filename,applied_at) VALUES(?,?,?)",
            (1, "001_initial_schema.sql", datetime.now(timezone.utc).isoformat())
        )
        db.execute(
            "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version',?)",
            (str(SCHEMA_VERSION),)
        )
        db.commit()
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO workspaces(workspace_id,name,owner,created_at) VALUES('default','Default Workspace','arcides',?)",
                   (datetime.now(timezone.utc).isoformat(),))
        db.commit()
    print(f"[db] schema v{SCHEMA_VERSION} ready: {DB_PATH}")

def append_event(db, event_id, principal, type_, payload, provenance=None, workspace_id="default"):
    """
    Idempotent event append with outbox pattern and hash chain.
    
    1. Write to outbox (visible only within this transaction)
    2. Compute hash chain
    3. Move to events (atomic with commit)
    
    Returns (seq, was_duplicate).
    Raises IdempotencyConflict if event_id exists with different payload.
    """
    ts           = datetime.now(timezone.utc).isoformat()
    payload_str  = json.dumps(payload, sort_keys=True)
    prov_str     = json.dumps(provenance or {}, sort_keys=True)

    # Idempotency check
    existing = db.execute(
        "SELECT seq, payload_json FROM events WHERE event_id=?", (event_id,)
    ).fetchone()
    if existing:
        if existing["payload_json"] == payload_str:
            return existing["seq"], True
        raise IdempotencyConflict(
            f"event_id={event_id} already exists with different payload"
        )

    # Hash chain — get last event's hash
    prev = db.execute("SELECT this_hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    prev_hash = prev["this_hash"] if prev else ""
    this_hash = _hash_event(event_id, principal, type_, payload_str, prev_hash)

    # Write to outbox first (outbox pattern)
    db.execute(
        "INSERT INTO outbox(event_id,ts,principal,type,payload_json,provenance_json,created_at) VALUES(?,?,?,?,?,?,?)",
        (event_id, ts, principal, type_, payload_str, prov_str, ts)
    )

    # Move from outbox to events (atomic within same transaction)
    db.execute(
        "INSERT INTO events(workspace_id,event_id,ts,principal,type,payload_json,provenance_json,prev_hash,this_hash) VALUES(?,?,?,?,?,?,?,?,?)",
        (workspace_id, event_id, ts, principal, type_, payload_str, prov_str, prev_hash, this_hash)
    )
    db.execute("DELETE FROM outbox WHERE event_id=?", (event_id,))

    seq = db.execute("SELECT seq FROM events WHERE event_id=?", (event_id,)).fetchone()["seq"]
    return seq, False

def flush_outbox(db):
    """
    Flush any events stuck in outbox (e.g. after a crash mid-transaction).
    Called on startup. Returns number flushed.
    """
    stuck = db.execute("SELECT * FROM outbox ORDER BY outbox_id").fetchall()
    flushed = 0
    for row in stuck:
        # Check if already in events (idempotent)
        exists = db.execute("SELECT 1 FROM events WHERE event_id=?", (row["event_id"],)).fetchone()
        if not exists:
            prev = db.execute("SELECT this_hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = prev["this_hash"] if prev else ""
            this_hash = _hash_event(row["event_id"], row["principal"], row["type"],
                                     row["payload_json"], prev_hash)
            db.execute(
                "INSERT INTO events(event_id,ts,principal,type,payload_json,provenance_json,prev_hash,this_hash) VALUES(?,?,?,?,?,?,?,?)",
                (row["event_id"], row["ts"], row["principal"], row["type"],
                 row["payload_json"], row["provenance_json"], prev_hash, this_hash)
            )
            flushed += 1
        db.execute("DELETE FROM outbox WHERE event_id=?", (row["event_id"],))
    if flushed:
        db.commit()
        print(f"[db] flushed {flushed} events from outbox on startup")
    return flushed

def verify_hash_chain(db) -> tuple:
    """
    Verify the hash chain integrity.
    Returns (ok: bool, first_bad_seq: int|None).
    """
    events = db.execute("SELECT seq, event_id, principal, type, payload_json, prev_hash, this_hash FROM events ORDER BY seq").fetchall()
    prev_hash = ""
    for ev in events:
        expected = _hash_event(ev["event_id"], ev["principal"], ev["type"], ev["payload_json"], prev_hash)
        if expected != ev["this_hash"]:
            return False, ev["seq"]
        prev_hash = ev["this_hash"]
    return True, None
