# Exam stage live validation

One deliberately small live run of the wired exam stage (G.5.8) on the
real Modal workspace, 2026-08-18, via
`scripts/exam_stage_live_validation.py`: a fabricated local round with a
genuinely persisted frozen package — a two-item corpus (devnet scoring
round 324's frozen input package, fetched and hash-verified live from
the scoring service, plus the `all_below_cutoff` constructed case) and a
minimal pool with the incumbent as the single examinee and a
never-deployed stand-in challenger fixed as the drawn judge — driven
through the real `exam_stage.run_exam` twice, then torn down. The stage
does not re-check freeze eligibility (that is the freeze's job, by
design), which is what makes the one-examinee pool a valid fragment.

The validation ran twice that day, and the interruption between the two
runs became evidence of its own. The first run executed the full cold
path — Modal deployment with verified warm-up, all six inferences, 659.1
seconds end to end. A later run of the final code started from scratch
(the test suite's database-wiping fixtures had cleared the earlier rows)
and was killed mid-exam; the recorded run below then **resumed that
interrupted attempt for real**: it reused the still-deployed Modal app,
kept every stored inference, completed only the missing ones, and
finished in 128.6 seconds — the stage's restart economics working on
genuinely paid GPU work, not a simulation.

Three results from the recorded run. Material reconstruction: the stage
rebuilt its exam items entirely from the frozen artifacts — the
historical request re-fetched by its pinned CID and verified against the
recorded package hashes, the constructed case checked against the
manifest's content hash — with the frozen historical validator map
driving the parser exactly as the synthetic map drove it for the
constructed case. Determinism and verdict: **all three runs of both
items came back bit-identical**, and both response hashes reproduced the
same-day cold-start run's hashes byte for byte — the constructed case
also matching the G.3 validation recorded on 2026-07-31, now across
multiple independent deployments — with mechanical disqualification
returning **SURVIVED** over the genuinely stored rows, persisted on the
run the round links in `governance_round_exam_runs`. Reuse: the
script's second `run_exam` pass **reused every stored inference and the
persisted verdict** — zero new Modal calls, no re-verdicting — and
converged on the identical links.

```json
{
  "bit_identical_across_runs": true,
  "corpus": {
    "constructed_case": "all_below_cutoff",
    "historical_round": 324,
    "input_package_cid": "QmP7ESnrTfHKZM6icCqCSJTcV9yYQjveFwaTrzo1Tdq34h"
  },
  "examinee": "Qwen/Qwen3.6-27B-FP8",
  "first_pass": {
    "examinees": 1,
    "survived": 1,
    "verdicts": {
      "Qwen/Qwen3.6-27B-FP8": "SURVIVED"
    }
  },
  "items": {
    "edge:all_below_cutoff": {
      "attempts": 3,
      "distinct_hashes": 1,
      "response_hashes": [
        "8f92477ad840a657687dce9e095d61e40b39e2b034b79444003409897362a92b"
      ]
    },
    "round-324": {
      "attempts": 3,
      "distinct_hashes": 1,
      "response_hashes": [
        "2978ba0751433a5c410ca82435f929b4eb13e97ddaf663671891c3a56f7fd554"
      ]
    }
  },
  "judge_stand_in": "Qwen/Qwen3-32B-FP8",
  "links": [
    {
      "hf_repo": "Qwen/Qwen3.6-27B-FP8",
      "run_id": 1091,
      "status": "COMPLETED",
      "verdict": "SURVIVED"
    }
  ],
  "rerun_reused_all_inferences": true,
  "revision": "e89b16ebf1988b3d6befa7de50abc2d76f26eb09",
  "round": {
    "id": 1726,
    "round_number": 2
  },
  "scoring_api": "https://scoring-devnet.postfiat.org",
  "second_pass": {
    "examinees": 1,
    "survived": 1,
    "verdicts": {
      "Qwen/Qwen3.6-27B-FP8": "SURVIVED"
    }
  },
  "status": "ok",
  "torn_down_app": "governance-exam-qwen--qwen3.6-27b-fp8",
  "total_seconds": 128.6
}
```

The validation round is marked `ABANDONED` by the script itself; the
permanent evidence is this document — local development rows do not
survive test-suite runs, whose fixtures wipe the shared database.
