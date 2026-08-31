"""
SPHERA Principal Edge Adapter — PEA v2.0
Wired into mission_loop. CAS transitions. 128-bit nonces. Fails closed.
FakeAdapter: SPHERA_TEST_PEA=1 only — fails closed in production.
"""
import json, os, secrets, sqlite3, uuid
from datetime import datetime, timezone, timedelta

DB_PATH      = os.environ.get("SPHERA_DB", "./sphera.db")
_TEST_MODE   = os.environ.get("SPHERA_TEST_PEA", "").lower() in ("1","true","yes")
ATTEMPT_TTL  = 7200  # 2h

TERMINAL_STATES = {
    "OBLIGATION_RESUMED","NO_EDGE","DELIVERY_FAILED","OBSERVATION_TIMEOUT",
    "BINDING_FAILED","BOSS_CAUSALITY_PRESENT","STALE_OR_REPLAYED_EVIDENCE",
    "BLOCKED_NATIVE_WAKE"
}
NONTERMINAL_STATES = {
    "OBLIGATION_CREATED","EDGE_SELECTED","CHALLENGE_EMITTED",
    "EDGE_OBSERVED","NATIVE_BINDING_VERIFIED","RESPONSE_ACCEPTED"
}

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def utcnow(): return datetime.now(timezone.utc).isoformat()

def make_nonce() -> str:
    return secrets.token_hex(16)  # 128 bits

# ── CAS transition — never unconditional ──────────────────────────────────────
def cas_transition(db, attempt_id: str, expected: str, new_state: str,
                   failure_reason: str = None, **fields) -> bool:
    """
    Compare-and-swap: only update if state == expected.
    Returns True on success, False if state has already moved.
    """
    now = utcnow()
    updates = {"state": new_state, "updated_at": now}
    if failure_reason:
        updates["failure_reason"] = failure_reason
    updates.update(fields)
    cols = ", ".join(f"{k}=?" for k in updates)
    vals = list(updates.values()) + [attempt_id, expected]
    cur  = db.execute(
        f"UPDATE principal_edge_attempts SET {cols} WHERE attempt_id=? AND state=?", vals
    )
    ok = cur.rowcount == 1
    if ok:
        print(f"[pea] {attempt_id[:8]} {expected}→{new_state}" +
              (f" ({failure_reason})" if failure_reason else ""))
    return ok

# ── Create attempt — atomic with WAITING_PRINCIPAL_EDGE status ────────────────
def create_attempt_atomic(db, work_id: str, mission_id: str,
                          principal_id: str, work_generation: int) -> str | None:
    """
    Atomically:
    1. Transition work READY → WAITING_PRINCIPAL_EDGE
    2. Insert exactly one attempt for this generation (UNIQUE constraint)
    Returns attempt_id or None if already exists / work not READY.
    """
    now  = utcnow()
    exp  = (datetime.now(timezone.utc) + timedelta(seconds=ATTEMPT_TTL)).isoformat()
    aid  = str(uuid.uuid4())
    # CAS work item: READY → WAITING_PRINCIPAL_EDGE
    cur = db.execute(
        "UPDATE work_items SET status='waiting_principal_edge', work_generation=?, "
        "waiting_reason='awaiting_edge_attempt', updated_at=? "
        "WHERE workspace_id='default' AND work_id=? AND status='ready'",
        (work_generation, now, work_id)
    )
    if cur.rowcount != 1:
        return None  # Not READY, or already claimed
    try:
        db.execute(
            "INSERT INTO principal_edge_attempts"
            "(attempt_id,workspace_id,work_id,mission_id,principal_id,work_generation,"
            " state,created_at,updated_at,expires_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (aid,'default',work_id,mission_id,principal_id,work_generation,
             'OBLIGATION_CREATED',now,now,exp)
        )
    except sqlite3.IntegrityError:
        # UNIQUE(workspace_id, work_id, work_generation) — already exists
        db.execute(
            "UPDATE work_items SET status='ready', waiting_reason=NULL "
            "WHERE workspace_id='default' AND work_id=?", (work_id,)
        )
        return None
    return aid

