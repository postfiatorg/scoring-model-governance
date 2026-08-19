-- Per-round grading identity. Grades live on the round's exam links —
-- never on the shared exam runs, whose rows sit inside earlier rounds'
-- published records — and grading runs link to the rounds they answer
-- for the same way exam runs do, with a per-judge outcome the redraw
-- resume and the blocklist booking read back. The repeat count joins
-- the grading-run identity exactly as migration 015 did for exam runs.
-- Migration 013's exam_runs.final_grade never carried data and loses
-- its last reader here, so it is dropped rather than left standing as
-- a second home for grades.

ALTER TABLE governance_round_exam_runs ADD COLUMN final_grade NUMERIC(4,1);
ALTER TABLE governance_round_exam_runs ADD COLUMN grade_receipts JSONB;

-- hf_repo and outcome are the stage's own bookkeeping: readers join on
-- run_id for run identity, and outcome ('PASSED' / 'FAILED', NULL while
-- undetermined) records each linked judge's fate under its mechanical bar.
CREATE TABLE governance_round_grading_runs (
    round_id INTEGER NOT NULL REFERENCES governance_rounds(id),
    run_id INTEGER NOT NULL REFERENCES grading_runs(id),
    hf_repo TEXT NOT NULL,
    outcome TEXT,
    PRIMARY KEY (round_id, run_id),
    -- One outcome per judge per round: a failed judge sits out the rest
    -- of the round, so a second run for the same judge is a logic error.
    UNIQUE (round_id, hf_repo)
);

CREATE INDEX idx_round_grading_links_run_id ON governance_round_grading_runs(run_id);

ALTER TABLE grading_runs ADD COLUMN repeats INTEGER;
UPDATE grading_runs SET repeats = 3;

ALTER TABLE exam_runs DROP COLUMN final_grade;
