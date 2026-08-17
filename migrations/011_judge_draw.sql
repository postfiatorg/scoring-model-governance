-- The judge draw identity: the drawn judge and the drawing ledger whose
-- hash selected it, recomputable by anyone from the on-chain
-- announcement and the frozen package.

ALTER TABLE governance_rounds ADD COLUMN judge_hf_repo TEXT;
ALTER TABLE governance_rounds ADD COLUMN draw_ledger_index BIGINT;
ALTER TABLE governance_rounds ADD COLUMN draw_ledger_hash TEXT;
