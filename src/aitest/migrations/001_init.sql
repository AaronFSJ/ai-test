-- T01 platform data model.
-- project / member / spec / spec_version / run / run_item / verdict /
-- attribution / evidence / review / cost_ledger.

CREATE TABLE project (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
    default_branch  TEXT NOT NULL DEFAULT 'main' CHECK (length(trim(default_branch)) > 0),
    quota_config    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(quota_config)),
    created_at      TEXT NOT NULL
);

CREATE TABLE member (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name         TEXT NOT NULL CHECK (length(trim(name)) > 0),
    email        TEXT,
    role         TEXT NOT NULL CHECK (role IN ('member', 'readonly')),
    token_hash   TEXT NOT NULL UNIQUE,
    token_prefix TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE (project_id, name),
    UNIQUE (id, project_id)
);

CREATE TABLE spec (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    channel     TEXT NOT NULL CHECK (channel IN ('A', 'B')),
    type        TEXT NOT NULL CHECK (type IN ('script', 'dataset')),
    name        TEXT NOT NULL CHECK (length(trim(name)) > 0),
    created_at  TEXT NOT NULL,
    UNIQUE (project_id, name),
    UNIQUE (id, channel),
    UNIQUE (id, project_id),
    CHECK ((channel = 'A' AND type = 'script') OR (channel = 'B' AND type = 'dataset'))
);

-- Spec versions are immutable; a new version is inserted for every change.
-- The channel-specific CHECK makes A/B field differences a database
-- invariant rather than a convention.
CREATE TABLE spec_version (
    id                  TEXT PRIMARY KEY,
    spec_id             TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    channel             TEXT NOT NULL CHECK (channel IN ('A', 'B')),
    version             INTEGER NOT NULL CHECK (version >= 1),
    content_ref         TEXT,
    params_json         TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(params_json)),
    repo_path           TEXT,
    repo_commit         TEXT,
    dataset_version     TEXT,
    scorer_version      TEXT,
    model_snapshot_json TEXT CHECK (model_snapshot_json IS NULL OR json_valid(model_snapshot_json)),
    created_by          TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    UNIQUE (spec_id, version),
    UNIQUE (id, project_id),
    UNIQUE (id, channel),
    FOREIGN KEY (spec_id, channel) REFERENCES spec(id, channel),
    FOREIGN KEY (spec_id, project_id) REFERENCES spec(id, project_id),
    FOREIGN KEY (created_by, project_id) REFERENCES member(id, project_id),
    CHECK (
        (channel = 'A'
            AND repo_path IS NOT NULL AND length(trim(repo_path)) > 0
            AND repo_commit IS NOT NULL AND length(trim(repo_commit)) > 0
            AND dataset_version IS NULL
            AND scorer_version IS NULL
            AND model_snapshot_json IS NULL)
        OR
        (channel = 'B'
            AND dataset_version IS NOT NULL AND length(trim(dataset_version)) > 0
            AND scorer_version IS NOT NULL AND length(trim(scorer_version)) > 0
            AND model_snapshot_json IS NOT NULL AND length(trim(model_snapshot_json)) > 0
            AND repo_path IS NULL
            AND repo_commit IS NULL)
    )
);

-- Every run binds exactly one spec version. Composite foreign keys make it
-- impossible to bind a run to a spec version from another project or channel.
CREATE TABLE run (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES project(id),
    spec_version_id     TEXT NOT NULL,
    channel             TEXT NOT NULL CHECK (channel IN ('A', 'B')),
    commit_sha          TEXT NOT NULL CHECK (length(trim(commit_sha)) > 0),
    branch              TEXT NOT NULL CHECK (length(trim(branch)) > 0),
    env                 TEXT,
    model_snapshot_json TEXT CHECK (model_snapshot_json IS NULL OR json_valid(model_snapshot_json)),
    sampling_config_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(sampling_config_json)),
    trigger             TEXT NOT NULL CHECK (trigger IN ('ci', 'manual', 'schedule', 'api')),
    status              TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'timeout')),
    started_at          TEXT,
    finished_at         TEXT,
    baseline_run_id     TEXT,
    created_at          TEXT NOT NULL,
    UNIQUE (id, project_id),
    UNIQUE (id, channel),
    FOREIGN KEY (spec_version_id, project_id) REFERENCES spec_version(id, project_id),
    FOREIGN KEY (spec_version_id, channel) REFERENCES spec_version(id, channel),
    FOREIGN KEY (baseline_run_id, project_id) REFERENCES run(id, project_id)
);

