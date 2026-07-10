"""Migration runner behavior against a real PostgreSQL database."""

import pytest

from governance_service import database
from governance_service.database import get_db, init_db_if_needed

TEST_MIGRATION = "900_test_migration.sql"
TEST_TABLE = "migration_runner_smoke_test"


@pytest.fixture()
def temp_migration(tmp_path, monkeypatch):
    """A temporary migrations directory holding one throwaway migration."""
    (tmp_path / TEST_MIGRATION).write_text(
        f"CREATE TABLE {TEST_TABLE} (id INTEGER PRIMARY KEY);\n"
        f"INSERT INTO {TEST_TABLE} (id) VALUES (1);\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(database, "MIGRATIONS_PATH", tmp_path)

    yield

    connection = get_db()
    cursor = connection.cursor()
    cursor.execute(f"DROP TABLE IF EXISTS {TEST_TABLE}")
    cursor.execute(
        "DELETE FROM schema_migrations WHERE filename = %s", (TEST_MIGRATION,)
    )
    connection.commit()
    connection.close()


def test_applies_pending_migration_and_records_it(temp_migration):
    init_db_if_needed()

    connection = get_db()
    cursor = connection.cursor()
    cursor.execute(f"SELECT id FROM {TEST_TABLE}")
    assert cursor.fetchone() == (1,)
    cursor.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE filename = %s",
        (TEST_MIGRATION,),
    )
    assert cursor.fetchone() == (1,)
    connection.close()


def test_applied_migration_is_not_rerun(temp_migration):
    init_db_if_needed()
    # A second run must skip the recorded migration; re-applying it would fail
    # on the primary-key insert.
    init_db_if_needed()

    connection = get_db()
    cursor = connection.cursor()
    cursor.execute(f"SELECT COUNT(*) FROM {TEST_TABLE}")
    assert cursor.fetchone() == (1,)
    connection.close()
