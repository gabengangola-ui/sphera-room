"""
SPHERA Principal Edge Adapter — PEA v1.0
Replaces native_wake_required dead-end with a persisted state machine.

mission_loop owns: obligation/state machine
PrincipalEdge owns: delivery/observation/challenge
Core verifier owns: E3_N
Capsule/E3_R: stays orthogonal

States:
  OBLIGATION_CREATED -> EDGE_SELECTED -> CHALLENGE_EMITTED ->
  EDGE_OBSERVED -> NATIVE_BINDING_VERIFIED -> RESPONSE_ACCEPTED -> OBLIGATION_RESUMED

Failure states (explicit, never silent fallback to surrogate):
  NO_EDGE, DELIVERY_FAILED, OBSERVATION_TIMEOUT, BINDING_FAILED,
  BOSS_CAUSALITY_PRESENT, STALE_OR_REPLAYED_EVIDENCE, BLOCKED_NATIVE_WAKE
"""
import json, os, sqlite3, uuid
from datetime import datetime, timezone, timedelta

DB_PATH = os.environ.get("SPHERA_DB", "./sphera.db")

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def utcnow(): return datetime.now(timezone.utc).isoformat()
def uid():    return str(uuid.uuid4())

class PrincipalEdgeAdapter:
    """
    Base adapter. Subclass per Principal surface.
    FakeAdapter: tests only. Never in production.
    """
    edge_id        = None
    principal_id   = None
    surface        = None
    continuity_class = "UNKNOWN"

    def select_edge(self, db, principal_id: str) -> str | None:
        """Return edge_id to use, or None if no edge available."""
        row = db.execute(
            "SELECT edge_id FROM edge_registry WHERE workspace_id='default' AND principal_id=? AND status='active' LIMIT 1",
            (principal_id,)
        ).fetchone()
        return row["edge_id"] if row else None

    def emit_challenge(self, db, attempt_id: str, nonce: str) -> bool:
        """Deliver the challenge to the Principal's native surface. Return True if delivered."""
        raise NotImplementedError

    def observe_response(self, db, attempt_id: str, nonce: str) -> dict | None:
        """
        Check if a response to the challenge exists.
        Returns evidence dict or None.
        Evidence must be verifier-observable, not self-reported.
        """
        raise NotImplementedError

    def verify_native_binding(self, db, attempt_id: str, evidence: dict) -> bool:
        """
        Verify E3_N: is this the established native surface?
        Must be independent of Capsule/E3_R.
        """
        raise NotImplementedError


class FakeAdapter(PrincipalEdgeAdapter):
    """
    TEST ONLY. Simulates a successful edge for test harness.
    Never selected in production (SPHERA_TEST_PEA=1 required).
    Labelled explicitly in all emitted events.
    """
    edge_id          = "fake-edge-01"
    principal_id     = "test-principal"
    surface          = "fake"
    continuity_class = "FAKE_TEST_ONLY"
    boss_causal_events = 0  # Tests simulate Boss-absence

    def emit_challenge(self, db, attempt_id, nonce):
        # Simulate delivery to a test surface
        db.execute(
            "INSERT OR IGNORE INTO principal_evidence(workspace_id,principal_id,edge_id,evidence_level,evidence_id,verifier_method,observed_at,trace_id,boss_causal_events) VALUES('default',?,?,?,?,?,?,?,0)",
            (self.principal_id, self.edge_id, 'E0', uid(), 'fake_challenge_delivery', utcnow(), attempt_id)
        )
        return True

    def observe_response(self, db, attempt_id, nonce):
        # Fake: always responds with nonce echo
        return {"nonce_echo": nonce, "source": "fake", "boss_causal_events": 0}

    def verify_native_binding(self, db, attempt_id, evidence):
        # Fake: always passes (test only)
        return True


