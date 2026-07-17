"""Published refresh records: every completed refresh becomes a public file.

Renders a completed refresh into the canonical record — a machine-readable
JSON document plus a short human-readable summary — pins the upstream
LiveBench snapshot files to IPFS (with best-effort Pinata replication),
and commits both record files to the public governance repository through
the GitHub Contents API. Publication failure is a visible state on the
refresh row, never an exception into the refresh flow and never a change
to the refresh outcome itself.
"""

import json
import logging
from datetime import datetime

from governance_service.clients.github_records import GitHubRecordsClient
from governance_service.clients.ipfs import IPFSClient
from governance_service.clients.pinata import PinataClient
from governance_service.config import settings
from governance_service.models import RefreshResult

logger = logging.getLogger(__name__)

RECORD_VERSION = 1
REVISION_DISPLAY_CHARS = 12

PUBLICATION_PUBLISHED = "PUBLISHED"
PUBLICATION_FAILED = "FAILED"
PUBLICATION_SKIPPED = "SKIPPED"


class RecordPublicationError(RuntimeError):
    """Raised inside the publication flow to mark the refresh FAILED-to-publish."""


def record_paths(refresh_id: int, completed_at: datetime) -> tuple[str, str]:
    """Repository paths of one refresh's JSON record and summary."""
    stem = (
        f"{settings.records_base_path}/{settings.environment}/"
        f"{completed_at.date().isoformat()}-refresh-{refresh_id}"
    )
    return f"{stem}.json", f"{stem}.md"


def render_record(
    refresh_id: int,
    result: RefreshResult,
    started_at: datetime,
    completed_at: datetime,
    snapshots_cid: str | None,
) -> dict:
    """Render the canonical machine-readable record of one refresh."""
    pool = None
    if result.release_used is not None:
        used = next(
            (r for r in result.releases if r.outcome.release == result.release_used),
            None,
        )
        if used is None:
            raise RecordPublicationError(
                f"release_used {result.release_used} is missing from the "
                "refresh's considered releases"
            )
        pool = [{"role": "incumbent", **result.incumbent.model_dump(mode="json")}] + [
            {"role": "challenger", **e.descriptor.model_dump(mode="json")}
            for e in used.evaluations
            if e.in_pool
        ]

    return {
        "record_version": RECORD_VERSION,
        "environment": settings.environment,
        "refresh_id": refresh_id,
        "status": result.status,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "release_used": result.release_used,
        "pool": pool,
        "incumbent": result.incumbent.model_dump(mode="json"),
        "releases_considered": [
            {
                **release.outcome.model_dump(mode="json"),
                "candidates": [
                    {
                        **evaluation.descriptor.model_dump(mode="json"),
                        "is_incumbent": evaluation.is_incumbent,
                        "in_pool": evaluation.in_pool,
                        "exclusion_rule": evaluation.exclusion_rule,
                    }
                    for evaluation in release.evaluations
                ],
            }
            for release in result.releases
        ],
        "upstream_snapshots": {
            "cid": snapshots_cid,
            "files": [
                snap.model_dump(mode="json", exclude={"content"})
                for snap in result.snapshots
            ],
        },
    }


def _cell(value: object) -> str:
    """One markdown table cell: upstream-controlled strings are escaped."""
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_summary(record: dict, json_path: str) -> str:
    """Render the short human-readable companion of one record."""
    lines = [
        f"# Pool refresh {record['refresh_id']} ({record['environment']})",
        "",
        f"- **Status:** {record['status']}",
        f"- **Completed:** {record['completed_at']}",
    ]

    if record["pool"] is not None:
        lines.append(f"- **Release used:** {record['release_used']}")
        lines.extend(["", "## Pool", ""])
        lines.append("| Role | Model | Family | Artifact | Precision | GPU |")
        lines.append("|------|-------|--------|----------|-----------|-----|")
        for member in record["pool"]:
            artifact = (
                f"{member['hf_repo']}@{member['revision'][:REVISION_DISPLAY_CHARS]}"
            )
            lines.append(
                f"| {_cell(member['role'])} "
                f"| {_cell(member.get('display_name') or member['hf_repo'])} "
                f"| {_cell(member.get('family') or '-')} | {_cell(artifact)} "
                f"| {_cell(member['precision'])} "
                f"| {_cell(member.get('assigned_gpu') or '-')} |"
            )
    else:
        lines.append(
            "- **Outcome:** no release could supply a viable pool — "
            "the standing pool is unchanged"
        )

    lines.extend(["", "## Release walk", ""])
    lines.append("| Release | Viable | Challengers | Reason |")
    lines.append("|---------|--------|-------------|--------|")
    for outcome in record["releases_considered"]:
        lines.append(
            f"| {_cell(outcome['release'])} | {'yes' if outcome['viable'] else 'no'} "
            f"| {outcome['challenger_count']} "
            f"| {_cell(outcome['fallback_reason'] or '-')} |"
        )

    snapshots = record["upstream_snapshots"]
    lines.extend(["", "## Upstream inputs", ""])
    if snapshots["cid"]:
        lines.append(f"- **IPFS CID:** `{snapshots['cid']}`")
    for snap in snapshots["files"]:
        lines.append(f"- `{snap['name']}` — sha256 `{snap['sha256']}`")

    lines.extend(["", f"The machine-readable record is `{json_path}`.", ""])
    return "\n".join(lines)


