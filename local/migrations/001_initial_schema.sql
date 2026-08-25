-- SPHERA Gate 1: Initial Schema
-- Migration 001 — forward only, never modify this file
-- Compatible with both SQLite (WAL) and PostgreSQL

-- Event ledger — append only, never UPDATE or DELETE
CREATE TABLE IF NOT EXISTS events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    id          TEXT    NOT NULL UNIQUE,
    ts          TEXT    NOT NULL,
    principal   TEXT    NOT NULL,
    type        TEXT    NOT NULL,
    payload     TEXT    NOT NULL,         -- JSON
    -- Hash chain: each event includes hash of previous event
    prev_hash   TEXT    NOT NULL DEFAULT '',
    this_hash   TEXT    NOT NULL DEFAULT ''
);

-- Outbox: events written here first, moved to events on commit
-- Prevents partial writes from becoming visible
CREATE TABLE IF NOT EXISTS outbox (
    outbox_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    id          TEXT    NOT NULL UNIQUE,
    ts          TEXT    NOT NULL,
    principal   TEXT    NOT NULL,
    type        TEXT    NOT NULL,
    payload     TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);

-- Decision state machine
-- States: pending -> approved -> claimed -> consumed
--         pending -> rejected (terminal)
--         pending -> expired  (terminal)
--         claimed -> approved (via execution_failed)
CREATE TABLE IF NOT EXISTS decisions (
    request_id           TEXT PRIMARY KEY,
    status               TEXT NOT NULL DEFAULT 'pending',
    requesting_principal TEXT NOT NULL,
    scope                TEXT NOT NULL,
    target               TEXT NOT NULL,
    params_json          TEXT NOT NULL,
    bound_digest         TEXT NOT NULL,
    deadline             TEXT,
    -- Fencing tokens: monotonic version counter prevents stale updates
    version              INTEGER NOT NULL DEFAULT 1,
    claimed_at           TEXT,
    claim_expires        TEXT,
    claim_fencing_token  INTEGER,        -- must match on consume/fail
    approved_at          TEXT,
    consumed_at          TEXT
);

-- Mission state machine
-- States: active -> complete (via owner accept)
--         active -> cancelled (via owner cancel)
CREATE TABLE IF NOT EXISTS missions (
    mission_id      TEXT PRIMARY KEY,
    objective       TEXT NOT NULL,
    owner           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    policy_json     TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    accepted_at     TEXT,
    acceptance_note TEXT,
    -- Version for optimistic concurrency
    version         INTEGER NOT NULL DEFAULT 1
);

-- Work item state machine
-- States: blocked -> ready -> leased -> done
--                          -> failed (terminal)
CREATE TABLE IF NOT EXISTS work_items (
    work_id         TEXT PRIMARY KEY,
    mission_id      TEXT NOT NULL REFERENCES missions(mission_id),
    description     TEXT NOT NULL,
    capability      TEXT NOT NULL,
    deps_json       TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'ready',
    -- Lease fields
    lease_id        TEXT,
    lease_holder    TEXT,
    lease_expires   TEXT,
    -- Fencing token: monotonic, increments on each new lease
    -- Prevents stale result submission after lease expiry + reclaim
    lease_fencing_token  INTEGER NOT NULL DEFAULT 0,
    result_seq      INTEGER REFERENCES events(seq),
    result_summary  TEXT,
    created_at      TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1
);

-- Migration tracking
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    filename    TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL,
    checksum    TEXT    NOT NULL
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_events_ts         ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_principal  ON events(principal);
CREATE INDEX IF NOT EXISTS idx_events_type       ON events(type);
CREATE INDEX IF NOT EXISTS idx_decisions_status  ON decisions(status);
CREATE INDEX IF NOT EXISTS idx_work_mission      ON work_items(mission_id);
CREATE INDEX IF NOT EXISTS idx_work_status       ON work_items(status);
CREATE INDEX IF NOT EXISTS idx_outbox_created    ON outbox(created_at);
