-- Thinking-mode eligibility: the curated thinking class each candidate
-- carried when its refresh evaluated it.

ALTER TABLE pool_refresh_candidates
    ADD COLUMN thinking TEXT;
