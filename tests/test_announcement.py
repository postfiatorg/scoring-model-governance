"""Governance announcement: memo formats, windows, and the announce stage."""

from datetime import datetime, timedelta, timezone

import pytest

from governance_service.config import settings
from governance_service.scoring import canonical_json_bytes
from governance_service.services import announcement as announcement_service
from governance_service.services import round_package
from governance_service.services.announcement import (
    ANNOUNCEMENT_PAYLOAD_FIELDS,
    GOVERNANCE_PROTOCOL_VERSION,
    RECEIPT_PAYLOAD_FIELDS,
    ROUND_ANNOUNCEMENT_TYPE,
    ROUND_RECEIPT_TYPE,
    AnnouncementError,
    announce_round,
    announcement_payload,
    build_announcement,
    compute_round_windows,
    receipt_payload,
)
from governance_service.services.orchestrator import (
    TRIGGER_MANUAL,
    RoundOrchestrator,
    RoundState,
)
from tests.test_round_package import _corpus, _pool, _seed_refresh, FROZEN_AT

PACKAGE_HASH = "ab" * 32
ANCHOR = datetime(2026, 8, 6, 14, 0, 0, tzinfo=timezone.utc)
COMMIT_WINDOW = timedelta(hours=48)
REVEAL_WINDOW = timedelta(hours=24)


class FakePFTLClient:
    """Records submissions; deterministic outcomes on demand."""

    def __init__(
        self,
        *,
        close_time: datetime | None = ANCHOR,
        success: bool = True,
        ledger_index: int | None = 4242,
    ):
        self.close_time = close_time
        self.success = success
        self.ledger_index = ledger_index
        self.submitted: list[dict] = []

    def latest_validated_ledger_close_time(self) -> datetime:
        if self.close_time is None:
            raise RuntimeError("ledger request failed")
        return self.close_time

    def submit_memo(self, memo_data: str, memo_type: str):
        self.submitted.append({"memo_data": memo_data, "memo_type": memo_type})
        if not self.success:
            return False, None, None, "tec_FAILURE"
        return True, "TXHASH123", self.ledger_index, None


def _windows():
    return compute_round_windows(
        frozen_at=FROZEN_AT,
        anchor=ANCHOR,
        commit_window=COMMIT_WINDOW,
        reveal_window=REVEAL_WINDOW,
    )


class TestWindows:
    def test_windows_anchor_at_emission(self):
        commit_opens, commit_closes, reveal_opens, reveal_closes = _windows()

        assert commit_opens == ANCHOR
        assert commit_closes == ANCHOR + COMMIT_WINDOW
        assert reveal_opens == commit_closes
        assert reveal_closes == commit_closes + REVEAL_WINDOW

    def test_commit_never_opens_before_the_freeze(self):
        early_anchor = FROZEN_AT - timedelta(hours=1)
        commit_opens, _, _, _ = compute_round_windows(
            frozen_at=FROZEN_AT,
            anchor=early_anchor,
            commit_window=COMMIT_WINDOW,
            reveal_window=REVEAL_WINDOW,
        )
        assert commit_opens == FROZEN_AT

    def test_non_positive_windows_are_rejected(self):
        with pytest.raises(AnnouncementError, match="commit_window"):
            compute_round_windows(
                frozen_at=FROZEN_AT,
                anchor=ANCHOR,
                commit_window=timedelta(0),
                reveal_window=REVEAL_WINDOW,
            )
        with pytest.raises(AnnouncementError, match="reveal_window"):
            compute_round_windows(
                frozen_at=FROZEN_AT,
                anchor=ANCHOR,
                commit_window=COMMIT_WINDOW,
                reveal_window=timedelta(seconds=-1),
            )


