"""RoundMemoPublisher behavior, and its wiring to a real PFTLClient.

Every ledger call is mocked, per the task's own verification requirement.
Payload/config/DB-lookup behavior is tested against real Postgres (this
repo's own rule); only the final ``submit_and_wait``/``autofill`` transport
calls are ever replaced.
"""

from governance_service.config import settings
from governance_service.services.orchestrator import RoundState
from governance_service.services.round_memo_publisher import (
    COMPLETE_STATUS,
    FROZEN_STATUS,
    RoundMemoPublisher,
    build_memo_payload,
)

TEST_SEED = "sEdTM1uX8pu2do5XvTnutH6HsouMaM2"


def _insert_round_with_hash(db, round_number, package_hash):
    cursor = db.cursor()
    cursor.execute(
        """
        INSERT INTO governance_rounds
            (round_number, status, trigger_source, package_hash)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (round_number, RoundState.FROZEN.value, "manual", package_hash),
    )
    round_id = cursor.fetchone()[0]
    db.commit()
    cursor.close()
    return round_id


def _insert_round_without_hash(db, round_number):
    cursor = db.cursor()
    cursor.execute(
        """
        INSERT INTO governance_rounds (round_number, status, trigger_source)
        VALUES (%s, %s, %s)
        RETURNING id
        """,
        (round_number, RoundState.CREATED.value, "manual"),
    )
    round_id = cursor.fetchone()[0]
    db.commit()
    cursor.close()
    return round_id


class TestStatusConstantsMatchRoundState:
    """FROZEN_STATUS/COMPLETE_STATUS are duplicated (not imported) to avoid
    a circular import — this test is the drift guard: if orchestrator.py's
    RoundState values ever change, this fails immediately in CI."""

    def test_frozen_status_matches_round_state(self):
        assert FROZEN_STATUS == RoundState.FROZEN.value

    def test_complete_status_matches_round_state(self):
        assert COMPLETE_STATUS == RoundState.COMPLETE.value


class TestBuildMemoPayload:
    def test_shape(self):
        payload = build_memo_payload(7, "FROZEN", "abc123")
        assert payload == {"round_id": 7, "status": "FROZEN", "package_hash": "abc123"}


class TestGetClient:
    def test_returns_none_when_pftl_disabled(self, monkeypatch):
        monkeypatch.setattr(settings, "pftl_rpc_url", "")
        monkeypatch.setattr(settings, "pftl_wallet_secret", "")
        monkeypatch.setattr(settings, "pftl_memo_destination", "")
        publisher = RoundMemoPublisher()
        assert publisher._get_client() is None

    def test_returns_injected_client_without_touching_settings(self):
        sentinel = object()
        publisher = RoundMemoPublisher(pftl_client=sentinel)
        assert publisher._get_client() is sentinel

    def test_lazily_constructs_and_caches_a_real_client_when_enabled(self, monkeypatch):
        monkeypatch.setattr(settings, "pftl_rpc_url", "https://rpc.example.com")
        monkeypatch.setattr(settings, "pftl_wallet_secret", TEST_SEED)
        monkeypatch.setattr(settings, "pftl_memo_destination", "rDestination")

        publisher = RoundMemoPublisher()
        first = publisher._get_client()
        second = publisher._get_client()

        assert first is not None
        assert first is second

    def test_returns_none_and_does_not_raise_if_client_init_fails(self, monkeypatch):
        # PFTLClient's own __init__ only validates presence (not validity)
        # of its three required settings, and wallet derivation is lazy —
        # so a garbage secret alone can't fail construction. To prove the
        # try/except in _get_client is genuinely wired up (not just
        # unreachable defensive code), PFTLClient itself is replaced with
        # something that raises on construction.
        monkeypatch.setattr(settings, "pftl_rpc_url", "https://rpc.example.com")
        monkeypatch.setattr(settings, "pftl_wallet_secret", TEST_SEED)
        monkeypatch.setattr(settings, "pftl_memo_destination", "rDestination")

        def _raise(*args, **kwargs):
            raise RuntimeError("client construction blew up")

        monkeypatch.setattr(
            "governance_service.services.round_memo_publisher.PFTLClient", _raise
        )

        publisher = RoundMemoPublisher()
        assert publisher._get_client() is None


class TestPublishRoundFrozen:
    def test_skips_when_package_hash_missing(self, db):
        round_id = _insert_round_without_hash(db, 1)
        publisher = RoundMemoPublisher(pftl_client=object())
        assert publisher.publish_round_frozen(db, round_id) is None

    def test_skips_when_pftl_not_configured(self, db, monkeypatch):
        monkeypatch.setattr(settings, "pftl_rpc_url", "")
        monkeypatch.setattr(settings, "pftl_wallet_secret", "")
        monkeypatch.setattr(settings, "pftl_memo_destination", "")
        round_id = _insert_round_with_hash(db, 1, "hash123")
        publisher = RoundMemoPublisher()
        assert publisher.publish_round_frozen(db, round_id) is None

    def test_calls_submit_memo_with_frozen_status_and_correct_memo_type(self, db):
        round_id = _insert_round_with_hash(db, 1, "hash123")

        class _FakeClient:
            def __init__(self):
                self.calls = []

            def submit_memo(self, memo_data, memo_type=None):
                self.calls.append((memo_data, memo_type))
                return True, "TXHASH", None

        fake_client = _FakeClient()
        publisher = RoundMemoPublisher(pftl_client=fake_client)

        tx_hash = publisher.publish_round_frozen(db, round_id)

        assert tx_hash == "TXHASH"
        assert len(fake_client.calls) == 1
        memo_data, memo_type = fake_client.calls[0]
        assert memo_type == "pf_governance_round_frozen_v1"
        assert '"round_id":' in memo_data
        assert '"status":"FROZEN"' in memo_data
        assert '"package_hash":"hash123"' in memo_data

    def test_returns_none_on_submit_failure_without_raising(self, db):
        round_id = _insert_round_with_hash(db, 1, "hash123")

        class _FailingClient:
            def submit_memo(self, memo_data, memo_type=None):
                return False, None, "tecUNFUNDED"

        publisher = RoundMemoPublisher(pftl_client=_FailingClient())
        assert publisher.publish_round_frozen(db, round_id) is None


class TestPublishRoundComplete:
    def test_skips_when_package_hash_missing(self, db):
        round_id = _insert_round_without_hash(db, 1)
        publisher = RoundMemoPublisher(pftl_client=object())
        assert publisher.publish_round_complete(db, round_id) is None

    def test_calls_submit_memo_with_complete_status_and_correct_memo_type(self, db):
        round_id = _insert_round_with_hash(db, 2, "hash456")

        class _FakeClient:
            def __init__(self):
                self.calls = []

            def submit_memo(self, memo_data, memo_type=None):
                self.calls.append((memo_data, memo_type))
                return True, "TXHASH2", None

        fake_client = _FakeClient()
        publisher = RoundMemoPublisher(pftl_client=fake_client)

        tx_hash = publisher.publish_round_complete(db, round_id)

        assert tx_hash == "TXHASH2"
        memo_data, memo_type = fake_client.calls[0]
        assert memo_type == "pf_governance_round_complete_v1"
        assert '"status":"COMPLETE"' in memo_data

    def test_returns_none_on_submit_failure_without_raising(self, db):
        round_id = _insert_round_with_hash(db, 2, "hash456")

        class _FailingClient:
            def submit_memo(self, memo_data, memo_type=None):
                return False, None, "boom"

        publisher = RoundMemoPublisher(pftl_client=_FailingClient())
        assert publisher.publish_round_complete(db, round_id) is None


class _FakeSuccessResponse:
    def __init__(self, tx_hash):
        self._tx_hash = tx_hash

    def is_successful(self):
        return True

    @property
    def result(self):
        return {"hash": self._tx_hash}


class _FakeFailureResponse:
    def is_successful(self):
        return False

    @property
    def result(self):
        return {"engine_result_message": "tecUNFUNDED_PAYMENT"}


class TestFullDefaultPathWithMockedLedgerTransport:
    """Proves the real default RoundMemoPublisher() -> real PFTLClient()
    construction chain works end-to-end, with only the ledger transport
    (autofill/submit_and_wait/JsonRpcClient) mocked — not the publisher or
    the client wrapping it."""

    def test_publish_round_frozen_end_to_end_with_pftl_enabled(self, db, monkeypatch):
        monkeypatch.setattr(settings, "pftl_rpc_url", "https://rpc.example.com")
        monkeypatch.setattr(settings, "pftl_wallet_secret", TEST_SEED)
        monkeypatch.setattr(settings, "pftl_memo_destination", "rDestination")
        monkeypatch.setattr(
            "governance_service.clients.pftl.JsonRpcClient", lambda url: object()
        )
        monkeypatch.setattr(
            "governance_service.clients.pftl.autofill", lambda tx, c: tx
        )
        monkeypatch.setattr(
            "governance_service.clients.pftl.submit_and_wait",
            lambda tx, c, w: _FakeSuccessResponse("E2ETXHASH"),
        )

        round_id = _insert_round_with_hash(db, 3, "e2ehash")
        publisher = RoundMemoPublisher()

        tx_hash = publisher.publish_round_frozen(db, round_id)

        assert tx_hash == "E2ETXHASH"

    def test_publish_round_frozen_end_to_end_reports_ledger_failure(
        self, db, monkeypatch
    ):
        monkeypatch.setattr(settings, "pftl_rpc_url", "https://rpc.example.com")
        monkeypatch.setattr(settings, "pftl_wallet_secret", TEST_SEED)
        monkeypatch.setattr(settings, "pftl_memo_destination", "rDestination")
        monkeypatch.setattr(
            "governance_service.clients.pftl.JsonRpcClient", lambda url: object()
        )
        monkeypatch.setattr(
            "governance_service.clients.pftl.autofill", lambda tx, c: tx
        )
        monkeypatch.setattr(
            "governance_service.clients.pftl.submit_and_wait",
            lambda tx, c, w: _FakeFailureResponse(),
        )

        round_id = _insert_round_with_hash(db, 4, "e2efailhash")
        publisher = RoundMemoPublisher()

        tx_hash = publisher.publish_round_frozen(db, round_id)

        assert tx_hash is None
