"""Mapping and schema freshness check.

Runs one real sourcing pass against the latest LiveBench release and fails
when an open-weight leaderboard model has no mapping entry or the upstream
data contract no longer parses. Run as:

    python -m governance_service.freshness
"""

import sys

import httpx

from governance_service.clients.huggingface import HuggingFaceError
from governance_service.clients.livebench import (
    LiveBenchRequestError,
    LiveBenchSchemaError,
)
from governance_service.config import settings
from governance_service.services.candidate_sourcing import (
    MappingError,
    source_candidates,
)


def run(client: httpx.Client | None = None) -> int:
    """One freshness pass; returns a process exit code."""
    owned_client = client is None
    if owned_client:
        client = httpx.Client(
            timeout=settings.http_timeout_seconds, follow_redirects=True
        )
    try:
        report = source_candidates(client)
    except (LiveBenchRequestError, LiveBenchSchemaError, HuggingFaceError, MappingError) as exc:
        print(f"[freshness] FAILED: {exc}")
        return 1
    finally:
        if owned_client:
            client.close()

    print(f"[freshness] Release: {report.release}")
    for candidate in report.candidates:
        gpu = candidate.assigned_gpu or "no single-GPU fit"
        print(
            f"[freshness] {candidate.livebench_key}: {candidate.hf_repo}"
            f"@{candidate.revision[:12]} ({candidate.precision.value}, {gpu})"
        )
    for key, reason in report.skipped.items():
        print(f"[freshness] skipped {key}: {reason}")
    for snap in report.snapshots:
        print(f"[freshness] snapshot {snap.name}: sha256={snap.sha256}")

    if report.unmapped:
        print(f"[freshness] FAILED: unmapped open-weight models: {report.unmapped}")
        return 1
    print(
        f"[freshness] OK: {len(report.candidates)} mapped, "
        f"{len(report.skipped)} known skips, none unmapped"
    )
    return 0


if __name__ == "__main__":
    sys.exit(run())
