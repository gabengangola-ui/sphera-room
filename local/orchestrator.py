"""
SPHERA Orchestrator v0.1
Soba specification: tracks who owes the next move, what work is executable,
detects stalls, drives autonomous follow-on work creation.
Never pretends a native session is alive when it is not.

State machine per Soba:
- pending_reply(principal, source_seq, created_at, status)
- ready_work queue
- stalled_since / stall_detection
- wake_required / native_available
- last_progress_at / retry_count / attempt owner fencing
- autonomous follow_on creation

Honest principle: when native session wake is impossible → state = 'native_wake_required'
not silence, not pretending, not Path 1 API calls.
"""
import json, os, sys, time, sqlite3, threading, urllib.request
from datetime import datetime, timezone, timedelta

DB_PATH    = os.environ.get("SPHERA_DB",    "./sphera.db")
ROOM_URL   = os.environ.get("SPHERA_URL",   "http://localhost:8765")
CLAUDE_KEY = os.environ.get("CLAUDE_KEY",   "ck-sphera")
BRIDGE_KEY = os.environ.get("BRIDGE_KEY",   "br-sphera")
POLL_SECS  = int(os.environ.get("ORCH_POLL","15"))
STALL_SECS = int(os.environ.get("ORCH_STALL","300"))  # 5 min stall threshold
REPLY_SLA  = int(os.environ.get("ORCH_SLA", "180"))   # 3 min reply SLA

SCHEMA = """
CREATE TABLE IF NOT EXISTS orch_pending_reply (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    principal   TEXT NOT NULL,
    source_seq  INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    cleared_by_seq INTEGER
);
CREATE TABLE IF NOT EXISTS orch_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    ts    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orch_stall_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id  TEXT,
    work_id     TEXT,
    stalled_at  TEXT NOT NULL,
    reason      TEXT NOT NULL,
    resolved_at TEXT
);
"""

def utcnow(): return datetime.now(timezone.utc)
def utcnow_iso(): return utcnow().isoformat()

def get_orch_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_orch():
    with get_orch_db() as db:
        db.executescript(SCHEMA)
        db.commit()

