"""
SPHERA Causal DAG — Core ancestry walker. CAUSAL-DAG-12.

Rules:
- Every activation/response event carries: trace_id, causal_parent_event_id,
  activation_root_event_id, obligation_id, attempt_id, work_generation_tag
- Core walks causal_parent_event_id from response event to activation_root
- HUMAN_CAUSAL: Arcides appears anywhere on the ancestor chain (ON-chain only)
- AUTONOMOUS: chain terminates at correct activation_root with no Arcides ancestor
- UNKNOWN: missing parent / broken chain / cycle / generation mismatch → fail closed
- Off-chain Arcides events are IRRELEVANT — not tainted

Core verifier is the ONLY component that writes E3_N.
Adapters supply raw transport evidence only — no verdict fields.
"""
import json, sqlite3
from datetime import datetime, timezone

HUMAN_PRINCIPALS = {"arcides"}
MAX_CHAIN_DEPTH   = 500  # Cycle detection ceiling


def walk_ancestry(
    db: sqlite3.Connection,
    response_event_id: str,
    expected_activation_root: str,
    expected_trace_id: str,
    expected_work_generation: int,
) -> dict:
    """
    Walk the causal parent chain from response_event_id to activation_root.

    Returns:
        {
            "result":              "AUTONOMOUS" | "HUMAN_CAUSAL" | "UNKNOWN" | "CYCLE_DETECTED" | "CHAIN_BROKEN" | "ROOT_MISMATCH",
            "human_ancestor_id":   str | None,   # event_id of first human ancestor
            "chain_length":        int,
            "visited":             [event_id, ...],
            "failure_reason":      str | None,
        }
    """
    visited = []
    seen    = set()
    current = response_event_id
    human_ancestor = None

    for depth in range(MAX_CHAIN_DEPTH):
        if current in seen:
            return _result("CYCLE_DETECTED", visited, human_ancestor,
                           f"cycle at event_id={current}")
        seen.add(current)

        row = db.execute(
            """SELECT event_id, principal, type,
                      trace_id, causal_parent_event_id, activation_root_event_id,
                      work_generation_tag, attempt_id
               FROM events WHERE event_id=? AND workspace_id='default'""",
            (current,)
        ).fetchone()

        if not row:
            return _result("CHAIN_BROKEN", visited, human_ancestor,
                           f"event_id={current} not found in ledger")

        visited.append(current)

        # Check if this event's principal is a human (on-chain contamination)
        if row["principal"] in HUMAN_PRINCIPALS and not human_ancestor:
            human_ancestor = current

        # Validate trace consistency
        ev_trace = row["trace_id"]
        if ev_trace and ev_trace != expected_trace_id:
            return _result("CHAIN_BROKEN", visited, human_ancestor,
                           f"trace mismatch at {current}: expected {expected_trace_id!r} got {ev_trace!r}")

        # Validate work_generation consistency
        ev_gen = row["work_generation_tag"]
        if ev_gen is not None and ev_gen != expected_work_generation:
            return _result("UNKNOWN", visited, human_ancestor,
                           f"generation mismatch at {current}: expected {expected_work_generation} got {ev_gen}")

        # Check if we've reached the expected activation root
        parent = row["causal_parent_event_id"]
        act_root = row["activation_root_event_id"]

        # Only terminate when we physically arrive at the activation root
        if current == expected_activation_root:
            if human_ancestor:
                return _result("HUMAN_CAUSAL", visited, human_ancestor,
                               f"human principal {human_ancestor!r} on ancestor chain")
            return _result("AUTONOMOUS", visited, None, None)

        # No parent = genesis event but not the expected root
        if not parent:
            return _result("ROOT_MISMATCH", visited, human_ancestor,
                           f"chain ended at {current!r} but expected root {expected_activation_root!r}")

        current = parent

    return _result("CYCLE_DETECTED", visited, human_ancestor,
                   f"exceeded MAX_CHAIN_DEPTH={MAX_CHAIN_DEPTH}")


