"""Pool endpoints — public reads plus the admin-guarded refresh trigger."""

import logging
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Header, Query, status
from fastapi.responses import JSONResponse

from governance_service.api._helpers import (
    acquire_refresh_lock,
    check_admin_auth,
    release_refresh_lock,
)
from governance_service.config import settings
from governance_service.database import get_db
from governance_service.services.pool_refresh import (
    STATUS_FAILED,
    create_refresh,
    execute_refresh,
    fail_refresh,
    get_pool_members,
    latest_completed_refresh,
)
from governance_service.services.record_publisher import PUBLICATION_FAILED

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/governance")

SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400

REFRESH_ROW_COLUMNS = """
    id, status, release_used, publication_status, snapshots_cid,
    record_commit_urls, error_message, publication_error,
    started_at, completed_at, created_at
"""


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _format_elapsed(seconds: float) -> str:
    if seconds < SECONDS_PER_MINUTE:
        return f"{int(seconds)} seconds"
    if seconds < SECONDS_PER_HOUR:
        return f"{int(seconds / SECONDS_PER_MINUTE)} minutes"
    if seconds < SECONDS_PER_DAY:
        hours = seconds / SECONDS_PER_HOUR
        if hours < 10:
            return f"{hours:.1f} hours"
        return f"{int(hours)} hours"
    return f"{int(seconds / SECONDS_PER_DAY)} days"


def _refresh_row_dict(row) -> dict:
    return {
        "id": row[0],
        "status": row[1],
        "release_used": row[2],
        "publication_status": row[3],
        "snapshots_cid": row[4],
        "record_commit_urls": row[5],
        "error_message": row[6],
        "publication_error": row[7],
        "started_at": _iso(row[8]),
        "completed_at": _iso(row[9]),
        "created_at": _iso(row[10]),
    }


def _run_refresh_in_background(lock_conn, refresh_id: int) -> None:
    """Background worker that owns the advisory lock lifecycle."""
    try:
        connection = None
        try:
            connection = get_db()
            execute_refresh(connection, refresh_id)
        except Exception as exc:
            logger.exception("Background refresh failed with unexpected error")
            if connection is not None:
                try:
                    fail_refresh(connection, refresh_id, f"UNEXPECTED: {exc}")
                except Exception:
                    logger.exception(
                        "Failed to mark refresh %d as failed", refresh_id
                    )
        finally:
            if connection is not None:
                connection.close()
    finally:
        try:
            release_refresh_lock(lock_conn)
        except Exception:
            logger.exception("Failed to release refresh advisory lock")


@router.get("/pool")
def get_pool():
    """The current pool from the latest completed refresh."""
    connection = get_db()
    try:
        row = latest_completed_refresh(connection)
        if row is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"error": "No completed pool refresh yet"},
            )
        members = get_pool_members(connection, row[0])
    finally:
        connection.close()

    return JSONResponse(
        content={
            "refresh_id": row[0],
            "release_used": row[1],
            "completed_at": _iso(row[2]),
            "pool": members,
        }
    )


