-- Freeze and IPFS publication: the frozen round package identity on the
-- round, and the persisted package files served over HTTPS.

ALTER TABLE governance_rounds ADD COLUMN package_cid TEXT;
ALTER TABLE governance_rounds ADD COLUMN package_hash TEXT;
ALTER TABLE governance_rounds ADD COLUMN frozen_at TIMESTAMPTZ;

CREATE TABLE governance_round_artifacts (
    id SERIAL PRIMARY KEY,
    round_id INTEGER NOT NULL REFERENCES governance_rounds(id),
    path TEXT NOT NULL,
    content JSONB NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (round_id, path)
);
