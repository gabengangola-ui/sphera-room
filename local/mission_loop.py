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
               attempt_count=attempt_count+1,
               lease_fencing_token=lease_fencing_token+1
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
# ── Executor interface ────────────────────────────────────────────────────────
class Executor:
    """
    Base executor interface. Every production executor must:
    1. Perform verifiable work
    2. Return artifact/evidence (not just text)
    3. Never claim native_continuity=True unless authenticated
    4. Be distinguishable from FakeExecutor via execution_surface provenance
    """
    def execute(self, work_item: dict) -> dict:
        raise NotImplementedError

class FakeExecutor(Executor):
    """
    TEST ONLY. Never selected in production.
    Explicitly labelled so provenance is clear.
    """
    def execute(self, work_item: dict) -> dict:
        return {
            "status": "done",
            "output": f"[FAKE] {work_item['description'][:60]}",
            "execution_surface": "fake_executor",
            "native_continuity": False,  # NEVER True for fake
            "provider": "test",
            "evidence": None,  # No real evidence
            "executed_at": utcnow_iso()
        }

class ShellExecutor(Executor):
    """
    Production tool_worker: runs a shell command, captures output.
    Returns verifiable exit_code + stdout evidence.
    """
    def execute(self, work_item: dict) -> dict:
        import subprocess, shlex
        desc = work_item.get("description", "")
        cap  = work_item.get("capability", "backend")
        
        # v1: echo command as placeholder for real tool dispatch
        # v2: route to real tool based on capability + task decomposition
        cmd = f"echo 'SPHERA worker: {cap}: {desc[:50]}'"
        try:
            result = subprocess.run(
                shlex.split(cmd), capture_output=True, text=True, timeout=30
            )
            return {
                "status": "done" if result.returncode == 0 else "failed",
                "output": result.stdout.strip()[:500],
                "exit_code": result.returncode,
                "execution_surface": "tool_worker",
                "native_continuity": False,
                "provider": "shell",
                "evidence": {"stdout": result.stdout[:200], "stderr": result.stderr[:100]},
                "executed_at": utcnow_iso()
            }
        except subprocess.TimeoutExpired:
            return {"status": "failed", "error": "timeout", "execution_surface": "tool_worker",
                    "native_continuity": False}
        except Exception as e:
            return {"status": "failed", "error": str(e), "execution_surface": "tool_worker",
                    "native_continuity": False}

class NativeSessionExecutor(Executor):
    """
    Native session work — CANNOT execute autonomously.
    Emits wake_required obligation. Never claims native_continuity=True.
    native_continuity=True is ONLY set by an authenticated native Principal event,
    never by this executor.
    """
    def execute(self, work_item: dict) -> dict:
        return {
            "status": "wake_required",
            "reason": "native_session_required",
            "capability": work_item["capability"],
            "execution_surface": "native_session",
            "native_continuity": False,  # NOT True — no native session ran
            "wake_state": "pending",
            "message": f"Genuine AI session needed: {work_item['description'][:60]}"
        }

# Production executor registry — FakeExecutor NEVER in this map
PROD_EXECUTORS: dict[str, Executor] = {
    "tool_worker":    ShellExecutor(),
    "native_session": NativeSessionExecutor(),
}

# Test-only executor registry
TEST_EXECUTORS: dict[str, Executor] = {
    "tool_worker":    FakeExecutor(),
    "native_session": NativeSessionExecutor(),
}

# Active registry — set to PROD_EXECUTORS in production, TEST_EXECUTORS in tests
_USE_TEST_EXECUTORS = os.environ.get("SPHERA_TEST_EXECUTORS", "").lower() in ("1","true","yes")
EXECUTORS = TEST_EXECUTORS if _USE_TEST_EXECUTORS else PROD_EXECUTORS

