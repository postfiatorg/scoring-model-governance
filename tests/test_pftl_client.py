"""PFTLClient behavior — wallet derivation and memo submission.

Ledger calls are always mocked here, per the task's own verification
requirement ("success and error paths with ledger calls mocked"). The
two fixtures below are well-known, documented non-production values used
only to exercise wallet derivation deterministically — never real
secrets.
"""

import asyncio
import threading

import pytest

from governance_service.clients.pftl import (
    PFTLClient,
    wallet_from_hex_key,
    wallet_from_secret,
)

# Non-production test fixtures only — never real secrets.
TEST_PRIVATE_KEY = "00a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1"
TEST_SEED = "sEdTM1uX8pu2do5XvTnutH6HsouMaM2"


class TestWalletFromHexKey:
    def test_derives_a_classic_address(self):
        wallet = wallet_from_hex_key(TEST_PRIVATE_KEY)
        assert wallet.classic_address.startswith("r")

    def test_is_deterministic(self):
        first = wallet_from_hex_key(TEST_PRIVATE_KEY)
        second = wallet_from_hex_key(TEST_PRIVATE_KEY)
        assert first.classic_address == second.classic_address
        assert first.public_key == second.public_key

    def test_accepts_0x_prefix(self):
        prefixed = wallet_from_hex_key("0x" + TEST_PRIVATE_KEY)
        unprefixed = wallet_from_hex_key(TEST_PRIVATE_KEY)
        assert prefixed.classic_address == unprefixed.classic_address

    def test_accepts_uppercase_0X_prefix(self):
        prefixed = wallet_from_hex_key("0X" + TEST_PRIVATE_KEY)
        unprefixed = wallet_from_hex_key(TEST_PRIVATE_KEY)
        assert prefixed.classic_address == unprefixed.classic_address

    def test_different_keys_derive_different_addresses(self):
        other_key = "1" + TEST_PRIVATE_KEY[1:]
        first = wallet_from_hex_key(TEST_PRIVATE_KEY)
        second = wallet_from_hex_key(other_key)
        assert first.classic_address != second.classic_address


class TestWalletFromSecret:
    def test_seed_prefixed_secret_uses_from_seed(self):
        wallet = wallet_from_secret(TEST_SEED)
        assert wallet.classic_address.startswith("r")

    def test_hex_secret_matches_wallet_from_hex_key(self):
        via_secret = wallet_from_secret(TEST_PRIVATE_KEY)
        via_hex = wallet_from_hex_key(TEST_PRIVATE_KEY)
        assert via_secret.classic_address == via_hex.classic_address

    def test_strips_surrounding_whitespace(self):
        padded = wallet_from_secret(f"  {TEST_SEED}  ")
        unpadded = wallet_from_secret(TEST_SEED)
        assert padded.classic_address == unpadded.classic_address

    def test_seed_and_hex_key_derive_different_wallets(self):
        seed_wallet = wallet_from_secret(TEST_SEED)
        hex_wallet = wallet_from_secret(TEST_PRIVATE_KEY)
        assert seed_wallet.classic_address != hex_wallet.classic_address


class TestInit:
    def test_requires_rpc_url(self, monkeypatch):
        monkeypatch.setattr("governance_service.clients.pftl.settings.pftl_rpc_url", "")
        with pytest.raises(ValueError, match="PFTL_RPC_URL"):
            PFTLClient(
                wallet_secret=TEST_SEED,
                memo_destination="rDestination",
                rpc_url="",
            )

    def test_requires_wallet_secret(self):
        with pytest.raises(ValueError, match="PFTL_WALLET_SECRET"):
            PFTLClient(
                rpc_url="https://rpc.example.com",
                memo_destination="rDestination",
                wallet_secret="",
            )

    def test_requires_memo_destination(self):
        with pytest.raises(ValueError, match="PFTL_MEMO_DESTINATION"):
            PFTLClient(
                rpc_url="https://rpc.example.com",
                wallet_secret=TEST_SEED,
                memo_destination="",
            )

    def test_explicit_args_are_used_over_settings(self, monkeypatch):
        monkeypatch.setattr(
            "governance_service.clients.pftl.settings.pftl_rpc_url",
            "https://settings-rpc.example.com",
        )
        client = PFTLClient(
            rpc_url="https://explicit-rpc.example.com",
            wallet_secret=TEST_SEED,
            memo_destination="rDestination",
        )
        assert client.rpc_url == "https://explicit-rpc.example.com"

    def test_network_id_defaults_from_settings_when_not_passed(self, monkeypatch):
        # pftl_network_id is a computed property (derived from pftl_network),
        # not a settable field, so the underlying field is what's patched.
        monkeypatch.setattr(
            "governance_service.clients.pftl.settings.pftl_network", "testnet"
        )
        client = PFTLClient(
            rpc_url="https://rpc.example.com",
            wallet_secret=TEST_SEED,
            memo_destination="rDestination",
        )
        assert client.network_id == 2025

    def test_network_id_zero_is_respected_not_treated_as_falsy(self):
        client = PFTLClient(
            rpc_url="https://rpc.example.com",
            wallet_secret=TEST_SEED,
            memo_destination="rDestination",
            network_id=0,
        )
        assert client.network_id == 0