# ── Adapters ──────────────────────────────────────────────────────────────────
class PrincipalEdgeAdapter:
    edge_id          = None
    principal_id     = None
    surface          = None
    continuity_class = "UNKNOWN"

    def select_edge(self, db, principal_id: str, work_generation: int) -> str | None:
        row = db.execute(
            "SELECT edge_id FROM edge_registry "
            "WHERE workspace_id='default' AND principal_id=? AND status='active' "
            "AND (revoked_at IS NULL) LIMIT 1",
            (principal_id,)
        ).fetchone()
        return row["edge_id"] if row else None

    def emit_challenge(self, db, attempt_id: str, nonce: str, edge_id: str) -> bool:
        raise NotImplementedError

    def observe_response(self, db, attempt_id: str, nonce: str, edge_id: str) -> dict | None:
        raise NotImplementedError

    def verify_native_binding(self, db, attempt_id: str, evidence: dict,
                              nonce: str, edge_id: str, work_generation: int) -> str | None:
        """Return E3_N evidence_id or None. Must write principal_evidence record."""
        raise NotImplementedError


class FakeAdapter(PrincipalEdgeAdapter):
    """
    TEST ONLY. Fails closed outside SPHERA_TEST_PEA=1.
    """
    edge_id          = "fake-edge-01"
    principal_id     = "test-principal"
    surface          = "fake"
    continuity_class = "FAKE_TEST_ONLY"

    def __init__(self):
        if not _TEST_MODE:
            raise RuntimeError(
                "FakeAdapter cannot be instantiated outside test mode "
                "(set SPHERA_TEST_PEA=1). Production must never use FakeAdapter."
            )

    def select_edge(self, db, principal_id, work_generation):
        return self.edge_id

    def emit_challenge(self, db, attempt_id, nonce, edge_id):
        return True  # Simulated delivery

    def observe_response(self, db, attempt_id, nonce, edge_id):
        return {"nonce_echo": nonce, "response_event_id": str(uuid.uuid4()),
                "boss_causal_events": 0, "source": "fake_test_only",
                "task_answer": {"fake_result": "test_completed", "adapter": "FakeAdapter"}}

    def verify_native_binding(self, db, attempt_id, evidence, nonce, edge_id, work_generation):
        # Write E3_N evidence record for test
        eid = str(uuid.uuid4())
        db.execute(
            "INSERT INTO principal_evidence(workspace_id,principal_id,edge_id,"
            "evidence_level,evidence_id,verifier_method,observed_at,trace_id,boss_causal_events)"
            " VALUES('default',?,?,?,?,?,?,?,0)",
            (self.principal_id, edge_id, 'E3_N', eid, 'fake_verifier_test_only', utcnow(), attempt_id)
        )
        return eid


class GmailBridgeAdapter(PrincipalEdgeAdapter):
    surface          = "gmail"
    continuity_class = "surrogate_transport"

    def emit_challenge(self, db, attempt_id, nonce, edge_id):
        # TODO: real smtp send here
        # Until wired: record E0 and return True only if smtp succeeds
        # For now: DELIVERY_FAILED to be honest — not yet implemented
        return False  # Honest: delivery not yet wired

    def observe_response(self, db, attempt_id, nonce, edge_id):
        # Only admissible for transport diagnostics, NOT E3_N
        row = db.execute(
            "SELECT seq FROM events WHERE principal=? "
            "AND json_extract(payload_json,'$.nonce')=? ORDER BY seq DESC LIMIT 1",
            (self.principal_id, nonce)
        ).fetchone()
        if not row:
            return None
        return {
            "nonce_echo": nonce, "event_seq": row["seq"],
            "boss_causal_events": -1,  # UNKNOWN — fails closed below
            "source": "gmail_ledger"
        }

    def verify_native_binding(self, db, attempt_id, evidence, nonce, edge_id, work_generation):
        # Gmail transport cannot prove E3_N — fails closed
        return None


