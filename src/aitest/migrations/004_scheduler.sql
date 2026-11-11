-- T02: scheduling, runner management and run lifecycle.
--
-- Design notes:
-- * The scheduler is pull-based: runners claim work. No background worker is
--   required; every API call runs a bounded maintenance pass, and
--   POST /projects/{pid}/scheduler/tick forces one explicitly.
-- * Concurrency and timeouts are project-scoped so the two-tier permission
--   model from T01 stays intact (a member cannot manage another project).
-- * run_event is the append-only execution ledger read by reports, gates and
--   later cost modules. Every status change writes exactly one row.

-- Per-project scheduler settings. Missing keys fall back to service defaults,
-- so rows created by the T01 code keep working unchanged.
ALTER TABLE project ADD COLUMN scheduler_config_json TEXT NOT NULL DEFAULT '{}'
    CHECK (json_valid(scheduler_config_json));

-- Project-scoped runners. A runner token authenticates claims and completion;
-- only the SHA-256 hash is stored.
CREATE TABLE runner (
    id                TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name              TEXT NOT NULL CHECK (length(trim(name)) > 0),
    token_hash        TEXT NOT NULL UNIQUE,
    token_prefix      TEXT NOT NULL,
    labels_json       TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(labels_json)),
    capacity          INTEGER NOT NULL DEFAULT 1 CHECK (capacity >= 1 AND capacity <= 64),
    status            TEXT NOT NULL DEFAULT 'online'
                      CHECK (status IN ('online', 'offline', 'unavailable', 'draining')),
    last_heartbeat_at TEXT,
    registered_at     TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    revoked_at        TEXT,
    UNIQUE (id, project_id),
    UNIQUE (project_id, name)
);

CREATE INDEX idx_runner_project ON runner(project_id, status);

-- Append-only status transition ledger.
CREATE TABLE run_event (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL CHECK (seq >= 1),
    from_status TEXT,
    to_status   TEXT NOT NULL,
    source      TEXT NOT NULL CHECK (source IN ('api', 'scheduler', 'runner', 'timeout', 'system')),
    actor       TEXT,
    reason      TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json)),
    created_at  TEXT NOT NULL,
    UNIQUE (run_id, seq)
);

CREATE INDEX idx_run_event_run ON run_event(run_id, seq);

-- Runner lifecycle audit (registration, heartbeat recovery, stale detection).
CREATE TABLE runner_event (
    id          TEXT PRIMARY KEY,
    runner_id   TEXT NOT NULL REFERENCES runner(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL
                CHECK (kind IN ('registered', 'heartbeat', 'went_unavailable', 'revoked', 'recovered', 'draining')),
    detail_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json)),
    created_at  TEXT NOT NULL
);

CREATE INDEX idx_runner_event_runner ON runner_event(runner_id, created_at);

-- Scheduling fields on run. Existing T01 rows receive safe defaults; their
-- queue_deadline_at stays NULL so a historical queued row is not expired
-- retroactively by the new maintenance pass.
ALTER TABLE run ADD COLUMN priority INTEGER NOT NULL DEFAULT 50
    CHECK (priority >= 0 AND priority <= 100);
ALTER TABLE run ADD COLUMN idempotency_key TEXT;
ALTER TABLE run ADD COLUMN params_digest TEXT NOT NULL DEFAULT '';
ALTER TABLE run ADD COLUMN timeout_seconds INTEGER NOT NULL DEFAULT 300
    CHECK (timeout_seconds >= 1);
ALTER TABLE run ADD COLUMN queue_timeout_seconds INTEGER NOT NULL DEFAULT 600
    CHECK (queue_timeout_seconds >= 1);
ALTER TABLE run ADD COLUMN queue_deadline_at TEXT;
ALTER TABLE run ADD COLUMN deadline_at TEXT;
ALTER TABLE run ADD COLUMN lease_expires_at TEXT;
ALTER TABLE run ADD COLUMN assigned_runner_id TEXT REFERENCES runner(id);
ALTER TABLE run ADD COLUMN rerun_of_run_id TEXT REFERENCES run(id);
ALTER TABLE run ADD COLUMN superseded_by_run_id TEXT REFERENCES run(id);
ALTER TABLE run ADD COLUMN cancel_source TEXT;
ALTER TABLE run ADD COLUMN cancel_reason TEXT;
ALTER TABLE run ADD COLUMN cancelled_by TEXT;
ALTER TABLE run ADD COLUMN cancelled_at TEXT;
ALTER TABLE run ADD COLUMN result_json TEXT
    CHECK (result_json IS NULL OR json_valid(result_json));
ALTER TABLE run ADD COLUMN updated_at TEXT;

-- At most one non-superseded run per idempotency key. A forced rerun first
-- marks the previous run as superseded, then inserts the new active run.
CREATE UNIQUE INDEX idx_run_active_idempotency
    ON run(project_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL AND superseded_by_run_id IS NULL;

CREATE INDEX idx_run_queue_order ON run(project_id, status, priority DESC, created_at);
CREATE INDEX idx_run_deadline ON run(status, deadline_at);
CREATE INDEX idx_run_lease ON run(status, lease_expires_at);
CREATE INDEX idx_run_runner ON run(assigned_runner_id, status);

-- Give projects created before this migration an explicit default config.
UPDATE project
SET scheduler_config_json =
    '{"default_queue_timeout_seconds":600,"default_timeout_seconds":300,"max_concurrency":2,"runner_stale_seconds":45}'
WHERE scheduler_config_json = '{}';