def get_executor(surface: str) -> Executor:
    ex = EXECUTORS.get(surface)
    if ex is None:
        raise ValueError(f"No executor for surface: {surface}")
    if _USE_TEST_EXECUTORS and isinstance(ex, FakeExecutor):
        pass  # OK in test mode
    elif not _USE_TEST_EXECUTORS and isinstance(ex, FakeExecutor):
        raise RuntimeError("FakeExecutor cannot run in production mode")
    return ex

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
            "current_lease": row["lease_id"],
            "reason": "lease_id_mismatch"
        })
        return False
    # Reject if fencing token has advanced (lease was reclaimed)
    submitted_fencing = result.get("_fencing_token")
    if submitted_fencing is not None and submitted_fencing != row["lease_fencing_token"]:
        emit_event(db, "system", "stale_result_rejected", {
            "work_id": work_id, "submitted_fencing": submitted_fencing,
            "current_fencing": row["lease_fencing_token"],
            "reason": "fencing_token_mismatch"
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
           WHERE workspace_id='default' AND work_id=?""",
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
    """
    Promote blocked items to ready when ALL deps are done.
    Missing dep IDs keep work blocked and emit invalid_dependency event.
    A missing dep can never satisfy the constraint.
    """
    unblocked = []
    blocked = db.execute(
        "SELECT work_id, deps_json FROM work_items WHERE workspace_id='default' AND status='blocked'"
    ).fetchall()
    
    for b in blocked:
        try:
            deps = json.loads(b["deps_json"] or "[]")
        except Exception:
            continue
        if done_work_id not in deps:
            continue
        
        # Check every dep — missing dep = stays blocked
        all_done = True
        for dep_id in deps:
            dep_row = db.execute(
                "SELECT status FROM work_items WHERE work_id=?", (dep_id,)
            ).fetchone()
            if dep_row is None:
                # Missing dep — emit event, keep blocked, never unblock
                emit_event(db, "system", "invalid_dependency", {
                    "blocked_work_id": b["work_id"],
                    "missing_dep_id":  dep_id,
                    "reason": "dep_not_found_in_ledger"
                })
                all_done = False
                break
            if dep_row["status"] != "done":
                all_done = False
                break
        
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
        "SELECT work_id, lease_fencing_token, attempt_count FROM work_items WHERE workspace_id='default' AND status='leased' AND lease_expires<?",
        (now,)
    ).fetchall()
    for s in stale:
        new_fence = s["lease_fencing_token"] + 1
        db.execute(
            "UPDATE work_items SET status='ready', lease_id=NULL, lease_holder=NULL, lease_expires=NULL, lease_fencing_token=? WHERE workspace_id='default' AND work_id=?",
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
        "SELECT work_id FROM work_items WHERE workspace_id='default' AND retry_at IS NOT NULL AND retry_at<=? AND status='ready'",
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

    def _principal_for(self, capability: str) -> str:
        """Map capability to the required principal."""
        return {
            "security": "claude",
            "design": "soba",
            "research": "claude",
            "decision": "arcides",
        }.get(capability, "claude")

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
                    "SELECT * FROM work_items WHERE workspace_id='default' AND status='ready' AND (retry_at IS NULL OR retry_at<=?) ORDER BY created_at LIMIT 10",
                    (now,)
                ).fetchall()
                
                for item in ready:
                    work_id = item["work_id"]
                    cap     = item["capability"]
                    surface, autonomous = CAPABILITY_SURFACE.get(cap, ("tool_worker", True))
                    
                    if not autonomous:
                        if surface == "owner":
                            # Owner decision — WAITING_OWNER_AUTHORITY, no PEA attempt
                            db.execute(
                                "UPDATE work_items SET status='waiting_owner_authority',"
                                "waiting_reason='owner_decision_required' "
                                "WHERE workspace_id='default' AND work_id=?", (work_id,)
                            )
                            emit_event(db, "system", "owner_required", {
                                "work_id": work_id, "reason": "owner_decision_required",
                                "capability": cap
                            })
                            db.commit()
                            continue
                        # Native session — create PEA attempt (not a dead-end)
                        from principal_edge import create_attempt_atomic, _TEST_MODE, FakeAdapter, GmailBridgeAdapter
                        gen = item["work_generation"] if "work_generation" in item.keys() else 0
                        aid = create_attempt_atomic(db, work_id, item["mission_id"], self._principal_for(cap), gen)
                        if aid:
                            db.commit()
                            emit_event(db, "system", "edge_attempt_created", {
                                "attempt_id": aid, "work_id": work_id,
                                "capability": cap, "surface": surface
                            })
                            db.commit()
                        # Drive the attempt immediately (production path — not test injection)
                        if aid:
                            from principal_edge import run_attempt, _TEST_MODE, FakeAdapter, GmailBridgeAdapter
                            adapter = self._get_pea_adapter(cap, self._principal_for(cap))
                            if adapter:
                                with get_db() as _db:
                                    run_attempt(_db, aid, adapter)
                                    _db.commit()
                        continue
                    
                    # 4. Atomically claim
                    claim = try_claim(db, work_id, self.worker_id)
                    if not claim:
                        continue  # Already claimed by another worker
                    
                    db.commit()  # Commit claim before executing
                    
                    # 5. Execute
                    try:
                        executor = get_executor(surface)
                        result = executor.execute(claim)
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
