-- Pool refresh persistence: one row per refresh, per-candidate rule
-- outcomes for every considered release, and the standing blocklist
-- mirrored from the curated governance_service/model_blocklist.yaml file.

CREATE TABLE pool_refreshes (
    id SERIAL PRIMARY KEY,
    status TEXT NOT NULL,
    release_used TEXT,
    releases_considered JSONB NOT NULL DEFAULT '[]'::jsonb,
    error_message TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_pool_refreshes_status ON pool_refreshes(status);

CREATE TABLE pool_refresh_candidates (
    id SERIAL PRIMARY KEY,
    refresh_id INTEGER NOT NULL REFERENCES pool_refreshes(id),
    release TEXT,
    livebench_key TEXT,
    display_name TEXT,
    organization TEXT,
    family TEXT,
    global_average DOUBLE PRECISION,
    category_averages JSONB,
    hf_repo TEXT NOT NULL,
    revision TEXT NOT NULL,
    precision TEXT NOT NULL,
    weight_bytes BIGINT NOT NULL,
    license TEXT,
    gated BOOLEAN NOT NULL,
    assigned_gpu TEXT,
    is_incumbent BOOLEAN NOT NULL DEFAULT FALSE,
    in_pool BOOLEAN NOT NULL,
    exclusion_rule TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_pool_refresh_candidates_refresh_id
    ON pool_refresh_candidates(refresh_id);
CREATE INDEX idx_pool_refresh_candidates_in_pool
    ON pool_refresh_candidates(refresh_id, in_pool);

CREATE TABLE blocklist (
    id SERIAL PRIMARY KEY,
    hf_repo TEXT NOT NULL,
    revision TEXT NOT NULL,
    reason TEXT NOT NULL,
    round_reference TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (hf_repo, revision)
);