CREATE TABLE run_item (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    case_ref    TEXT NOT NULL CHECK (length(trim(case_ref)) > 0),
    state       TEXT NOT NULL CHECK (state IN ('pass', 'fail', 'uncertain', 'error')),
    confidence  REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    cost_json   TEXT CHECK (cost_json IS NULL OR json_valid(cost_json)),
    created_at  TEXT NOT NULL,
    UNIQUE (run_id, case_ref)
);

CREATE TABLE verdict (
    id                TEXT PRIMARY KEY,
    run_item_id       TEXT NOT NULL UNIQUE REFERENCES run_item(id) ON DELETE CASCADE,
    state             TEXT NOT NULL CHECK (state IN ('pass', 'fail', 'uncertain', 'error')),
    confidence        REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    scorer_trace_json TEXT NOT NULL CHECK (json_valid(scorer_trace_json)),
    created_at        TEXT NOT NULL
);

CREATE TABLE attribution (
    id                TEXT PRIMARY KEY,
    run_item_id       TEXT NOT NULL REFERENCES run_item(id) ON DELETE CASCADE,
    label             TEXT NOT NULL CHECK (label IN ('product_defect', 'script_failure', 'env_issue', 'data_issue', 'unknown')),
    sub_label         TEXT,
    evidence_refs_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(evidence_refs_json)),
    model_used        TEXT,
    confidence        REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    created_at        TEXT NOT NULL
);

CREATE TABLE evidence (
    id          TEXT PRIMARY KEY,
    run_item_id TEXT NOT NULL REFERENCES run_item(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('log', 'dom', 'screenshot', 'request', 'model_io')),
    uri         TEXT NOT NULL CHECK (length(trim(uri)) > 0),
    bytes       INTEGER CHECK (bytes IS NULL OR bytes >= 0),
    created_at  TEXT NOT NULL
);

CREATE TABLE review (
    id          TEXT PRIMARY KEY,
    run_item_id TEXT NOT NULL REFERENCES run_item(id) ON DELETE CASCADE,
    reviewer    TEXT NOT NULL CHECK (length(trim(reviewer)) > 0),
    conclusion  TEXT NOT NULL CHECK (conclusion IN ('confirmed_defect', 'false_positive', 'sample_error')),
    note        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE cost_ledger (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    channel     TEXT NOT NULL CHECK (channel IN ('A', 'B')),
    tokens_in   INTEGER CHECK (tokens_in IS NULL OR tokens_in >= 0),
    tokens_out  INTEGER CHECK (tokens_out IS NULL OR tokens_out >= 0),
    gpu_seconds REAL CHECK (gpu_seconds IS NULL OR gpu_seconds >= 0),
    est_cost    REAL CHECK (est_cost IS NULL OR est_cost >= 0),
    created_at  TEXT NOT NULL,
    UNIQUE (run_id),
    FOREIGN KEY (run_id, channel) REFERENCES run(id, channel)
);

CREATE INDEX idx_member_project ON member(project_id);
CREATE INDEX idx_spec_project ON spec(project_id);
CREATE INDEX idx_spec_version_spec ON spec_version(spec_id, version);
CREATE INDEX idx_spec_version_project ON spec_version(project_id);
CREATE INDEX idx_run_project_created ON run(project_id, created_at);
CREATE INDEX idx_run_spec_version ON run(spec_version_id);
CREATE INDEX idx_run_item_run ON run_item(run_id);
CREATE INDEX idx_attribution_item ON attribution(run_item_id);
CREATE INDEX idx_evidence_item ON evidence(run_item_id);
CREATE INDEX idx_review_item ON review(run_item_id);
CREATE INDEX idx_cost_ledger_run ON cost_ledger(run_id);

-- Spec versions cannot be edited or deleted in place.
CREATE TRIGGER spec_version_no_update
BEFORE UPDATE ON spec_version
BEGIN
    SELECT RAISE(ABORT, 'spec_version is immutable; create a new version');
END;

CREATE TRIGGER spec_version_no_delete
BEFORE DELETE ON spec_version
BEGIN
    SELECT RAISE(ABORT, 'spec_version is immutable; delete the owning project instead');
END;

-- Version numbers must be contiguous (1, 2, 3, ...).
CREATE TRIGGER spec_version_contiguous
BEFORE INSERT ON spec_version
BEGIN
    SELECT RAISE(ABORT, 'spec_version must be the next contiguous version')
    WHERE NEW.version != (
        SELECT COALESCE(MAX(version), 0) + 1
        FROM spec_version
        WHERE spec_id = NEW.spec_id
    );
END;
