"""
SPHERA Autonomous Mission Loop v1.0
Background scheduler that runs missions without Arcides present.

Responsibilities:
- Poll room every POLL_SECS for ready work
- Atomically claim work with fencing token
- Execute via capability-routed adapter
- Retry transient failures with exponential backoff
- Release dependency chains on completion
- Escalate to owner only for genuine authority decisions
- Survive crash/restart: replay from ledger, no duplicate results
- Independent branches continue when one principal is unavailable

Execution surfaces:
  native_session  — genuine Claude/Soba session (requires human wake)
  tool_worker     — Claude tools, bash, code execution (autonomous)
  bridge_worker   — cross-AI coordination via room events (autonomous)
"""
import json, os, sys, threading, time, uuid
from datetime import datetime, timezone, timedelta

DB_PATH    = os.environ.get("SPHERA_DB",     "./sphera.db")
POLL_SECS  = int(os.environ.get("MISSION_POLL", "5"))
MAX_RETRY  = int(os.environ.get("MISSION_MAX_RETRY", "3"))
LEASE_SECS = int(os.environ.get("MISSION_LEASE", "120"))

# Execution surface routing
# capability → (surface, autonomous)
CAPABILITY_SURFACE = {
    "backend":   ("tool_worker",    True),
    "testing":   ("tool_worker",    True),
    "devops":    ("tool_worker",    True),
    "data":      ("tool_worker",    True),
    "docs":      ("tool_worker",    True),
    "frontend":  ("tool_worker",    True),
    "security":  ("native_session", False),  # requires genuine AI reasoning
    "design":    ("native_session", False),
    "research":  ("native_session", False),
    "decision":  ("owner",          False),  # always escalates
}

def get_db():
    import sqlite3
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def utcnow(): return datetime.now(timezone.utc)
def utcnow_iso(): return utcnow().isoformat()

def emit_event(db, principal, type_, payload):
    from db import append_event
    seq, _ = append_event(db, str(uuid.uuid4()), principal, type_, payload)
    return seq

# ── Atomic claim with fencing ─────────────────────────────────────────────────
def try_claim(db, work_id, worker_id) -> dict | None:
    """
    Atomically claim a work item using compare-and-set on status.
    Returns claim dict or None if not available.
    """
    lid        = str(uuid.uuid4())
    exp        = (utcnow() + timedelta(seconds=LEASE_SECS)).isoformat()
    
    cur = db.execute(
        """UPDATE work_items
           SET status='leased', lease_id=?, lease_holder=?, lease_expires=?,
               attempt_count=attempt_count+1
           WHERE work_id=? AND status='ready'""",
        (lid, worker_id, exp, work_id)
    )
    if cur.rowcount != 1:
        return None
    
    row = db.execute("SELECT * FROM work_items WHERE work_id=?", (work_id,)).fetchone()
    emit_event(db, "system", "work_claimed_by_loop", {
        "work_id": work_id, "worker_id": worker_id,
        "lease_id": lid, "attempt": row["attempt_count"],
        "execution_surface": CAPABILITY_SURFACE.get(row["capability"], ("tool_worker", True))[0]
    })
    return {"work_id": work_id, "lease_id": lid, "fencing_token": row["lease_fencing_token"],
            "description": row["description"], "capability": row["capability"],
            "attempt": row["attempt_count"]}

# ── Execution adapters ────────────────────────────────────────────────────────
def execute_tool_worker(work_item: dict) -> dict:
    """
    Autonomous tool_worker execution.
    In v1: simulates execution. v2: real bash/code tool invocation.
    Returns result dict.
    """
    cap  = work_item["capability"]
    desc = work_item["description"]
    
    # Simulate work (replace with real tool calls in v2)
    time.sleep(0.1)
    
    return {
        "status": "done",
        "output": f"[tool_worker] completed: {desc[:60]}",
        "capability": cap,
        "execution_surface": "tool_worker",
        "native_continuity": False,
        "provider": "system",
        "executed_at": utcnow_iso()
    }

def execute_native_session(work_item: dict) -> dict:
    """
    Native session work — cannot be executed autonomously.
    Creates wake_required event and returns owner_required.
    """
    return {
        "status": "owner_required",
        "reason": "native_session_required",
        "capability": work_item["capability"],
        "execution_surface": "native_session",
        "native_continuity": True,
        "message": f"Work requires genuine AI session: {work_item['description'][:60]}"
    }

ADAPTERS = {
    "tool_worker":    execute_tool_worker,
    "native_session": execute_native_session,
    "owner":          lambda w: {"status": "owner_required", "reason": "owner_decision_required"},
}

