-- The round's examinee evidence set: which exam run answers for each pool
-- member in this round. Exam runs are reusable across rounds and a reused
-- terminal run keeps the round_id that paid for it, so the runs a round
-- decides and publishes over are linked here instead of being inferred
-- from exam_runs.round_id.

-- hf_repo exists only to make one-run-per-pool-member a primary-key
-- guarantee; readers join on run_id and take hf_repo from exam_runs.
CREATE TABLE governance_round_exam_runs (
    round_id INTEGER NOT NULL REFERENCES governance_rounds(id),
    run_id INTEGER NOT NULL REFERENCES exam_runs(id),
    hf_repo TEXT NOT NULL,
    PRIMARY KEY (round_id, hf_repo)
);

CREATE INDEX idx_round_exam_links_run_id ON governance_round_exam_runs(run_id);
