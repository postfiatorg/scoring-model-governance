"""Pool refresh: the methodology's candidate-pool rules over a release walk.

One refresh turns leaderboard standings into a persisted candidate pool.
Challengers must survive five rules — blocklist, artifact variant,
thinking-mode eligibility, single-GPU fit, and one-per-family
deduplication — and a release is viable
only when at least two challengers survive alongside the incumbent, which
is a pool member by right and exempt from every rule. The refresh walks
back one release at a time until it finds a viable pool, recording each
considered release and the reason it fell back; when no release qualifies,
the no-viable-pool finding is persisted and the current pool stands.
"""

import json
import logging
from pathlib import Path

import httpx
import yaml
from pydantic import ValidationError

from governance_service.clients import huggingface, livebench
from governance_service.config import MODEL_BLOCKLIST_PATH, settings
from governance_service.models import (
    BlocklistEntry,
    CandidateDescriptor,
    CandidateEvaluation,
    IncumbentMember,
    ModelArtifact,
    Precision,
    RefreshResult,
    ReleaseEvaluation,
    ReleaseOutcome,
    SnapshotFile,
    ThinkingMode,
)
from governance_service.services.candidate_sourcing import (
    MappingError,
    source_candidates,
)
from governance_service.services.gpu_fit import cheapest_fit
from governance_service.services.record_publisher import publish_record

logger = logging.getLogger(__name__)

REFRESH_ADVISORY_LOCK_ID = 99101

STATUS_RUNNING = "RUNNING"
STATUS_COMPLETED = "COMPLETED"
STATUS_NO_VIABLE_POOL = "NO_VIABLE_POOL"
STATUS_FAILED = "FAILED"

RULE_BLOCKLISTED = "BLOCKLISTED"
RULE_THINKING_NOT_DISABLEABLE = "THINKING_NOT_DISABLEABLE"
RULE_THINKING_UNVERIFIED = "THINKING_UNVERIFIED"
RULE_VARIANT_INELIGIBLE = "VARIANT_INELIGIBLE"
RULE_NO_SINGLE_GPU_FIT = "NO_SINGLE_GPU_FIT"
RULE_FAMILY_DEDUPLICATED = "FAMILY_DEDUPLICATED"
RULE_IS_INCUMBENT = "IS_INCUMBENT"

POOL_ELIGIBLE_PRECISIONS = {Precision.FP8, Precision.BF16, Precision.FP16}
POOL_ELIGIBLE_THINKING = {ThinkingMode.NONE, ThinkingMode.HYBRID}
MIN_CHALLENGERS = 2


class BlocklistError(RuntimeError):
    """Raised when the standing blocklist file is malformed."""


