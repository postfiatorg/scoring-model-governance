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


class TestPftlSettings:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("PFTL_RPC_URL", raising=False)
        monkeypatch.delenv("PFTL_WALLET_SECRET", raising=False)
        monkeypatch.delenv("PFTL_MEMO_DESTINATION", raising=False)
        settings = Settings(_env_file=None)
        assert settings.pftl_enabled is False

    def test_enabled_only_when_all_three_are_set(self, monkeypatch):
        monkeypatch.setenv("PFTL_RPC_URL", "https://rpc.example.com")
        monkeypatch.setenv("PFTL_WALLET_SECRET", "secret")
        monkeypatch.delenv("PFTL_MEMO_DESTINATION", raising=False)
        assert Settings(_env_file=None).pftl_enabled is False

        monkeypatch.setenv("PFTL_MEMO_DESTINATION", "rAddr")
        assert Settings(_env_file=None).pftl_enabled is True

    def test_network_id_defaults_and_maps_known_networks(self, monkeypatch):
        monkeypatch.delenv("PFTL_NETWORK", raising=False)
        assert Settings(_env_file=None).pftl_network_id == 2024  # devnet default

        monkeypatch.setenv("PFTL_NETWORK", "testnet")
        assert Settings(_env_file=None).pftl_network_id == 2025

        monkeypatch.setenv("PFTL_NETWORK", "mainnet")
        assert Settings(_env_file=None).pftl_network_id == 2026

    def test_network_id_falls_back_to_devnet_for_unknown_network(self, monkeypatch):
        monkeypatch.setenv("PFTL_NETWORK", "not-a-real-network")
        assert Settings(_env_file=None).pftl_network_id == 2024
