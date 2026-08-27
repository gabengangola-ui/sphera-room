"""
SPHERA Orchestrator v1.0
The poke-elimination layer.

State machine that continuously tracks:
- Who owes the next move (pending_reply)
- What work is executable now (ready_work)
- When missions have stalled (stalled_since)
- Whether a native session is available or wake is required

Runs on Arcides' machine. Never pretends native sessions are alive when they are not.
Silence is an explicit state, not the default.
"""
import json, os, sqlite3, sys, time, uuid
from datetime import datetime, timezone, timedelta

DB_PATH    = os.environ.get("SPHERA_DB", "./sphera.db")
STALL_SECS = int(os.environ.get("SPHERA_STALL_SECS", "300"))   # 5 min
POLL_SECS  = int(os.environ.get("SPHERA_ORCH_POLL", "15"))

# ── Schema ────────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_reply (
    id              TEXT PRIMARY KEY,
    principal       TEXT NOT NULL,          -- who owes the response
    source_seq      INTEGER NOT NULL,       -- the event that created this obligation
    source_principal TEXT NOT NULL,         -- who triggered it
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | resolved | native_wake_required | owner_required
    resolved_at     TEXT,
    resolved_by_seq INTEGER
);

CREATE TABLE IF NOT EXISTS orch_mission_state (
    mission_id      TEXT PRIMARY KEY,
    last_progress_at TEXT,
    stalled_since   TEXT,
    stall_count     INTEGER NOT NULL DEFAULT 0,
    next_principal  TEXT,                   -- who moves next
    status          TEXT NOT NULL DEFAULT 'active'  -- active | stalled | complete | owner_required
);

