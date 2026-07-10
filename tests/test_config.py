"""Settings loading behavior."""

from governance_service.config import MIGRATIONS_PATH, Settings

LOCAL_DEV_DATABASE_URL = (
    "postgresql://postgres:dev_password@localhost:5433/scoring_model_governance"
)


def test_database_url_defaults_to_local_dev(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(_env_file=None)
    assert settings.database_url == LOCAL_DEV_DATABASE_URL


def test_database_url_read_from_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example:secret@db:5432/governance_test")
    settings = Settings(_env_file=None)
    assert settings.database_url == "postgresql://example:secret@db:5432/governance_test"


def test_migrations_path_points_into_repo():
    assert MIGRATIONS_PATH.name == "migrations"
    assert (MIGRATIONS_PATH / "001_init.sql").exists()
