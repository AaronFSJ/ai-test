-- T12: human review queue, review write-back and calibration samples.
--
-- A review is appended, never overwriting history. The current item state is a
-- projection: run_item.review_state overrides the raw scorer state for the
-- report, while the immutable verdict keeps the exact state the scorer produced.
-- A sample_error review maps the item to 'excluded', and execution errors stay
-- 'error'; neither enters the quality denominator.

ALTER TABLE run_item ADD COLUMN actual_json TEXT;
ALTER TABLE run_item ADD COLUMN context_json TEXT;
ALTER TABLE run_item ADD COLUMN review_state TEXT
    CHECK (review_state IS NULL OR review_state IN ('pass', 'fail', 'excluded'));
ALTER TABLE run_item ADD COLUMN reviewed_at TEXT;

ALTER TABLE review ADD COLUMN project_id TEXT;
ALTER TABLE review ADD COLUMN reviewer_id TEXT;
ALTER TABLE review ADD COLUMN prior_state TEXT;
ALTER TABLE review ADD COLUMN resulting_state TEXT;
ALTER TABLE review ADD COLUMN scorer_type TEXT;
ALTER TABLE review ADD COLUMN attribution_label TEXT;
ALTER TABLE review ADD COLUMN evidence_snapshot_json TEXT
    CHECK (evidence_snapshot_json IS NULL OR json_valid(evidence_snapshot_json));
ALTER TABLE review ADD COLUMN calibration_sample_id TEXT;

UPDATE review
SET project_id = (
    SELECT r.project_id
    FROM run_item ri JOIN run r ON r.id = ri.run_id
    WHERE ri.id = review.run_item_id
)
WHERE project_id IS NULL;

CREATE INDEX idx_review_project_created ON review(project_id, created_at);
CREATE INDEX idx_review_item_created ON review(run_item_id, created_at);

-- A review conclusion is sunk into a calibration sample that a scorer
-- evaluation can consume directly. One current calibration sample per item;
-- the full review history remains in the review table.
CREATE TABLE calibration_sample (
    id                 TEXT PRIMARY KEY,
    project_id         TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    review_id          TEXT NOT NULL REFERENCES review(id) ON DELETE CASCADE,
    run_id             TEXT NOT NULL,
    run_item_id        TEXT NOT NULL,
    dataset_id         TEXT,
    dataset_version_id TEXT,
    sample_key         TEXT NOT NULL CHECK (length(trim(sample_key)) > 0),
    input_json         TEXT NOT NULL CHECK (json_valid(input_json)),
    expected_json      TEXT CHECK (expected_json IS NULL OR json_valid(expected_json)),
    actual_json        TEXT CHECK (actual_json IS NULL OR json_valid(actual_json)),
    context_json       TEXT CHECK (context_json IS NULL OR json_valid(context_json)),
    scored_state       TEXT NOT NULL CHECK (scored_state IN ('pass', 'fail', 'uncertain', 'error')),
    expected_state     TEXT CHECK (expected_state IS NULL OR expected_state IN ('pass', 'fail')),
    conclusion         TEXT NOT NULL CHECK (conclusion IN ('confirmed_defect', 'false_positive', 'sample_error')),
    scorer_type        TEXT,
    scorer_name        TEXT,
    scorer_version_id  TEXT,
    score              REAL,
    confidence         REAL,
    evidence_json      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(evidence_json)),
    usable             INTEGER NOT NULL CHECK (usable IN (0, 1)),
    exclusion_reason   TEXT,
    created_by         TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE (run_item_id),
    UNIQUE (id, project_id)
);

CREATE INDEX idx_calibration_project_created ON calibration_sample(project_id, created_at);
CREATE INDEX idx_calibration_scorer_type ON calibration_sample(project_id, scorer_type);
CREATE INDEX idx_calibration_conclusion ON calibration_sample(project_id, conclusion);
