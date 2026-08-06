"""Round endpoints — the manual round trigger and the package fetch path.

The package routes are the HTTPS side of the methodology's fetch contract:
verifiers fetch over HTTPS with IPFS as the fallback. The full rounds API
(round list and detail, the sidecar config endpoint) arrives with G.5.7.
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
from governance_service.database import get_db
from governance_service.services.orchestrator import (
    TRIGGER_MANUAL,
    RoundOrchestrator,
    cleanup_interrupted_rounds,
    get_active_round,
)
from governance_service.services.round_package import BUNDLE_FILE_PATH, get_package_file
from governance_service.services.scheduler import reanchor_schedule

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/governance")


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
