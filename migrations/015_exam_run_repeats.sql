-- The repeat count joins the exam run identity: stored outputs are only
-- reusable under the repeat count they were produced with — reusing a
-- three-run exam under a different frozen repeat_count would fail the
-- determinism rule on attempt counts alone, mechanically disqualifying
-- candidates with no real failure. Every run recorded before this column
-- existed was produced with the original repeat count of three.

ALTER TABLE exam_runs ADD COLUMN repeats INTEGER;
UPDATE exam_runs SET repeats = 3;
