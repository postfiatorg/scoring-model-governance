# Exam account readiness — smoke deployments

One smoke deployment per assigned GPU class, run 2026-07-28 through
`scripts/exam_smoke_deploy.py` on the exam runtime manager: deploy the
candidate's pinned profile, verify warm-up, prove one real inference,
tear the app down. The workspace was left with no `governance-exam-*`
apps after both runs (`modal app list` clean).

Both account-readiness questions the roadmap's G.3.3 step names are
answered affirmatively:

- **H200 is available to the workspace** — the first H200 ever requested
  on it cold-started and served.
- **The production-pinned SGLang image serves the full-precision
  challenger's architecture** — Gemma 4 31B (bf16) loaded and answered
  under the same digest-pinned image production serves Qwen with, and
  the explicit `chat_template_kwargs.enable_thinking: false` setting
  produced a plain non-thinking reply.

## H100 — incumbent profile

This run's `reused: true` (5.8s deploy) reflects that a first deploy of
the same profile had already registered the app minutes earlier, so it
exercised the reuse path end to end; the cold start below is the real
first boot.

```json
{
  "app_name": "governance-exam-qwen--qwen3.6-27b-fp8",
  "hf_repo": "Qwen/Qwen3.6-27B-FP8",
  "revision": "e89b16ebf1988b3d6befa7de50abc2d76f26eb09",
  "gpu": "H100",
  "deployed": true,
  "reused": true,
  "deploy_seconds": 5.8,
  "warmup_seconds": 637.7,
  "served_inference": true,
  "inference_content": "ready",
  "torn_down": false,
  "total_seconds": 648.2
}
```

The recorded `torn_down: false` is honest history, not a leak: this run
surfaced that non-interactive `modal app stop` requires `--yes`, the
manager was fixed in the same change set, and the app was stopped
immediately afterward (confirmed via `modal app list`).

## H200 — full-precision challenger profile

```json
{
  "app_name": "governance-exam-google--gemma-4-31b-it",
  "hf_repo": "google/gemma-4-31B-it",
  "revision": "842da3794eaa0b77d5f08bae87a17459d91ff475",
  "gpu": "H200",
  "deployed": true,
  "reused": false,
  "deploy_seconds": 8.8,
  "warmup_seconds": 471.0,
  "served_inference": true,
  "inference_content": "ready",
  "torn_down": true,
  "total_seconds": 481.8
}
```

## Operational notes

- Deploys are seconds when the serving image is already in the Modal
  builder cache (production pins the same image); the wall-clock cost is
  the cold start — weight download plus SGLang startup, ~8-11 minutes at
  these weight sizes.
- Exam apps consume zero GPUs while idle and are deployed sequentially,
  so the workspace's 10-concurrent-GPU limit holds even with every
  production app warm.
- These runs are self-attested operational records; the reproducible
  artifacts are the runtime manager, its tests, and the smoke tool in
  this repository.
