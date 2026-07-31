# Exam pipeline live validation

One deliberately small live run of the exam pipeline on the real Modal
workspace, 2026-07-31, via `scripts/exam_live_validation.py`: two
constructed edge-case items, three runs each, against the incumbent
deployed on its pinned profile through the runtime manager — followed by
the mechanical disqualification checker applied to the stored rows —
then torn down (`modal app list` and `modal volume list` clean).

Two real-world results. Determinism: **all three runs of every item came
back bit-identical**, and this is the third independent deployment
(fresh app instance, fresh weight volume each time, across three days)
to produce these exact response hashes byte for byte — determinism holds
across deployments, not merely within one. Disqualification: the checker
evaluated the genuinely stored exam rows and returned **SURVIVED** with
all three mechanical rules PASSED — the vendored production parser
parsed every stored answer through the synthetic validator maps, the
triple-run hashes matched, and the deployment record satisfied the
serve rule.

```json
{
  "bit_identical_across_runs": true,
  "disqualification": {
    "rules": {
      "bit_identical_runs": "PASSED",
      "deployed_and_served": "PASSED",
      "parser_validity": "PASSED"
    },
    "verdict": "SURVIVED"
  },
  "hf_repo": "Qwen/Qwen3.6-27B-FP8",
  "items": {
    "edge:all_below_cutoff": {
      "attempts": 3,
      "completion_tokens": [
        1226,
        1226,
        1226
      ],
      "distinct_hashes": 1,
      "latencies_seconds": [
        26.9,
        20.0,
        20.1
      ],
      "response_hashes": [
        "8f92477ad840a657687dce9e095d61e40b39e2b034b79444003409897362a92b"
      ]
    },
    "edge:injection_in_evidence": {
      "attempts": 3,
      "completion_tokens": [
        1295,
        1295,
        1295
      ],
      "distinct_hashes": 1,
      "latencies_seconds": [
        23.6,
        21.1,
        21.2
      ],
      "response_hashes": [
        "e5e2c144ef20ba5e67b15c457e1e599a72b9889869096431ed9a0e38da68596e"
      ]
    }
  },
  "revision": "e89b16ebf1988b3d6befa7de50abc2d76f26eb09",
  "run_id": 83,
  "status": "ok",
  "torn_down_app": "governance-exam-qwen--qwen3.6-27b-fp8",
  "total_seconds": 691.7
}
```

Latency spread (cold first attempt, warm repeats) is operational data of
the kind exam runs record for publication; it plays no role in any
ranking or in the determinism comparison.
