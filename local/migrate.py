"""
SPHERA Schema Migration — v1 (legacy server.py schema) → v3 (canonical db.py schema)
Idempotent. Transactional. Preserves existing rows. Zero data loss.
"""
import json, os, sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("SPHERA_DB", "./sphera.db")

CANONICAL_COLUMNS = {
    "events":     {"event_id", "ts", "principal", "type", "payload_json", "provenance_json", "prev_hash", "this_hash"},
    "work_items": {"work_id", "mission_id", "description", "capability", "deps_json", "status",
                   "lease_id", "lease_holder", "lease_expires", "lease_fencing_token",
                   "result_seq", "result_summary", "created_at", "version",
                   "attempt_count", "max_attempts", "retry_at", "last_error"},
    "decisions":  {"request_id", "status", "requesting_principal", "scope", "target",
                   "params_json", "bound_digest", "deadline", "version",
                   "claimed_at", "claim_expires", "claim_fencing_token", "approved_at", "consumed_at"},
}

def get_columns(db, table):
    rows = db.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}

def table_exists(db, table):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())

def migrate(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=OFF")  # OFF during migration only

    issues = []

    try:
        # ── Check what we're working with ────────────────────────────────────
        has_events     = table_exists(conn, "events")
        has_schema_meta = table_exists(conn, "schema_meta")
        has_outbox     = table_exists(conn, "outbox")

        if not has_events:
            print("[migrate] fresh DB — no migration needed")
            conn.close()
            return True

        existing_cols = get_columns(conn, "events") if has_events else set()
        is_legacy = "payload" in existing_cols and "payload_json" not in existing_cols

        print(f"[migrate] events columns: {sorted(existing_cols)}")
        print(f"[migrate] schema type: {'legacy' if is_legacy else 'canonical'}")

        # ── Ensure schema_meta exists ─────────────────────────────────────────
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY, value TEXT NOT NULL)""")

        # ── Ensure outbox exists ──────────────────────────────────────────────
        conn.execute("""CREATE TABLE IF NOT EXISTS outbox (
            outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE, ts TEXT NOT NULL,
            principal TEXT NOT NULL, type TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            provenance_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL)""")

        # ── Migrate events table ──────────────────────────────────────────────
        if is_legacy:
            print("[migrate] migrating events: payload → payload_json, id → event_id")
            conn.execute("""CREATE TABLE events_v3 (
                seq         INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id    TEXT NOT NULL UNIQUE,
                ts          TEXT NOT NULL,
                principal   TEXT NOT NULL,
                type        TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                provenance_json TEXT NOT NULL DEFAULT '{}',
                prev_hash   TEXT NOT NULL DEFAULT '',
                this_hash   TEXT NOT NULL DEFAULT '')""")
            rows = conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
            for r in rows:
                eid = r["id"] if "id" in r.keys() else str(r["seq"])
                payload = r["payload"] if "payload" in r.keys() else "{}"
                conn.execute(
                    "INSERT INTO events_v3(event_id,ts,principal,type,payload_json,provenance_json,prev_hash,this_hash) VALUES(?,?,?,?,?,?,?,?)",
                    (eid, r["ts"], r["principal"], r["type"], payload, "{}", "", "")
                )
            conn.execute("DROP TABLE events")
            conn.execute("ALTER TABLE events_v3 RENAME TO events")
            print(f"[migrate] migrated {len(rows)} events rows")

        # ── Add missing columns to events ─────────────────────────────────────
        ev_cols = get_columns(conn, "events")
        for col, defn in [("prev_hash","TEXT NOT NULL DEFAULT ''"),
                          ("this_hash","TEXT NOT NULL DEFAULT ''"),
                          ("provenance_json","TEXT NOT NULL DEFAULT '{}'"  )]:
            if col not in ev_cols:
                conn.execute(f"ALTER TABLE events ADD COLUMN {col} {defn}")
                print(f"[migrate] added events.{col}")

        # ── Migrate work_items ────────────────────────────────────────────────
        if table_exists(conn, "work_items"):
            wk_cols = get_columns(conn, "work_items")
            renames = [("deps","deps_json"), ("result","result_summary")]
            needs_rebuild = any(old in wk_cols for old,_ in renames)
            if needs_rebuild:
                print("[migrate] rebuilding work_items with canonical schema")
                conn.execute("""CREATE TABLE work_items_v3 (
                    work_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL,
                    description TEXT NOT NULL, capability TEXT NOT NULL,
                    deps_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'ready',
                    lease_id TEXT, lease_holder TEXT, lease_expires TEXT,
                    lease_fencing_token INTEGER NOT NULL DEFAULT 0,
                    result_seq INTEGER, result_summary TEXT, created_at TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    retry_at TEXT, last_error TEXT)""")
                rows = conn.execute("SELECT * FROM work_items").fetchall()
                for r in rows:
                    deps = r["deps_json"] if "deps_json" in r.keys() else (r["deps"] if "deps" in r.keys() else "[]")
                    res  = r["result_summary"] if "result_summary" in r.keys() else (r["result"] if "result" in r.keys() else None)
                    created = r["created_at"] if "created_at" in r.keys() else datetime.now(timezone.utc).isoformat()
                    conn.execute(
                        """INSERT INTO work_items_v3(work_id,mission_id,description,capability,deps_json,status,
                           lease_id,lease_holder,lease_expires,lease_fencing_token,result_seq,result_summary,
                           created_at,version,attempt_count,max_attempts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,0,3)""",
                        (r["work_id"],r["mission_id"],r["description"],r["capability"],
                         deps,r["status"],
                         r["lease_id"] if "lease_id" in r.keys() else None,
                         r["lease_holder"] if "lease_holder" in r.keys() else None,
                         r["lease_expires"] if "lease_expires" in r.keys() else None,
                         0,
                         r["result_seq"] if "result_seq" in r.keys() else None,
                         res, created)
                    )
                conn.execute("DROP TABLE work_items")
                conn.execute("ALTER TABLE work_items_v3 RENAME TO work_items")
                print(f"[migrate] migrated {len(rows)} work_items rows")
            else:
                # Just add missing columns
                for col,defn in [("lease_fencing_token","INTEGER NOT NULL DEFAULT 0"),
                                  ("version","INTEGER NOT NULL DEFAULT 1"),
                                  ("attempt_count","INTEGER NOT NULL DEFAULT 0"),
                                  ("max_attempts","INTEGER NOT NULL DEFAULT 3"),
                                  ("retry_at","TEXT"), ("last_error","TEXT"),
                                  ("result_summary","TEXT")]:
                    if col not in wk_cols:
                        conn.execute(f"ALTER TABLE work_items ADD COLUMN {col} {defn}")
                        print(f"[migrate] added work_items.{col}")

        # ── Migrate decisions ─────────────────────────────────────────────────
        if table_exists(conn, "decisions"):
            dc_cols = get_columns(conn, "decisions")
            for col,defn in [("params_json","TEXT NOT NULL DEFAULT '{}'"),
                              ("bound_digest","TEXT NOT NULL DEFAULT ''"),
                              ("claim_fencing_token","INTEGER"),
                              ("version","INTEGER NOT NULL DEFAULT 1"),
                              ("approved_at","TEXT"), ("consumed_at","TEXT")]:
                if col not in dc_cols:
                    conn.execute(f"ALTER TABLE decisions ADD COLUMN {col} {defn}")
                    print(f"[migrate] added decisions.{col}")
            # Populate params_json from params if exists
            if "params" in dc_cols and "params_json" not in dc_cols:
                conn.execute("UPDATE decisions SET params_json=params WHERE params_json='{}'")

        # ── Indexes ────────────────────────────────────────────────────────────
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_events_seq ON events(seq);
            CREATE INDEX IF NOT EXISTS idx_events_principal ON events(principal);
            CREATE INDEX IF NOT EXISTS idx_work_status ON work_items(status);
            CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status);
        """)

        # ── Record migration ───────────────────────────────────────────────────
        conn.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version','3')")
        conn.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('migrated_at',?)",
                     (datetime.now(timezone.utc).isoformat(),))
        conn.commit()
        print("[migrate] migration complete — schema v3")
        return True

    except Exception as e:
        conn.rollback()
        print(f"[migrate] ERROR: {e}")
        return False
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.close()

if __name__ == "__main__":
    ok = migrate()
    exit(0 if ok else 1)