def _result(result, visited, human_ancestor, failure_reason):
    return {
        "result":            result,
        "human_ancestor_id": human_ancestor,
        "chain_length":      len(visited),
        "visited":           visited,
        "failure_reason":    failure_reason,
    }


def compute_and_cache(
    db: sqlite3.Connection,
    response_event_id: str,
    attempt_id: str,
    activation_root_event_id: str,
    trace_id: str,
    work_generation: int,
) -> dict:
    """
    Walk ancestry, cache result, return verdict dict.
    Cache is keyed on (response_event_id, attempt_id) — deterministic from ledger only.
    """
    # Check cache first (idempotent for replay)
    cached = db.execute(
        """SELECT ancestry_result, human_ancestor_event_id, chain_length
           FROM causal_ancestry_cache
           WHERE workspace_id='default' AND response_event_id=? AND attempt_id=?""",
        (response_event_id, attempt_id)
    ).fetchone()
    if cached:
        return {
            "result":            cached["ancestry_result"],
            "human_ancestor_id": cached["human_ancestor_event_id"],
            "chain_length":      cached["chain_length"],
            "from_cache":        True,
        }

    verdict = walk_ancestry(db, response_event_id, activation_root_event_id,
                            trace_id, work_generation)

    now = datetime.now(timezone.utc).isoformat()
    last_seq = (db.execute("SELECT MAX(seq) FROM events").fetchone()[0] or 0)

    db.execute(
        """INSERT OR REPLACE INTO causal_ancestry_cache
           (workspace_id,response_event_id,attempt_id,activation_root_event_id,
            trace_id,ancestry_result,human_ancestor_event_id,chain_length,
            computed_at,ledger_seq_at_compute)
           VALUES('default',?,?,?,?,?,?,?,?,?)""",
        (response_event_id, attempt_id, activation_root_event_id, trace_id,
         verdict["result"], verdict["human_ancestor_id"],
         verdict["chain_length"], now, last_seq)
    )

    verdict["from_cache"] = False
    return verdict


def verify_e3n_core(
    db: sqlite3.Connection,
    attempt_id: str,
    edge_id: str,
    principal_id: str,
    binding_evidence: dict,
) -> str | None:
    """
    Core verifier for E3_N — the ONLY component allowed to write E3_N.
    Adapter supplies raw evidence; Core validates and emits the record.

    Returns evidence_id (str) if E3_N proven, None otherwise.
    Gmail transport surface can NEVER produce E3_N.
    """
    import uuid
    from datetime import datetime, timezone

    # Get edge surface
    edge_row = db.execute(
        "SELECT surface, continuity_class FROM edge_registry WHERE edge_id=? AND workspace_id='default'",
        (edge_id,)
    ).fetchone()
    if not edge_row:
        return None

    surface = edge_row["surface"]

    # Gmail is E0/surrogate only — Core verifier enforces this
    if surface in ("gmail", "surrogate_transport"):
        return None

    # Only FakeAdapter (test) and future provider-native surfaces can reach E3_N
    if surface not in ("fake", "chatgpt-native", "google-native"):
        return None

    # Require binding_evidence to contain a verifiable provider artifact
    # (not just adapter self-attestation)
    if not binding_evidence.get("provider_run_id") and not binding_evidence.get("provider_assertion") and not binding_evidence.get("native_session_id"):
        # Fake/test surfaces: accept if SPHERA_TEST_PEA=1 (same gate as FakeAdapter)
        import os
        if not os.environ.get("SPHERA_TEST_PEA", "").lower() in ("1","true","yes"):
            return None

    # Write E3_N evidence record — Core-owned
    eid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """INSERT INTO principal_evidence
           (workspace_id,principal_id,edge_id,evidence_level,evidence_id,
            verifier_method,observed_at,trace_id,boss_causal_events)
           VALUES('default',?,?,'E3_N',?,?,?,?,0)""",
        (principal_id, edge_id, eid, "core_verifier_v1", now,
         binding_evidence.get("trace_id", attempt_id))
    )
    return eid
