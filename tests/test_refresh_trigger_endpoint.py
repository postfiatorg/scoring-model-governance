"""Tests for the manual pool-refresh trigger endpoint."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import status

from governance_service.api.pool import _run_refresh_in_background


class TestAuth:
    @patch("governance_service.api._helpers.settings")
    def test_returns_403_when_admin_key_not_configured(self, mock_settings, client):
        mock_settings.admin_api_key = ""

        response = client.post("/api/governance/pool/refresh")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert "not configured" in response.json()["error"]

    @patch("governance_service.api._helpers.settings")
    def test_returns_403_when_api_key_missing(self, mock_settings, client):
        mock_settings.admin_api_key = "secret-key"

        response = client.post("/api/governance/pool/refresh")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert "Invalid" in response.json()["error"]

    @patch("governance_service.api._helpers.settings")
    def test_returns_403_when_api_key_wrong(self, mock_settings, client):
        mock_settings.admin_api_key = "secret-key"

        response = client.post(
            "/api/governance/pool/refresh",
            headers={"X-API-Key": "wrong-key"},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN


class TestLockContention:
    @patch("governance_service.api._helpers.try_advisory_lock", return_value=False)
    @patch("governance_service.api._helpers.get_db")
    @patch("governance_service.api._helpers.settings")
    def test_returns_409_when_refresh_in_progress(
        self, mock_settings, mock_get_db, mock_lock, client,
    ):
        mock_settings.admin_api_key = "secret-key"
        mock_get_db.return_value = MagicMock()

        response = client.post(
            "/api/governance/pool/refresh",
            headers={"X-API-Key": "secret-key"},
        )
        assert response.status_code == status.HTTP_409_CONFLICT
        assert "already in progress" in response.json()["error"]


class TestBackgroundExecution:
    @patch("governance_service.api.pool.threading.Thread")
    @patch("governance_service.api.pool.create_refresh", return_value=42)
    @patch("governance_service.api.pool.get_db")
    @patch("governance_service.api._helpers.try_advisory_lock", return_value=True)
    @patch("governance_service.api._helpers.get_db")
    @patch("governance_service.api._helpers.settings")
    def test_returns_202_and_starts_thread(
        self, mock_settings, mock_helpers_get_db, mock_lock,
        mock_pool_get_db, mock_create_refresh, mock_thread, client,
    ):
        mock_settings.admin_api_key = "secret-key"
        lock_conn = MagicMock()
        mock_helpers_get_db.return_value = lock_conn
        mock_pool_get_db.return_value = MagicMock()
        mock_thread_instance = MagicMock()
        mock_thread.return_value = mock_thread_instance

        response = client.post(
            "/api/governance/pool/refresh",
            headers={"X-API-Key": "secret-key"},
        )

        assert response.status_code == status.HTTP_202_ACCEPTED
        assert response.json() == {"status": "started", "refresh_id": 42}
        assert mock_thread.call_args.kwargs["args"] == (lock_conn, 42)
        assert lock_conn.autocommit is True
        mock_thread_instance.start.assert_called_once()

    @patch("governance_service.api.pool.fail_refresh")
    @patch("governance_service.api.pool.release_refresh_lock")
    @patch(
        "governance_service.api.pool.create_refresh",
        side_effect=RuntimeError("insert failed"),
    )
    @patch("governance_service.api.pool.get_db")
    @patch("governance_service.api._helpers.try_advisory_lock", return_value=True)
    @patch("governance_service.api._helpers.get_db")
    @patch("governance_service.api._helpers.settings")
    def test_releases_lock_when_row_creation_fails(
        self, mock_settings, mock_helpers_get_db, mock_lock,
        mock_pool_get_db, mock_create_refresh, mock_release, mock_fail, client,
    ):
        mock_settings.admin_api_key = "secret-key"
        lock_conn = MagicMock()
        mock_helpers_get_db.return_value = lock_conn
        mock_pool_get_db.return_value = MagicMock()

        with pytest.raises(RuntimeError):
            client.post(
                "/api/governance/pool/refresh",
                headers={"X-API-Key": "secret-key"},
            )

        mock_release.assert_called_once_with(lock_conn)
        mock_fail.assert_not_called()

    @patch("governance_service.api.pool.fail_refresh")
    @patch("governance_service.api.pool.release_refresh_lock")
    @patch("governance_service.api.pool.threading.Thread")
    @patch("governance_service.api.pool.create_refresh", return_value=42)
    @patch("governance_service.api.pool.get_db")
    @patch("governance_service.api._helpers.try_advisory_lock", return_value=True)
    @patch("governance_service.api._helpers.get_db")
    @patch("governance_service.api._helpers.settings")
    def test_marks_refresh_failed_when_thread_start_fails(
        self, mock_settings, mock_helpers_get_db, mock_lock, mock_pool_get_db,
        mock_create_refresh, mock_thread, mock_release, mock_fail, client,
    ):
        mock_settings.admin_api_key = "secret-key"
        lock_conn = MagicMock()
        mock_helpers_get_db.return_value = lock_conn
        worker_conn = MagicMock()
        mock_pool_get_db.return_value = worker_conn
        mock_thread.return_value.start.side_effect = RuntimeError("no threads")

        with pytest.raises(RuntimeError):
            client.post(
                "/api/governance/pool/refresh",
                headers={"X-API-Key": "secret-key"},
            )

        mock_fail.assert_called_once_with(
            worker_conn, 42, "THREAD_START: no threads"
        )
        mock_release.assert_called_once_with(lock_conn)


class TestBackgroundWorker:
    @patch("governance_service.api.pool.release_refresh_lock")
    @patch("governance_service.api.pool.execute_refresh")
    @patch("governance_service.api.pool.get_db")
    def test_releases_lock_after_refresh(
        self, mock_get_db, mock_execute, mock_release,
    ):
        lock_conn = MagicMock()
        worker_conn = MagicMock()
        mock_get_db.return_value = worker_conn

        _run_refresh_in_background(lock_conn, 7)

        mock_execute.assert_called_once_with(worker_conn, 7)
        worker_conn.close.assert_called_once()
        mock_release.assert_called_once_with(lock_conn)

    @patch("governance_service.api.pool.release_refresh_lock")
    @patch("governance_service.api.pool.fail_refresh")
    @patch(
        "governance_service.api.pool.execute_refresh",
        side_effect=RuntimeError("boom"),
    )
    @patch("governance_service.api.pool.get_db")
    def test_marks_failed_and_releases_lock_after_unexpected_failure(
        self, mock_get_db, mock_execute, mock_fail, mock_release,
    ):
        lock_conn = MagicMock()
        worker_conn = MagicMock()
        mock_get_db.return_value = worker_conn

        _run_refresh_in_background(lock_conn, 7)

        mock_fail.assert_called_once_with(worker_conn, 7, "UNEXPECTED: boom")
        worker_conn.close.assert_called_once()
        mock_release.assert_called_once_with(lock_conn)

    @patch("governance_service.api.pool.release_refresh_lock")
    @patch(
        "governance_service.api.pool.get_db",
        side_effect=RuntimeError("db down"),
    )
    def test_releases_lock_when_connection_fails(self, mock_get_db, mock_release):
        lock_conn = MagicMock()

        _run_refresh_in_background(lock_conn, 7)

        mock_release.assert_called_once_with(lock_conn)
