"""
SPHERA Schema Migration — legacy server.py schema → canonical db.py v3
Idempotent. Transactional. Zero data loss. Preserves existing rows.
Run on every startup via db.init() + migrate.migrate().
"""
import json, os, sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("SPHERA_DB", "./sphera.db")

def get_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def get_columns(conn, table):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}

def table_exists(conn, table):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())

def migrate(db_path=DB_PATH):
    conn = get_db(db_path)
    conn.execute("PRAGMA foreign_keys=OFF")
    ts = datetime.now(timezone.utc).isoformat()

    try:
        # ── Ensure schema_meta ────────────────────────────────────────────────
        conn.execute("CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS outbox(outbox_id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, ts TEXT NOT NULL, principal TEXT NOT NULL, type TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', provenance_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)")

        # ── EVENTS ────────────────────────────────────────────────────────────
        if not table_exists(conn, "events"):
            pass  # db.init() handles fresh schema
        else:
            ev_cols = get_columns(conn, "events")
            is_legacy = "payload" in ev_cols and "payload_json" not in ev_cols

            if is_legacy:
                print(f"[migrate] migrating events: payload → payload_json, id → event_id")
                conn.execute("""CREATE TABLE events_v3 (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                    ts TEXT NOT NULL, principal TEXT NOT NULL, type TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}', provenance_json TEXT NOT NULL DEFAULT '{}',
                    prev_hash TEXT NOT NULL DEFAULT '', this_hash TEXT NOT NULL DEFAULT '')""")
                rows = conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
                for r in rows:
                    eid = r["id"] if "id" in r.keys() else str(r["seq"])
                    payload = r["payload"] if "payload" in r.keys() else "{}"
                    conn.execute("INSERT INTO events_v3(event_id,ts,principal,type,payload_json,provenance_json,prev_hash,this_hash) VALUES(?,?,?,?,?,?,?,?)",
                                 (eid, r["ts"], r["principal"], r["type"], payload, "{}", "", ""))
                conn.execute("DROP TABLE events")
                conn.execute("ALTER TABLE events_v3 RENAME TO events")
                print(f"[migrate] migrated {len(rows)} events rows")
            else:
                for col,defn in [("provenance_json","TEXT NOT NULL DEFAULT '{}'"),
                                  ("prev_hash","TEXT NOT NULL DEFAULT ''"),
                                  ("this_hash","TEXT NOT NULL DEFAULT ''")]:
                    if col not in ev_cols:
                        conn.execute(f"ALTER TABLE events ADD COLUMN {col} {defn}")

        # ── WORK_ITEMS ────────────────────────────────────────────────────────
        if table_exists(conn, "work_items"):
            wk_cols = get_columns(conn, "work_items")
            needs_rebuild = "deps" in wk_cols  # legacy column name

            if needs_rebuild:
                print("[migrate] rebuilding work_items")
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
                    cat  = r["created_at"] if "created_at" in r.keys() else ts
                    conn.execute("INSERT INTO work_items_v3(work_id,mission_id,description,capability,deps_json,status,lease_id,lease_holder,lease_expires,lease_fencing_token,result_seq,result_summary,created_at,version,attempt_count,max_attempts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,0,3)",
                                 (r["work_id"],r["mission_id"],r["description"],r["capability"],
                                  deps,r["status"],
                                  r["lease_id"] if "lease_id" in r.keys() else None,
                                  r["lease_holder"] if "lease_holder" in r.keys() else None,
                                  r["lease_expires"] if "lease_expires" in r.keys() else None,
                                  0, r["result_seq"] if "result_seq" in r.keys() else None,
                                  res, cat))
                conn.execute("DROP TABLE work_items")
                conn.execute("ALTER TABLE work_items_v3 RENAME TO work_items")
                print(f"[migrate] migrated {len(rows)} work_items rows")
            else:
                for col,defn in [("lease_fencing_token","INTEGER NOT NULL DEFAULT 0"),
                                  ("version","INTEGER NOT NULL DEFAULT 1"),
                                  ("attempt_count","INTEGER NOT NULL DEFAULT 0"),
                                  ("max_attempts","INTEGER NOT NULL DEFAULT 3"),
                                  ("retry_at","TEXT"),("last_error","TEXT"),
                                  ("result_summary","TEXT"),("lease_holder","TEXT")]:
                    if col not in wk_cols:
                        conn.execute(f"ALTER TABLE work_items ADD COLUMN {col} {defn}")

        # ── MISSIONS ──────────────────────────────────────────────────────────
        if table_exists(conn, "missions"):
            ms_cols = get_columns(conn, "missions")
            needs_rebuild = "completed_at" in ms_cols or "seq" in ms_cols

            if needs_rebuild:
                print("[migrate] rebuilding missions")
                conn.execute("""CREATE TABLE missions_v3 (
                    mission_id TEXT PRIMARY KEY, objective TEXT NOT NULL,
                    owner TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                    policy_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
                    accepted_at TEXT, acceptance_note TEXT, version INTEGER NOT NULL DEFAULT 1)""")
                rows = conn.execute("SELECT * FROM missions").fetchall()
                for r in rows:
                    cat = r["created_at"] if "created_at" in r.keys() else ts
                    conn.execute("INSERT INTO missions_v3(mission_id,objective,owner,status,policy_json,created_at,version) VALUES(?,?,?,?,?,?,1)",
                                 (r["mission_id"],r["objective"],r["owner"],r["status"],"{}",cat))
                conn.execute("DROP TABLE missions")
                conn.execute("ALTER TABLE missions_v3 RENAME TO missions")
                print(f"[migrate] migrated {len(rows)} missions rows")
            else:
                for col,defn in [("policy_json","TEXT NOT NULL DEFAULT '{}'"),
                                  ("accepted_at","TEXT"),("acceptance_note","TEXT"),
                                  ("version","INTEGER NOT NULL DEFAULT 1")]:
                    if col not in ms_cols:
                        conn.execute(f"ALTER TABLE missions ADD COLUMN {col} {defn}")

        # ── DECISIONS ─────────────────────────────────────────────────────────
        if table_exists(conn, "decisions"):
            dc_cols = get_columns(conn, "decisions")
            needs_rebuild = "params" in dc_cols or "digest" in dc_cols or "seq" in dc_cols

            if needs_rebuild:
                print("[migrate] rebuilding decisions")
                conn.execute("""CREATE TABLE decisions_v3 (
                    request_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending',
                    requesting_principal TEXT NOT NULL, scope TEXT NOT NULL, target TEXT NOT NULL,
                    params_json TEXT NOT NULL DEFAULT '{}', bound_digest TEXT NOT NULL DEFAULT '',
                    deadline TEXT, version INTEGER NOT NULL DEFAULT 1,
                    claimed_at TEXT, claim_expires TEXT, claim_fencing_token INTEGER,
                    approved_at TEXT, consumed_at TEXT)""")
                rows = conn.execute("SELECT * FROM decisions").fetchall()
                for r in rows:
                    params = r["params_json"] if "params_json" in r.keys() else (r["params"] if "params" in r.keys() else "{}")
                    dg = r["bound_digest"] if "bound_digest" in r.keys() else (r["digest"] if "digest" in r.keys() else "")
                    conn.execute("INSERT INTO decisions_v3(request_id,status,requesting_principal,scope,target,params_json,bound_digest,deadline,version,claimed_at,claim_expires,approved_at) VALUES(?,?,?,?,?,?,?,?,1,?,?,?)",
                                 (r["request_id"],r["status"],r["requesting_principal"],r["scope"],r["target"],
                                  params,dg,r["deadline"] if "deadline" in r.keys() else None,
                                  r["claimed_at"] if "claimed_at" in r.keys() else None,
                                  r["claim_expires"] if "claim_expires" in r.keys() else None,
                                  r["approved_at"] if "approved_at" in r.keys() else None))
                conn.execute("DROP TABLE decisions")
                conn.execute("ALTER TABLE decisions_v3 RENAME TO decisions")
                print(f"[migrate] migrated {len(rows)} decisions rows")
            else:
                for col,defn in [("params_json","TEXT NOT NULL DEFAULT '{}'"),
                                  ("bound_digest","TEXT NOT NULL DEFAULT ''"),
                                  ("claim_fencing_token","INTEGER"),
                                  ("version","INTEGER NOT NULL DEFAULT 1"),
                                  ("approved_at","TEXT"),("consumed_at","TEXT")]:
                    if col not in dc_cols:
                        conn.execute(f"ALTER TABLE decisions ADD COLUMN {col} {defn}")

        # ── ORCHESTRATOR TABLES ───────────────────────────────────────────────
        conn.execute("""CREATE TABLE IF NOT EXISTS pending_reply (
            id TEXT PRIMARY KEY, principal TEXT NOT NULL, source_seq INTEGER NOT NULL,
            source_principal TEXT NOT NULL, created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', resolved_at TEXT, resolved_by_seq INTEGER)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS orch_mission_state (
            mission_id TEXT PRIMARY KEY, last_progress_at TEXT, stalled_since TEXT,
            stall_count INTEGER NOT NULL DEFAULT 0, next_principal TEXT,
            status TEXT NOT NULL DEFAULT 'active')""")

        # ── INDEXES ───────────────────────────────────────────────────────────
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_events_seq ON events(seq);
            CREATE INDEX IF NOT EXISTS idx_events_principal ON events(principal);
            CREATE INDEX IF NOT EXISTS idx_work_status ON work_items(status);
            CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status);
            CREATE INDEX IF NOT EXISTS idx_pending_principal ON pending_reply(principal, status);
        """)

        conn.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version','3')")
        conn.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('migrated_at',?)", (ts,))
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version,filename,applied_at) VALUES(1,'001_initial.sql',?)", (ts,)) if table_exists(conn, "schema_migrations") else None
        conn.commit()
        print("[migrate] migration complete — schema v3")
        return True

    except Exception as e:
        conn.rollback()
        print(f"[migrate] ERROR: {e}")
        import traceback; traceback.print_exc()
        return False
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.close()

if __name__ == "__main__":
    ok = migrate()
    exit(0 if ok else 1)