class TestPayloads:
    def _announcement(self):
        commit_opens, commit_closes, reveal_opens, reveal_closes = _windows()
        return build_announcement(
            network="devnet",
            round_number=7,
            package_cid="QmPackage",
            package_hash=PACKAGE_HASH,
            commit_opens_at=commit_opens,
            commit_closes_at=commit_closes,
            reveal_opens_at=reveal_opens,
            reveal_closes_at=reveal_closes,
        )

    def test_memo_types_are_versioned_governance_namespace(self):
        assert ROUND_ANNOUNCEMENT_TYPE == "pf_governance_round_announcement_v1"
        assert ROUND_RECEIPT_TYPE == "pf_governance_round_receipt_v1"

    def test_payload_carries_exactly_the_frozen_field_set(self):
        payload = announcement_payload(self._announcement())

        assert tuple(sorted(payload)) == tuple(sorted(ANNOUNCEMENT_PAYLOAD_FIELDS))
        assert payload["protocol_version"] == GOVERNANCE_PROTOCOL_VERSION
        assert payload["package_cid"] == "QmPackage"
        assert payload["package_hash"] == PACKAGE_HASH
        assert payload["commit_closes_at"] == (ANCHOR + COMMIT_WINDOW).isoformat()

    def test_payload_canonical_bytes_are_stable(self):
        first = canonical_json_bytes(announcement_payload(self._announcement()))
        second = canonical_json_bytes(announcement_payload(self._announcement()))
        assert first == second

    def test_invalid_inputs_are_rejected(self):
        commit_opens, commit_closes, reveal_opens, reveal_closes = _windows()
        base = {
            "network": "devnet",
            "round_number": 7,
            "package_cid": "QmPackage",
            "package_hash": PACKAGE_HASH,
            "commit_opens_at": commit_opens,
            "commit_closes_at": commit_closes,
            "reveal_opens_at": reveal_opens,
            "reveal_closes_at": reveal_closes,
        }
        for override, match in (
            ({"round_number": 0}, "round_number"),
            ({"package_cid": ""}, "package_cid"),
            ({"package_hash": "zz"}, "sha256"),
            ({"reveal_closes_at": commit_opens}, "not ordered"),
        ):
            with pytest.raises(AnnouncementError, match=match):
                build_announcement(**{**base, **override})

    def test_receipt_payload_carries_exactly_the_frozen_field_set(self):
        payload = receipt_payload(
            network="devnet",
            round_number=7,
            package_hash=PACKAGE_HASH,
            final_record_cid="QmRecord",
        )
        assert tuple(sorted(payload)) == tuple(sorted(RECEIPT_PAYLOAD_FIELDS))

    def test_receipt_rejects_bad_inputs(self):
        with pytest.raises(AnnouncementError, match="sha256"):
            receipt_payload(
                network="devnet", round_number=7,
                package_hash="nope", final_record_cid="QmRecord",
            )
        with pytest.raises(AnnouncementError, match="final_record_cid"):
            receipt_payload(
                network="devnet", round_number=7,
                package_hash=PACKAGE_HASH, final_record_cid="",
            )


