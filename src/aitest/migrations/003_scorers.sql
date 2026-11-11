-- T09: scorer framework, built-in scorers and combinators.
--
-- A scorer is a project-scoped logical name. Its configuration and rubric are
-- written as immutable scorer_version rows; editing a config or rubric adds a
-- new version and leaves every historical Run trace pointing at the version it
-- actually used.
--
-- A scorer_profile groups scorers with a combinator (and / or / weighted /
-- majority). Profile membership is also immutable and versioned, so a Run can
-- be traced back to both the profile version and the exact scorer versions.

CREATE TABLE scorer (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name        TEXT NOT NULL CHECK (length(trim(name)) > 0),
    description TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (project_id, name),
    UNIQUE (id, project_id)
);

CREATE TABLE scorer_version (
    id            TEXT PRIMARY KEY,
    scorer_id     TEXT NOT NULL,
    project_id    TEXT NOT NULL,
    version       INTEGER NOT NULL CHECK (version >= 1),
    type          TEXT NOT NULL CHECK (type IN (
                      'exact_match', 'regex', 'json_schema', 'rule_assertion',
                      'semantic_similarity', 'llm_judge', 'human_review')),
    config_json   TEXT NOT NULL CHECK (json_valid(config_json) AND json_type(config_json) = 'object'),
    rubric_text   TEXT,
    rubric_digest TEXT CHECK (rubric_digest IS NULL OR length(rubric_digest) = 64),
    frozen_digest TEXT NOT NULL CHECK (length(frozen_digest) = 64),
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (scorer_id, version),
    UNIQUE (id, project_id),
    FOREIGN KEY (scorer_id, project_id) REFERENCES scorer(id, project_id),
    FOREIGN KEY (created_by, project_id) REFERENCES member(id, project_id),
    CHECK (
        (type = 'llm_judge' AND rubric_text IS NOT NULL AND length(trim(rubric_text)) > 0)
        OR type != 'llm_judge'
    )
);

CREATE TABLE scorer_profile (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name        TEXT NOT NULL CHECK (length(trim(name)) > 0),
    description TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (project_id, name),
    UNIQUE (id, project_id)
);

CREATE TABLE scorer_profile_version (
    id            TEXT PRIMARY KEY,
    profile_id    TEXT NOT NULL,
    project_id    TEXT NOT NULL,
    version       INTEGER NOT NULL CHECK (version >= 1),
    combinator    TEXT NOT NULL CHECK (combinator IN ('and', 'or', 'weighted', 'majority')),
    config_json   TEXT NOT NULL CHECK (json_valid(config_json) AND json_type(config_json) = 'object'),
    members_json  TEXT NOT NULL CHECK (json_valid(members_json) AND json_type(members_json) = 'array'),
    frozen_digest TEXT NOT NULL CHECK (length(frozen_digest) = 64),
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (profile_id, version),
    UNIQUE (id, project_id),
    FOREIGN KEY (profile_id, project_id) REFERENCES scorer_profile(id, project_id),
    FOREIGN KEY (created_by, project_id) REFERENCES member(id, project_id),
    CHECK (json_array_length(members_json) >= 1)
);

-- A binding attaches a profile to a whole dataset (sample_key IS NULL) or to
-- one sample. A sample binding overrides the dataset default.
CREATE TABLE scorer_binding (
    id                TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL,
    dataset_id        TEXT NOT NULL,
    sample_key        TEXT,
    profile_id        TEXT NOT NULL,
    profile_version_id TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (id, project_id),
    FOREIGN KEY (dataset_id, project_id) REFERENCES dataset(id, project_id) ON DELETE CASCADE,
    FOREIGN KEY (dataset_id, sample_key) REFERENCES sample(dataset_id, sample_key) ON DELETE CASCADE,
    FOREIGN KEY (profile_id, project_id) REFERENCES scorer_profile(id, project_id) ON DELETE CASCADE,
    FOREIGN KEY (profile_version_id, project_id)
        REFERENCES scorer_profile_version(id, project_id) ON DELETE CASCADE
);

CREATE INDEX idx_scorer_project ON scorer(project_id);
CREATE INDEX idx_scorer_version_scorer ON scorer_version(scorer_id, version);
CREATE INDEX idx_scorer_version_project ON scorer_version(project_id);
CREATE INDEX idx_scorer_profile_project ON scorer_profile(project_id);
CREATE INDEX idx_scorer_profile_version_profile ON scorer_profile_version(profile_id, version);
CREATE INDEX idx_scorer_profile_version_project ON scorer_profile_version(project_id);
CREATE INDEX idx_scorer_binding_dataset ON scorer_binding(dataset_id, sample_key);
CREATE UNIQUE INDEX idx_scorer_binding_default
    ON scorer_binding(dataset_id) WHERE sample_key IS NULL;
CREATE UNIQUE INDEX idx_scorer_binding_sample
    ON scorer_binding(dataset_id, sample_key) WHERE sample_key IS NOT NULL;

-- Scorer versions are immutable, like spec versions.
CREATE TRIGGER scorer_version_no_update
BEFORE UPDATE ON scorer_version
BEGIN
    SELECT RAISE(ABORT, 'scorer_version is immutable; create a new version');
END;

CREATE TRIGGER scorer_version_no_delete
BEFORE DELETE ON scorer_version
BEGIN
    SELECT RAISE(ABORT, 'scorer_version is immutable; delete the owning project instead');
END;

CREATE TRIGGER scorer_version_contiguous
BEFORE INSERT ON scorer_version
BEGIN
    SELECT RAISE(ABORT, 'scorer_version must be the next contiguous version')
    WHERE NEW.version != (
        SELECT COALESCE(MAX(version), 0) + 1
        FROM scorer_version
        WHERE scorer_id = NEW.scorer_id
    );
END;

CREATE TRIGGER scorer_profile_version_no_update
BEFORE UPDATE ON scorer_profile_version
BEGIN
    SELECT RAISE(ABORT, 'scorer_profile_version is immutable; create a new version');
END;

CREATE TRIGGER scorer_profile_version_no_delete
BEFORE DELETE ON scorer_profile_version
BEGIN
    SELECT RAISE(ABORT, 'scorer_profile_version is immutable; delete the owning project instead');
END;

CREATE TRIGGER scorer_profile_version_contiguous
BEFORE INSERT ON scorer_profile_version
BEGIN
    SELECT RAISE(ABORT, 'scorer_profile_version must be the next contiguous version')
    WHERE NEW.version != (
        SELECT COALESCE(MAX(version), 0) + 1
        FROM scorer_profile_version
        WHERE profile_id = NEW.profile_id
    );
END;
