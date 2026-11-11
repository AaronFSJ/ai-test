-- T03: evidence storage, redaction metadata, retention policy and read-only
-- report snapshots.
--
-- Evidence content lives in an object store partitioned by project/run. Only
-- redacted content is written there; these columns describe that stored object
-- and the retention decision made when it was ingested.

ALTER TABLE run_item ADD COLUMN priority TEXT NOT NULL DEFAULT 'P1'
    CHECK (priority IN ('P0', 'P1'));

ALTER TABLE evidence ADD COLUMN project_id TEXT;
ALTER TABLE evidence ADD COLUMN run_id TEXT;
ALTER TABLE evidence ADD COLUMN filename TEXT;
ALTER TABLE evidence ADD COLUMN storage_key TEXT;
ALTER TABLE evidence ADD COLUMN content_sha256 TEXT;
ALTER TABLE evidence ADD COLUMN content_type TEXT;
ALTER TABLE evidence ADD COLUMN bytes_original INTEGER;
ALTER TABLE evidence ADD COLUMN redacted INTEGER NOT NULL DEFAULT 0
    CHECK (redacted IN (0, 1));
ALTER TABLE evidence ADD COLUMN redaction_json TEXT NOT NULL DEFAULT '{}'
    CHECK (json_valid(redaction_json));
ALTER TABLE evidence ADD COLUMN retention_class TEXT
    CHECK (retention_class IS NULL OR retention_class IN ('P0', 'P1'));
ALTER TABLE evidence ADD COLUMN retention_expires_at TEXT;
ALTER TABLE evidence ADD COLUMN deleted_at TEXT;
ALTER TABLE evidence ADD COLUMN created_by TEXT;

CREATE INDEX idx_evidence_run ON evidence(run_id, created_at);
CREATE INDEX idx_evidence_project ON evidence(project_id);
CREATE INDEX idx_evidence_expiry ON evidence(retention_expires_at, deleted_at);
CREATE UNIQUE INDEX idx_evidence_storage_key ON evidence(storage_key);

-- Retention is configured per project and case priority. Missing rows fall
-- back to the documented defaults (P0 = 90 days, P1 = 30 days).
CREATE TABLE retention_policy (
    project_id    TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    case_priority TEXT NOT NULL CHECK (case_priority IN ('P0', 'P1')),
    retain_days   INTEGER NOT NULL CHECK (retain_days >= 0 AND retain_days <= 3650),
    updated_by    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (project_id, case_priority),
    FOREIGN KEY (updated_by, project_id) REFERENCES member(id, project_id)
);

-- Every destructive retention action is recorded. Metadata rows are never
-- hard-deleted, so an expired evidence id still explains what was removed.
CREATE TABLE evidence_cleanup_log (
    id                   TEXT PRIMARY KEY,
    project_id           TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    evidence_id          TEXT NOT NULL,
    run_id               TEXT NOT NULL,
    storage_key          TEXT NOT NULL,
    retention_expires_at TEXT,
    deleted_at           TEXT NOT NULL,
    reason               TEXT NOT NULL CHECK (reason IN ('expired')),
    actor                TEXT NOT NULL,
    bytes                INTEGER,
    FOREIGN KEY (actor, project_id) REFERENCES member(id, project_id)
);
CREATE INDEX idx_cleanup_project_time ON evidence_cleanup_log(project_id, deleted_at);

-- A report snapshot is generated once and then read-only. Its canonical JSON
-- text and digest are stored so later config/scorer edits cannot change it.
CREATE TABLE report_snapshot (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    run_id          TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    spec_version_id TEXT NOT NULL,
    report_version  INTEGER NOT NULL CHECK (report_version >= 1),
    content_json    TEXT NOT NULL CHECK (json_valid(content_json)),
    content_sha256  TEXT NOT NULL CHECK (length(content_sha256) = 64),
    storage_key     TEXT NOT NULL,
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (run_id, report_version),
    UNIQUE (id, project_id),
    FOREIGN KEY (created_by, project_id) REFERENCES member(id, project_id)
);
CREATE INDEX idx_report_project_created ON report_snapshot(project_id, created_at);
CREATE INDEX idx_report_run ON report_snapshot(run_id, report_version);

CREATE TRIGGER report_snapshot_no_update
BEFORE UPDATE ON report_snapshot
BEGIN
    SELECT RAISE(ABORT, 'report_snapshot is immutable; generate a new snapshot');
END;

CREATE TRIGGER report_snapshot_no_delete
BEFORE DELETE ON report_snapshot
BEGIN
    SELECT RAISE(ABORT, 'report_snapshot is immutable; retention cleanup never deletes reports');
END;

-- Evidence metadata is retained after cleanup; the object bytes are removed
-- and deleted_at marks the tombstone. Hard deletes would erase the audit trail.
CREATE TRIGGER evidence_no_hard_delete
BEFORE DELETE ON evidence
BEGIN
    SELECT RAISE(ABORT, 'evidence metadata is retained; mark deleted_at instead');
END;

-- Evidence must point at a run item inside the same project/run, and the
-- author must be a member of that project. These guards exist in addition to
-- the service checks so direct SQL cannot create a cross-project reference.
CREATE TRIGGER evidence_context_guard
BEFORE INSERT ON evidence
BEGIN
    SELECT RAISE(ABORT, 'evidence run/item/project mismatch')
    WHERE NEW.run_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM run_item ri JOIN run r ON r.id = ri.run_id
        WHERE ri.id = NEW.run_item_id
          AND ri.run_id = NEW.run_id
          AND r.project_id = NEW.project_id
    );
    SELECT RAISE(ABORT, 'evidence created_by must belong to the project')
    WHERE NEW.created_by IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM member
        WHERE id = NEW.created_by AND project_id = NEW.project_id
    );
END;

-- A report snapshot must be generated from the run's bound spec version.
CREATE TRIGGER report_snapshot_context_guard
BEFORE INSERT ON report_snapshot
BEGIN
    SELECT RAISE(ABORT, 'report snapshot run not found in project')
    WHERE NOT EXISTS (
        SELECT 1 FROM run WHERE id = NEW.run_id AND project_id = NEW.project_id
    );
    SELECT RAISE(ABORT, 'report snapshot must bind the run spec version')
    WHERE NEW.spec_version_id != (
        SELECT spec_version_id FROM run
        WHERE id = NEW.run_id AND project_id = NEW.project_id
    );
END;