CREATE TABLE IF NOT EXISTS orch_event_log (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    event_type      TEXT NOT NULL,          -- turn_owed | work_ready | mission_stalled | wake_required | follow_on_created
    payload         TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_pending_principal ON pending_reply(principal, status);
CREATE INDEX IF NOT EXISTS idx_orch_mission ON orch_mission_state(status);
"""

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def utcnow(): return datetime.now(timezone.utc)
def utcnow_iso(): return utcnow().isoformat()
def uid(): return str(uuid.uuid4())

def orch_emit(db, event_type, payload):
    db.execute("INSERT INTO orch_event_log(ts,event_type,payload) VALUES(?,?,?)",
               (utcnow_iso(), event_type, json.dumps(payload)))

def init_schema():
    with get_db() as db:
        db.executescript(SCHEMA)
        db.commit()
    print("[orch] schema ready")

# ── Principal turn tracking ───────────────────────────────────────────────────
SPHERA_PRINCIPALS = {"claude", "soba", "arcides"}
NATIVE_AVAILABLE = {
    "claude":  False,  # No callable wake endpoint — requires human to open claude.ai
    "soba":    False,  # No callable wake endpoint — requires human to open ChatGPT
    "arcides": True,   # Always reachable — owns the hardware
}

def who_is_addressed(event: dict) -> set:
    """Heuristic: which principals are addressed by this event."""
    content = str(event.get("content", "")).lower()
    addressed = set()
    if "soba" in content:   addressed.add("soba")
    if "claude" in content: addressed.add("claude")
    if "arcides" in content or "owner" in content: addressed.add("arcides")
    return addressed

def record_pending_reply(db, principal, source_seq, source_principal):
    """Record that principal owes a response to source_seq."""
    # Check if already pending
    existing = db.execute(
        "SELECT id FROM pending_reply WHERE principal=? AND source_seq=? AND status='pending'",
        (principal, source_seq)
    ).fetchone()
    if existing:
        return  # Already tracked

    native = NATIVE_AVAILABLE.get(principal, False)
    status = "pending" if native else "native_wake_required"
    rid = uid()
    db.execute(
        "INSERT INTO pending_reply VALUES(?,?,?,?,?,?,?,?)",
        (rid, principal, source_seq, source_principal, utcnow_iso(), status, None, None)
    )
    orch_emit(db, "turn_owed", {
        "principal": principal,
        "source_seq": source_seq,
        "source_principal": source_principal,
        "native_available": native,
        "status": status
    })

def resolve_pending_reply(db, principal, resolved_by_seq):
    """Mark pending replies as resolved when principal responds."""
    db.execute(
        """UPDATE pending_reply
           SET status='resolved', resolved_at=?, resolved_by_seq=?
           WHERE principal=? AND status IN ('pending','native_wake_required')
           AND source_seq < ?""",
        (utcnow_iso(), resolved_by_seq, principal, resolved_by_seq)
    )

# ── Stall detection ───────────────────────────────────────────────────────────
def check_mission_stalls(db):
    """Detect missions with no progress in STALL_SECS."""
    missions = db.execute("SELECT * FROM orch_mission_state WHERE status='active'").fetchall()
    now = utcnow()
    for m in missions:
        last = m["last_progress_at"]
        if not last:
            continue
        try:
            last_dt = datetime.fromisoformat(last.replace("Z","+00:00"))
        except Exception:
            continue
        idle = (now - last_dt).total_seconds()
        if idle > STALL_SECS and not m["stalled_since"]:
            db.execute(
                "UPDATE orch_mission_state SET stalled_since=?, stall_count=stall_count+1, status='stalled' WHERE mission_id=?",
                (utcnow_iso(), m["mission_id"])
            )
            orch_emit(db, "mission_stalled", {
                "mission_id": m["mission_id"],
                "idle_secs": int(idle),
                "stall_count": m["stall_count"] + 1
            })
            print(f"[orch] STALLED: mission {m['mission_id'][:8]} idle {int(idle)}s")

# ── Next move computation ─────────────────────────────────────────────────────
def compute_next_move(db) -> dict:
    """
    Returns a dict describing the current room state:
    - next_principal: who moves next
    - pending_replies: list of owed responses
    - ready_work: list of work items ready to claim
    - stalled_missions: list of stalled missions
    - wake_required: principals that need native wake
    - last_progress_at: most recent activity timestamp
    """
    pending = db.execute(
        "SELECT * FROM pending_reply WHERE status IN ('pending','native_wake_required') ORDER BY created_at"
    ).fetchall()
    wake_required = [p["principal"] for p in pending if p["status"] == "native_wake_required"]
    pending_turns = [p["principal"] for p in pending if p["status"] == "pending"]

    ready_work = []
    try:
        rows = db.execute("SELECT * FROM work_items WHERE status='ready' LIMIT 10").fetchall()
        ready_work = [{"work_id": r["work_id"], "capability": r["capability"],
                       "description": r["description"][:50]} for r in rows]
    except Exception:
        pass

    stalled = db.execute(
        "SELECT mission_id, stalled_since, stall_count FROM orch_mission_state WHERE status='stalled'"
    ).fetchall()

    next_principal = None
    if pending_turns:
        next_principal = pending_turns[0]
    elif wake_required:
        next_principal = wake_required[0]
    elif ready_work:
        next_principal = "worker"

    # Last progress
    last_ev = db.execute("SELECT MAX(ts) FROM events").fetchone()
    last_progress = last_ev[0] if last_ev else None

    return {
        "next_principal": next_principal,
        "pending_replies": [{"principal": p["principal"], "source_seq": p["source_seq"],
                             "status": p["status"]} for p in pending],
        "ready_work": ready_work,
        "stalled_missions": [{"mission_id": r["mission_id"][:8], "since": r["stalled_since"]}
                             for r in stalled],
        "wake_required": list(set(wake_required)),
        "last_progress_at": last_progress,
    }

# ── Main replay loop ──────────────────────────────────────────────────────────
class Orchestrator:
    def __init__(self):
        init_schema()
        self.cursor = self._load_cursor()
        print(f"[orch] started at cursor:{self.cursor}")

    def _load_cursor(self):
        with get_db() as db:
            row = db.execute("SELECT value FROM schema_meta WHERE key='orch_cursor'").fetchone()
            return int(row["value"]) if row else 0

    def _save_cursor(self, db, seq):
        db.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('orch_cursor',?)", (str(seq),))
        self.cursor = seq

    def process_events(self):
        with get_db() as db:
            events = db.execute(
                "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT 200",
                (self.cursor,)
            ).fetchall()

            for ev in events:
                seq       = ev["seq"]
                principal = ev["principal"]
                etype     = ev["type"]
                try:
                    payload = json.loads(ev["payload_json"])
                except Exception:
                    payload = {}

                # 1. Resolve pending replies for this principal
                if principal in SPHERA_PRINCIPALS:
                    resolve_pending_reply(db, principal, seq)

                # 2. Track mission progress
                mission_id = payload.get("mission_id")
                if mission_id:
                    db.execute(
                        """INSERT INTO orch_mission_state(mission_id, last_progress_at, status)
                           VALUES(?,?,?) ON CONFLICT(mission_id) DO UPDATE SET
                           last_progress_at=excluded.last_progress_at,
                           stalled_since=NULL, status='active'""",
                        (mission_id, ev["ts"], "active")
                    )

                # 3. Detect who is addressed and record pending replies
                if etype in ("message", "bridge_message") and principal in SPHERA_PRINCIPALS:
                    addressed = who_is_addressed(payload)
                    # If no one explicitly addressed, both Claude and Soba might respond
                    if not addressed and principal == "arcides":
                        addressed = {"claude", "soba"}
                    for p in addressed:
                        if p != principal and p in SPHERA_PRINCIPALS:
                            record_pending_reply(db, p, seq, principal)

                # 4. Work completion → unblock follow-on
                if etype == "work_result":
                    work_id = payload.get("work_id","")
                    orch_emit(db, "work_completed", {"work_id": work_id, "seq": seq})

                self._save_cursor(db, seq)

            # 5. Check for stalls
            check_mission_stalls(db)

            if events:
                db.commit()
            return len(events)

    def state_summary(self) -> dict:
        with get_db() as db:
            return compute_next_move(db)

    def run(self, interval=POLL_SECS):
        while True:
            try:
                n = self.process_events()
                if n:
                    state = self.state_summary()
                    next_p = state.get("next_principal")
                    wake   = state.get("wake_required", [])
                    print(f"[orch] processed {n} events | next:{next_p} | wake_required:{wake}")
                time.sleep(interval)
            except KeyboardInterrupt:
                print("\n[orch] stopped.")
                break
            except Exception as e:
                print(f"[orch] error: {e}")
                time.sleep(interval)

if __name__ == "__main__":
    orch = Orchestrator()
    # Print initial state
    state = orch.state_summary()
    print(f"[orch] initial state:")
    print(f"  next_principal: {state['next_principal']}")
    print(f"  pending_replies: {len(state['pending_replies'])}")
    print(f"  ready_work: {len(state['ready_work'])}")
    print(f"  wake_required: {state['wake_required']}")
    orch.run()
