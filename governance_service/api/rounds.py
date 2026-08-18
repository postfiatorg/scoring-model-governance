"""Round endpoints — the rounds API plus the manual round trigger.

The read surface sidecars and the explorer consume (G.5.7): the round
list and detail, the frozen-package and final-record artifact routes —
the HTTPS side of the methodology's fetch-with-IPFS-fallback contract —
and the participation config endpoint. Record routes enforce the
output-withholding rule: nothing a round produced is served before its
commit window closes.
"""

import logging
import threading

from fastapi import APIRouter, Header, Query, status
from fastapi.responses import JSONResponse

from governance_service.api._helpers import (
    acquire_round_lock,
    check_admin_auth,
    release_round_lock,
)
from governance_service.clients.pftl import PFTLClient
from governance_service.config import settings
from governance_service.database import get_db
from governance_service.services.announcement import (
    GOVERNANCE_PROTOCOL_VERSION,
    ROUND_ANNOUNCEMENT_TYPE,
    ROUND_RECEIPT_TYPE,
)
from governance_service.services.final_publication import (
    build_final_record,
    commit_window_closed,
)
from governance_service.services.orchestrator import (
    TRIGGER_MANUAL,
    RoundOrchestrator,
    cleanup_interrupted_rounds,
    get_active_round,
)
from governance_service.services.round_package import (
    BUNDLE_FILE_PATH,
    DRAW_LEDGER_OFFSET,
    INCUMBENT_MARGIN_POINTS,
    get_package_file,
)
from governance_service.services.scheduler import reanchor_schedule

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/governance")

_ROUND_API_COLUMNS = (
    "round_number",
    "status",
    "trigger_source",
    "package_cid",
    "package_hash",
    "frozen_at",
    "announcement_tx_hash",
    "announcement_ledger_index",
    "commit_opens_at",
    "commit_closes_at",
    "reveal_opens_at",
    "reveal_closes_at",
    "judge_hf_repo",
    "draw_ledger_index",
    "draw_ledger_hash",
    "decision",
    "winner_hf_repo",
    "decision_rationale",
    "decided_at",
    "final_record_cid",
    "receipt_tx_hash",
    "record_commit_url",
    "error_message",
    "started_at",
    "completed_at",
)


def _round_dict(row) -> dict:
    return {
        name: value.isoformat() if hasattr(value, "isoformat") else value
        for name, value in zip(_ROUND_API_COLUMNS, row)
    }


@router.get("/rounds")
def list_rounds(
    limit: int = Query(default=settings.default_page_limit, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """List governance rounds, newest first."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT {', '.join(_ROUND_API_COLUMNS)} FROM governance_rounds
            ORDER BY round_number DESC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        )
        rounds = [_round_dict(row) for row in cursor.fetchall()]
        cursor.execute("SELECT COUNT(*) FROM governance_rounds")
        total = cursor.fetchone()[0]
        cursor.close()
    finally:
        conn.close()

    return JSONResponse(content={
        "rounds": rounds,
        "total": total,
        "limit": limit,
        "offset": offset,
    })


@router.get("/rounds/{round_number}/record")
def get_round_record(round_number: int):
    """The final record's bundle manifest, re-assembled from round data."""
    return _record_response(round_number, None)


@router.get("/rounds/{round_number}/record/{file_path:path}")
def get_round_record_file(round_number: int, file_path: str):
    """One final-record file, re-assembled from round data on demand."""
    return _record_response(round_number, file_path)


def _record_response(round_number: int, file_path: str | None):
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, commit_closes_at FROM governance_rounds WHERE round_number = %s",
            (round_number,),
        )
        row = cursor.fetchone()
        cursor.close()
        if row is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"error": f"Round {round_number} not found"},
            )
        round_id, commit_closes_at = row
        # The withholding rule applies to the API exactly as it applies
        # to publication: no round output leaves before the commit close.
        if not commit_window_closed(commit_closes_at):
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={
                    "error": (
                        f"Round {round_number} outputs are withheld until "
                        "its commit window closes"
                    )
                },
            )
        files, bundle = build_final_record(conn, round_id, round_number)
    finally:
        conn.close()

    if file_path is None:
        return JSONResponse(content=bundle)
    if file_path not in files:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": f"No record file {file_path} for round {round_number}"
            },
        )
    return JSONResponse(content=files[file_path])


