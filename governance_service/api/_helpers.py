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


def acquire_refresh_lock() -> tuple[object | None, JSONResponse | None]:
    """Acquire the refresh lock and return its owning DB connection.

    The returned connection must remain open for the full execution window
    because PostgreSQL advisory locks are session-scoped. Callers that
    receive a connection are responsible for releasing the lock and
    closing the connection.
    """
    connection = get_db()
    try:
        connection.autocommit = True
        if try_advisory_lock(connection, REFRESH_ADVISORY_LOCK_ID):
            return connection, None
    except Exception:
        connection.close()
        raise

    connection.close()
    return None, JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"error": "A pool refresh is already in progress"},
    )


def release_refresh_lock(connection) -> None:
    """Release a previously acquired refresh lock and close its connection."""
    try:
        release_advisory_lock(connection, REFRESH_ADVISORY_LOCK_ID)
    finally:
        connection.close()
