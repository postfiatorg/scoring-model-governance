"""The rounds API: list, detail, withheld record routes, and config."""

from datetime import datetime, timedelta, timezone

from governance_service.scoring import canonical_json_hash
from governance_service.services.announcement import (
    ROUND_ANNOUNCEMENT_TYPE,
    ROUND_RECEIPT_TYPE,
)
from governance_service.services.orchestrator import TRIGGER_MANUAL, RoundState
from tests.test_decision import _seed_graded_run
from tests.test_final_publication import _round as _publication_round
from tests.test_final_publication import _seed_runs


def _insert_rounds(db, count: int) -> None:
    cursor = db.cursor()
    for number in range(1, count + 1):
        cursor.execute(
            """
            INSERT INTO governance_rounds (round_number, status, trigger_source)
            VALUES (%s, %s, %s)
            """,
            (number, RoundState.FAILED.value, TRIGGER_MANUAL),
        )
    db.commit()
    cursor.close()


class TestList:
    def test_lists_newest_first_with_pagination(self, db, client):
        _insert_rounds(db, 5)

        response = client.get("/api/governance/rounds?limit=2&offset=1")

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 5
        assert body["limit"] == 2
        assert body["offset"] == 1
        assert [r["round_number"] for r in body["rounds"]] == [4, 3]

    def test_empty_list(self, db, client):
        body = client.get("/api/governance/rounds").json()
        assert body == {"rounds": [], "total": 0, "limit": 20, "offset": 0}


class TestDetail:
    def test_round_detail_carries_the_full_identity(self, db, client):
        round_id = _publication_round(db)
        _seed_runs(db, round_id)

        response = client.get("/api/governance/rounds/1")

        assert response.status_code == 200
        body = response.json()
        assert body["round_number"] == 1
        assert body["status"] == RoundState.DECIDED.value
        assert body["package_cid"] == "QmPackage"
        assert body["announcement_tx_hash"] == "TXHASH123"
        assert body["judge_hf_repo"] == "Qwen/Qwen3-32B-FP8"
        assert body["commit_closes_at"] is not None

    def test_missing_round_is_404(self, db, client):
        assert client.get("/api/governance/rounds/9").status_code == 404


class TestRecordRoutes:
    def test_closed_round_serves_hash_consistent_record(self, db, client):
        round_id = _publication_round(db)
        _seed_runs(db, round_id)
        # A graded run too, so the walk below would surface any value the
        # plain JSON response cannot serialize (e.g. a NUMERIC Decimal).
        _seed_graded_run(
            db, round_id, "Qwen/Qwen3.6-27B-FP8", "82.0", revision="dd" * 20
        )

        bundle = client.get("/api/governance/rounds/1/record").json()

        assert bundle["package_kind"] == "governance_round_record"
        assert bundle["round_number"] == 1
        for path, digest in bundle["file_hashes"].items():
            served = client.get(f"/api/governance/rounds/1/record/{path}")
            assert served.status_code == 200
            assert canonical_json_hash(served.json()) == digest

    def test_pre_close_round_is_withheld(self, db, client):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        _publication_round(db, status=RoundState.AWAITING_COMMIT_CLOSE,
                           commit_closes_at=future)

        response = client.get("/api/governance/rounds/1/record")

        assert response.status_code == 403
        assert "withheld" in response.json()["error"]

    def test_parked_round_without_commit_close_is_withheld(self, db, client):
        _publication_round(db, status=RoundState.GRADED, commit_closes_at=None)

        assert client.get("/api/governance/rounds/1/record").status_code == 403

    def test_unknown_record_file_is_404(self, db, client):
        round_id = _publication_round(db)
        _seed_runs(db, round_id)

        assert (
            client.get("/api/governance/rounds/1/record/no/such.json").status_code
            == 404
        )

    def test_missing_round_record_is_404(self, db, client):
        assert client.get("/api/governance/rounds/9/record").status_code == 404

    def test_record_reflects_the_decision(self, db, client):
        round_id = _publication_round(db)
        _seed_graded_run(db, round_id, "Qwen/Qwen3.6-27B-FP8", "82.0")
        cursor = db.cursor()
        cursor.execute(
            "UPDATE governance_rounds SET decision = 'incumbent_retained', "
            "winner_hf_repo = 'Qwen/Qwen3.6-27B-FP8' WHERE id = %s",
            (round_id,),
        )
        db.commit()
        cursor.close()

        body = client.get("/api/governance/rounds/1/record/round.json").json()

        assert body["package_hash"] == "ab" * 32
        assert body["decision"] == "incumbent_retained"
        assert body["winner_hf_repo"] == "Qwen/Qwen3.6-27B-FP8"


class TestConfig:
    def test_config_carries_the_participation_surface(self, db, client):
        body = client.get("/api/governance/config").json()

        assert body["protocol_version"] == 1
        assert body["round_cadence_days"] == 30.0
        assert body["incumbent_margin_points"] == 5
        assert body["draw_ledger_offset"] == 10
        assert body["announcement_memo_type"] == ROUND_ANNOUNCEMENT_TYPE
        assert body["receipt_memo_type"] == ROUND_RECEIPT_TYPE
        assert body["commit_window_seconds"] == 172800
        assert body["reveal_window_seconds"] == 86400
        # No wallet configured in the hermetic suite.
        assert body["foundation_publisher_address"] is None
