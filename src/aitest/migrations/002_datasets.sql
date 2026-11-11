-- T08: dataset and versioned sample management.
--
-- A dataset owns a mutable working set of samples. Every mutation publishes a
-- new immutable dataset_version containing a full sample snapshot, so a Run
-- that binds an older version keeps seeing exactly the samples it was scored
-- against. Samples keep a stable logical key (sample_id) inside a dataset.

CREATE TABLE dataset (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name        TEXT NOT NULL CHECK (length(trim(name)) > 0),
    description TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (project_id, name),
    UNIQUE (id, project_id)
);

CREATE TABLE dataset_version (
    id             TEXT PRIMARY KEY,
    dataset_id     TEXT NOT NULL,
    project_id     TEXT NOT NULL,
    version        INTEGER NOT NULL CHECK (version >= 1),
    sample_count   INTEGER NOT NULL CHECK (sample_count >= 0),
    content_digest TEXT NOT NULL CHECK (length(content_digest) = 64),
    source         TEXT NOT NULL CHECK (source IN ('import', 'api', 'manual')),
    import_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(import_json)),
    created_by     TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE (dataset_id, version),
    UNIQUE (id, project_id),
    FOREIGN KEY (dataset_id, project_id) REFERENCES dataset(id, project_id),
    FOREIGN KEY (created_by, project_id) REFERENCES member(id, project_id)
);

CREATE TABLE sample (
    id           TEXT PRIMARY KEY,
    dataset_id   TEXT NOT NULL,
    project_id   TEXT NOT NULL,
    sample_key   TEXT NOT NULL CHECK (length(trim(sample_key)) > 0),
    input_json   TEXT NOT NULL CHECK (json_valid(input_json)),
    expected_json TEXT CHECK (expected_json IS NULL OR json_valid(expected_json)),
    context_json TEXT CHECK (context_json IS NULL OR json_valid(context_json)),
    context_ref  TEXT,
    priority     TEXT NOT NULL CHECK (priority IN ('P0', 'P1')),
    tags_json    TEXT NOT NULL DEFAULT '[]'
                 CHECK (json_valid(tags_json) AND json_type(tags_json) = 'array'),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE (dataset_id, sample_key),
    UNIQUE (id, project_id),
    FOREIGN KEY (dataset_id, project_id) REFERENCES dataset(id, project_id)
);

-- Immutable snapshot rows. Export and Run detail read from here, never from
-- the mutable sample table, which is what keeps historical Runs stable.
CREATE TABLE dataset_version_sample (
    id                 TEXT PRIMARY KEY,
    dataset_version_id TEXT NOT NULL REFERENCES dataset_version(id) ON DELETE CASCADE,
    sample_key         TEXT NOT NULL CHECK (length(trim(sample_key)) > 0),
    input_json         TEXT NOT NULL CHECK (json_valid(input_json)),
    expected_json      TEXT CHECK (expected_json IS NULL OR json_valid(expected_json)),
    context_json       TEXT CHECK (context_json IS NULL OR json_valid(context_json)),
    context_ref        TEXT,
    priority           TEXT NOT NULL CHECK (priority IN ('P0', 'P1')),
    tags_json          TEXT NOT NULL DEFAULT '[]'
                       CHECK (json_valid(tags_json) AND json_type(tags_json) = 'array'),
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE (dataset_version_id, sample_key)
);

CREATE INDEX idx_dataset_project ON dataset(project_id);
CREATE INDEX idx_dataset_version_dataset ON dataset_version(dataset_id, version);
CREATE INDEX idx_dataset_version_project ON dataset_version(project_id);
CREATE INDEX idx_sample_dataset ON sample(dataset_id, sample_key);
CREATE INDEX idx_sample_priority ON sample(dataset_id, priority);
CREATE INDEX idx_dataset_version_sample_dv ON dataset_version_sample(dataset_version_id);

-- Dataset versions are immutable, exactly like spec versions.
CREATE TRIGGER dataset_version_no_update
BEFORE UPDATE ON dataset_version
BEGIN
    SELECT RAISE(ABORT, 'dataset_version is immutable; publish a new version');
END;

CREATE TRIGGER dataset_version_no_delete
BEFORE DELETE ON dataset_version
BEGIN
    SELECT RAISE(ABORT, 'dataset_version is immutable; delete the owning project instead');
END;

CREATE TRIGGER dataset_version_contiguous
BEFORE INSERT ON dataset_version
BEGIN
    SELECT RAISE(ABORT, 'dataset_version must be the next contiguous version')
    WHERE NEW.version != (
        SELECT COALESCE(MAX(version), 0) + 1
        FROM dataset_version
        WHERE dataset_id = NEW.dataset_id
    );
END;

CREATE TRIGGER dataset_version_sample_no_update
BEFORE UPDATE ON dataset_version_sample
BEGIN
    SELECT RAISE(ABORT, 'dataset_version_sample is immutable');
END;

CREATE TRIGGER dataset_version_sample_no_delete
BEFORE DELETE ON dataset_version_sample
BEGIN
    SELECT RAISE(ABORT, 'dataset_version_sample is immutable');
END;