def _pin_snapshots(result: RefreshResult, refresh_id: int) -> str | None:
    """Pin the refresh's upstream files; None when IPFS is not configured.

    Mirrors the dynamic-unl-scoring pin-with-fallback contract: the
    primary node pin is replicated to Pinata by CID, and when the primary
    pin fails Pinata's direct upload is the write fallback, so a primary
    outage alone never aborts record publication.
    """
    if not settings.ipfs_enabled:
        logger.warning(
            "IPFS not configured — publishing refresh %d record without a "
            "snapshot CID",
            refresh_id,
        )
        return None
    if not result.snapshots:
        return None

    files = {snap.name: snap.content for snap in result.snapshots}
    pin_name = f"pool-refresh-{settings.environment}-{refresh_id}-snapshots"

    cid = IPFSClient().pin_directory(files)
    if cid is not None:
        if settings.pinata_enabled:
            PinataClient().pin_by_cid(cid, name=pin_name)
        return cid

    if settings.pinata_enabled:
        logger.warning(
            "Primary IPFS pin failed for refresh %d — falling back to "
            "Pinata direct upload",
            refresh_id,
        )
        cid = PinataClient().pin_directory(files, name=pin_name)
        if cid is not None:
            return cid

    raise RecordPublicationError("IPFS snapshot pinning failed")


def _fetch_refresh_times(connection, refresh_id: int) -> tuple[datetime, datetime]:
    cursor = connection.cursor()
    cursor.execute(
        "SELECT started_at, completed_at FROM pool_refreshes WHERE id = %s",
        (refresh_id,),
    )
    row = cursor.fetchone()
    cursor.close()
    # Close the read's transaction so the connection does not sit idle in
    # transaction across the (potentially minutes-long) publish retries.
    connection.rollback()
    return row[0], row[1]


def _mark_publication(
    connection,
    refresh_id: int,
    publication_status: str,
    snapshots_cid: str | None = None,
    commit_urls: list[str] | None = None,
    error: str | None = None,
) -> None:
    cursor = connection.cursor()
    cursor.execute(
        """
        UPDATE pool_refreshes
        SET publication_status = %s, snapshots_cid = %s,
            record_commit_urls = %s, publication_error = %s
        WHERE id = %s
        """,
        (
            publication_status,
            snapshots_cid,
            json.dumps(commit_urls) if commit_urls is not None else None,
            error,
            refresh_id,
        ),
    )
    cursor.close()
    connection.commit()


def publish_record(connection, refresh_id: int, result: RefreshResult) -> None:
    """Publish one completed refresh's record; failures become row state.

    This function must never raise: an exception escaping it would reach
    the background worker's generic handler, which would overwrite the
    already-committed refresh outcome with FAILED and silently revert the
    standing pool. Even the failure-marking recovery is guarded.
    """
    snapshots_cid = None
    commit_urls: list[str] = []
    try:
        if not settings.records_enabled:
            logger.info(
                "Record publication skipped for refresh %d: "
                "RECORDS_GITHUB_TOKEN not configured",
                refresh_id,
            )
            _mark_publication(connection, refresh_id, PUBLICATION_SKIPPED)
            return

        snapshots_cid = _pin_snapshots(result, refresh_id)
        started_at, completed_at = _fetch_refresh_times(connection, refresh_id)
        record = render_record(
            refresh_id, result, started_at, completed_at, snapshots_cid
        )
        json_path, md_path = record_paths(refresh_id, completed_at)
        summary = render_summary(record, json_path)

        client = GitHubRecordsClient()
        commit_message = (
            f"Publish the {settings.environment} pool refresh {refresh_id} record"
        )
        commit_urls.append(
            client.publish(
                json_path, json.dumps(record, indent=2) + "\n", commit_message
            )
        )
        commit_urls.append(client.publish(md_path, summary, commit_message))

        _mark_publication(
            connection,
            refresh_id,
            PUBLICATION_PUBLISHED,
            snapshots_cid=snapshots_cid,
            commit_urls=commit_urls,
        )
        logger.info("Refresh %d record published: %s", refresh_id, commit_urls[0])
    except Exception as exc:
        logger.exception("Record publication failed for refresh %d", refresh_id)
        try:
            connection.rollback()
            # Preserve whatever already succeeded — a pinned CID or a
            # partially committed record file — on the audit row.
            _mark_publication(
                connection,
                refresh_id,
                PUBLICATION_FAILED,
                snapshots_cid=snapshots_cid,
                commit_urls=commit_urls or None,
                error=str(exc),
            )
        except Exception:
            logger.exception(
                "Failed to mark refresh %d publication as failed", refresh_id
            )