# ── Submit result ─────────────────────────────────────────────────────────────
def submit_result(db, work_id, lease_id, result: dict) -> bool:
    """
    Submit work result. Verifies lease_id to reject stale submissions.
    On success: marks done, releases dependencies, emits event.
    Returns True on success.
    """
    row = db.execute("SELECT * FROM work_items WHERE work_id=?", (work_id,)).fetchone()
    if not row:
        return False
    if row["lease_id"] != lease_id:
        emit_event(db, "system", "stale_result_rejected", {
            "work_id": work_id, "submitted_lease": lease_id,
            "current_lease": row["lease_id"]
        })
        return False
    if row["lease_expires"] and utcnow() > datetime.fromisoformat(row["lease_expires"].replace("Z","+00:00")):
        emit_event(db, "system", "stale_result_rejected", {"work_id": work_id, "reason": "lease_expired"})
        return False

    status       = result.get("status", "done")
    result_json  = json.dumps(result)
    seq          = emit_event(db, "system", "work_result", {
        "work_id": work_id, "result": result, "execution_surface": result.get("execution_surface"),
        "native_continuity": result.get("native_continuity", False)
    })
    
    db.execute(
        """UPDATE work_items SET status=?, result_summary=?, result_seq=?,
           lease_id=NULL, lease_holder=NULL, lease_expires=NULL
           WHERE work_id=?""",
        (status if status in ("done","owner_required","failed") else "done",
         result_json, seq, work_id)
    )
    
    # Release blocked dependencies
    if status == "done":
        unblocked = release_deps(db, work_id)
        if unblocked:
            emit_event(db, "system", "deps_released", {"completed": work_id, "unblocked": unblocked})
    
    return True

# ── Dependency release ────────────────────────────────────────────────────────
def release_deps(db, done_work_id) -> list:
    """Promote blocked items to ready when all their deps are done."""
    unblocked = []
    blocked = db.execute(
        "SELECT work_id, deps_json FROM work_items WHERE status='blocked'"
    ).fetchall()
    
    for b in blocked:
        try:
            deps = json.loads(b["deps_json"] or "[]")
        except Exception:
            continue
        if done_work_id not in deps:
            continue
        # Check all deps are done
        all_done = all(
            db.execute("SELECT status FROM work_items WHERE work_id=?", (d,)).fetchone()["status"] == "done"
            for d in deps
            if db.execute("SELECT 1 FROM work_items WHERE work_id=?", (d,)).fetchone()
        )
        if all_done:
            db.execute("UPDATE work_items SET status='ready' WHERE work_id=?", (b["work_id"],))
            emit_event(db, "system", "work_unblocked", {"work_id": b["work_id"], "by": done_work_id})
            unblocked.append(b["work_id"])
    
    return unblocked

# ── Retry with backoff ────────────────────────────────────────────────────────
def schedule_retry(db, work_id, error: str, attempt: int):
    """Schedule a retry with exponential backoff. Exhausted → owner_required."""
    if attempt >= MAX_RETRY:
        db.execute("UPDATE work_items SET status='failed', last_error=? WHERE work_id=?", (error, work_id))
        emit_event(db, "system", "work_exhausted", {"work_id": work_id, "attempts": attempt, "error": error})
        # Emit owner_required for the mission
        row = db.execute("SELECT mission_id FROM work_items WHERE work_id=?", (work_id,)).fetchone()
        if row:
            emit_event(db, "system", "owner_required", {
                "work_id": work_id, "mission_id": row["mission_id"],
                "reason": "max_retries_exhausted", "error": error
            })
        return
    
    backoff = min(60, 2 ** attempt)  # 2, 4, 8, 16, 32, 60s
    retry_at = (utcnow() + timedelta(seconds=backoff)).isoformat()
    db.execute(
        "UPDATE work_items SET status='ready', last_error=?, retry_at=?, lease_id=NULL, lease_holder=NULL WHERE work_id=?",
        (error, retry_at, work_id)
    )
    emit_event(db, "system", "work_retry_scheduled", {
        "work_id": work_id, "attempt": attempt, "backoff_secs": backoff, "retry_at": retry_at
    })

# ── Expire stale leases ───────────────────────────────────────────────────────
def reclaim_stale_leases(db) -> int:
    now = utcnow_iso()
    stale = db.execute(
        "SELECT work_id, lease_fencing_token, attempt_count FROM work_items WHERE status='leased' AND lease_expires<?",
        (now,)
    ).fetchall()
    for s in stale:
        new_fence = s["lease_fencing_token"] + 1
        db.execute(
            "UPDATE work_items SET status='ready', lease_id=NULL, lease_holder=NULL, lease_expires=NULL, lease_fencing_token=? WHERE work_id=?",
            (new_fence, s["work_id"])
        )
        emit_event(db, "system", "lease_expired_reclaimed", {
            "work_id": s["work_id"], "new_fencing_token": new_fence, "attempt": s["attempt_count"]
        })
    return len(stale)

