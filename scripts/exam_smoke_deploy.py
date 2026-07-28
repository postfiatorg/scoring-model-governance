"""Account-readiness smoke deployment for the exam runtime.

Deploys one candidate per assigned GPU class through the exam runtime
manager, proves the endpoint serves a real inference, and tears the app
down — leaving the workspace unchanged. Run per candidate:

    PYTHONPATH=. python scripts/exam_smoke_deploy.py "Qwen/Qwen3.6-27B-FP8" out.json

Requires a Modal CLI login for deployment and MODAL_KEY / MODAL_SECRET in
the environment for the proxy-authenticated inference probe.
"""

import json
import sys
import time
from dataclasses import dataclass, field

import httpx

from governance_service.services.candidate_profiles import CURRENT_POOL_PROFILES
from governance_service.services.runtime_manager import (
    ExamRuntimeManager,
    candidate_app_name,
    proxy_auth_headers,
)


@dataclass
class SmokeResult:
    """One smoke deployment's outcome, recorded in the readiness artifact."""

    hf_repo: str
    revision: str
    gpu: str
    app_name: str
    deployed: bool = False
    served_inference: bool = False
    torn_down: bool = False
    detail: str = ""
    extra: dict = field(default_factory=dict)

SMOKE_PROMPT = "Reply with exactly the word: ready"
INFERENCE_TIMEOUT_SECONDS = 600.0


def smoke_one(hf_repo: str) -> SmokeResult:
    profile = CURRENT_POOL_PROFILES[hf_repo]
    manager = ExamRuntimeManager()
    result = SmokeResult(
        hf_repo=hf_repo,
        revision=profile.revision,
        gpu=profile.gpu,
        app_name=candidate_app_name(hf_repo),
    )
    started = time.monotonic()
    try:
        ensured = manager.ensure_deployed(profile)
        result.app_name = ensured.app_name
        result.deployed = True
        result.extra["reused"] = ensured.reused
        result.extra["deploy_seconds"] = round(time.monotonic() - started, 1)

        warm_started = time.monotonic()
        manager.verify_warmup(profile, ensured)
        result.extra["warmup_seconds"] = round(time.monotonic() - warm_started, 1)

        body = {
            "model": profile.hf_repo,
            "messages": [{"role": "user", "content": SMOKE_PROMPT}],
            "temperature": 0,
            "max_tokens": 32,
            **profile.extra_body,
        }
        response = httpx.post(
            f"{ensured.endpoint_url.rstrip('/')}/v1/chat/completions",
            json=body,
            headers=proxy_auth_headers(),
            timeout=INFERENCE_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
        result.served_inference = True
        result.extra["inference_content"] = content.strip()[:200]
    except Exception as exc:  # recorded, not raised — the artifact carries the truth
        result.detail = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            manager.teardown(hf_repo)
            result.torn_down = True
        except Exception as exc:
            result.detail = f"{result.detail} | teardown failed: {exc}".strip(" |")
        if not result.detail:
            result.detail = "deployed, served, torn down"
        result.extra["total_seconds"] = round(time.monotonic() - started, 1)
    return result


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in CURRENT_POOL_PROFILES:
        known = ", ".join(sorted(CURRENT_POOL_PROFILES))
        print(f"usage: exam_smoke_deploy.py <hf_repo> <output.json>\nknown repos: {known}")
        return 2
    hf_repo, output_path = sys.argv[1], sys.argv[2]
    result = smoke_one(hf_repo)
    document = {
        "hf_repo": result.hf_repo,
        "revision": result.revision,
        "gpu": result.gpu,
        "app_name": result.app_name,
        "deployed": result.deployed,
        "served_inference": result.served_inference,
        "torn_down": result.torn_down,
        "detail": result.detail,
        **result.extra,
    }
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0 if (result.served_inference and result.torn_down) else 1


if __name__ == "__main__":
    sys.exit(main())