# ── State machine runner ──────────────────────────────────────────────────────
def run_attempt(db, attempt_id: str, adapter: PrincipalEdgeAdapter):
    row = db.execute(
        "SELECT * FROM principal_edge_attempts WHERE attempt_id=?", (attempt_id,)
    ).fetchone()
    if not row or row["state"] in TERMINAL_STATES:
        return

    state          = row["state"]
    principal      = row["principal_id"]
    work_id        = row["work_id"]
    work_gen       = row["work_generation"]

    # Check expiry
    if row["expires_at"] and utcnow() > row["expires_at"]:
        cas_transition(db, attempt_id, state, "OBSERVATION_TIMEOUT", "attempt expired")
        db.execute("UPDATE work_items SET status='blocked',waiting_reason='OBSERVATION_TIMEOUT' "
                   "WHERE workspace_id='default' AND work_id=?", (work_id,))
        return

    # OBLIGATION_CREATED → EDGE_SELECTED
    if state == "OBLIGATION_CREATED":
        edge_id = adapter.select_edge(db, principal, work_gen)
        if not edge_id:
            cas_transition(db, attempt_id, state, "NO_EDGE", "no active edge for principal")
            db.execute("UPDATE work_items SET status='blocked',waiting_reason='NO_EDGE' "
                       "WHERE workspace_id='default' AND work_id=?", (work_id,))
            return
        # Verify edge belongs to correct principal
        edge_row = db.execute(
            "SELECT principal_id, status, revoked_at FROM edge_registry "
            "WHERE edge_id=? AND workspace_id='default'", (edge_id,)
        ).fetchone()
        if not edge_row or edge_row["principal_id"] != principal or edge_row["status"] != "active" or edge_row["revoked_at"]:
            cas_transition(db, attempt_id, state, "BINDING_FAILED",
                           f"edge {edge_id!r} invalid/revoked/wrong-principal")
            db.execute("UPDATE work_items SET status='blocked',waiting_reason='BINDING_FAILED' "
                       "WHERE workspace_id='default' AND work_id=?", (work_id,))
            return
        cas_transition(db, attempt_id, state, "EDGE_SELECTED", edge_id=edge_id)
        state = "EDGE_SELECTED"
        row = db.execute("SELECT * FROM principal_edge_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()

    # EDGE_SELECTED → CHALLENGE_EMITTED
    if state == "EDGE_SELECTED":
        nonce     = make_nonce()  # 128-bit
        edge_id   = row["edge_id"]
        delivered = adapter.emit_challenge(db, attempt_id, nonce, edge_id)
        if not delivered:
            cas_transition(db, attempt_id, state, "DELIVERY_FAILED",
                           "adapter.emit_challenge returned False")
            db.execute("UPDATE work_items SET status='blocked',waiting_reason='DELIVERY_FAILED' "
                       "WHERE workspace_id='default' AND work_id=?", (work_id,))
            return
        cas_transition(db, attempt_id, state, "CHALLENGE_EMITTED",
                       challenge_nonce=nonce, challenge_emitted_at=utcnow())
        state = "CHALLENGE_EMITTED"
        row = db.execute("SELECT * FROM principal_edge_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()

    # CHALLENGE_EMITTED → EDGE_OBSERVED
    if state == "CHALLENGE_EMITTED":
        nonce   = row["challenge_nonce"]
        edge_id = row["edge_id"]
        evidence = adapter.observe_response(db, attempt_id, nonce, edge_id)
        if not evidence:
            return  # Not yet — try next cycle

        # EXACT nonce comparison
        if evidence.get("nonce_echo") != nonce:
            cas_transition(db, attempt_id, state, "STALE_OR_REPLAYED_EVIDENCE",
                           "nonce_echo mismatch")
            db.execute("UPDATE work_items SET status='blocked',waiting_reason='STALE_OR_REPLAYED_EVIDENCE' "
                       "WHERE workspace_id='default' AND work_id=?", (work_id,))
            return

        # Boss causality: UNKNOWN (-1) fails closed
        boss_events = evidence.get("boss_causal_events", -1)
        if boss_events != 0:
            reason = f"boss_causal_events={boss_events}: " + \
                     ("not autonomous" if boss_events > 0 else "unknown causality fails closed")
            cas_transition(db, attempt_id, state, "BOSS_CAUSALITY_PRESENT", reason)
            db.execute("UPDATE work_items SET status='blocked',waiting_reason='BOSS_CAUSALITY_PRESENT' "
                       "WHERE workspace_id='default' AND work_id=?", (work_id,))
            return

        # Replay check
        obs_event = str(evidence.get("event_seq", ""))
        if obs_event:
            existing = db.execute(
                "SELECT 1 FROM principal_edge_attempts "
                "WHERE observation_event_id=? AND attempt_id!=?",
                (obs_event, attempt_id)
            ).fetchone()
            if existing:
                cas_transition(db, attempt_id, state, "STALE_OR_REPLAYED_EVIDENCE",
                               f"observation_event_id={obs_event} already used")
                db.execute("UPDATE work_items SET status='blocked',waiting_reason='STALE_OR_REPLAYED_EVIDENCE' "
                           "WHERE workspace_id='default' AND work_id=?", (work_id,))
                return

        # Preserve FULL observed evidence — do not reconstruct later
        db.execute(
            "UPDATE principal_edge_attempts SET failure_reason=? WHERE attempt_id=?",
            (json.dumps({"_observed_evidence": evidence}), attempt_id)
        )
        cas_transition(db, attempt_id, state, "EDGE_OBSERVED",
                       observation_event_id=obs_event or None)
        state = "EDGE_OBSERVED"
        row = db.execute("SELECT * FROM principal_edge_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()

    # EDGE_OBSERVED → NATIVE_BINDING_VERIFIED
    if state == "EDGE_OBSERVED":
        nonce    = row["challenge_nonce"]
        edge_id  = row["edge_id"]
        # Use preserved observed evidence — NEVER reconstruct from nonce alone
        try:
            preserved = json.loads(row["failure_reason"] or "{}")
            evidence = preserved.get("_observed_evidence", {"nonce_echo": nonce})
        except Exception:
            evidence = {"nonce_echo": nonce}
        # Revalidate edge at verification time
        edge_row = db.execute(
            "SELECT principal_id, status, revoked_at, binding_version FROM edge_registry "
            "WHERE edge_id=? AND workspace_id='default'", (edge_id,)
        ).fetchone()
        if not edge_row or edge_row["status"] != "active" or edge_row["revoked_at"]:
            cas_transition(db, attempt_id, state, "BINDING_FAILED",
                           "edge revoked/invalid at verification time")
            db.execute("UPDATE work_items SET status='blocked',waiting_reason='BINDING_FAILED' "
                       "WHERE workspace_id='default' AND work_id=?", (work_id,))
            return

        e3n_eid = adapter.verify_native_binding(
            db, attempt_id, evidence, nonce, edge_id, work_gen
        )
        if not e3n_eid:
            cas_transition(db, attempt_id, state, "BLOCKED_NATIVE_WAKE",
                           "E3_N unproven: native continuity not verified on current surface")
            db.execute("UPDATE work_items SET status='blocked',waiting_reason='BLOCKED_NATIVE_WAKE' "
                       "WHERE workspace_id='default' AND work_id=?", (work_id,))
            return

        cas_transition(db, attempt_id, state, "NATIVE_BINDING_VERIFIED", e3n_evidence_id=e3n_eid)
        state = "NATIVE_BINDING_VERIFIED"

    # NATIVE_BINDING_VERIFIED → RESPONSE_VERIFIED → RESPONSE_ACCEPTED → OBLIGATION_RESUMED
    # E3_N alone cannot release obligation — actual task answer payload required (RESPONSE-BINDING-03)
    if state == "NATIVE_BINDING_VERIFIED":
        attempt_row = db.execute("SELECT failure_reason, challenge_nonce, edge_id FROM principal_edge_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        try:
            preserved = json.loads(attempt_row["failure_reason"] or "{}")
            obs_evidence = preserved.get("_observed_evidence", {})
        except Exception:
            obs_evidence = {}

        task_payload = obs_evidence.get("task_answer") or obs_evidence.get("payload")
        if not task_payload:
            # Valid E3_N but no task answer — native wake cannot satisfy obligation alone
            cas_transition(db, attempt_id, state, "RESPONSE_MISSING",
                           "E3_N proven but no task_answer: native wake alone cannot satisfy obligation")
            db.execute("UPDATE work_items SET status='blocked',waiting_reason='RESPONSE_MISSING' WHERE workspace_id='default' AND work_id=? AND status='waiting_principal_edge'", (work_id,))
            return

        # Build response artifact using committed schema
        import hashlib as _hl
        payload_hash = _hl.sha256(json.dumps(task_payload, sort_keys=True).encode()).hexdigest()
        artifact_id  = str(uuid.uuid4())
        nonce_val    = attempt_row["challenge_nonce"]
        # Look up challenge_artifact_id for this attempt
        ca = db.execute("SELECT artifact_id FROM challenge_artifacts WHERE workspace_id='default' AND attempt_id=?", (attempt_id,)).fetchone()
        ca_id = ca["artifact_id"] if ca else "UNKNOWN"
        # obligation_hash = hash of (work_id, work_gen, principal, edge)
        ob_hash = _hl.sha256(f"{work_id}:{work_gen}:{principal}:{row['edge_id']}".encode()).hexdigest()[:16]
        resp_event = obs_evidence.get("response_event_id") or obs_evidence.get("event_seq", "")

        # Replay check
        if resp_event:
            dup = db.execute("SELECT 1 FROM response_artifacts WHERE workspace_id='default' AND observation_event_id=?", (str(resp_event),)).fetchone()
            if dup:
                cas_transition(db, attempt_id, state, "BINDING_FAILED", f"response_event_id {resp_event} already used")
                return

        try:
            db.execute(
                """INSERT INTO response_artifacts
                   (artifact_id,workspace_id,attempt_id,challenge_artifact_id,obligation_hash,
                    nonce_echo,observed_evidence,payload_hash,observation_event_id,
                    native_binding_proven,response_verified,no_boss_ancestry,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,1,1,1,?)""",
                (artifact_id,"default",attempt_id,ca_id,ob_hash,
                 nonce_val,json.dumps(obs_evidence),payload_hash,
                 str(resp_event) if resp_event else None,utcnow())
            )
        except Exception as insert_err:
            cas_transition(db, attempt_id, state, "BINDING_FAILED", f"artifact insert failed: {insert_err}")
            return

        cas_transition(db, attempt_id, state, "RESPONSE_VERIFIED")
        cas_transition(db, attempt_id, "RESPONSE_VERIFIED", "RESPONSE_ACCEPTED")
        # Resume work — CAS against WAITING_PRINCIPAL_EDGE for same generation
        cur = db.execute(
            "UPDATE work_items SET status='ready', waiting_reason=NULL "
            "WHERE workspace_id='default' AND work_id=? "
            "AND status='waiting_principal_edge' AND work_generation=?",
            (work_id, work_gen)
        )
        if cur.rowcount != 1:
            cas_transition(db, attempt_id, "RESPONSE_ACCEPTED", "BINDING_FAILED",
                           "work item generation mismatch at resume")
            return
        cas_transition(db, attempt_id, "RESPONSE_ACCEPTED", "OBLIGATION_RESUMED")
        db.execute("UPDATE principal_edge_attempts SET failure_reason=NULL WHERE attempt_id=?", (attempt_id,))
        print(f"[pea] obligation RESUMED work={work_id[:8]} gen={work_gen} artifact={artifact_id[:8]}")


# ── Reconciler — idempotent, runs on server startup ──────────────────────────
def reconcile_nonterminal_attempts():
    """
    Recover non-terminal attempts after restart.
    Idempotent: safe to call multiple times.
    Does NOT advance state — only detects orphans and expirations.
    """
    with get_db() as db:
        # Find non-terminal attempts
        rows = db.execute(
            "SELECT * FROM principal_edge_attempts WHERE state NOT IN "
            "('OBLIGATION_RESUMED','NO_EDGE','DELIVERY_FAILED','OBSERVATION_TIMEOUT',"
            " 'BINDING_FAILED','BOSS_CAUSALITY_PRESENT','STALE_OR_REPLAYED_EVIDENCE',"
            " 'BLOCKED_NATIVE_WAKE') AND workspace_id='default'"
        ).fetchall()
        now = utcnow()
        recovered = 0
        for r in rows:
            if r["expires_at"] and now > r["expires_at"]:
                # Expired — terminal
                cas_transition(db, r["attempt_id"], r["state"], "OBSERVATION_TIMEOUT",
                               "expired during reconciliation")
                db.execute(
                    "UPDATE work_items SET status='blocked',waiting_reason='OBSERVATION_TIMEOUT' "
                    "WHERE workspace_id='default' AND work_id=?", (r["work_id"],)
                )
                recovered += 1
            else:
                # Still valid — verify work is in waiting_principal_edge state
                work = db.execute(
                    "SELECT status, work_generation FROM work_items WHERE workspace_id='default' AND work_id=?",
                    (r["work_id"],)
                ).fetchone()
                if work and work["status"] != "waiting_principal_edge":
                    # Orphan attempt — work moved without attempt being resolved
                    cas_transition(db, r["attempt_id"], r["state"], "BINDING_FAILED",
                                   f"work status={work['status']} at reconciliation, expected waiting_principal_edge")
                    recovered += 1
        if recovered:
            db.commit()
            print(f"[pea] reconciler: recovered {recovered} attempts")
        return recovered
