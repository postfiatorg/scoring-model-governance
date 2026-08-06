-- Round orchestration: the governance round lifecycle and its persisted schedule.

CREATE TABLE governance_rounds (
    id SERIAL PRIMARY KEY,
    round_number INTEGER NOT NULL UNIQUE,
    status TEXT NOT NULL,
    trigger_source TEXT NOT NULL,
    commit_closes_at TIMESTAMPTZ,
    error_message TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE governance_round_schedule (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_due_at TIMESTAMPTZ NOT NULL
);

ALTER TABLE exam_runs ADD COLUMN round_id INTEGER REFERENCES governance_rounds(id);
ALTER TABLE grading_runs ADD COLUMN round_id INTEGER REFERENCES governance_rounds(id);

CREATE INDEX idx_exam_runs_round_id ON exam_runs(round_id);
CREATE INDEX idx_grading_runs_round_id ON grading_runs(round_id);