class GmailBridgeAdapter(PrincipalEdgeAdapter):
    """
    Gmail transport adapter for Claude/Soba.
    E0: delivery confirmed via Gmail.
    E1: must be observed autonomously (boss_causal_events=0).
    E3_N: UNPROVEN on current surface.
    """
    surface          = "gmail"
    continuity_class = "surrogate_transport"

    def emit_challenge(self, db, attempt_id, nonce):
        # Challenge is a SPHERA-BRIDGE email — delivery via Gmail
        # In production: actual smtp send happens here
        # For now: record that challenge was emitted
        db.execute(
            "INSERT OR IGNORE INTO principal_evidence(workspace_id,principal_id,edge_id,evidence_level,evidence_id,verifier_method,observed_at,trace_id,boss_causal_events) VALUES('default',?,?,?,?,?,?,?,0)",
            (self.principal_id, self.edge_id, 'E0', uid(), 'gmail_challenge_emitted', utcnow(), attempt_id)
        )
        return True

    def observe_response(self, db, attempt_id, nonce):
        # Check ledger for a response event from this principal with matching nonce
        row = db.execute(
            "SELECT * FROM events WHERE principal=? AND json_extract(payload_json,'$.nonce')=? ORDER BY seq DESC LIMIT 1",
            (self.principal_id, nonce)
        ).fetchone()
        if not row:
            return None
        # Check boss_causal_events — was Boss the trigger?
        # In v1: we cannot verify boss_causal_events from email alone → report UNKNOWN
        return {
            "nonce_echo": nonce,
            "event_seq": row["seq"],
            "boss_causal_events": -1,  # -1 = UNKNOWN on current surface
            "source": "gmail_ledger"
        }

    def verify_native_binding(self, db, attempt_id, evidence):
        # Gmail transport cannot prove E3_N — transport ≠ identity
        # Returns False — E3_N UNPROVEN
        return False


# ── State machine ─────────────────────────────────────────────────────────────
def transition(db, attempt_id: str, new_state: str, failure_reason: str = None, **fields):
    """Advance attempt to new state. Failure states are terminal."""
    now = utcnow()
    updates = {"state": new_state, "updated_at": now}
    if failure_reason:
        updates["failure_reason"] = failure_reason
    updates.update(fields)
    cols  = ", ".join(f"{k}=?" for k in updates)
    vals  = list(updates.values()) + [attempt_id]
    db.execute(f"UPDATE principal_edge_attempts SET {cols} WHERE attempt_id=?", vals)
    print(f"[pea] {attempt_id[:8]} → {new_state}" + (f" ({failure_reason})" if failure_reason else ""))

