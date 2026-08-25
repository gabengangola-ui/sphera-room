import sqlite3, os
DB = os.environ.get("SPHERA_DB", "/home/claude/sphera/sphera.db")
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, ts TEXT NOT NULL, principal TEXT NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agents (agent_id TEXT PRIMARY KEY, name TEXT NOT NULL, capabilities TEXT NOT NULL, registered_by TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'available', current_work_id TEXT, lease_id TEXT, lease_expires TEXT, registered_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS missions (mission_id TEXT PRIMARY KEY, objective TEXT NOT NULL, owner TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, completed_at TEXT, seq INTEGER);
CREATE TABLE IF NOT EXISTS work_items (work_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL, description TEXT NOT NULL, capability TEXT NOT NULL, deps TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'ready', assigned_to TEXT, lease_id TEXT, lease_expires TEXT, result TEXT, result_seq INTEGER, created_at TEXT NOT NULL, seq INTEGER);
CREATE TABLE IF NOT EXISTS decisions (request_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending', requesting_principal TEXT NOT NULL, scope TEXT NOT NULL, target TEXT NOT NULL, params TEXT NOT NULL, digest TEXT NOT NULL, deadline TEXT, claimed_at TEXT, claim_expires TEXT, seq INTEGER);
CREATE INDEX IF NOT EXISTS idx_events_seq ON events(seq);
CREATE INDEX IF NOT EXISTS idx_work_mission ON work_items(mission_id);
CREATE INDEX IF NOT EXISTS idx_work_status ON work_items(status);
CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status);
"""
def get_db():
    conn = sqlite3.connect(DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn
def init():
    with get_db() as db:
        db.executescript(SCHEMA)
    print(f"[db] ready: {DB}")