class TestWalletProperty:
    def test_wallet_is_lazily_constructed_and_cached(self):
        client = PFTLClient(
            rpc_url="https://rpc.example.com",
            wallet_secret=TEST_SEED,
            memo_destination="rDestination",
        )
        assert client._wallet is None
        first = client.wallet
        assert client._wallet is not None
        second = client.wallet
        assert first is second


class TestPublisherAddress:
    def test_matches_wallet_classic_address(self):
        client = PFTLClient(
            rpc_url="https://rpc.example.com",
            wallet_secret=TEST_SEED,
            memo_destination="rDestination",
        )
        assert client.publisher_address == client.wallet.classic_address
        assert client.publisher_address.startswith("r")


class _FakeSuccessResponse:
    def __init__(self, tx_hash="ABC123"):
        self._tx_hash = tx_hash

    def is_successful(self):
        return True

    @property
    def result(self):
        return {"hash": self._tx_hash}


class _FakeFailureResponse:
    def __init__(self, error="tecUNFUNDED"):
        self._error = error

    def is_successful(self):
        return False

    @property
    def result(self):
        return {"engine_result_message": self._error}


class TestSubmitMemo:
    def _client(self):
        return PFTLClient(
            rpc_url="https://rpc.example.com",
            wallet_secret=TEST_SEED,
            memo_destination="rDestination",
            network_id=2024,
        )

    def test_success_path_returns_tx_hash(self, monkeypatch):
        client = self._client()
        monkeypatch.setattr(
            "governance_service.clients.pftl.autofill", lambda tx, c: tx
        )
        monkeypatch.setattr(
            "governance_service.clients.pftl.submit_and_wait",
            lambda tx, c, w: _FakeSuccessResponse("DEADBEEF"),
        )

        success, tx_hash, error = client.submit_memo("hello", memo_type="my_type")

        assert success is True
        assert tx_hash == "DEADBEEF"
        assert error is None

    def test_error_path_returns_engine_result_message(self, monkeypatch):
        client = self._client()
        monkeypatch.setattr(
            "governance_service.clients.pftl.autofill", lambda tx, c: tx
        )
        monkeypatch.setattr(
            "governance_service.clients.pftl.submit_and_wait",
            lambda tx, c, w: _FakeFailureResponse("tecUNFUNDED_PAYMENT"),
        )

        success, tx_hash, error = client.submit_memo("hello")

        assert success is False
        assert tx_hash is None
        assert error == "tecUNFUNDED_PAYMENT"

    def test_exception_path_returns_stringified_exception(self, monkeypatch):
        client = self._client()

        def _raise(tx, c):
            raise RuntimeError("network unreachable")

        monkeypatch.setattr("governance_service.clients.pftl.autofill", _raise)

        success, tx_hash, error = client.submit_memo("hello")

        assert success is False
        assert tx_hash is None
        assert "network unreachable" in error

    def test_memo_type_and_data_round_trip_as_hex(self, monkeypatch):
        client = self._client()
        captured_tx = {}

        def _capture_autofill(tx, c):
            captured_tx["tx"] = tx
            return tx

        monkeypatch.setattr(
            "governance_service.clients.pftl.autofill", _capture_autofill
        )
        monkeypatch.setattr(
            "governance_service.clients.pftl.submit_and_wait",
            lambda tx, c, w: _FakeSuccessResponse(),
        )

        client.submit_memo('{"round_id": 1}', memo_type="pf_governance_round_frozen_v1")

        memo = captured_tx["tx"].memos[0]
        assert bytes.fromhex(memo.memo_type).decode("utf-8") == (
            "pf_governance_round_frozen_v1"
        )
        assert bytes.fromhex(memo.memo_data).decode("utf-8") == '{"round_id": 1}'

    def test_no_memo_type_omits_memo_type_field(self, monkeypatch):
        client = self._client()
        captured_tx = {}

        def _capture_autofill(tx, c):
            captured_tx["tx"] = tx
            return tx

        monkeypatch.setattr(
            "governance_service.clients.pftl.autofill", _capture_autofill
        )
        monkeypatch.setattr(
            "governance_service.clients.pftl.submit_and_wait",
            lambda tx, c, w: _FakeSuccessResponse(),
        )

        client.submit_memo("no type here")

        memo = captured_tx["tx"].memos[0]
        assert memo.memo_type is None

    def test_payment_uses_one_drop_and_configured_destination(self, monkeypatch):
        client = self._client()
        captured_tx = {}

        def _capture_autofill(tx, c):
            captured_tx["tx"] = tx
            return tx

        monkeypatch.setattr(
            "governance_service.clients.pftl.autofill", _capture_autofill
        )
        monkeypatch.setattr(
            "governance_service.clients.pftl.submit_and_wait",
            lambda tx, c, w: _FakeSuccessResponse(),
        )

        client.submit_memo("payload")

        tx = captured_tx["tx"]
        assert tx.amount == "1"
        assert tx.destination == "rDestination"
        assert tx.network_id == 2024
        assert tx.account == client.wallet.classic_address


