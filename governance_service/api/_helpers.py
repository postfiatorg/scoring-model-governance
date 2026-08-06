"""Shared precondition helpers for admin-gated API endpoints."""

from fastapi import status
from fastapi.responses import JSONResponse

from governance_service.config import settings
from governance_service.database import (
    get_db,
    release_advisory_lock,
    try_advisory_lock,
)
from governance_service.services.pool_refresh import REFRESH_ADVISORY_LOCK_ID
from governance_service.services.scheduler import ROUND_ADVISORY_LOCK_ID


def check_admin_auth(x_api_key: str | None) -> JSONResponse | None:
    """Return a 403 response if admin auth fails, otherwise ``None``."""
    if not settings.admin_api_key:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"error": "Admin endpoint not configured"},
        )
    if x_api_key != settings.admin_api_key:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"error": "Invalid API key"},
        )
    return None


def _acquire_lock(lock_id: int, busy_error: str) -> tuple[object | None, JSONResponse | None]:
    """Acquire an advisory lock and return its owning DB connection.

    The returned connection must remain open for the full execution window
    because PostgreSQL advisory locks are session-scoped. Callers that
    receive a connection are responsible for releasing the lock and
    closing the connection.
    """
    connection = get_db()
    try:
        connection.autocommit = True
        if try_advisory_lock(connection, lock_id):
            return connection, None
    except Exception:
        connection.close()
        raise

    connection.close()
    return None, JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"error": busy_error},
    )


def _release_lock(connection, lock_id: int) -> None:
    try:
        release_advisory_lock(connection, lock_id)
    finally:
        connection.close()


def acquire_refresh_lock() -> tuple[object | None, JSONResponse | None]:
    """Acquire the refresh lock and return its owning DB connection."""
    return _acquire_lock(
        REFRESH_ADVISORY_LOCK_ID, "A pool refresh is already in progress"
    )


def release_refresh_lock(connection) -> None:
    """Release a previously acquired refresh lock and close its connection."""
    _release_lock(connection, REFRESH_ADVISORY_LOCK_ID)


def acquire_round_lock() -> tuple[object | None, JSONResponse | None]:
    """Acquire the governance round lock and return its owning DB connection."""
    return _acquire_lock(
        ROUND_ADVISORY_LOCK_ID, "A governance round is already in progress"
    )


def release_round_lock(connection) -> None:
    """Release a previously acquired round lock and close its connection."""
    _release_lock(connection, ROUND_ADVISORY_LOCK_ID)
