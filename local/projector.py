"""
SPHERA Projector v1.0 — P1: Restart recovery via ledger replay.

Soba P1 requirements:
1. Persist last applied event cursor per projector
2. On start: rebuild state by replaying ledger from cursor 0 (or snapshot)
3. Reject/ignore duplicate event IDs idempotently
4. Crash between append and projection does NOT lose the event (ledger is truth)
5. Crash after projection but before cursor checkpoint does NOT double-apply (idempotent handlers)
6. Deterministic restart test: create mission+claim+presence+message → kill → restart → byte-equivalent state
7. Health endpoint shows ledger head seq, projector cursor, replay lag, last error
"""
import json, os, sqlite3, threading, time
from datetime import datetime, timezone

DB_PATH = os.environ.get("SPHERA_DB", "./sphera.db")

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def utcnow(): return datetime.now(timezone.utc).isoformat()


class Projector:
    """
    Replays the canonical event ledger to rebuild derived room state.
    Cursor is persisted in schema_meta — crash-safe.
    All handlers are idempotent (INSERT OR IGNORE / INSERT OR REPLACE).
    """

    PROJECTOR_KEY = "projector_cursor"
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS room_state (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        seq   INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS presence (
        principal   TEXT PRIMARY KEY,
        status      TEXT NOT NULL DEFAULT 'offline',
        last_seq    INTEGER NOT NULL DEFAULT 0,
        last_ts     TEXT,
        transport   TEXT
    );
    CREATE TABLE IF NOT EXISTS missions_proj (
        mission_id  TEXT PRIMARY KEY,
        objective   TEXT NOT NULL,
        owner       TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'active',
        seq         INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS work_proj (
        work_id     TEXT PRIMARY KEY,
        mission_id  TEXT NOT NULL,
        description TEXT NOT NULL,
        capability  TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'ready',
        assigned_to TEXT,
        seq         INTEGER NOT NULL
    );
    """

    def __init__(self):
        self.cursor    = 0
        self.head_seq  = 0
        self.last_error = None
        self.lock       = threading.Lock()
        self._init_schema()
        self._load_cursor()

    def _init_schema(self):
        with get_db() as db:
            db.executescript(self.SCHEMA)
            db.commit()

    def _load_cursor(self):
        with get_db() as db:
            row = db.execute("SELECT value FROM schema_meta WHERE key=?", (self.PROJECTOR_KEY,)).fetchone()
            self.cursor = int(row["value"]) if row else 0

    def _save_cursor(self, db, seq):
        """Atomically persist cursor inside the same transaction as the projection."""
        db.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES(?,?)",
            (self.PROJECTOR_KEY, str(seq))
        )
        self.cursor = seq

    def _handle(self, db, ev):
        """
        Idempotent event handler.
        All writes use INSERT OR IGNORE or INSERT OR REPLACE.
        Safe to re-apply after a crash (seq < cursor was not saved → replay).
        """
        t       = ev["type"]
        payload = json.loads(ev["payload_json"] or "{}")
        p       = ev["principal"]
        seq     = ev["seq"]
        ts      = ev["ts"]

        # ── Presence update (every event updates the principal's last_seen) ──
        if p not in ("system",):
            db.execute(
                "INSERT OR REPLACE INTO presence(principal,status,last_seq,last_ts,transport) VALUES(?,?,?,?,?)",
                (p, "online", seq, ts, payload.get("transport_provenance","native"))
            )

        # ── Mission created ───────────────────────────────────────────────────
        if t == "mission_created":
            db.execute(
                "INSERT OR IGNORE INTO missions_proj(mission_id,objective,owner,status,seq) VALUES(?,?,?,?,?)",
                (payload.get("mission_id",""), payload.get("objective",""), p, "active", seq)
            )

        # ── Work created ──────────────────────────────────────────────────────
        elif t in ("work_created", "work_item_created"):
            db.execute(
                "INSERT OR IGNORE INTO work_proj(work_id,mission_id,description,capability,status,assigned_to,seq) VALUES(?,?,?,?,?,?,?)",
                (payload.get("work_id",""), payload.get("mission_id",""),
                 payload.get("description",""), payload.get("capability","capability"),
                 "ready", None, seq)
            )

        # ── Work claimed ──────────────────────────────────────────────────────
        elif t == "work_claimed":
            db.execute(
                "UPDATE work_proj SET status='leased', assigned_to=? WHERE work_id=? AND seq<?",
                (p, payload.get("work_id",""), seq)
            )

        # ── Work done ─────────────────────────────────────────────────────────
        elif t == "work_result":
            db.execute(
                "UPDATE work_proj SET status='done', assigned_to=NULL WHERE work_id=? AND seq<?",
                (payload.get("work_id",""), seq)
            )

        # ── Room state snapshot ───────────────────────────────────────────────
        db.execute(
            "INSERT OR REPLACE INTO room_state(key,value,seq) VALUES('last_event_seq',?,?)",
            (str(seq), seq)
        )

    def replay(self, batch_size=500):
        """
        Replay all events from cursor+1 to head.
        Each batch: handle → save cursor → commit (atomic per event).
        Crash-safe: if killed mid-batch, replays from last saved cursor.
        """
        with self.lock:
            try:
                with get_db() as db:
                    events = db.execute(
                        "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?",
                        (self.cursor, batch_size)
                    ).fetchall()

                applied = 0
                for ev in events:
                    with get_db() as db:
                        self._handle(db, ev)
                        self._save_cursor(db, ev["seq"])
                        db.commit()
                    applied += 1

                # Update head_seq
                with get_db() as db:
                    row = db.execute("SELECT MAX(seq) FROM events").fetchone()
                    self.head_seq = row[0] or 0

                return applied

            except Exception as e:
                self.last_error = str(e)
                return 0

    def state(self):
        with get_db() as db:
            row = db.execute("SELECT MAX(seq) FROM events").fetchone()
            self.head_seq = row[0] or 0
            missions = db.execute("SELECT * FROM missions_proj").fetchall()
            presence = db.execute("SELECT * FROM presence").fetchall()
            work     = db.execute("SELECT * FROM work_proj").fetchall()
        return {
            "ledger_head_seq":  self.head_seq,
            "projector_cursor": self.cursor,
            "replay_lag":       self.head_seq - self.cursor,
            "last_error":       self.last_error,
            "missions":         [dict(m) for m in missions],
            "presence":         [dict(p) for p in presence],
            "work_items":       [dict(w) for w in work],
        }

    def run_continuous(self, interval=2):
        """Background replay loop — keeps projector caught up."""
        print(f"[projector] started at cursor:{self.cursor}")
        while True:
            applied = self.replay()
            if applied:
                print(f"[projector] applied {applied} events, cursor:{self.cursor}, lag:{self.head_seq - self.cursor}")
            time.sleep(interval)
