-- Final publication identity: the pinned complete-record bundle, the
-- on-chain round-close receipt, and the published repository record.

ALTER TABLE governance_rounds ADD COLUMN final_record_cid TEXT;
ALTER TABLE governance_rounds ADD COLUMN receipt_tx_hash TEXT;
ALTER TABLE governance_rounds ADD COLUMN record_commit_url TEXT;