def load_blocklist(path: Path = MODEL_BLOCKLIST_PATH) -> list[BlocklistEntry]:
    """Load the standing blocklist; an empty or comment-only file is valid."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise BlocklistError(f"Blocklist file is not valid YAML: {exc}") from exc
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise BlocklistError("Blocklist file must be a list of entries")

    entries = []
    for index, fields in enumerate(raw):
        if not isinstance(fields, dict):
            raise BlocklistError(f"Blocklist entry {index} must be a mapping")
        try:
            entries.append(BlocklistEntry(**fields))
        except ValidationError as exc:
            raise BlocklistError(f"Blocklist entry {index} is invalid: {exc}") from exc
    return entries


def evaluate_release(
    candidates: list[CandidateDescriptor],
    blocklist: list[BlocklistEntry],
    incumbent_hf_repo: str,
) -> list[CandidateEvaluation]:
    """Apply the pool rules to one release's sourced candidates.

    Candidates arrive leaderboard-ordered, so family deduplication keeps
    the best-ranked survivor per family and a blocked or unfit model's
    slot passes to the next eligible candidate naturally. The incumbent's
    own leaderboard entry never competes — the incumbent is a member by
    right — which leaves its family's challenger slot open to a
    better-ranked successor.
    """
    blocked = {(entry.hf_repo, entry.revision) for entry in blocklist}
    evaluations = []
    families_in_pool: set[str] = set()

    for candidate in candidates:
        if candidate.hf_repo == incumbent_hf_repo:
            evaluations.append(
                CandidateEvaluation(
                    descriptor=candidate,
                    is_incumbent=True,
                    in_pool=False,
                    exclusion_rule=RULE_IS_INCUMBENT,
                )
            )
            continue

        # Checks follow the methodology's rule numbering so a dual-failure
        # candidate's recorded exclusion matches the published order.
        exclusion = None
        if (candidate.hf_repo, candidate.revision) in blocked:
            exclusion = RULE_BLOCKLISTED
        elif candidate.precision not in POOL_ELIGIBLE_PRECISIONS:
            exclusion = RULE_VARIANT_INELIGIBLE
        elif candidate.thinking == ThinkingMode.ALWAYS:
            exclusion = RULE_THINKING_NOT_DISABLEABLE
        elif candidate.thinking not in POOL_ELIGIBLE_THINKING:
            exclusion = RULE_THINKING_UNVERIFIED
        elif candidate.assigned_gpu is None:
            exclusion = RULE_NO_SINGLE_GPU_FIT
        elif candidate.family in families_in_pool:
            exclusion = RULE_FAMILY_DEDUPLICATED

        if exclusion is None:
            families_in_pool.add(candidate.family)
        evaluations.append(
            CandidateEvaluation(
                descriptor=candidate,
                in_pool=exclusion is None,
                exclusion_rule=exclusion,
            )
        )
    return evaluations


def resolve_incumbent(
    client: httpx.Client,
    evaluations: list[CandidateEvaluation],
) -> IncumbentMember:
    """Resolve the incumbent's pool entry from its configured artifact.

    The configured repository (and pinned revision, when set) is
    authoritative; the leaderboard standing is attached only when the
    incumbent happens to appear on the release used.
    """
    artifact = huggingface.fetch_artifact(
        client, settings.incumbent_hf_repo, settings.incumbent_revision
    )
    gpu = cheapest_fit(artifact)
    standing = next(
        (e.descriptor for e in evaluations if e.is_incumbent), None
    )
    return IncumbentMember(
        hf_repo=artifact.repo_id,
        revision=artifact.revision,
        precision=artifact.precision,
        weight_bytes=artifact.weight_bytes,
        license=artifact.license,
        gated=artifact.gated,
        assigned_gpu=gpu.name if gpu else None,
        livebench_key=standing.livebench_key if standing else None,
        display_name=standing.display_name if standing else None,
        organization=standing.organization if standing else None,
        family=standing.family if standing else None,
        global_average=standing.global_average if standing else None,
        category_averages=standing.category_averages if standing else None,
    )


def run_refresh(
    client: httpx.Client, blocklist: list[BlocklistEntry]
) -> RefreshResult:
    """Walk releases newest-first until one supplies a viable pool.

    The blocklist is loaded once by the caller so a single refresh cannot
    observe two versions of the file.
    """
    releases = livebench.discover_releases(client)
    registry_raw = livebench.fetch_registry(client)
    artifact_cache: dict[str, ModelArtifact] = {}

    considered: list[ReleaseEvaluation] = []
    viable_evaluation: ReleaseEvaluation | None = None
    snapshots: dict[str, SnapshotFile] = {}

    for release in reversed(releases):
        report = source_candidates(
            client,
            release=release,
            registry_raw=registry_raw,
            artifact_cache=artifact_cache,
        )
        for snap in report.snapshots:
            snapshots.setdefault(snap.name, snap)
        evaluations = evaluate_release(
            report.candidates, blocklist, settings.incumbent_hf_repo
        )
        challenger_count = sum(1 for e in evaluations if e.in_pool)
        viable = challenger_count >= MIN_CHALLENGERS
        evaluation = ReleaseEvaluation(
            outcome=ReleaseOutcome(
                release=release,
                challenger_count=challenger_count,
                viable=viable,
                fallback_reason=None
                if viable
                else f"{challenger_count} of {MIN_CHALLENGERS} required "
                "challengers survived the pool rules",
                unmapped=report.unmapped,
                skipped=report.skipped,
            ),
            evaluations=evaluations,
        )
        considered.append(evaluation)
        if viable:
            viable_evaluation = evaluation
            break
        logger.info(
            "Release %s not viable (%d challengers)", release, challenger_count
        )

    if viable_evaluation is not None:
        incumbent = resolve_incumbent(client, viable_evaluation.evaluations)
        return RefreshResult(
            status=STATUS_COMPLETED,
            release_used=viable_evaluation.outcome.release,
            incumbent=incumbent,
            releases=considered,
            snapshots=list(snapshots.values()),
        )

    newest_evaluations = considered[0].evaluations if considered else []
    incumbent = resolve_incumbent(client, newest_evaluations)
    return RefreshResult(
        status=STATUS_NO_VIABLE_POOL,
        release_used=None,
        incumbent=incumbent,
        releases=considered,
        snapshots=list(snapshots.values()),
    )


def sync_blocklist(connection, blocklist: list[BlocklistEntry]) -> None:
    """Mirror the standing blocklist file into the database.

    The file is the source of truth; entries are append-only per the
    methodology, so existing rows are never updated or removed.
    """
    cursor = connection.cursor()
    for entry in blocklist:
        cursor.execute(
            """
            INSERT INTO blocklist (hf_repo, revision, reason, round_reference)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (hf_repo, revision) DO NOTHING
            """,
            (entry.hf_repo, entry.revision, entry.reason, entry.round_reference),
        )
    cursor.close()
    connection.commit()


def create_refresh(connection) -> int:
    """Insert the RUNNING refresh row and return its id."""
    cursor = connection.cursor()
    cursor.execute(
        "INSERT INTO pool_refreshes (status) VALUES (%s) RETURNING id",
        (STATUS_RUNNING,),
    )
    refresh_id = cursor.fetchone()[0]
    cursor.close()
    connection.commit()
    return refresh_id


def _persist_candidate(
    cursor, refresh_id: int, release: str, evaluation: CandidateEvaluation
) -> None:
    descriptor = evaluation.descriptor
    cursor.execute(
        """
        INSERT INTO pool_refresh_candidates (
            refresh_id, release, livebench_key, display_name, organization,
            family, thinking, global_average, category_averages, hf_repo,
            revision, precision, weight_bytes, license, gated, assigned_gpu,
            is_incumbent, in_pool, exclusion_rule
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  %s, %s, %s, %s)
        """,
        (
            refresh_id,
            release,
            descriptor.livebench_key,
            descriptor.display_name,
            descriptor.organization,
            descriptor.family,
            descriptor.thinking.value,
            descriptor.global_average,
            json.dumps(descriptor.category_averages),
            descriptor.hf_repo,
            descriptor.revision,
            descriptor.precision.value,
            descriptor.weight_bytes,
            descriptor.license,
            descriptor.gated,
            descriptor.assigned_gpu,
            evaluation.is_incumbent,
            evaluation.in_pool,
            evaluation.exclusion_rule,
        ),
    )


def _persist_incumbent(
    cursor, refresh_id: int, release_used: str | None, incumbent: IncumbentMember
) -> None:
    cursor.execute(
        """
        INSERT INTO pool_refresh_candidates (
            refresh_id, release, livebench_key, display_name, organization,
            family, global_average, category_averages, hf_repo, revision,
            precision, weight_bytes, license, gated, assigned_gpu,
            is_incumbent, in_pool, exclusion_rule
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  TRUE, TRUE, NULL)
        """,
        (
            refresh_id,
            release_used,
            incumbent.livebench_key,
            incumbent.display_name,
            incumbent.organization,
            incumbent.family,
            incumbent.global_average,
            json.dumps(incumbent.category_averages)
            if incumbent.category_averages is not None
            else None,
            incumbent.hf_repo,
            incumbent.revision,
            incumbent.precision.value,
            incumbent.weight_bytes,
            incumbent.license,
            incumbent.gated,
            incumbent.assigned_gpu,
        ),
    )


def persist_result(connection, refresh_id: int, result: RefreshResult) -> None:
    """Persist one refresh's outcome and full per-candidate audit trail."""
    cursor = connection.cursor()
    cursor.execute(
        """
        UPDATE pool_refreshes
        SET status = %s, release_used = %s, releases_considered = %s,
            snapshots = %s, completed_at = NOW()
        WHERE id = %s
        """,
        (
            result.status,
            result.release_used,
            json.dumps([r.outcome.model_dump() for r in result.releases]),
            json.dumps(
                [s.model_dump(exclude={"content"}) for s in result.snapshots]
            ),
            refresh_id,
        ),
    )
    _persist_incumbent(cursor, refresh_id, result.release_used, result.incumbent)
    for release_evaluation in result.releases:
        for evaluation in release_evaluation.evaluations:
            _persist_candidate(
                cursor,
                refresh_id,
                release_evaluation.outcome.release,
                evaluation,
            )
    cursor.close()
    connection.commit()


