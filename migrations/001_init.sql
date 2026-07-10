-- Seed file for the numbered-migration convention. The migration runner
-- creates this table itself before applying migrations; recording 001 as
-- applied exercises the full apply-and-record path on a fresh database.
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
