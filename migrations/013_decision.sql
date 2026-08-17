-- The decision identity: verdict, winner, rationale, and the per-candidate
-- final grade the grading-stage wiring populates for the decision to read.

ALTER TABLE governance_rounds ADD COLUMN decision TEXT;
ALTER TABLE governance_rounds ADD COLUMN winner_hf_repo TEXT;
ALTER TABLE governance_rounds ADD COLUMN decision_rationale JSONB;
ALTER TABLE governance_rounds ADD COLUMN decided_at TIMESTAMPTZ;

ALTER TABLE exam_runs ADD COLUMN final_grade NUMERIC(4,1);