def fail_refresh(connection, refresh_id: int, error_message: str) -> None:
    """Mark a refresh FAILED with its error, leaving the pool unchanged.

    The rollback clears any aborted transaction left by the failure that
    brought us here, so the FAILED update itself can commit.
    """
    connection.rollback()
    cursor = connection.cursor()
    cursor.execute(
        """
        UPDATE pool_refreshes
        SET status = %s, error_message = %s, completed_at = NOW()
        WHERE id = %s
        """,
        (STATUS_FAILED, error_message, refresh_id),
    )
    cursor.close()
    connection.commit()


def execute_refresh(connection, refresh_id: int) -> RefreshResult | None:
    """Run one full refresh against a pre-created RUNNING row.

    Any upstream or curated-file failure marks the row FAILED instead of
    raising: a refresh that cannot complete never touches the pool.
    """
    try:
        blocklist = load_blocklist()
        sync_blocklist(connection, blocklist)

        with httpx.Client(
            timeout=settings.http_timeout_seconds, follow_redirects=True
        ) as client:
            result = run_refresh(client, blocklist)
        persist_result(connection, refresh_id, result)
        try:
            publish_record(connection, refresh_id, result)
        except Exception:
            # publish_record guards itself; this backstop keeps any escape
            # from reaching the worker's handler, which would overwrite
            # the committed refresh outcome with FAILED.
            logger.exception(
                "Record publication escaped its guard for refresh %d", refresh_id
            )
        logger.info(
            "Pool refresh %d finished: status=%s, release_used=%s",
            refresh_id,
            result.status,
            result.release_used,
        )
        return result
    except (
        livebench.LiveBenchRequestError,
        livebench.LiveBenchSchemaError,
        huggingface.HuggingFaceError,
        MappingError,
        BlocklistError,
    ) as exc:
        logger.exception("Pool refresh %d failed", refresh_id)
        fail_refresh(connection, refresh_id, str(exc))
        return None


