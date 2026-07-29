# Exam engine live validation

One deliberately small live run of the exam execution engine on the real
Modal workspace, 2026-07-28, via `scripts/exam_live_validation.py`: two
constructed edge-case items, three runs each, against the incumbent
deployed on its pinned profile through the runtime manager, torn down
afterward (`modal app list` and `modal volume list` clean).

The result is the first real-world data point for the methodology's
determinism discipline on the exam path: **all three runs of every item
came back bit-identical** — one distinct canonical response hash per
item, with identical completion-token counts across runs. An earlier
run of the same fragment on a separate deployment (fresh app instance,
fresh weight volume, different container, hours apart) produced the
same response hashes byte for byte, so determinism held across
independent deployments, not merely within one.

```json
{
  "bit_identical_across_runs": true,
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
        28.9,
        20.8,
        20.5
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
        24.8,
        21.8,
        21.8
      ],
      "response_hashes": [
        "e5e2c144ef20ba5e67b15c457e1e599a72b9889869096431ed9a0e38da68596e"
      ]
    }
  },
  "revision": "e89b16ebf1988b3d6befa7de50abc2d76f26eb09",
  "run_id": 27,
  "status": "ok",
  "torn_down_app": "governance-exam-qwen--qwen3.6-27b-fp8",
  "total_seconds": 713.5
}
```

Latency spread (cold first attempt, warm repeats) is operational data of
the kind exam runs record for publication; it plays no role in any
ranking or in the determinism comparison.
