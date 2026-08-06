"""Tests for the manual governance round trigger endpoint."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import status

from governance_service.api.rounds import _run_round_in_background


class TestAuth:
    @patch("governance_service.api._helpers.settings")
    def test_returns_403_when_admin_key_not_configured(self, mock_settings, client):
        mock_settings.admin_api_key = ""

        response = client.post("/api/governance/rounds/trigger")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert "not configured" in response.json()["error"]

    @patch("governance_service.api._helpers.settings")
    def test_returns_403_when_api_key_wrong(self, mock_settings, client):
        mock_settings.admin_api_key = "secret-key"

        response = client.post(
            "/api/governance/rounds/trigger",
            headers={"X-API-Key": "wrong-key"},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN


class TestReanchorChoice:
    @patch("governance_service.api._helpers.settings")
    def test_returns_400_when_reanchor_missing(self, mock_settings, client):
        mock_settings.admin_api_key = "secret-key"

        response = client.post(
            "/api/governance/rounds/trigger",
            headers={"X-API-Key": "secret-key"},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "reanchor" in response.json()["error"]


class TestLockContention:
    @patch("governance_service.api._helpers.try_advisory_lock", return_value=False)
    @patch("governance_service.api._helpers.get_db")
    @patch("governance_service.api._helpers.settings")
    def test_returns_409_when_round_in_progress(
        self, mock_settings, mock_get_db, mock_lock, client,
    ):
        mock_settings.admin_api_key = "secret-key"
        mock_get_db.return_value = MagicMock()

        response = client.post(
            "/api/governance/rounds/trigger?reanchor=true",
            headers={"X-API-Key": "secret-key"},
        )
        assert response.status_code == status.HTTP_409_CONFLICT
        assert "already in progress" in response.json()["error"]

    @patch("governance_service.api.rounds.release_round_lock")
    @patch(
        "governance_service.api.rounds.get_active_round",
        return_value={"id": 1, "round_number": 1, "status": "AWAITING_COMMIT_CLOSE"},
    )
    @patch("governance_service.api.rounds.cleanup_interrupted_rounds")
    @patch("governance_service.api.rounds.get_db")
    @patch("governance_service.api._helpers.try_advisory_lock", return_value=True)
    @patch("governance_service.api._helpers.get_db")
    @patch("governance_service.api._helpers.settings")
    def test_returns_409_when_a_round_is_active(
        self, mock_settings, mock_helpers_get_db, mock_lock,
        mock_rounds_get_db, mock_cleanup, mock_active, mock_release, client,
    ):
        mock_settings.admin_api_key = "secret-key"
        lock_conn = MagicMock()
        mock_helpers_get_db.return_value = lock_conn
        check_conn = MagicMock()
        mock_rounds_get_db.return_value = check_conn

        response = client.post(
            "/api/governance/rounds/trigger?reanchor=true",
            headers={"X-API-Key": "secret-key"},
        )

        assert response.status_code == status.HTTP_409_CONFLICT
        assert "round 1" in response.json()["error"]
        mock_cleanup.assert_called_once_with(check_conn)
        mock_release.assert_called_once_with(lock_conn)


class TestBackgroundExecution:
    @patch("governance_service.api.rounds.threading.Thread")
    @patch("governance_service.api.rounds.reanchor_schedule")
    @patch("governance_service.api.rounds.get_active_round", return_value=None)
    @patch("governance_service.api.rounds.cleanup_interrupted_rounds")
    @patch("governance_service.api.rounds.get_db")
    @patch("governance_service.api._helpers.try_advisory_lock", return_value=True)
    @patch("governance_service.api._helpers.get_db")
    @patch("governance_service.api._helpers.settings")
    def test_returns_202_and_starts_thread_with_reanchor(
        self, mock_settings, mock_helpers_get_db, mock_lock,
        mock_rounds_get_db, mock_cleanup, mock_active, mock_reanchor, mock_thread, client,
    ):
        mock_settings.admin_api_key = "secret-key"
        lock_conn = MagicMock()
        mock_helpers_get_db.return_value = lock_conn
        mock_rounds_get_db.return_value = MagicMock()
        mock_thread_instance = MagicMock()
        mock_thread.return_value = mock_thread_instance

        response = client.post(
            "/api/governance/rounds/trigger?reanchor=true",
            headers={"X-API-Key": "secret-key"},
        )

        assert response.status_code == status.HTTP_202_ACCEPTED
        assert response.json() == {"status": "started", "reanchor": True}
        mock_reanchor.assert_called_once_with(lock_conn)
        assert mock_thread.call_args.kwargs["args"] == (lock_conn,)
        mock_thread_instance.start.assert_called_once()

    @patch("governance_service.api.rounds.threading.Thread")
    @patch("governance_service.api.rounds.reanchor_schedule")
    @patch("governance_service.api.rounds.get_active_round", return_value=None)
    @patch("governance_service.api.rounds.cleanup_interrupted_rounds")
    @patch("governance_service.api.rounds.get_db")
    @patch("governance_service.api._helpers.try_advisory_lock", return_value=True)
    @patch("governance_service.api._helpers.get_db")
    @patch("governance_service.api._helpers.settings")
    def test_reanchor_false_leaves_schedule_untouched(
        self, mock_settings, mock_helpers_get_db, mock_lock,
        mock_rounds_get_db, mock_cleanup, mock_active, mock_reanchor, mock_thread, client,
    ):
        mock_settings.admin_api_key = "secret-key"
        mock_helpers_get_db.return_value = MagicMock()
        mock_rounds_get_db.return_value = MagicMock()
        mock_thread.return_value = MagicMock()

        response = client.post(
            "/api/governance/rounds/trigger?reanchor=false",
            headers={"X-API-Key": "secret-key"},
        )

        assert response.status_code == status.HTTP_202_ACCEPTED
        assert response.json() == {"status": "started", "reanchor": False}
        mock_reanchor.assert_not_called()

    @patch("governance_service.api.rounds.release_round_lock")
    @patch("governance_service.api.rounds.threading.Thread")
    @patch("governance_service.api.rounds.reanchor_schedule")
    @patch("governance_service.api.rounds.get_active_round", return_value=None)
    @patch("governance_service.api.rounds.cleanup_interrupted_rounds")
    @patch("governance_service.api.rounds.get_db")
    @patch("governance_service.api._helpers.try_advisory_lock", return_value=True)
    @patch("governance_service.api._helpers.get_db")
    @patch("governance_service.api._helpers.settings")
    def test_releases_lock_when_thread_start_fails(
        self, mock_settings, mock_helpers_get_db, mock_lock, mock_rounds_get_db,
        mock_cleanup, mock_active, mock_reanchor, mock_thread, mock_release, client,
    ):
        mock_settings.admin_api_key = "secret-key"
        lock_conn = MagicMock()
        mock_helpers_get_db.return_value = lock_conn
        mock_rounds_get_db.return_value = MagicMock()
        mock_thread.return_value.start.side_effect = RuntimeError("no threads")

        with pytest.raises(RuntimeError):
            client.post(
                "/api/governance/rounds/trigger?reanchor=false",
                headers={"X-API-Key": "secret-key"},
            )

        mock_release.assert_called_once_with(lock_conn)


class TestBackgroundWorker:
    @patch("governance_service.api.rounds.release_round_lock")
    @patch("governance_service.api.rounds.RoundOrchestrator")
    def test_releases_lock_after_round(self, mock_orchestrator_cls, mock_release):
        lock_conn = MagicMock()
        mock_orchestrator_cls.return_value.run_round.return_value = {
            "status": "FAILED",
            "round_number": 1,
        }

        _run_round_in_background(lock_conn)

        mock_orchestrator_cls.return_value.run_round.assert_called_once_with("manual")
        mock_release.assert_called_once_with(lock_conn)

    @patch("governance_service.api.rounds.release_round_lock")
    @patch("governance_service.api.rounds.RoundOrchestrator")
    def test_releases_lock_after_unexpected_failure(
        self, mock_orchestrator_cls, mock_release,
    ):
        lock_conn = MagicMock()
        mock_orchestrator_cls.return_value.run_round.side_effect = RuntimeError("boom")

        _run_round_in_background(lock_conn)

        mock_release.assert_called_once_with(lock_conn)