def latest_completed_refresh(connection) -> tuple | None:
    """(id, release_used, completed_at) of the newest COMPLETED refresh."""
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT id, release_used, completed_at FROM pool_refreshes
        WHERE status = %s
        ORDER BY id DESC
        LIMIT 1
        """,
        (STATUS_COMPLETED,),
    )
    row = cursor.fetchone()
    cursor.close()
    return row


def get_pool_members(connection, refresh_id: int) -> list[dict]:
    """One completed refresh's pool members, incumbent first.

    in_pool audit rows exist for every considered release; the pool is
    only what survived on the release the refresh actually used.
    """
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT c.livebench_key, c.display_name, c.family, c.hf_repo,
               c.revision, c.precision, c.assigned_gpu, c.is_incumbent
        FROM pool_refresh_candidates c
        JOIN pool_refreshes r ON r.id = c.refresh_id
        WHERE c.refresh_id = %s AND c.in_pool AND c.release = r.release_used
        ORDER BY c.is_incumbent DESC, c.global_average DESC NULLS LAST, c.id
        """,
        (refresh_id,),
    )
    members = [
        {
            "livebench_key": r[0],
            "display_name": r[1],
            "family": r[2],
            "hf_repo": r[3],
            "revision": r[4],
            "precision": r[5],
            "assigned_gpu": r[6],
            "is_incumbent": r[7],
        }
        for r in cursor.fetchall()
    ]
    cursor.close()
    return members


def get_current_pool(connection) -> list[dict]:
    """The pool as of the latest completed refresh; empty before one exists.

    A NO_VIABLE_POOL or FAILED refresh never becomes current — the pool
    stands as the last COMPLETED refresh left it.
    """
    row = latest_completed_refresh(connection)
    if row is None:
        return []
    return get_pool_members(connection, row[0])
