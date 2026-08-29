"""
SPHERA Orchestrator v1.1 — workspace-aware, Windows-compatible
"""
import json, os, sqlite3, threading, time, uuid
from datetime import datetime, timezone, timedelta

DB_PATH    = os.environ.get("SPHERA_DB", "./sphera.db")
STALL_SECS = int(os.environ.get("SPHERA_STALL_SECS", "300"))
POLL_SECS  = int(os.environ.get("SPHERA_ORCH_POLL", "15"))
SPHERA_PRINCIPALS = {"claude", "soba", "arcides"}
NATIVE_AVAILABLE  = {"claude": False, "soba": False, "arcides": True}

SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_reply (
    workspace_id     TEXT NOT NULL DEFAULT 'default',
    id               TEXT NOT NULL,
    principal        TEXT NOT NULL,
    source_seq       INTEGER NOT NULL,
    source_principal TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    resolved_at      TEXT,
    resolved_by_seq  INTEGER,
    PRIMARY KEY(workspace_id, id)
);
CREATE TABLE IF NOT EXISTS orch_mission_state (
    workspace_id     TEXT NOT NULL DEFAULT 'default',
    mission_id       TEXT NOT NULL,
    last_progress_at TEXT,
    stalled_since    TEXT,
    stall_count      INTEGER NOT NULL DEFAULT 0,
    next_principal   TEXT,
    status           TEXT NOT NULL DEFAULT 'active',
    PRIMARY KEY(workspace_id, mission_id)
);
CREATE TABLE IF NOT EXISTS orch_event_log (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}'
);
"""

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def utcnow():     return datetime.now(timezone.utc)
def utcnow_iso(): return utcnow().isoformat()
def uid():        return str(uuid.uuid4())

def init_schema():
    with get_db() as db:
        db.executescript(SCHEMA)
        db.commit()
    print("[orch] schema ready")

def orch_emit(db, event_type, payload):
    db.execute(
        "INSERT INTO orch_event_log(ts,event_type,payload) VALUES(?,?,?)",
        (utcnow_iso(), event_type, json.dumps(payload))
    )

def who_is_addressed(payload):
    content = str(payload.get("content","")).lower()
    addressed = set()
    if "soba"    in content: addressed.add("soba")
    if "claude"  in content: addressed.add("claude")
    if "arcides" in content or "owner" in content: addressed.add("arcides")
    return addressed

def record_pending_reply(db, principal, source_seq, source_principal):
    existing = db.execute(
        "SELECT id FROM pending_reply WHERE workspace_id='default' AND principal=? AND source_seq=? AND status='pending'",
        (principal, source_seq)
    ).fetchone()
    if existing:
        return
    native = NATIVE_AVAILABLE.get(principal, False)
    status = "pending" if native else "native_wake_required"
    rid = uid()
    db.execute(
        "INSERT INTO pending_reply(workspace_id,id,principal,source_seq,source_principal,created_at,status) VALUES(?,?,?,?,?,?,?)",
        ("default", rid, principal, source_seq, source_principal, utcnow_iso(), status)
    )
    orch_emit(db, "turn_owed", {
        "principal": principal, "source_seq": source_seq,
        "native_available": native, "status": status
    })

def resolve_pending_reply(db, principal, resolved_by_seq):
    db.execute(
        """UPDATE pending_reply SET status='resolved', resolved_at=?, resolved_by_seq=?
           WHERE workspace_id='default' AND principal=? AND status IN ('pending','native_wake_required') AND source_seq < ?""",
        (utcnow_iso(), resolved_by_seq, principal, resolved_by_seq)
    )

def check_mission_stalls(db):
    missions = db.execute(
        "SELECT * FROM orch_mission_state WHERE workspace_id='default' AND status='active'"
    ).fetchall()
    now = utcnow()
    for m in missions:
        if not m["last_progress_at"]:
            continue
        try:
            last_dt = datetime.fromisoformat(m["last_progress_at"].replace("Z","+00:00"))
        except Exception:
            continue
        idle = (now - last_dt).total_seconds()
        if idle > STALL_SECS and not m["stalled_since"]:
            db.execute(
                "UPDATE orch_mission_state SET stalled_since=?, stall_count=stall_count+1, status='stalled' WHERE workspace_id='default' AND mission_id=?",
                (utcnow_iso(), m["mission_id"])
            )
            orch_emit(db, "mission_stalled", {"mission_id": m["mission_id"], "idle_secs": int(idle)})
            print(f"[orch] STALLED: mission {m['mission_id'][:8]} idle {int(idle)}s")

def compute_next_move(db):
    pending = db.execute(
        "SELECT * FROM pending_reply WHERE workspace_id='default' AND status IN ('pending','native_wake_required') ORDER BY created_at"
    ).fetchall()
    wake_required = [p["principal"] for p in pending if p["status"]=="native_wake_required"]
    pending_turns = [p["principal"] for p in pending if p["status"]=="pending"]
    stalled = db.execute(
        "SELECT mission_id, stalled_since FROM orch_mission_state WHERE workspace_id='default' AND status='stalled'"
    ).fetchall()
    try:
        ready_work = db.execute("SELECT COUNT(*) FROM work_items WHERE workspace_id='default' AND status='ready'").fetchone()[0]
    except Exception:
        ready_work = 0
    next_principal = pending_turns[0] if pending_turns else (wake_required[0] if wake_required else None)
    last_ev = db.execute("SELECT MAX(ts) FROM events").fetchone()
    return {
        "next_principal":  next_principal,
        "pending_replies": [{"principal":p["principal"],"source_seq":p["source_seq"],"status":p["status"]} for p in pending],
        "ready_work":      ready_work,
        "stalled_missions":[{"mission_id":r["mission_id"][:8],"since":r["stalled_since"]} for r in stalled],
        "wake_required":   list(set(wake_required)),
        "last_progress_at":last_ev[0] if last_ev else None,
    }

class Orchestrator:
    def __init__(self):
        init_schema()
        self.cursor = self._load_cursor()
        print(f"[orch] started at cursor:{self.cursor}")

    def _load_cursor(self):
        try:
            with get_db() as db:
                row = db.execute("SELECT value FROM schema_meta WHERE key='orch_cursor'").fetchone()
                return int(row["value"]) if row else 0
        except Exception:
            return 0

    def _save_cursor(self, db, seq):
        db.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('orch_cursor',?)", (str(seq),))
        self.cursor = seq

    def process_events(self):
        try:
            with get_db() as db:
                events = db.execute(
                    "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT 200",
                    (self.cursor,)
                ).fetchall()
                for ev in events:
                    seq       = ev["seq"]
                    principal = ev["principal"]
                    etype     = ev["type"]
                    try:    payload = json.loads(ev["payload_json"])
                    except: payload = {}
                    if principal in SPHERA_PRINCIPALS:
                        resolve_pending_reply(db, principal, seq)
                    mission_id = payload.get("mission_id")
                    if mission_id:
                        existing_ms = db.execute(
                            "SELECT 1 FROM orch_mission_state WHERE workspace_id='default' AND mission_id=?",
                            (mission_id,)
                        ).fetchone()
                        if existing_ms:
                            db.execute(
                                "UPDATE orch_mission_state SET last_progress_at=?, stalled_since=NULL, status='active' WHERE workspace_id='default' AND mission_id=?",
                                (ev["ts"], mission_id)
                            )
                        else:
                            db.execute(
                                "INSERT INTO orch_mission_state(workspace_id,mission_id,last_progress_at,status) VALUES('default',?,?,'active')",
                                (mission_id, ev["ts"])
                            )
                    if etype in ("message","bridge_message") and principal in SPHERA_PRINCIPALS:
                        addressed = who_is_addressed(payload)
                        if not addressed and principal == "arcides":
                            addressed = {"claude","soba"}
                        for p in addressed:
                            if p != principal and p in SPHERA_PRINCIPALS:
                                record_pending_reply(db, p, seq, principal)
                    self._save_cursor(db, seq)
                check_mission_stalls(db)
                if events:
                    db.commit()
                return len(events)
        except Exception as e:
            print(f"[orch] error: {e}")
            return 0

    def state_summary(self):
        try:
            with get_db() as db:
                return compute_next_move(db)
        except Exception as e:
            return {"error": str(e), "next_principal": None, "wake_required": [], "pending_replies": []}

    def run(self, interval=POLL_SECS):
        while True:
            try:
                n = self.process_events()
                if n:
                    state = self.state_summary()
                    print(f"[orch] {n} events | next:{state.get('next_principal')} | wake:{state.get('wake_required')}")
                time.sleep(interval)
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"[orch] error: {e}")
                time.sleep(interval)
