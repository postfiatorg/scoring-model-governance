"""Pool endpoints — the admin-guarded manual refresh trigger."""

import logging
import threading

from fastapi import APIRouter, Header, status
from fastapi.responses import JSONResponse

from governance_service.api._helpers import (
    acquire_refresh_lock,
    check_admin_auth,
    release_refresh_lock,
)
from governance_service.database import get_db
from governance_service.services.pool_refresh import (
    create_refresh,
    execute_refresh,
    fail_refresh,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/governance")


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
