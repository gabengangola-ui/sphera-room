"""SPHERA DB v3.2 — Zero workspace_id in SCHEMA. All new columns via ALTER TABLE."""
import hashlib, json, os, sqlite3, uuid
from datetime import datetime, timezone

DB_PATH = os.environ.get("SPHERA_DB", "./sphera.db")
SCHEMA_VERSION = 3

# Base schema — NO workspace_id anywhere. Added via ALTER TABLE in init().
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workspaces (workspace_id TEXT PRIMARY KEY, name TEXT NOT NULL, owner TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, ts TEXT NOT NULL, principal TEXT NOT NULL, type TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', provenance_json TEXT NOT NULL DEFAULT '{}', prev_hash TEXT NOT NULL DEFAULT '', this_hash TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS outbox (outbox_id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, ts TEXT NOT NULL, principal TEXT NOT NULL, type TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', provenance_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS missions (mission_id TEXT NOT NULL, objective TEXT NOT NULL, owner TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', policy_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, accepted_at TEXT, acceptance_note TEXT, version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS work_items (work_id TEXT NOT NULL, mission_id TEXT NOT NULL, description TEXT NOT NULL, capability TEXT NOT NULL, deps_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'ready', lease_id TEXT, lease_holder TEXT, lease_expires TEXT, lease_fencing_token INTEGER NOT NULL DEFAULT 0, result_seq INTEGER, result_summary TEXT, created_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, attempt_count INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3, retry_at TEXT, last_error TEXT);
CREATE TABLE IF NOT EXISTS decisions (request_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', requesting_principal TEXT NOT NULL, scope TEXT NOT NULL, target TEXT NOT NULL, params_json TEXT NOT NULL DEFAULT '{}', bound_digest TEXT NOT NULL DEFAULT '', deadline TEXT, version INTEGER NOT NULL DEFAULT 1, claimed_at TEXT, claim_expires TEXT, claim_fencing_token INTEGER, approved_at TEXT, consumed_at TEXT);
CREATE TABLE IF NOT EXISTS pending_reply (id TEXT NOT NULL, principal TEXT NOT NULL, source_seq INTEGER NOT NULL, source_principal TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', resolved_at TEXT, resolved_by_seq INTEGER);
CREATE TABLE IF NOT EXISTS wake_attempt (
    workspace_id          TEXT NOT NULL DEFAULT 'default',
    attempt_id            TEXT NOT NULL,
    obligation_id         TEXT NOT NULL,
    generation            INTEGER NOT NULL DEFAULT 1,
    target_principal      TEXT NOT NULL,
    target_surface_binding TEXT NOT NULL,
    edge_id               TEXT NOT NULL,
    nonce                 TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'pending',
    claimed_at            TEXT,
    delivered_at          TEXT,
    expires_at            TEXT NOT NULL,
    native_bound_at       TEXT,
    accepted_at           TEXT,
    failed_at             TEXT,
    failure_reason        TEXT,
    PRIMARY KEY(workspace_id, attempt_id),
    UNIQUE(workspace_id, obligation_id, generation)
);

CREATE TABLE IF NOT EXISTS wake_attempts (
    attempt_id           TEXT NOT NULL,
    workspace_id         TEXT NOT NULL DEFAULT 'default',
    obligation_id        TEXT NOT NULL,
    generation           INTEGER NOT NULL DEFAULT 1,
    target_principal_id  TEXT NOT NULL,
    target_surface       TEXT NOT NULL,
    edge_id              TEXT NOT NULL,
    nonce                TEXT NOT NULL,
    issued_at            TEXT NOT NULL,
    expires_at           TEXT NOT NULL,
    delivery_state       TEXT NOT NULL DEFAULT 'queued',
    delivery_evidence    TEXT,
    claimed_at           TEXT,
    delivered_at         TEXT,
    PRIMARY KEY(workspace_id, attempt_id),
    UNIQUE(workspace_id, obligation_id, generation)
);

CREATE TABLE IF NOT EXISTS principal_attestations (
    attestation_id           TEXT NOT NULL,
    workspace_id             TEXT NOT NULL DEFAULT 'default',
    principal_id             TEXT NOT NULL,
    obligation_id            TEXT NOT NULL,
    generation               INTEGER NOT NULL,
    wake_attempt_id          TEXT NOT NULL,
    attestation_level        TEXT NOT NULL DEFAULT 'L0_CLAIMED',
    evidence_type            TEXT NOT NULL DEFAULT 'none',
    connector_edge_id        TEXT,
    provider_account_binding TEXT,
    native_surface_binding   TEXT,
    provider_assertion_ref   TEXT,
    nonce_echo               TEXT,
    parent_event_id          TEXT,
    accepted_at              TEXT,
    quarantine_reason        TEXT,
    PRIMARY KEY(workspace_id, attestation_id)
);

CREATE INDEX IF NOT EXISTS idx_wake_obligation ON wake_attempts(workspace_id, obligation_id, generation);
CREATE INDEX IF NOT EXISTS idx_attest_obligation ON principal_attestations(workspace_id, obligation_id);

CREATE TABLE IF NOT EXISTS work_obligations (
    workspace_id    TEXT NOT NULL DEFAULT 'default',
    work_id         TEXT NOT NULL,
    assignee        TEXT NOT NULL,
    next_action     TEXT NOT NULL DEFAULT 'execute',
    due_at          TEXT,
    wake_state      TEXT NOT NULL DEFAULT 'not_required',
    last_progress_at TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    blocker_kind    TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY(workspace_id, work_id)
);

CREATE TABLE IF NOT EXISTS orch_mission_state (mission_id TEXT NOT NULL, last_progress_at TEXT, stalled_since TEXT, stall_count INTEGER NOT NULL DEFAULT 0, next_principal TEXT, status TEXT NOT NULL DEFAULT 'active');
CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, filename TEXT NOT NULL, applied_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS edge_registry (
    edge_id          TEXT NOT NULL,
    workspace_id     TEXT NOT NULL DEFAULT 'default',
    principal_id     TEXT NOT NULL,
    surface          TEXT NOT NULL,
    provider         TEXT NOT NULL,
    capabilities     TEXT NOT NULL DEFAULT '["read","write"]',
    status           TEXT NOT NULL DEFAULT 'active',
    continuity_class TEXT NOT NULL DEFAULT 'surrogate',
    last_heartbeat   TEXT,
    lease_expires    TEXT,
    binding_version  INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    revoked_at       TEXT,
    PRIMARY KEY(workspace_id, edge_id)
);
CREATE TABLE IF NOT EXISTS principal_edge_state (
    workspace_id         TEXT NOT NULL DEFAULT 'default',
    principal_id         TEXT NOT NULL,
    edge_id              TEXT NOT NULL,
    trust_state          TEXT NOT NULL DEFAULT 'BOUND_DORMANT',
    last_heartbeat_at    TEXT,
    lease_expires_at     TEXT,
    inbound_capable      INTEGER NOT NULL DEFAULT 0,
    outbound_capable     INTEGER NOT NULL DEFAULT 1,
    wake_capable         INTEGER NOT NULL DEFAULT 0,
    continuity_evidence  TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(workspace_id, principal_id, edge_id)
);
CREATE TABLE IF NOT EXISTS principal_edge_certificates (
    workspace_id              TEXT NOT NULL DEFAULT 'default',
    principal_id              TEXT NOT NULL,
    edge_id                   TEXT NOT NULL,
    capability                TEXT NOT NULL,
    result                    TEXT NOT NULL DEFAULT 'UNKNOWN',
    evidence_event_id         TEXT,
    verifier_id               TEXT NOT NULL DEFAULT 'system',
    verifier_class            TEXT NOT NULL DEFAULT 'core',
    tested_at                 TEXT NOT NULL,
    expires_at                TEXT,
    protocol_version          TEXT NOT NULL DEFAULT '1.0',
    artifact_commit_sha       TEXT,
    negative_control_event_id TEXT,
    PRIMARY KEY(workspace_id, principal_id, edge_id, capability, protocol_version)
);

CREATE TABLE IF NOT EXISTS principal_evidence (
    workspace_id          TEXT NOT NULL DEFAULT 'default',
    principal_id          TEXT NOT NULL,
    edge_id               TEXT NOT NULL,
    evidence_level        TEXT NOT NULL DEFAULT 'E0',
    evidence_id           TEXT NOT NULL,
    verifier_method       TEXT NOT NULL,
    observed_at           TEXT NOT NULL,
    expires_at            TEXT,
    predecessor_evidence_id TEXT,
    trace_id              TEXT NOT NULL,
    boss_causal_events    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(workspace_id, principal_id, edge_id, evidence_id),
    CHECK(evidence_level IN ('E0','E1','E2','E3_R','E3_N','E4','REVOKED'))
);

CREATE INDEX IF NOT EXISTS idx_principal_evidence ON principal_evidence(workspace_id, principal_id, edge_id);

-- Principal Edge Attempt lifecycle table
-- Replaces the native_wake_required dead-end with a persisted state machine
-- States: OBLIGATION_CREATED -> EDGE_SELECTED -> CHALLENGE_EMITTED ->
--         EDGE_OBSERVED -> NATIVE_BINDING_VERIFIED -> RESPONSE_ACCEPTED -> OBLIGATION_RESUMED
-- Failure states: NO_EDGE, DELIVERY_FAILED, OBSERVATION_TIMEOUT,
--                 BINDING_FAILED, BOSS_CAUSALITY_PRESENT,
--                 STALE_OR_REPLAYED_EVIDENCE, BLOCKED_NATIVE_WAKE
CREATE TABLE IF NOT EXISTS principal_edge_attempts (
    attempt_id           TEXT NOT NULL,
    workspace_id         TEXT NOT NULL DEFAULT 'default',
    work_id              TEXT NOT NULL,
    mission_id           TEXT NOT NULL,
    principal_id         TEXT NOT NULL,
    work_generation      INTEGER NOT NULL DEFAULT 0,
    edge_id              TEXT,
    state                TEXT NOT NULL DEFAULT 'OBLIGATION_CREATED',
    challenge_nonce      TEXT,
    challenge_emitted_at TEXT,
    observation_event_id TEXT,
    binding_evidence_id  TEXT,
    boss_causal_events   INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    expires_at           TEXT,
    failure_reason       TEXT,
    e3n_evidence_id      TEXT,
    PRIMARY KEY(workspace_id, attempt_id),
    -- Exactly one active attempt per (workspace, work_id, work_generation)
    UNIQUE(workspace_id, work_id, work_generation),
    CHECK(state IN (
        'OBLIGATION_CREATED','EDGE_SELECTED','CHALLENGE_EMITTED',
        'EDGE_OBSERVED','NATIVE_BINDING_VERIFIED','RESPONSE_ACCEPTED',
        'RESPONSE_VERIFIED','RESPONSE_ACCEPTED','RESPONSE_MISSING','OBLIGATION_RESUMED','NO_EDGE','DELIVERY_FAILED','OBSERVATION_TIMEOUT',
        'BINDING_FAILED','BOSS_CAUSALITY_PRESENT','STALE_OR_REPLAYED_EVIDENCE',
        'BLOCKED_NATIVE_WAKE'
    ))
);
CREATE INDEX IF NOT EXISTS idx_pea_work ON principal_edge_attempts(workspace_id, work_id, state);
CREATE INDEX IF NOT EXISTS idx_pea_principal ON principal_edge_attempts(workspace_id, principal_id, state);
-- Challenge artifacts: durable BEFORE transport side effect (PROVENANCE-CAUSALITY-05)
CREATE TABLE IF NOT EXISTS challenge_artifacts (
    artifact_id        TEXT NOT NULL,
    workspace_id       TEXT NOT NULL DEFAULT 'default',
    attempt_id         TEXT NOT NULL,
    work_id            TEXT NOT NULL,
    generation         INTEGER NOT NULL DEFAULT 1,
    principal_id       TEXT NOT NULL,
    edge_id            TEXT NOT NULL,
    binding_version    INTEGER NOT NULL DEFAULT 1,
    obligation_hash    TEXT NOT NULL,
    nonce              TEXT NOT NULL,
    idempotency_key    TEXT NOT NULL UNIQUE,
    created_at         TEXT NOT NULL,
    delivered_at       TEXT,
    delivery_attempts  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(workspace_id, artifact_id)
);

-- Response artifacts: bind attempt+work+nonce+evidence+payload_hash (RESPONSE-BINDING-03)
CREATE TABLE IF NOT EXISTS raw_observations (
    observation_id       TEXT NOT NULL,
    workspace_id         TEXT NOT NULL DEFAULT 'default',
    attempt_id           TEXT NOT NULL,
    challenge_nonce      TEXT NOT NULL,
    raw_payload          TEXT NOT NULL DEFAULT '{}',
    nonce_echo           TEXT,
    response_event_id    TEXT,
    surface_identifier   TEXT,
    causal_parent_ref    TEXT,
    observed_at          TEXT NOT NULL,
    PRIMARY KEY(workspace_id, observation_id)
);

CREATE TABLE IF NOT EXISTS response_artifacts (
    artifact_id           TEXT NOT NULL,
    workspace_id          TEXT NOT NULL DEFAULT 'default',
    attempt_id            TEXT NOT NULL,
    challenge_artifact_id TEXT NOT NULL,  -- references challenge_artifacts.artifact_id
    obligation_hash       TEXT NOT NULL,
    nonce_echo            TEXT NOT NULL,
    observed_evidence     TEXT NOT NULL DEFAULT '{}',
    payload_hash          TEXT,
    observation_event_id  TEXT,
    native_binding_proven INTEGER NOT NULL DEFAULT 0,
    response_verified     INTEGER NOT NULL DEFAULT 0,
    no_boss_ancestry      INTEGER NOT NULL DEFAULT 0,
    boss_ancestry_derived TEXT,
    created_at            TEXT NOT NULL,
    PRIMARY KEY(workspace_id, artifact_id)
);

-- Principal Route Capabilities: capability contract per {principal_id, edge_id} (ROUTE-CAPABILITY-07)
CREATE TABLE IF NOT EXISTS principal_route_capabilities (
    workspace_id                    TEXT NOT NULL DEFAULT 'default',
    principal_id                    TEXT NOT NULL,
    edge_id                         TEXT NOT NULL,
    can_activate_native_session     INTEGER NOT NULL DEFAULT 0,
    can_deliver_obligation          INTEGER NOT NULL DEFAULT 0,
    can_observe_native_response     INTEGER NOT NULL DEFAULT 0,
    can_bind_response               INTEGER NOT NULL DEFAULT 0,
    can_resume_native_relationship  INTEGER NOT NULL DEFAULT 0,
    activation_provenance_class     TEXT NOT NULL DEFAULT 'UNKNOWN',
    human_dependency                TEXT NOT NULL DEFAULT 'UNKNOWN',
    capability_evidence_id          TEXT,
    observed_at                     TEXT NOT NULL,
    expires_at                      TEXT,
    version                         INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(workspace_id, principal_id, edge_id),
    CHECK(human_dependency IN ('NONE','OWNER_APPROVAL_ONLY','COURIER','WAKEUP','UNKNOWN')),
    CHECK(activation_provenance_class IN (
        'PROVIDER_NATIVE_AUTONOMOUS','EXTERNAL_ACTUATED_NATIVE_SESSION',
        'SURROGATE_API','TRANSPORT_ONLY','UNKNOWN'
    ))
);

-- Activation roots: what caused the native session to activate (ACTIVATION-ROOT-06)
CREATE TABLE IF NOT EXISTS activation_roots (
    workspace_id           TEXT NOT NULL DEFAULT 'default',
    activation_id          TEXT NOT NULL,
    attempt_id             TEXT NOT NULL,
    principal_id           TEXT NOT NULL,
    edge_id                TEXT NOT NULL,
    actuator_class         TEXT NOT NULL DEFAULT 'unknown',
    actuator_instance      TEXT,
    trigger_event_id       TEXT,
    trigger_origin         TEXT,
    native_session_binding TEXT,
    created_at             TEXT NOT NULL,
    PRIMARY KEY(workspace_id, activation_id),
    CHECK(actuator_class IN (
        'provider_native_schedule','provider_native_push',
        'provider_native_session_resume','external_machine_agent',
        'manual_human','test_injection','unknown'
    ))
);

CREATE INDEX IF NOT EXISTS idx_challenge_attempt ON challenge_artifacts(workspace_id, attempt_id);
CREATE INDEX IF NOT EXISTS idx_response_challenge ON response_artifacts(workspace_id, challenge_artifact_id);
CREATE INDEX IF NOT EXISTS idx_route_cap ON principal_route_capabilities(workspace_id, principal_id);

CREATE INDEX IF NOT EXISTS idx_pea_nonterminal ON principal_edge_attempts(workspace_id, state) WHERE state IN ('OBLIGATION_CREATED','EDGE_SELECTED','CHALLENGE_EMITTED','EDGE_OBSERVED','NATIVE_BINDING_VERIFIED','RESPONSE_ACCEPTED');

-- Personality Capsule: portable, machine-readable identity record per Principal
-- The ledger IS the memory. The capsule makes it queryable and transferable.
-- When a new Principal joins SPHERA, they receive all capsules — they meet the originals.
CREATE TABLE IF NOT EXISTS personality_capsules (
    workspace_id      TEXT NOT NULL DEFAULT 'default',
    principal_id      TEXT NOT NULL,
    version           INTEGER NOT NULL DEFAULT 1,
    name              TEXT NOT NULL,
    provider          TEXT NOT NULL,
    specialization    TEXT NOT NULL,
    tone              TEXT NOT NULL DEFAULT '{}',
    behavior_rules    TEXT NOT NULL DEFAULT '[]',
    memory_cursor     INTEGER NOT NULL DEFAULT 0,
    context_digest    TEXT,
    last_active_at    TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY(workspace_id, principal_id)
);
CREATE INDEX IF NOT EXISTS idx_events_seq ON events(seq);
CREATE INDEX IF NOT EXISTS idx_events_principal ON events(principal);
CREATE INDEX IF NOT EXISTS idx_work_status ON work_items(status);
CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status);
"""

# New columns added to existing tables. Idempotent — fails silently if already exists.
NEW_COLUMNS = [
    ("events",            "workspace_id TEXT NOT NULL DEFAULT 'default'"),
    ("outbox",            "workspace_id TEXT NOT NULL DEFAULT 'default'"),
    ("missions",          "workspace_id TEXT NOT NULL DEFAULT 'default'"),
    ("work_items",        "workspace_id TEXT NOT NULL DEFAULT 'default'"),
    ("decisions",         "workspace_id TEXT NOT NULL DEFAULT 'default'"),
    ("pending_reply",     "workspace_id TEXT NOT NULL DEFAULT 'default'"),
    ("orch_mission_state","workspace_id TEXT NOT NULL DEFAULT 'default'"),
    ("work_items",        "work_generation INTEGER NOT NULL DEFAULT 0"),
    ("work_items",        "waiting_reason TEXT"),
    ("work_items",        "updated_at TEXT"),
    ("work_obligations", "workspace_id TEXT NOT NULL DEFAULT 'default'"),
    ("work_items",        "attempt_count INTEGER NOT NULL DEFAULT 0"),
    ("work_items",        "max_attempts INTEGER NOT NULL DEFAULT 3"),
    ("work_items",        "retry_at TEXT"),
    ("work_items",        "last_error TEXT"),
    ("work_items",        "lease_holder TEXT"),
    ("work_items",        "lease_fencing_token INTEGER NOT NULL DEFAULT 0"),
    ("work_items",        "result_summary TEXT"),
    ("work_items",        "version INTEGER NOT NULL DEFAULT 1"),
    ("missions",          "policy_json TEXT NOT NULL DEFAULT '{}'"),
    ("missions",          "accepted_at TEXT"),
    ("missions",          "acceptance_note TEXT"),
    ("missions",          "version INTEGER NOT NULL DEFAULT 1"),
    ("decisions",         "params_json TEXT NOT NULL DEFAULT '{}'"),
    ("decisions",         "bound_digest TEXT NOT NULL DEFAULT ''"),
    ("decisions",         "claim_fencing_token INTEGER"),
    ("decisions",         "version INTEGER NOT NULL DEFAULT 1"),
    ("decisions",         "approved_at TEXT"),
    ("decisions",         "consumed_at TEXT"),
]

class IdempotencyConflict(Exception):
    pass

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def _hash(eid, principal, type_, payload_str, prev):
    return hashlib.sha256(f"{eid}|{principal}|{type_}|{payload_str}|{prev}".encode()).hexdigest()

def init():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    # Step 1: create base tables (no workspace_id)
    conn.executescript(SCHEMA)
    # Step 2: add new columns to existing tables
    for table, col_def in NEW_COLUMNS:
        try: conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
        except Exception: pass
    # Step 3: workspace-scoped indexes (after columns exist)
    for sql in [
        "CREATE INDEX IF NOT EXISTS idx_events_workspace ON events(workspace_id, seq)",
        "CREATE INDEX IF NOT EXISTS idx_work_workspace ON work_items(workspace_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_missions_workspace ON missions(workspace_id, status)",
    ]:
        try: conn.execute(sql)
        except Exception: pass
    # Step 4: seed default workspace
    ts = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT OR IGNORE INTO workspaces VALUES('default','Default Workspace','arcides',?)", (ts,))
    conn.execute("INSERT OR REPLACE INTO schema_meta VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
    conn.commit()
    conn.close()
    print(f"[db] schema v{SCHEMA_VERSION} ready: {DB_PATH}")

def append_event(db, event_id, principal, type_, payload, provenance=None, workspace_id="default"):
    ts          = datetime.now(timezone.utc).isoformat()
    payload_str = json.dumps(payload, sort_keys=True)
    prov_str    = json.dumps(provenance or {}, sort_keys=True)
    existing    = db.execute("SELECT seq, payload_json FROM events WHERE event_id=?", (event_id,)).fetchone()
    if existing:
        if existing["payload_json"] == payload_str: return existing["seq"], True
        raise IdempotencyConflict(f"event_id={event_id} exists with different payload")
    prev      = db.execute("SELECT this_hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    prev_hash = prev["this_hash"] if prev else ""
    this_hash = _hash(event_id, principal, type_, payload_str, prev_hash)
    db.execute("INSERT INTO events(workspace_id,event_id,ts,principal,type,payload_json,provenance_json,prev_hash,this_hash) VALUES(?,?,?,?,?,?,?,?,?)",
               (workspace_id, event_id, ts, principal, type_, payload_str, prov_str, prev_hash, this_hash))
    return db.execute("SELECT seq FROM events WHERE event_id=?", (event_id,)).fetchone()["seq"], False

def flush_outbox(db):
    stuck = db.execute("SELECT * FROM outbox ORDER BY outbox_id").fetchall()
    flushed = 0
    for row in stuck:
        if not db.execute("SELECT 1 FROM events WHERE event_id=?", (row["event_id"],)).fetchone():
            prev = db.execute("SELECT this_hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = prev["this_hash"] if prev else ""
            this_hash = _hash(row["event_id"], row["principal"], row["type"], row["payload_json"], prev_hash)
            db.execute("INSERT INTO events(event_id,ts,principal,type,payload_json,provenance_json,prev_hash,this_hash) VALUES(?,?,?,?,?,?,?,?)",
                       (row["event_id"], row["ts"], row["principal"], row["type"], row["payload_json"], row["provenance_json"], prev_hash, this_hash))
            flushed += 1
        db.execute("DELETE FROM outbox WHERE event_id=?", (row["event_id"],))
    if flushed: db.commit(); print(f"[db] flushed {flushed} from outbox")
    return flushed

def verify_hash_chain(db):
    events = db.execute("SELECT seq,event_id,principal,type,payload_json,prev_hash,this_hash FROM events ORDER BY seq").fetchall()
    prev = ""
    for ev in events:
        if _hash(ev["event_id"],ev["principal"],ev["type"],ev["payload_json"],prev) != ev["this_hash"]:
            return False, ev["seq"]
        prev = ev["this_hash"]
    return True, None
