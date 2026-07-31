-- Mechanical disqualification: the per-run verdict and its rule evidence.

ALTER TABLE exam_runs
    ADD COLUMN verdict TEXT,
    ADD COLUMN verdict_evidence JSONB,
    ADD COLUMN verdict_at TIMESTAMPTZ;