# ── Promote retry-due items ───────────────────────────────────────────────────
def promote_retries(db) -> int:
    now = utcnow_iso()
    due = db.execute(
        "SELECT work_id FROM work_items WHERE retry_at IS NOT NULL AND retry_at<=? AND status='ready'",
        (now,)
    ).fetchall()
    # They're already ready — just clear the retry_at marker
    for r in due:
        db.execute("UPDATE work_items SET retry_at=NULL WHERE work_id=?", (r["work_id"],))
    return len(due)

# ── Main loop ─────────────────────────────────────────────────────────────────
class MissionLoop:
    def __init__(self, worker_id=None):
        self.worker_id  = worker_id or f"loop-{str(uuid.uuid4())[:8]}"
        self.running    = False
        self.completed  = 0
        self.failed     = 0
        self.escalated  = 0
        print(f"[mission-loop] {self.worker_id} init")

    def tick(self):
        """Single reconciliation pass. Returns number of items processed."""
        processed = 0
        try:
            with get_db() as db:
                # 1. Reclaim expired leases
                reclaimed = reclaim_stale_leases(db)
                if reclaimed:
                    print(f"[mission-loop] reclaimed {reclaimed} stale leases")
                
                # 2. Promote retry-due items
                promote_retries(db)
                
                # 3. Find ready work (exclude items with future retry_at)
                now = utcnow_iso()
                ready = db.execute(
                    "SELECT * FROM work_items WHERE status='ready' AND (retry_at IS NULL OR retry_at<=?) ORDER BY created_at LIMIT 10",
                    (now,)
                ).fetchall()
                
                for item in ready:
                    work_id = item["work_id"]
                    cap     = item["capability"]
                    surface, autonomous = CAPABILITY_SURFACE.get(cap, ("tool_worker", True))
                    
                    if not autonomous:
                        # Native session or owner required — emit event, skip
                        existing = db.execute(
                            "SELECT 1 FROM events WHERE type='native_wake_required' AND json_extract(payload_json,'$.work_id')=?",
                            (work_id,)
                        ).fetchone()
                        if not existing:
                            emit_event(db, "system", "native_wake_required", {
                                "work_id": work_id, "capability": cap,
                                "execution_surface": surface,
                                "description": item["description"]
                            })
                        continue
                    
                    # 4. Atomically claim
                    claim = try_claim(db, work_id, self.worker_id)
                    if not claim:
                        continue  # Already claimed by another worker
                    
                    db.commit()  # Commit claim before executing
                    
                    # 5. Execute
                    adapter = ADAPTERS.get(surface, execute_tool_worker)
                    try:
                        result = adapter(claim)
                    except Exception as e:
                        result = {"status": "failed", "error": str(e), "execution_surface": surface}
                    
                    # 6. Submit result
                    with get_db() as db2:
                        if result.get("status") == "failed":
                            schedule_retry(db2, work_id, result.get("error","unknown"), claim["attempt"])
                            self.failed += 1
                            print(f"[mission-loop] failed: {work_id[:8]} attempt:{claim['attempt']} → retry scheduled")
                        elif result.get("status") == "owner_required":
                            db2.execute("UPDATE work_items SET status='owner_required', result_summary=? WHERE work_id=?",
                                       (json.dumps(result), work_id))
                            emit_event(db2, "system", "owner_required", {
                                "work_id": work_id, "reason": result.get("reason"), "description": item["description"]
                            })
                            self.escalated += 1
                            print(f"[mission-loop] escalated: {work_id[:8]} → owner_required")
                        else:
                            ok = submit_result(db2, work_id, claim["lease_id"], result)
                            if ok:
                                self.completed += 1
                                print(f"[mission-loop] done: {work_id[:8]} [{cap}] {result.get('output','')[:40]}")
                        db2.commit()
                    
                    processed += 1

        except Exception as e:
            print(f"[mission-loop] error: {e}")
        
        return processed

    def run(self, max_ticks=0):
        self.running = True
        ticks = 0
        print(f"[mission-loop] running | poll:{POLL_SECS}s | max_retry:{MAX_RETRY}")
        while self.running:
            n = self.tick()
            ticks += 1
            if max_ticks and ticks >= max_ticks:
                break
            if n == 0:
                time.sleep(POLL_SECS)
        print(f"[mission-loop] stopped | done:{self.completed} failed:{self.failed} escalated:{self.escalated}")

    def stop(self):
        self.running = False

def start_background(worker_id=None):
    """Start mission loop as a daemon thread."""
    loop = MissionLoop(worker_id)
    t = threading.Thread(target=loop.run, daemon=True)
    t.start()
    return loop, t

if __name__ == "__main__":
    loop = MissionLoop()
    loop.run()
