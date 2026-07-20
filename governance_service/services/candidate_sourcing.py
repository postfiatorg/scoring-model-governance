"""Candidate sourcing: one auditable pass over the leaderboard.

Reads one LiveBench release, filters to open-weight models, resolves each
through the curated mapping to a pinned HuggingFace artifact, assigns the
cheapest fitting GPU, and reports unmapped models instead of guessing.
"""

from pathlib import Path

import httpx
import yaml
from pydantic import ValidationError

from governance_service.clients import huggingface, livebench
from governance_service.config import MODEL_MAPPING_PATH
from governance_service.models import (
    CandidateDescriptor,
    MappingEntry,
    ModelArtifact,
    SourcingReport,
)
from governance_service.services.gpu_fit import cheapest_fit

MAPPING_REQUIRED_FIELDS = {"hf_repo", "family", "thinking"}
MAPPING_ALLOWED_FIELDS = MAPPING_REQUIRED_FIELDS | {"note"}
SKIP_FIELD = "skip_reason"


class MappingError(RuntimeError):
    """Raised when the curated mapping file is malformed."""


def load_mapping(
    path: Path = MODEL_MAPPING_PATH,
) -> tuple[dict[str, MappingEntry], dict[str, str]]:
    """Load the curated mapping: resolvable entries plus known skips.

    A skip entry records a model whose artifact is known to be unresolvable
    (the methodology's skip rule) so the freshness check treats it as
    handled rather than as a new unmapped model.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise MappingError(f"Mapping file is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict) or not raw:
        raise MappingError("Mapping file must be a non-empty mapping")

    mapping = {}
    skips = {}
    for key, fields in raw.items():
        if not isinstance(fields, dict):
            raise MappingError(f"Mapping entry '{key}' must be a mapping")
        if SKIP_FIELD in fields:
            if set(fields) != {SKIP_FIELD} or not isinstance(fields[SKIP_FIELD], str):
                raise MappingError(
                    f"Skip entry '{key}' must contain only a {SKIP_FIELD} string"
                )
            skips[key] = fields[SKIP_FIELD]
            continue
        unknown = set(fields) - MAPPING_ALLOWED_FIELDS
        if unknown:
            raise MappingError(f"Mapping entry '{key}' has unknown fields: {unknown}")
        missing = MAPPING_REQUIRED_FIELDS - set(fields)
        if missing:
            raise MappingError(f"Mapping entry '{key}' is missing fields: {missing}")
        try:
            mapping[key] = MappingEntry(**fields)
        except ValidationError as exc:
            raise MappingError(f"Mapping entry '{key}' is invalid: {exc}") from exc
    return mapping, skips


def source_candidates(
    client: httpx.Client,
    release: str | None = None,
    mapping_path: Path = MODEL_MAPPING_PATH,
    registry_raw: bytes | None = None,
    artifact_cache: dict[str, ModelArtifact] | None = None,
) -> SourcingReport:
    """Run one sourcing pass against a release (latest when not given).

    A release walk (the pool refresh's fallback) passes the shared registry
    bytes and an artifact cache so per-release passes do not re-fetch what
    cannot change within one refresh.
    """
    mapping, skips = load_mapping(mapping_path)

    if release is None:
        release = livebench.discover_releases(client)[-1]

    if registry_raw is None:
        registry_raw = livebench.fetch_registry(client)
    table_raw, categories_raw = livebench.fetch_release_files(client, release)

    registry = livebench.parse_registry(registry_raw)
    categories = livebench.parse_categories(categories_raw)
    standings = livebench.compute_standings(table_raw, categories, registry)

    candidates = []
    unmapped = []
    skipped = {}
    for standing in standings:
        if not standing.openweight:
            continue
        if standing.model_key in skips:
            skipped[standing.model_key] = skips[standing.model_key]
            continue
        entry = mapping.get(standing.model_key)
        if entry is None:
            unmapped.append(standing.model_key)
            continue

        if artifact_cache is not None and entry.hf_repo in artifact_cache:
            artifact = artifact_cache[entry.hf_repo]
        else:
            artifact = huggingface.fetch_artifact(client, entry.hf_repo)
            if artifact_cache is not None:
                artifact_cache[entry.hf_repo] = artifact
        gpu = cheapest_fit(artifact)
        candidates.append(
            CandidateDescriptor(
                livebench_key=standing.model_key,
                display_name=standing.display_name,
                organization=standing.organization,
                family=entry.family,
                thinking=entry.thinking,
                global_average=standing.global_average,
                category_averages=standing.category_averages,
                hf_repo=artifact.repo_id,
                revision=artifact.revision,
                precision=artifact.precision,
                weight_bytes=artifact.weight_bytes,
                license=artifact.license,
                gated=artifact.gated,
                assigned_gpu=gpu.name if gpu else None,
                release=release,
            )
        )

    return SourcingReport(
        release=release,
        candidates=candidates,
        unmapped=unmapped,
        skipped=skipped,
        snapshots=[
            livebench.snapshot(f"table_{release}.csv", table_raw),
            livebench.snapshot(f"categories_{release}.json", categories_raw),
            livebench.snapshot("modelLinks.js", registry_raw),
        ],
    )
