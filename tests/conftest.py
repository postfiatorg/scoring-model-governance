"""Shared test fixtures."""

import pytest
from fastapi.testclient import TestClient

from governance_service.main import app


@pytest.fixture()
def client():
    """FastAPI test client running the full startup lifecycle against the test database."""
    with TestClient(app) as c:
        yield c