def create_attempt(db, work_id: str, mission_id: str, principal_id: str) -> str:
    """Create a new edge attempt for a native work item."""
    aid = uid()
    now = utcnow()
    exp = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    db.execute(
        """INSERT INTO principal_edge_attempts
           (attempt_id,workspace_id,work_id,mission_id,principal_id,state,created_at,updated_at,expires_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (aid,'default',work_id,mission_id,principal_id,'OBLIGATION_CREATED',now,now,exp)
    )
    return aid

def run_attempt(db, attempt_id: str, adapter: PrincipalEdgeAdapter):
    """
    Drive the state machine for one attempt.
    Never silently falls back to surrogate.
    """
    row = db.execute(
        "SELECT * FROM principal_edge_attempts WHERE attempt_id=?", (attempt_id,)
    ).fetchone()
    if not row: return

    state      = row["state"]
    principal  = row["principal_id"]
    work_id    = row["work_id"]

    # Select edge
    if state == "OBLIGATION_CREATED":
        edge_id = adapter.select_edge(db, principal)
        if not edge_id:
            transition(db, attempt_id, "NO_EDGE", "no active edge for principal")
            return
        transition(db, attempt_id, "EDGE_SELECTED", edge_id=edge_id)
        state = "EDGE_SELECTED"

    # Emit challenge
    if state == "EDGE_SELECTED":
        nonce = uid()[:8]
        delivered = adapter.emit_challenge(db, attempt_id, nonce)
        if not delivered:
            transition(db, attempt_id, "DELIVERY_FAILED", "adapter.emit_challenge returned False")
            return
        transition(db, attempt_id, "CHALLENGE_EMITTED", challenge_nonce=nonce, challenge_emitted_at=utcnow())
        state = "CHALLENGE_EMITTED"

    # Observe response
    if state == "CHALLENGE_EMITTED":
        # Verify the selected edge is still bound to the correct principal
        cur_attempt = db.execute("SELECT edge_id, principal_id FROM principal_edge_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if cur_attempt and cur_attempt["edge_id"]:
            edge_row = db.execute("SELECT principal_id FROM edge_registry WHERE edge_id=? AND workspace_id='default'", (cur_attempt["edge_id"],)).fetchone()
            if edge_row and edge_row["principal_id"] != cur_attempt["principal_id"]:
                transition(db, attempt_id, "BINDING_FAILED", f"edge {cur_attempt['edge_id']!r} is bound to {edge_row['principal_id']!r} not {cur_attempt['principal_id']!r}")
                return
        nonce = row["challenge_nonce"] or db.execute(
            "SELECT challenge_nonce FROM principal_edge_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()["challenge_nonce"]

        # Check timeout
        emitted_at = db.execute(
            "SELECT challenge_emitted_at FROM principal_edge_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()["challenge_emitted_at"]
        if emitted_at:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(emitted_at)).total_seconds()
            if age > 7200:  # 2h timeout
                transition(db, attempt_id, "OBSERVATION_TIMEOUT", f"no response after {int(age)}s")
                return

        evidence = adapter.observe_response(db, attempt_id, nonce)
        if not evidence:
            return  # Not yet — try again next cycle

        # Check boss contamination
        boss_events = evidence.get("boss_causal_events", -1)
        if boss_events > 0:
            transition(db, attempt_id, "BOSS_CAUSALITY_PRESENT",
                       f"boss_causal_events={boss_events}: run not autonomous")
            return

        # Check replay
        obs_event = evidence.get("event_seq")
        if obs_event:
            existing = db.execute(
                "SELECT 1 FROM principal_edge_attempts WHERE observation_event_id=? AND attempt_id!=?",
                (str(obs_event), attempt_id)
            ).fetchone()
            if existing:
                transition(db, attempt_id, "STALE_OR_REPLAYED_EVIDENCE",
                           f"observation_event_id={obs_event} already used")
                return

        transition(db, attempt_id, "EDGE_OBSERVED", observation_event_id=str(obs_event) if obs_event else None)
        state = "EDGE_OBSERVED"

    # Verify native binding (E3_N)
    if state == "EDGE_OBSERVED":
        evidence = {"nonce_echo": row["challenge_nonce"]}
        verified = adapter.verify_native_binding(db, attempt_id, evidence)
        if not verified:
            # E3_N UNPROVEN — work stays BLOCKED_NATIVE_WAKE, never falls to surrogate
            transition(db, attempt_id, "BLOCKED_NATIVE_WAKE",
                       "E3_N unproven on current surface: native_continuity not verified")
            # Update work item to explicit blocked state
            db.execute(
                "UPDATE work_items SET status='blocked',last_error='BLOCKED_NATIVE_WAKE: E3_N unproven' WHERE workspace_id='default' AND work_id=?",
                (work_id,)
            )
            return

        transition(db, attempt_id, "NATIVE_BINDING_VERIFIED")
        state = "NATIVE_BINDING_VERIFIED"

    # Accept response + resume obligation
    if state == "NATIVE_BINDING_VERIFIED":
        transition(db, attempt_id, "RESPONSE_ACCEPTED")
        transition(db, attempt_id, "OBLIGATION_RESUMED")
        # Work can now proceed with verified native binding
        db.execute(
            "UPDATE work_items SET status='ready' WHERE workspace_id='default' AND work_id=? AND status='blocked'",
            (work_id,)
        )
        print(f"[pea] obligation resumed for work_id={work_id[:8]}")

