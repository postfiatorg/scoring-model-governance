"""Shared test fixtures."""

import pytest
from fastapi.testclient import TestClient

from governance_service.config import settings
from governance_service.database import get_db, init_db_if_needed
from governance_service.main import app


@pytest.fixture(autouse=True)
def _publication_disabled(monkeypatch):
    """Keep the suite from ever publishing or pinning for real.

    A developer's .env may carry live RECORDS_GITHUB_TOKEN or IPFS
    credentials; tests that exercise publication enable it explicitly on
    top of this guard.
    """
    monkeypatch.setattr(settings, "records_github_token", "")
    monkeypatch.setattr(settings, "ipfs_api_url", "")
    monkeypatch.setattr(settings, "pinata_api_key", "")
    monkeypatch.setattr(settings, "pinata_api_secret", "")


@pytest.fixture()
def client():
    """FastAPI test client running the full startup lifecycle against the test database."""
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def db():
    """A clean database connection; refresh tables are wiped around each test."""
    init_db_if_needed()
    connection = get_db()
    cursor = connection.cursor()
    cursor.execute("DELETE FROM pool_refresh_candidates")
    cursor.execute("DELETE FROM pool_refreshes")
    cursor.execute("DELETE FROM blocklist")
    connection.commit()

    yield connection

    connection.rollback()
    cursor = connection.cursor()
    cursor.execute("DELETE FROM pool_refresh_candidates")
    cursor.execute("DELETE FROM pool_refreshes")
    cursor.execute("DELETE FROM blocklist")
    connection.commit()
    connection.close()
