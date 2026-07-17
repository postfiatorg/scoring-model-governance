-- Published refresh records: upstream snapshot provenance and the
-- publication state of each refresh's public record.

ALTER TABLE pool_refreshes
    ADD COLUMN snapshots JSONB,
    ADD COLUMN snapshots_cid TEXT,
    ADD COLUMN publication_status TEXT,
    ADD COLUMN publication_error TEXT,
    ADD COLUMN record_commit_urls JSONB;