@router.get("/refreshes")
def list_refreshes(
    limit: int = Query(default=settings.default_page_limit, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """List refreshes, newest first."""
    connection = get_db()
    try:
        cursor = connection.cursor()
        cursor.execute(
            f"""
            SELECT {REFRESH_ROW_COLUMNS}
            FROM pool_refreshes
            ORDER BY id DESC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        )
        rows = cursor.fetchall()
        cursor.execute("SELECT COUNT(*) FROM pool_refreshes")
        total = cursor.fetchone()[0]
        cursor.close()
    finally:
        connection.close()

    return JSONResponse(
        content={
            "refreshes": [_refresh_row_dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


@router.get("/refreshes/{refresh_id}")
def get_refresh(refresh_id: int):
    """One refresh's full audit: the release walk and every rule outcome."""
    connection = get_db()
    try:
        cursor = connection.cursor()
        cursor.execute(
            f"""
            SELECT {REFRESH_ROW_COLUMNS}, releases_considered, snapshots
            FROM pool_refreshes
            WHERE id = %s
            """,
            (refresh_id,),
        )
        row = cursor.fetchone()
        if row is None:
            cursor.close()
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"error": f"Refresh {refresh_id} not found"},
            )

        cursor.execute(
            """
            SELECT release, livebench_key, display_name, organization, family,
                   thinking, global_average, category_averages, hf_repo,
                   revision, precision, weight_bytes, license, gated,
                   assigned_gpu, is_incumbent, in_pool, exclusion_rule
            FROM pool_refresh_candidates
            WHERE refresh_id = %s
            ORDER BY release DESC NULLS FIRST, is_incumbent DESC,
                     global_average DESC NULLS LAST, id
            """,
            (refresh_id,),
        )
        candidate_rows = cursor.fetchall()
        cursor.close()
    finally:
        connection.close()

    refresh = _refresh_row_dict(row)
    # Indices continue past the 11 REFRESH_ROW_COLUMNS entries.
    refresh["releases_considered"] = row[11]
    refresh["snapshots"] = row[12]

    candidates = [
        {
            "release": r[0],
            "livebench_key": r[1],
            "display_name": r[2],
            "organization": r[3],
            "family": r[4],
            "thinking": r[5],
            "global_average": r[6],
            "category_averages": r[7],
            "hf_repo": r[8],
            "revision": r[9],
            "precision": r[10],
            "weight_bytes": r[11],
            "license": r[12],
            "gated": r[13],
            "assigned_gpu": r[14],
            "is_incumbent": r[15],
            "in_pool": r[16],
            "exclusion_rule": r[17],
        }
        for r in candidate_rows
    ]

    return JSONResponse(content={"refresh": refresh, "candidates": candidates})


@router.get("/blocklist")
def get_blocklist():
    """The standing blocklist as consumed by refreshes."""
    connection = get_db()
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT hf_repo, revision, reason, round_reference, created_at
            FROM blocklist
            ORDER BY id
            """
        )
        rows = cursor.fetchall()
        cursor.close()
    finally:
        connection.close()

    return JSONResponse(
        content={
            "blocklist": [
                {
                    "hf_repo": r[0],
                    "revision": r[1],
                    "reason": r[2],
                    "round_reference": r[3],
                    "created_at": _iso(r[4]),
                }
                for r in rows
            ]
        }
    )


def _check_latest_refresh(connection) -> dict:
    """Healthy while the newest refresh did not fail outright.

    There is no scheduler yet, so age is reported as context rather than
    judged against a cadence; cadence-based health arrives with round
    orchestration.
    """
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT id, status, created_at FROM pool_refreshes "
            "ORDER BY id DESC LIMIT 1"
        )
        row = cursor.fetchone()
    finally:
        cursor.close()

    if row is None:
        return {"healthy": False, "detail": "no refreshes yet"}

    refresh_id, refresh_status, created_at = row
    elapsed = (datetime.now(tz=timezone.utc) - created_at).total_seconds()
    detail = (
        f"refresh {refresh_id} {refresh_status}, "
        f"{_format_elapsed(max(0.0, elapsed))} ago"
    )
    return {"healthy": refresh_status != STATUS_FAILED, "detail": detail}


def _check_record_publication(connection) -> dict:
    """Unhealthy when the latest attempted record publication failed."""
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT id, publication_status FROM pool_refreshes
            WHERE publication_status IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """
        )
        row = cursor.fetchone()
    finally:
        cursor.close()

    if row is None:
        return {"healthy": True, "detail": "no publications yet"}

    refresh_id, publication_status = row
    return {
        "healthy": publication_status != PUBLICATION_FAILED,
        "detail": f"refresh {refresh_id} publication {publication_status}",
    }


@router.get("/health")
def get_pipeline_health():
    """Public pipeline-health signals, distinct from the bare /health probe."""
    connection = get_db()
    try:
        latest_refresh = _check_latest_refresh(connection)
        record_publication = _check_record_publication(connection)
    finally:
        connection.close()

    return JSONResponse(
        content={
            "latest_refresh": latest_refresh,
            "record_publication": record_publication,
        }
    )


@router.post("/pool/refresh")
def trigger_refresh(x_api_key: str | None = Header(default=None)):
    """Trigger a pool refresh manually.

    Returns 202 with the refresh id if started, 409 if a refresh is
    already in progress, 403 if auth fails or the endpoint is not
    configured.
    """
    auth_error = check_admin_auth(x_api_key)
    if auth_error is not None:
        return auth_error

    lock_conn, lock_error = acquire_refresh_lock()
    if lock_error is not None:
        return lock_error

    refresh_id = None
    try:
        connection = get_db()
        try:
            refresh_id = create_refresh(connection)
        finally:
            connection.close()
        thread = threading.Thread(
            target=_run_refresh_in_background,
            args=(lock_conn, refresh_id),
            daemon=True,
        )
        thread.start()
    except Exception as exc:
        if refresh_id is not None:
            try:
                connection = get_db()
                try:
                    fail_refresh(connection, refresh_id, f"THREAD_START: {exc}")
                finally:
                    connection.close()
            except Exception:
                logger.exception("Failed to mark refresh %d as failed", refresh_id)
        release_refresh_lock(lock_conn)
        raise

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"status": "started", "refresh_id": refresh_id},
    )
