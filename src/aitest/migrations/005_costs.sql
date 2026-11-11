-- T16: cost aggregation and quota gate.
--
-- cost_entry is the append-only detail ledger. Every model call, embedding
-- call, runner duration or manual adjustment becomes one row. Aggregates in
-- cost_ledger are derived from these rows so a summary can always be traced
-- back to detail records.
--
-- Missing usage or missing pricing is stored as NULL with an explicit
-- cost_status='missing'/'partial' and a missing_reason. It is never recorded
-- as a zero-cost entry.

ALTER TABLE cost_ledger ADD COLUMN tokens_total INTEGER
    CHECK (tokens_total IS NULL OR tokens_total >= 0);
ALTER TABLE cost_ledger ADD COLUMN currency TEXT;
ALTER TABLE cost_ledger ADD COLUMN cost_status TEXT NOT NULL DEFAULT 'missing'
    CHECK (cost_status IN ('complete', 'partial', 'missing'));
ALTER TABLE cost_ledger ADD COLUMN missing_entries INTEGER NOT NULL DEFAULT 0
    CHECK (missing_entries >= 0);
ALTER TABLE cost_ledger ADD COLUMN entry_count INTEGER NOT NULL DEFAULT 0
    CHECK (entry_count >= 0);
ALTER TABLE cost_ledger ADD COLUMN missing_components_json TEXT NOT NULL DEFAULT '[]'
    CHECK (json_valid(missing_components_json));
ALTER TABLE cost_ledger ADD COLUMN updated_at TEXT;

CREATE TABLE cost_entry (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    run_id          TEXT NOT NULL,
    run_item_id     TEXT REFERENCES run_item(id) ON DELETE SET NULL,
    channel         TEXT NOT NULL CHECK (channel IN ('A', 'B')),
    branch          TEXT NOT NULL,
    entry_type      TEXT NOT NULL CHECK (entry_type IN (
                        'model_call', 'embedding', 'runner', 'manual')),
    source          TEXT NOT NULL CHECK (source IN (
                        'scorer', 'runner', 'api', 'cli', 'system')),
    model           TEXT,
    tokens_in       INTEGER CHECK (tokens_in IS NULL OR tokens_in >= 0),
    tokens_out      INTEGER CHECK (tokens_out IS NULL OR tokens_out >= 0),
    tokens_total    INTEGER CHECK (tokens_total IS NULL OR tokens_total >= 0),
    gpu_seconds     REAL CHECK (gpu_seconds IS NULL OR gpu_seconds >= 0),
    est_cost        REAL CHECK (est_cost IS NULL OR est_cost >= 0),
    currency        TEXT,
    cost_status     TEXT NOT NULL CHECK (cost_status IN ('complete', 'partial', 'missing')),
    missing_reason  TEXT,
    usage_json      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(usage_json)),
    pricing_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(pricing_json)),
    metadata_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    dedupe_key      TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (run_id, dedupe_key),
    FOREIGN KEY (run_id, project_id) REFERENCES run(id, project_id),
    FOREIGN KEY (run_id, channel) REFERENCES run(id, channel)
);

CREATE INDEX idx_cost_entry_run ON cost_entry(run_id, created_at);
CREATE INDEX idx_cost_entry_project_created ON cost_entry(project_id, created_at);
CREATE INDEX idx_cost_entry_branch ON cost_entry(project_id, branch, created_at);
CREATE INDEX idx_cost_entry_channel ON cost_entry(project_id, channel, created_at);
CREATE INDEX idx_cost_entry_item ON cost_entry(run_item_id);

-- Quota checks and terminations are audit records; a reader can see why a Run
-- stopped even after the quota configuration changes later.
CREATE TABLE cost_quota_event (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    run_id        TEXT REFERENCES run(id) ON DELETE CASCADE,
    scope         TEXT NOT NULL CHECK (scope IN ('run', 'period')),
    action        TEXT NOT NULL CHECK (action IN (
                      'checked', 'exceeded', 'terminated', 'violation_after_terminal')),
    reason        TEXT NOT NULL,
    limit_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(limit_json)),
    consumed_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(consumed_json)),
    detail_json   TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json)),
    created_at    TEXT NOT NULL
);

CREATE INDEX idx_cost_quota_event_run ON cost_quota_event(run_id, created_at);
CREATE INDEX idx_cost_quota_event_project ON cost_quota_event(project_id, created_at);

-- Existing T01 cost_ledger rows (if any) were not produced by the T16 detail
-- ledger, so they stay explicitly marked as missing rather than being trusted
-- as complete zero-cost records.
UPDATE cost_ledger
SET cost_status = 'missing',
    missing_components_json = '["detail_entries"]',
    updated_at = created_at
WHERE entry_count = 0;
