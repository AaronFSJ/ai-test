-- T10: multi-sample evaluation and consistency aggregation.
--
-- A Run keeps the sampling configuration it was created with in
-- run.sampling_config_json. The configuration actually used at scoring time
-- (defaults applied, model snapshot resolved, concurrency recorded) is written
-- to run.sampling_effective_json so a historical Run can be reproduced and
-- explained without recomputing defaults.
--
-- Every repetition is stored as an immutable run_item_sample row. The
-- aggregated conclusion stays on run_item/verdict and is explained by
-- verdict.aggregation_json (strategy, counts, pass ratio, mean, variance).

ALTER TABLE run ADD COLUMN sampling_effective_json TEXT
    CHECK (sampling_effective_json IS NULL OR json_valid(sampling_effective_json));

ALTER TABLE verdict ADD COLUMN sample_count INTEGER NOT NULL DEFAULT 1
    CHECK (sample_count >= 1);

ALTER TABLE verdict ADD COLUMN aggregation_json TEXT
    CHECK (aggregation_json IS NULL OR json_valid(aggregation_json));

CREATE TABLE run_item_sample (
    id                TEXT PRIMARY KEY,
    run_item_id       TEXT NOT NULL REFERENCES run_item(id) ON DELETE CASCADE,
    run_id            TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    sample_index      INTEGER NOT NULL CHECK (sample_index >= 1),
    state             TEXT NOT NULL CHECK (state IN ('pass', 'fail', 'uncertain', 'error')),
    confidence        REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    score             REAL CHECK (score IS NULL OR (score >= 0 AND score <= 1)),
    duration_ms       INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    actual_digest     TEXT CHECK (actual_digest IS NULL OR length(actual_digest) = 64),
    scorer_trace_json TEXT NOT NULL CHECK (json_valid(scorer_trace_json)),
    cost_json         TEXT CHECK (cost_json IS NULL OR json_valid(cost_json)),
    created_at        TEXT NOT NULL,
    UNIQUE (run_item_id, sample_index)
);

CREATE INDEX idx_run_item_sample_item ON run_item_sample(run_item_id, sample_index);
CREATE INDEX idx_run_item_sample_run ON run_item_sample(run_id);

-- A recorded repetition is evidence: later edits would make the aggregate
-- conclusion unexplainable.
CREATE TRIGGER run_item_sample_no_update
BEFORE UPDATE ON run_item_sample
BEGIN
    SELECT RAISE(ABORT, 'run_item_sample is immutable; scoring is not repeated in place');
END;
