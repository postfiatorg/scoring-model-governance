"""Mapping and schema freshness check.

Runs one real sourcing pass against the latest LiveBench release and fails
when an open-weight leaderboard model has no mapping entry or the upstream
data contract no longer parses. Also validates every mapping entry's
curated thinking class against the model's public chat template. Run as:

    python -m governance_service.freshness
"""

import sys

import httpx

from governance_service.clients.huggingface import (
    HuggingFaceError,
    fetch_chat_template,
)
from governance_service.clients.livebench import (
    LiveBenchRequestError,
    LiveBenchSchemaError,
)
from governance_service.config import MODEL_MAPPING_PATH, settings
from governance_service.models import MappingEntry, ThinkingMode
from governance_service.services.candidate_sourcing import (
    MappingError,
    load_mapping,
    source_candidates,
)

THINKING_TOGGLE_MARKER = "enable_thinking"


def validate_thinking_class(entry: MappingEntry, template: str | None) -> str | None:
    """One entry's template-vs-class verdict; None when consistent.

    The template distinguishes hybrid (toggle present) from non-hybrid,
    and present from absent; the none-vs-always split within
    "no toggle" stays curator judgment, which the eligibility boundary
    tolerates because both classes validate identically here.
    """
    if template is None:
        if entry.thinking is not ThinkingMode.UNKNOWN:
            return (
                f"no public chat template found, but thinking is declared "
                f"'{entry.thinking.value}' — only 'unknown' is consistent "
                "with absent evidence"
            )
        return None

    has_toggle = THINKING_TOGGLE_MARKER in template
    if entry.thinking is ThinkingMode.UNKNOWN:
        return (
            "a public chat template exists, so the thinking class can be "
            "established — reclassify instead of 'unknown'"
        )
    if entry.thinking is ThinkingMode.HYBRID and not has_toggle:
        return "declared 'hybrid' but the chat template has no thinking toggle"
    if entry.thinking is not ThinkingMode.HYBRID and has_toggle:
        return (
            f"declared '{entry.thinking.value}' but the chat template "
            "carries a thinking toggle — reclassify as 'hybrid'"
        )
    return None


def check_thinking_classes(client: httpx.Client) -> list[str]:
    """Validate every mapping entry's thinking class; returns failures."""
    mapping, _ = load_mapping(MODEL_MAPPING_PATH)
    failures = []
    for key, entry in mapping.items():
        template = fetch_chat_template(client, entry.hf_repo)
        verdict = validate_thinking_class(entry, template)
        state = "ok" if verdict is None else f"MISMATCH: {verdict}"
        print(f"[freshness] thinking {key}: {entry.thinking.value} — {state}")
        if verdict is not None:
            failures.append(f"{key}: {verdict}")
    return failures


def run(client: httpx.Client | None = None) -> int:
    """One freshness pass; returns a process exit code."""
    owned_client = client is None
    if owned_client:
        client = httpx.Client(
            timeout=settings.http_timeout_seconds, follow_redirects=True
        )
    try:
        report = source_candidates(client)
        thinking_failures = check_thinking_classes(client)
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
    if thinking_failures:
        print(
            "[freshness] FAILED: thinking classes contradict chat templates: "
            f"{thinking_failures}"
        )
        return 1
    print(
        f"[freshness] OK: {len(report.candidates)} mapped, "
        f"{len(report.skipped)} known skips, none unmapped, "
        "thinking classes consistent"
    )
    return 0


if __name__ == "__main__":
    sys.exit(run())
