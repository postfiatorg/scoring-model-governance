"""GitHub Contents API records client against a mocked transport."""

import json

import httpx
import pytest

from governance_service.clients import github_records
from governance_service.clients.github_records import (
    GitHubRecordsClient,
    GitHubRecordsError,
)
from governance_service.config import settings

FILE_PATH = "records/pool-refreshes/local/2026-07-17-refresh-1.json"
COMMIT_URL = "https://github.com/postfiatorg/scoring-model-governance/commit/abc123"


def _mock_transport(monkeypatch, handler):
    real_client = httpx.Client

    def factory(**kwargs):
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(github_records.httpx, "Client", factory)


def _client() -> GitHubRecordsClient:
    return GitHubRecordsClient(token="test-token", repo="postfiatorg/scoring-model-governance")


def test_requires_token():
    with pytest.raises(ValueError, match="RECORDS_GITHUB_TOKEN"):
        GitHubRecordsClient(token="", repo="org/repo")


def test_creates_file_when_absent(monkeypatch):
    puts = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(404)
        puts.append(json.loads(request.content))
        return httpx.Response(201, json={"commit": {"html_url": COMMIT_URL}})

    _mock_transport(monkeypatch, handler)
    url = _client().publish(FILE_PATH, '{"a": 1}', "Publish record")

    assert url == COMMIT_URL
    assert len(puts) == 1
    assert "sha" not in puts[0]
    assert puts[0]["branch"] == settings.records_github_branch


def test_updates_existing_file_with_sha(monkeypatch):
    puts = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"sha": "existing-sha"})
        puts.append(json.loads(request.content))
        return httpx.Response(200, json={"commit": {"html_url": COMMIT_URL}})

    _mock_transport(monkeypatch, handler)
    url = _client().publish(FILE_PATH, "content", "Update record")

    assert url == COMMIT_URL
    assert puts[0]["sha"] == "existing-sha"


def test_retries_conflict_then_succeeds(monkeypatch):
    monkeypatch.setattr(settings, "http_retry_base_delay", 0)
    attempts = {"put": 0}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"sha": f"sha-{attempts['put']}"})
        attempts["put"] += 1
        if attempts["put"] == 1:
            return httpx.Response(409, text="sha mismatch")
        return httpx.Response(200, json={"commit": {"html_url": COMMIT_URL}})

    _mock_transport(monkeypatch, handler)
    assert _client().publish(FILE_PATH, "content", "Publish") == COMMIT_URL
    assert attempts["put"] == 2


def test_fails_fast_on_auth_error(monkeypatch):
    def handler(request):
        return httpx.Response(401, text="bad credentials")

    _mock_transport(monkeypatch, handler)
    with pytest.raises(GitHubRecordsError, match="failed fast with HTTP 401"):
        _client().publish(FILE_PATH, "content", "Publish")


def test_error_when_commit_url_missing(monkeypatch):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(404)
        return httpx.Response(201, json={"commit": {}})

    _mock_transport(monkeypatch, handler)
    with pytest.raises(GitHubRecordsError, match="commit.html_url"):
        _client().publish(FILE_PATH, "content", "Publish")