class TestSubmitMemoEventLoopSafety:
    """submit_memo must work whether or not an asyncio event loop is
    already running on the calling thread — the ThreadPoolExecutor
    isolation is what makes this true."""

    def _client(self):
        return PFTLClient(
            rpc_url="https://rpc.example.com",
            wallet_secret=TEST_SEED,
            memo_destination="rDestination",
        )

    def test_works_from_a_plain_thread_with_no_event_loop(self, monkeypatch):
        client = self._client()
        monkeypatch.setattr(
            "governance_service.clients.pftl.autofill", lambda tx, c: tx
        )
        monkeypatch.setattr(
            "governance_service.clients.pftl.submit_and_wait",
            lambda tx, c, w: _FakeSuccessResponse("FROMTHREAD"),
        )

        result_box = {}

        def _run():
            result_box["result"] = client.submit_memo("payload")

        thread = threading.Thread(target=_run)
        thread.start()
        thread.join(timeout=5)

        assert result_box["result"] == (True, "FROMTHREAD", None)

    def test_works_when_an_event_loop_is_already_running(self, monkeypatch):
        client = self._client()
        monkeypatch.setattr(
            "governance_service.clients.pftl.autofill", lambda tx, c: tx
        )
        monkeypatch.setattr(
            "governance_service.clients.pftl.submit_and_wait",
            lambda tx, c, w: _FakeSuccessResponse("FROMLOOP"),
        )

        async def _run_inside_loop():
            # submit_memo is sync and internally isolates the real
            # ledger call in its own thread pool, so calling it from
            # inside a running event loop must not deadlock or raise.
            return client.submit_memo("payload")

        result = asyncio.run(_run_inside_loop())

        assert result == (True, "FROMLOOP", None)

    def test_exception_inside_the_thread_pool_is_caught_not_raised(self, monkeypatch):
        client = self._client()

        def _raise(tx, c, w):
            raise RuntimeError("boom inside pool")

        monkeypatch.setattr(
            "governance_service.clients.pftl.autofill", lambda tx, c: tx
        )
        monkeypatch.setattr(
            "governance_service.clients.pftl.submit_and_wait", _raise
        )

        success, tx_hash, error = client.submit_memo("payload")

        assert success is False
        assert tx_hash is None
        assert "boom inside pool" in error
