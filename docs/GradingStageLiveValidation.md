# Grading stage live validation

Live runs of the wired grading stage (G.5.9) on the real Modal
workspace, 2026-08-18, via `scripts/grading_stage_live_validation.py`:
a fabricated local round with a genuinely persisted frozen package over
a two-item corpus (devnet scoring round 324's frozen input package,
fetched and hash-verified live, plus the `all_below_cutoff` constructed
case), the incumbent as the single examinee, and one real challenger
fixed as the drawn judge. The exam stage produced the survivor answers
through the real engine, then `grading_stage.run_grading` ran for real —
twice per attempt, to prove reuse. The validation ended up recording
three distinct real-world behaviors, two of them unplanned.

## A real judge failure, redraw, and abandonment (gemma-4-31B)

The first run drew `google/gemma-4-31B-it` — the first live exercise of
any challenger as judge under grading prompt v2. Every one of its
outputs **parsed cleanly under the judge defect schema**, but it failed
the mechanical bar on **repeat determinism**: the round-324 pair's
first attempt (hash `bd8cd487…`, 690 bytes) differed from attempts two
and three (hash `11d3ca8c…`, 679 bytes), while the constructed pair was
bit-identical across all three. A judge that cannot grade bit-identically
is mechanically unfit, and the stage did exactly what the methodology
prescribes: recorded the outcome on the round's grading link, applied
the frozen redraw ordering, found the single-challenger validation pool
exhausted, raised `RoundAbandoned`, and **booked gemma's pinned revision
(`842da379…`) into the blocklist** as the abandonment's only outcome —
the redraw and abandonment paths executing live, not simulated. This is
also pool evidence beyond this task: the same nondeterminism would
disqualify gemma as an exam candidate in a real round.

## Two fail-closed catches, both fixed in this change

The next attempt promoted `Qwen/Qwen3-32B-FP8` as judge — it **passed
its mechanical bar live** — and then the grade computation fail-closed
twice on real devnet material, exactly as designed:

- **Unknown scoring era.** Devnet round 324 embeds scoring prompt
  **v10** (the agreement `incomplete` data-quality flags), which
  postdates the G.4 rules-table curation. The checker refused the
  unknown instructions hash rather than guessing; the v10 row is now
  curated (`governance_service/scoring_rules.yaml`, rationale in
  `docs/MechanicalGradingChecker.md`) and machine-validated against the
  upstream prompt by the Vendor Freshness workflow.
- **Present-but-null evidence.** Devnet's validator entries carry
  `identity` as null on every validator, which the mis-curation guard
  read as a wrong field name. The guard now distinguishes a key the
  format never carries (mis-curation, still a hard error) from a field
  that is legitimately null everywhere (real data; null values already
  exclude a validator from that dimension's comparisons).

## The recorded run (Qwen3-32B judge, grades produced)

```json
{
  "corpus": {
    "constructed_case": "all_below_cutoff",
    "historical_round": 324
  },
  "exam": {
    "examinees": 1,
    "survived": 1,
    "verdicts": {
      "Qwen/Qwen3.6-27B-FP8": "SURVIVED"
    }
  },
  "examinee": "Qwen/Qwen3.6-27B-FP8",
  "first_pass": {
    "graded": 1,
    "grades": {
      "Qwen/Qwen3.6-27B-FP8": "22.5"
    },
    "judge_hf_repo": "Qwen/Qwen3-32B-FP8",
    "redraws": 0
  },
  "grades": [
    {
      "final_grade": "22.5",
      "hf_repo": "Qwen/Qwen3.6-27B-FP8",
      "item_grades": [
        {
          "condition": "defect_across_the_set",
          "defects": 4,
          "grade": 20,
          "item_id": "round-324"
        },
        {
          "condition": "defect_across_the_set",
          "defects": 3,
          "grade": 25,
          "item_id": "edge:all_below_cutoff"
        }
      ]
    }
  ],
  "grading_links": [
    {
      "hf_repo": "Qwen/Qwen3-32B-FP8",
      "outcome": "PASSED",
      "run_id": 1,
      "status": "COMPLETED"
    }
  ],
  "judge": "Qwen/Qwen3-32B-FP8",
  "judge_determinism": [
    {
      "attempts": 6,
      "bit_identical": true,
      "pairs": 2,
      "response_hashes": [
        "1a2279eec9eb8f2d80cc5da039ec18b8fbd9ed4df2c72e054772f11db5faa11a",
        "6fa211e653f64b2843dbba2b2ce018ebd1d2fc919dfa327c85f7f58e7e1fd073"
      ],
      "run_id": 1
    }
  ],
  "judge_revision": "aa55da1ecc13d006e8b8e4f54579b1ea8c3db2df",
  "rerun_reused_all_judge_inferences": true,
  "round": {
    "id": 1,
    "round_number": 1
  },
  "scoring_api": "https://scoring-devnet.postfiat.org",
  "second_pass": {
    "graded": 1,
    "grades": {
      "Qwen/Qwen3.6-27B-FP8": "22.5"
    },
    "judge_hf_repo": "Qwen/Qwen3-32B-FP8",
    "redraws": 0
  },
  "status": "ok",
  "torn_down_apps": [
    "governance-exam-qwen--qwen3.6-27b-fp8",
    "governance-exam-qwen--qwen3-32b-fp8"
  ],
  "total_seconds": 1251.0
}
```

Three results. Judge determinism and schema: all six judge inferences
(two pairs × three repeats) came back **bit-identical and schema-valid**
— a real challenger passing the judge bar live. Grades: the incumbent's
final grade of **22.5** is the checker/judge/formula split working as
one — the receipts persisted on the round's exam link merge judge-owned
kinds (`false_claim` systemic on both items, `ignored_evidence`,
`report_mismatch`) with a checker-owned `ordering_violation` on
reliability, and grade formula v1's across-the-set condition placed both
items in the 20–35 band. The grade's absolute level is a data point for
the G.7 rehearsal, not a verdict — a two-item fragment under one judge
carries no margin decision. Reuse: the second `run_grading` pass reused
**every stored judge inference** — zero new Modal calls — and recomputed
byte-identical grades.

The validation rounds are marked `ABANDONED` by the script; the runs
live in a dedicated `governance_live` database so test-suite fixtures
(which wipe the default development database) cannot erase the evidence
between passes. The permanent record is this document.