def _frozen_round(db, round_number: int = 1) -> int:
    cursor = db.cursor()
    cursor.execute(
        """
        INSERT INTO governance_rounds
            (round_number, status, trigger_source, package_cid, package_hash, frozen_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            round_number,
            RoundState.FROZEN.value,
            TRIGGER_MANUAL,
            "QmPackage",
            PACKAGE_HASH,
            FROZEN_AT,
        ),
    )
    round_id = cursor.fetchone()[0]
    db.commit()
    cursor.close()
    return round_id


class TestAnnounceRound:
    def test_announce_persists_the_onchain_identity(self, db):
        round_id = _frozen_round(db)
        client = FakePFTLClient()

        result = announce_round(db, round_id, 1, client=client)

        assert result["announcement_tx_hash"] == "TXHASH123"
        assert result["announcement_ledger_index"] == 4242
        cursor = db.cursor()
        cursor.execute(
            """
            SELECT announcement_tx_hash, announcement_ledger_index,
                   commit_opens_at, commit_closes_at, reveal_opens_at, reveal_closes_at
            FROM governance_rounds WHERE id = %s
            """,
            (round_id,),
        )
        tx_hash, ledger_index, commit_opens, commit_closes, reveal_opens, reveal_closes = cursor.fetchone()
        cursor.close()
        assert (tx_hash, ledger_index) == ("TXHASH123", 4242)
        assert commit_opens == ANCHOR
        assert commit_closes == ANCHOR + timedelta(seconds=settings.round_commit_window_seconds)
        assert reveal_opens == commit_closes
        assert reveal_closes == commit_closes + timedelta(seconds=settings.round_reveal_window_seconds)

    def test_announce_submits_the_canonical_typed_memo(self, db):
        round_id = _frozen_round(db)
        client = FakePFTLClient()

        announce_round(db, round_id, 1, client=client)

        assert len(client.submitted) == 1
        submission = client.submitted[0]
        assert submission["memo_type"] == ROUND_ANNOUNCEMENT_TYPE
        assert (
            submission["memo_data"].encode("utf-8")
            == canonical_json_bytes(
                {
                    "protocol_version": GOVERNANCE_PROTOCOL_VERSION,
                    "network": settings.environment,
                    "round_number": 1,
                    "package_cid": "QmPackage",
                    "package_hash": PACKAGE_HASH,
                    "commit_opens_at": ANCHOR.isoformat(),
                    "commit_closes_at": (
                        ANCHOR + timedelta(seconds=settings.round_commit_window_seconds)
                    ).isoformat(),
                    "reveal_opens_at": (
                        ANCHOR + timedelta(seconds=settings.round_commit_window_seconds)
                    ).isoformat(),
                    "reveal_closes_at": (
                        ANCHOR
                        + timedelta(seconds=settings.round_commit_window_seconds)
                        + timedelta(seconds=settings.round_reveal_window_seconds)
                    ).isoformat(),
                }
            )
        )

    def test_unfrozen_round_cannot_announce(self, db):
        cursor = db.cursor()
        cursor.execute(
            """
            INSERT INTO governance_rounds (round_number, status, trigger_source)
            VALUES (1, %s, %s) RETURNING id
            """,
            (RoundState.CREATED.value, TRIGGER_MANUAL),
        )
        round_id = cursor.fetchone()[0]
        db.commit()
        cursor.close()

        with pytest.raises(AnnouncementError, match="no frozen package identity"):
            announce_round(db, round_id, 1, client=FakePFTLClient())

    def test_submission_failure_raises_with_the_error(self, db):
        round_id = _frozen_round(db)

        with pytest.raises(AnnouncementError, match="tec_FAILURE"):
            announce_round(db, round_id, 1, client=FakePFTLClient(success=False))

    def test_missing_ledger_index_raises(self, db):
        round_id = _frozen_round(db)

        with pytest.raises(AnnouncementError, match="ledger index"):
            announce_round(db, round_id, 1, client=FakePFTLClient(ledger_index=None))

    def test_ledger_time_failure_falls_back_to_now(self, db):
        round_id = _frozen_round(db)
        client = FakePFTLClient(close_time=None)
        fallback_now = datetime(2026, 8, 6, 15, 0, 0, tzinfo=timezone.utc)

        announce_round(db, round_id, 1, client=client, fallback_now=fallback_now)

        cursor = db.cursor()
        cursor.execute(
            "SELECT commit_opens_at FROM governance_rounds WHERE id = %s", (round_id,)
        )
        assert cursor.fetchone()[0] == fallback_now
        cursor.close()

    def test_announced_round_is_not_reannounced_on_rerun(self, db):
        round_id = _frozen_round(db)
        first_client = FakePFTLClient()
        first = announce_round(db, round_id, 1, client=first_client)

        second_client = FakePFTLClient(ledger_index=9999)
        second = announce_round(db, round_id, 1, client=second_client)

        assert second_client.submitted == []
        assert second["announcement_tx_hash"] == first["announcement_tx_hash"]
        assert second["announcement_ledger_index"] == first["announcement_ledger_index"]

    def test_orchestrator_runs_the_real_announcement(self, db, monkeypatch):
        _seed_refresh(db)
        monkeypatch.setattr(round_package, "_build_corpus_default", _corpus)
        monkeypatch.setattr(
            round_package, "pin_package", lambda files, bundle, n: "QmWired"
        )
        fake_client = FakePFTLClient()
        monkeypatch.setattr(
            announcement_service, "PFTLClient", lambda: fake_client
        )

        result = RoundOrchestrator().run_round(TRIGGER_MANUAL)

        # Freeze and announcement succeed; the round fails at the judge draw.
        assert result["status"] == RoundState.FAILED.value
        assert "judge draw" in result["error"]
        cursor = db.cursor()
        cursor.execute(
            """
            SELECT announcement_tx_hash, announcement_ledger_index, error_message
            FROM governance_rounds WHERE round_number = %s
            """,
            (result["round_number"],),
        )
        tx_hash, ledger_index, error_message = cursor.fetchone()
        cursor.close()
        assert (tx_hash, ledger_index) == ("TXHASH123", 4242)
        assert error_message.startswith("ANNOUNCED:")
        assert len(fake_client.submitted) == 1
        assert fake_client.submitted[0]["memo_type"] == ROUND_ANNOUNCEMENT_TYPE


class TestPackageFormatSpec:
    def test_frozen_package_carries_the_announcement_format(self):
        files, _ = round_package.build_package(7, _corpus(), _pool(), FROZEN_AT)

        spec = files["round/announcement_format.json"]
        assert spec["memo_types"]["round_announcement"] == ROUND_ANNOUNCEMENT_TYPE
        assert spec["memo_types"]["round_receipt"] == ROUND_RECEIPT_TYPE
        assert spec["announcement_fields"] == list(ANNOUNCEMENT_PAYLOAD_FIELDS)
        assert spec["receipt_fields"] == list(RECEIPT_PAYLOAD_FIELDS)
