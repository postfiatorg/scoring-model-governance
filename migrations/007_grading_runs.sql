-- Grading execution: one run per (judge, material), one row per inference.

CREATE TABLE grading_runs (
    id SERIAL PRIMARY KEY,
    hf_repo TEXT NOT NULL,
    revision TEXT NOT NULL,
    profile_hash TEXT NOT NULL,
    material_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    judge_failure JSONB,
    error_message TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE grading_outputs (
    id SERIAL PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES grading_runs(id),
    item_id TEXT NOT NULL,
    answer_hash TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    response_hash TEXT NOT NULL,
    raw_response TEXT NOT NULL,
    latency_seconds DOUBLE PRECISION NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, item_id, answer_hash, attempt)
);