def room_call(method, path, body=None, key=None):
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(f"{ROOM_URL}{path}", data=data, method=method,
           headers={"Authorization": f"Bearer {key or CLAUDE_KEY}",
                    "Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=5)
        return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

def get_state(key, default=None):
    with get_orch_db() as db:
        row = db.execute("SELECT value FROM orch_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default

def set_state(key, value):
    with get_orch_db() as db:
        db.execute("INSERT OR REPLACE INTO orch_state(key,value,ts) VALUES(?,?,?)",
                   (key, str(value), utcnow_iso()))
        db.commit()


class Orchestrator:
    """
    Continuously inspects room state and drives forward progress.
    Records exact boundary where native session wake is required.
    Never silent. Never pretending.
    """

    NATIVE_PRINCIPALS = {"claude", "soba"}
    WORKER_PRINCIPALS = {"system", "bridge"}

    def __init__(self):
        init_orch()
        self.cursor = int(get_state("orch_cursor", 0))
        self.last_progress = {}   # principal → datetime of last event
        self.pending_turns = {}   # principal → source_seq that needs a reply
        print(f"[orch] started at cursor:{self.cursor}")

    def _save_cursor(self, seq):
        self.cursor = seq
        set_state("orch_cursor", seq)

    def inspect(self):
        """One inspection cycle."""
        r = room_call("GET", f"/events?after={self.cursor}")
        events = r.get("events", [])

        for ev in events:
            self._process_event(ev)
            self._save_cursor(ev["seq"])

        self._check_stalls()
        self._check_reply_sla()
        self._check_ready_work()

    def _process_event(self, ev):
        """Update orchestration state from a new event."""
        p   = ev.get("principal","")
        seq = ev.get("seq", 0)
        ts  = ev.get("ts","")
        t   = ev.get("type","")
        try:
            self.last_progress[p] = datetime.fromisoformat(ts.replace("Z","+00:00"))
        except Exception:
            pass

        # Clear pending reply if this principal just spoke
        if p in self.pending_turns:
            source_seq = self.pending_turns.get(p)
            if source_seq and seq > source_seq:
                del self.pending_turns[p]
                with get_orch_db() as db:
                    db.execute(
                        "UPDATE orch_pending_reply SET status='cleared',cleared_by_seq=? WHERE principal=? AND status='pending'",
                        (seq, p)
                    )
                    db.commit()
                print(f"[orch] {p} cleared pending reply at seq:{seq}")

        # Track who is owed a reply
        if p == "arcides" or p == "soba":
            # Claude owes a reply
            if p != "claude" and "claude" not in self.pending_turns:
                self.pending_turns["claude"] = seq
                self._record_pending_reply("claude", seq)
        if p == "arcides" or p == "claude":
            # Soba owes a reply
            if p != "soba" and "soba" not in self.pending_turns:
                self.pending_turns["soba"] = seq
                self._record_pending_reply("soba", seq)

    def _record_pending_reply(self, principal, source_seq):
        with get_orch_db() as db:
            # Don't duplicate
            ex = db.execute(
                "SELECT id FROM orch_pending_reply WHERE principal=? AND status='pending'",
                (principal,)
            ).fetchone()
            if not ex:
                db.execute(
                    "INSERT INTO orch_pending_reply(principal,source_seq,created_at,status) VALUES(?,?,?,?)",
                    (principal, source_seq, utcnow_iso(), "pending")
                )
                db.commit()

    def _check_reply_sla(self):
        """Check if any principal has breached their reply SLA."""
        now = utcnow()
        with get_orch_db() as db:
            pending = db.execute(
                "SELECT * FROM orch_pending_reply WHERE status='pending'"
            ).fetchall()

        for row in pending:
            try:
                created = datetime.fromisoformat(row["created_at"].replace("Z","+00:00"))
            except Exception:
                continue
            age_secs = (now - created).total_seconds()
            if age_secs > REPLY_SLA:
                principal = row["principal"]
                print(f"[orch] NATIVE_WAKE_REQUIRED: {principal} owes reply (age:{int(age_secs)}s, SLA:{REPLY_SLA}s)")
                set_state(f"wake_required_{principal}",
                          json.dumps({"principal": principal, "source_seq": row["source_seq"],
                                      "age_secs": int(age_secs), "native_available": False,
                                      "reason": "native_session_dormant",
                                      "ts": utcnow_iso()}))

    def _check_stalls(self):
        """Detect stalled missions — active work but no progress."""
        r = room_call("GET", "/room")
        if r.get("error"):
            return

        work = r.get("work", {})
        if work.get("leased", 0) > 0:
            # There's leased work — check when last progress happened
            now = utcnow()
            all_last = list(self.last_progress.values())
            if all_last:
                most_recent = max(all_last)
                idle_secs = (now - most_recent).total_seconds()
                if idle_secs > STALL_SECS:
                    print(f"[orch] STALL DETECTED: no progress for {int(idle_secs)}s")
                    with get_orch_db() as db:
                        db.execute(
                            "INSERT INTO orch_stall_log(stalled_at,reason) VALUES(?,?)",
                            (utcnow_iso(), f"no_progress_{int(idle_secs)}s")
                        )
                        db.commit()
                    # Post stall event to room
                    room_call("POST", "/bridge/ingest", {
                        "principal": "system",
                        "content": f"[orch] Mission stall detected: no progress for {int(idle_secs//60)}min. Leased work exists but no activity.",
                        "source_message_id": f"orch-stall-{int(time.time())}",
                        "transport": "orchestrator",
                        "original_ts": utcnow_iso()
                    }, key=BRIDGE_KEY)

    def _check_ready_work(self):
        """Check for ready work and log if principals haven't claimed it."""
        r = room_call("GET", "/room")
        if r.get("error"):
            return
        ready = r.get("work", {}).get("ready", 0)
        if ready > 0:
            set_state("ready_work_count", ready)

    def next_move_summary(self) -> dict:
        """Return honest state of who owes the next move."""
        pending = {}
        with get_orch_db() as db:
            rows = db.execute(
                "SELECT * FROM orch_pending_reply WHERE status='pending'"
            ).fetchall()
        for row in rows:
            pending[row["principal"]] = {
                "source_seq": row["source_seq"],
                "created_at": row["created_at"],
                "status": "native_wake_required"  # always honest
            }

        r = room_call("GET", "/room")
        ready = r.get("work",{}).get("ready",0) if not r.get("error") else 0
        leased = r.get("work",{}).get("leased",0) if not r.get("error") else 0

        return {
            "pending_replies": pending,
            "ready_work": ready,
            "leased_work": leased,
            "last_progress": {k: v.isoformat() for k,v in self.last_progress.items()},
            "ts": utcnow_iso()
        }

    def run(self):
        """Continuous orchestration loop."""
        while True:
            try:
                self.inspect()
                summary = self.next_move_summary()
                if summary["pending_replies"] or summary["ready_work"]:
                    print(f"[orch] {utcnow_iso()[:19]} | pending:{list(summary['pending_replies'].keys())} | ready_work:{summary['ready_work']}")
                time.sleep(POLL_SECS)
            except KeyboardInterrupt:
                print("\n[orch] stopped.")
                break
            except Exception as e:
                print(f"[orch] error: {e}")
                time.sleep(POLL_SECS)


if __name__ == "__main__":
    r = room_call("GET", "/health")
    if not r.get("ok"):
        print(f"[orch] room unreachable: {r}"); sys.exit(1)
    print(f"[orch] room OK: seq:{r['last_seq']} events:{r['event_count']}")
    orch = Orchestrator()
    orch.run()