@router.get("/rounds/{round_number:int}")
def get_round(round_number: int):
    """One governance round's full persisted identity."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT {', '.join(_ROUND_API_COLUMNS)} FROM governance_rounds
            WHERE round_number = %s
            """,
            (round_number,),
        )
        row = cursor.fetchone()
        cursor.close()
    finally:
        conn.close()

    if row is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": f"Round {round_number} not found"},
        )
    return JSONResponse(content=_round_dict(row))


def _foundation_publisher_address() -> str | None:
    if not settings.pftl_wallet_secret:
        return None
    try:
        return PFTLClient().publisher_address
    except Exception:
        logger.warning("Could not derive publisher address for config", exc_info=True)
        return None


@router.get("/config")
def get_config():
    """Public read-only configuration for governance participation.

    The chain-discovery fields a sidecar needs to find and decode the
    on-chain governance memos, plus the frozen-procedure constants the
    explorer renders without hardcoding.
    """
    return JSONResponse(content={
        "protocol_version": GOVERNANCE_PROTOCOL_VERSION,
        "round_cadence_days": float(settings.round_cadence_days),
        "incumbent_margin_points": INCUMBENT_MARGIN_POINTS,
        "draw_ledger_offset": DRAW_LEDGER_OFFSET,
        "foundation_publisher_address": _foundation_publisher_address(),
        "announcement_memo_type": ROUND_ANNOUNCEMENT_TYPE,
        "receipt_memo_type": ROUND_RECEIPT_TYPE,
        "commit_window_seconds": settings.round_commit_window_seconds,
        "reveal_window_seconds": settings.round_reveal_window_seconds,
    })


@router.get("/rounds/{round_number}/package")
def get_round_package(round_number: int):
    """The frozen package's bundle manifest — package kind, hashes, identity."""
    return _package_file_response(round_number, BUNDLE_FILE_PATH)


@router.get("/rounds/{round_number}/package/{file_path:path}")
def get_round_package_file(round_number: int, file_path: str):
    """One frozen package file, served from the persisted freeze."""
    return _package_file_response(round_number, file_path)


def _package_file_response(round_number: int, file_path: str):
    conn = get_db()
    try:
        content = get_package_file(conn, round_number, file_path)
    finally:
        conn.close()

    if content is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": f"No package file {file_path} for round {round_number}"
            },
        )
    return JSONResponse(content=content)


def _run_round_in_background(lock_conn) -> None:
    """Background worker for manual rounds. Owns the advisory lock lifecycle."""
    try:
        orchestrator = RoundOrchestrator()
        result = orchestrator.run_round(TRIGGER_MANUAL)
        logger.info(
            "Manual governance round finished: status=%s, round_number=%s",
            result.get("status"),
            result.get("round_number"),
        )
    except Exception:
        logger.exception("Manual governance round failed with unexpected error")
    finally:
        try:
            release_round_lock(lock_conn)
        except Exception:
            logger.exception("Failed to release round advisory lock")


@router.post("/rounds/trigger")
def trigger_round(
    reanchor: bool | None = Query(default=None),
    x_api_key: str | None = Header(default=None),
):
    """Trigger a governance round manually.

    Requires an explicit `reanchor` choice: true resets the schedule so
    the next automated round runs one cadence after this trigger, false
    leaves the schedule untouched (extra out-of-band run).

    Returns 202 if started, 400 if `reanchor` is missing, 409 if a round
    is already in progress, 403 if auth fails or the endpoint is not
    configured.
    """
    auth_error = check_admin_auth(x_api_key)
    if auth_error is not None:
        return auth_error

    if reanchor is None:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": (
                    "reanchor query parameter is required: "
                    "reanchor=true resets the schedule to now + cadence, "
                    "reanchor=false leaves the next scheduled round unchanged"
                )
            },
        )

    lock_conn, lock_error = acquire_round_lock()
    if lock_error is not None:
        return lock_error

    try:
        check_conn = get_db()
        try:
            cleanup_interrupted_rounds(check_conn)
            active = get_active_round(check_conn)
        finally:
            check_conn.close()
        if active is not None:
            release_round_lock(lock_conn)
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "error": (
                        f"Governance round {active['round_number']} is still "
                        f"{active['status']}"
                    )
                },
            )

        if reanchor:
            reanchor_schedule(lock_conn)
        thread = threading.Thread(
            target=_run_round_in_background,
            args=(lock_conn,),
            daemon=True,
        )
        thread.start()
    except Exception:
        release_round_lock(lock_conn)
        raise

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"status": "started", "reanchor": reanchor},
    )
