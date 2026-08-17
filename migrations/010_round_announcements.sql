-- On-chain announcement identity: the memo transaction, its validated
-- ledger (the judge-draw anchor), and the absolute windows derived at
-- emission. commit_closes_at exists since migration 008.

ALTER TABLE governance_rounds ADD COLUMN announcement_tx_hash TEXT;
ALTER TABLE governance_rounds ADD COLUMN announcement_ledger_index BIGINT;
ALTER TABLE governance_rounds ADD COLUMN commit_opens_at TIMESTAMPTZ;
ALTER TABLE governance_rounds ADD COLUMN reveal_opens_at TIMESTAMPTZ;
ALTER TABLE governance_rounds ADD COLUMN reveal_closes_at TIMESTAMPTZ;
