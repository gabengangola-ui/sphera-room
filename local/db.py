"""
SPHERA db v2.0 — canonical event store.
Soba spec: idempotent append, schema_version, WAL, proper indexes.
"""
import sqlite3, os

DB = os.environ.get("SPHERA_DB", "./sphera.db")
SCHEMA_VERSION = 2

def get_db():
    conn = sqlite3.connect(DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")  # wait up to 5s on lock
    return conn

SCHEMA = """
-- Schema version tracking (Soba: controlled migrations, not ad hoc)
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Canonical event ledger (Soba: append-only, DB-allocated seq, source of truth)
CREATE TABLE IF NOT EXISTS events (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      TEXT NOT NULL UNIQUE,
    principal     TEXT NOT NULL,
    type          TEXT NOT NULL,
    ts            TEXT NOT NULL,
    payload_json  TEXT NOT NULL DEFAULT '{}',
    provenance_json TEXT NOT NULL DEFAULT '{}'
);

-- Soba: explicit index on (type, seq) and (principal, seq)
CREATE INDEX IF NOT EXISTS idx_events_type_seq      ON events(type, seq);
CREATE INDEX IF NOT EXISTS idx_events_principal_seq ON events(principal, seq);

-- Principals / presence
CREATE TABLE IF NOT EXISTS principals (
    principal_id  TEXT PRIMARY KEY,
    role          TEXT NOT NULL DEFAULT 'agent',
    status        TEXT NOT NULL DEFAULT 'offline',
    heartbeat_at  TEXT,
    last_seen_seq INTEGER DEFAULT 0,
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    registered_at TEXT NOT NULL
);

-- Missions
CREATE TABLE IF NOT EXISTS missions (
    mission_id   TEXT PRIMARY KEY,
    objective    TEXT NOT NULL,
    owner        TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active'
                     CHECK(status IN ('active','complete','cancelled')),
    created_at   TEXT NOT NULL,
    seq          INTEGER,
    FOREIGN KEY (seq) REFERENCES events(seq)
);

-- Work items
CREATE TABLE IF NOT EXISTS work_items (
    work_id       TEXT PRIMARY KEY,
    mission_id    TEXT NOT NULL REFERENCES missions(mission_id),
    description   TEXT NOT NULL,
    capability    TEXT NOT NULL,
    deps_json     TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL DEFAULT 'ready'
                      CHECK(status IN ('ready','blocked','leased','done','failed')),
    assigned_to   TEXT,
    lease_id      TEXT,
    lease_expires TEXT,
    result_json   TEXT,
    result_seq    INTEGER,
    created_at    TEXT NOT NULL,
    seq           INTEGER,
    FOREIGN KEY (mission_id) REFERENCES missions(mission_id)
);

CREATE INDEX IF NOT EXISTS idx_work_mission ON work_items(mission_id);
CREATE INDEX IF NOT EXISTS idx_work_status  ON work_items(status);

-- Decisions
CREATE TABLE IF NOT EXISTS decisions (
    request_id   TEXT PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'pending'
                     CHECK(status IN ('pending','approved','rejected','claimed','consumed','expired')),
    requesting_principal TEXT NOT NULL,
    scope        TEXT NOT NULL,
    target       TEXT NOT NULL,
    params_json  TEXT NOT NULL DEFAULT '{}',
    digest       TEXT NOT NULL,
    deadline     TEXT,
    claimed_at   TEXT,
    claim_expires TEXT,
    seq          INTEGER
);
"""

def init():
    with get_db() as db:
        db.executescript(SCHEMA)
        # Set schema version
        current = db.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
        if not current:
            db.execute("INSERT INTO schema_meta(key,value) VALUES('version',?)", (str(SCHEMA_VERSION),))
            db.commit()
            print(f"[db] schema v{SCHEMA_VERSION} initialised: {DB}")
        else:
            v = int(current["value"])
            if v < SCHEMA_VERSION:
                # Future migrations go here
                db.execute("UPDATE schema_meta SET value=? WHERE key='version'", (str(SCHEMA_VERSION),))
                db.commit()
                print(f"[db] schema migrated v{v}→v{SCHEMA_VERSION}: {DB}")
            else:
                print(f"[db] schema v{v} ready: {DB}")

def append_event(db, event_id, principal, type_, payload, provenance=None):
    """
    Soba: idempotent append.
    If event_id already exists, return original row (no duplicate).
    If event_id exists but payload differs, raise IdempotencyConflict.
    Returns (seq, was_duplicate).
    """
    import json
    payload_str    = json.dumps(payload, sort_keys=True)
    provenance_str = json.dumps(provenance or {}, sort_keys=True)
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).isoformat()

    try:
        db.execute(
            "INSERT INTO events(event_id,principal,type,ts,payload_json,provenance_json) VALUES(?,?,?,?,?,?)",
            (event_id, principal, type_, ts, payload_str, provenance_str)
        )
        row = db.execute("SELECT seq FROM events WHERE event_id=?", (event_id,)).fetchone()
        return row["seq"], False
    except Exception as e:
        if "UNIQUE constraint failed" in str(e):
            # Soba: duplicate — verify content matches
            existing = db.execute("SELECT seq,payload_json FROM events WHERE event_id=?", (event_id,)).fetchone()
            if existing["payload_json"] != payload_str:
                raise IdempotencyConflict(f"event_id={event_id} exists with different payload")
            return existing["seq"], True
        raise

class IdempotencyConflict(Exception):
    pass
