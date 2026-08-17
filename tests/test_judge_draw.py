"""The judge draw: mapping vectors, redraw ordering, waiting, persistence."""

import pytest

from governance_service.services import announcement as announcement_service
from governance_service.services import judge_draw, round_package
from governance_service.services.judge_draw import (
    DRAW_LEDGER_OFFSET,
    JudgeDrawError,
    draw_judge,
    drawing_ledger_index,
    map_hash_to_challenger,
    next_judge,
    sorted_challengers,
)
from governance_service.services.orchestrator import (
    TRIGGER_MANUAL,
    RoundOrchestrator,
    RoundState,
)
from tests.test_announcement import FakePFTLClient as FakeAnnouncementClient
from tests.test_round_package import (
    _corpus,
    _pool,
    _seed_refresh,
    CHALLENGER_REPOS,
    FROZEN_AT,
)

CHALLENGERS = sorted(CHALLENGER_REPOS)  # [Qwen/Qwen3-32B-FP8, google/gemma-4-31B-it]

ANNOUNCEMENT_LEDGER = 5_000_000
TARGET_LEDGER = ANNOUNCEMENT_LEDGER + DRAW_LEDGER_OFFSET

EVEN_HASH = "A" * 63 + "0"  # int % 2 == 0 -> first sorted challenger
ODD_HASH = "A" * 63 + "1"  # int % 2 == 1 -> second sorted challenger


class FakeDrawClient:
    """Validated-index sequence plus a fixed hash per ledger index."""

    def __init__(
        self,
        *,
        validated_indexes: list[int] | None = None,
        hashes: dict[int, str] | None = None,
    ):
        self.validated_indexes = validated_indexes or [TARGET_LEDGER]
        self.hashes = hashes or {TARGET_LEDGER: EVEN_HASH}
        self.index_calls = 0
        self.hash_calls: list[int] = []

    def latest_validated_ledger_index(self) -> int:
        index = self.validated_indexes[
            min(self.index_calls, len(self.validated_indexes) - 1)
        ]
        self.index_calls += 1
        return index

    def ledger_hash(self, ledger_index: int) -> str:
        self.hash_calls.append(ledger_index)
        return self.hashes[ledger_index]


class TestMapping:
    def test_known_vectors_with_two_challengers(self):
        assert map_hash_to_challenger(EVEN_HASH, CHALLENGERS) == CHALLENGERS[0]
        assert map_hash_to_challenger(ODD_HASH, CHALLENGERS) == CHALLENGERS[1]

    def test_known_vector_with_three_challengers(self):
        three = ["c/model", "a/model", "b/model"]
        # int("f"*64, 16) % 3 == 0 -> first in sorted order ("a/model").
        assert map_hash_to_challenger("f" * 64, three) == "a/model"

    def test_mapping_sorts_challengers_itself(self):
        unsorted = list(reversed(CHALLENGERS))
        assert map_hash_to_challenger(EVEN_HASH, unsorted) == CHALLENGERS[0]

    def test_non_hex_hash_is_rejected(self):
        with pytest.raises(JudgeDrawError, match="not hex"):
            map_hash_to_challenger("zz", CHALLENGERS)

    def test_empty_challengers_are_rejected(self):
        with pytest.raises(JudgeDrawError, match="zero challengers"):
            map_hash_to_challenger(EVEN_HASH, [])

    def test_drawing_ledger_index_applies_the_frozen_offset(self):
        assert drawing_ledger_index(ANNOUNCEMENT_LEDGER) == ANNOUNCEMENT_LEDGER + 10


class TestRedrawOrdering:
    def test_no_failures_returns_the_drawn_judge(self):
        assert next_judge(EVEN_HASH, CHALLENGERS) == CHALLENGERS[0]

    def test_failed_judge_advances_cyclically(self):
        assert next_judge(EVEN_HASH, CHALLENGERS, {CHALLENGERS[0]}) == CHALLENGERS[1]
        assert next_judge(ODD_HASH, CHALLENGERS, {CHALLENGERS[1]}) == CHALLENGERS[0]

    def test_wraparound_skips_multiple_failures(self):
        three = sorted(["a/model", "b/model", "c/model"])
        # f*64 % 3 == 0 -> draw order starts at a/model.
        assert next_judge("f" * 64, three, {"a/model", "b/model"}) == "c/model"

    def test_exhaustion_returns_none(self):
        assert next_judge(EVEN_HASH, CHALLENGERS, set(CHALLENGERS)) is None


class TestSortedChallengers:
    def test_reads_frozen_package_shape(self):
        file = {
            "challengers": [
                {"profile": {"hf_repo": CHALLENGER_REPOS[0]}},
                {"profile": {"hf_repo": CHALLENGER_REPOS[1]}},
            ]
        }
        assert sorted_challengers(file) == CHALLENGERS

    def test_missing_or_empty_challengers_are_rejected(self):
        with pytest.raises(JudgeDrawError, match="no challengers"):
            sorted_challengers({"challengers": []})
        with pytest.raises(JudgeDrawError, match="no hf_repo"):
            sorted_challengers({"challengers": [{"profile": {}}]})


def _announced_round(db, round_number: int = 1) -> int:
    """A FROZEN+ANNOUNCED round whose package artifacts are persisted."""
    cursor = db.cursor()
    cursor.execute(
        """
        INSERT INTO governance_rounds
            (round_number, status, trigger_source, package_cid, package_hash,
             frozen_at, announcement_tx_hash, announcement_ledger_index)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            round_number,
            RoundState.ANNOUNCED.value,
            TRIGGER_MANUAL,
            "QmPackage",
            "ab" * 32,
            FROZEN_AT,
            "TXHASH123",
            ANNOUNCEMENT_LEDGER,
        ),
    )
    round_id = cursor.fetchone()[0]
    db.commit()
    cursor.close()
    files, bundle = round_package.build_package(round_number, _corpus(), _pool(), FROZEN_AT)
    round_package.persist_package(db, round_id, files, bundle, "QmPackage", FROZEN_AT)
    return round_id


class TestDrawJudge:
    def test_draw_persists_the_judge_and_ledger_identity(self, db):
        round_id = _announced_round(db)
        client = FakeDrawClient()

        result = draw_judge(db, round_id, 1, client=client, sleep=lambda s: None)

        assert result == {
            "judge_hf_repo": CHALLENGERS[0],
            "draw_ledger_index": TARGET_LEDGER,
            "draw_ledger_hash": EVEN_HASH,
        }
        cursor = db.cursor()
        cursor.execute(
            """
            SELECT judge_hf_repo, draw_ledger_index, draw_ledger_hash
            FROM governance_rounds WHERE id = %s
            """,
            (round_id,),
        )
        assert cursor.fetchone() == (CHALLENGERS[0], TARGET_LEDGER, EVEN_HASH)
        cursor.close()
        assert client.hash_calls == [TARGET_LEDGER]

    def test_draw_uses_the_frozen_challengers(self, db):
        round_id = _announced_round(db)
        client = FakeDrawClient(hashes={TARGET_LEDGER: ODD_HASH})

        result = draw_judge(db, round_id, 1, client=client, sleep=lambda s: None)

        assert result["judge_hf_repo"] == CHALLENGERS[1]

    def test_draw_waits_until_the_target_ledger_validates(self, db):
        round_id = _announced_round(db)
        client = FakeDrawClient(
            validated_indexes=[TARGET_LEDGER - 4, TARGET_LEDGER - 1, TARGET_LEDGER]
        )
        sleeps: list[float] = []

        draw_judge(db, round_id, 1, client=client, sleep=sleeps.append)

        assert client.index_calls == 3
        assert len(sleeps) == 2

    def test_draw_times_out_when_the_ledger_never_validates(self, db):
        round_id = _announced_round(db)
        client = FakeDrawClient(validated_indexes=[TARGET_LEDGER - 1])

        with pytest.raises(JudgeDrawError, match="did not validate"):
            draw_judge(db, round_id, 1, client=client, sleep=lambda s: None)

    def test_drawn_round_is_not_redrawn_on_rerun(self, db):
        round_id = _announced_round(db)
        first = draw_judge(
            db, round_id, 1, client=FakeDrawClient(), sleep=lambda s: None
        )

        second_client = FakeDrawClient(hashes={TARGET_LEDGER: ODD_HASH})
        second = draw_judge(db, round_id, 1, client=second_client, sleep=lambda s: None)

        assert second == first
        assert second_client.index_calls == 0
        assert second_client.hash_calls == []

    def test_unannounced_round_cannot_draw(self, db):
        cursor = db.cursor()
        cursor.execute(
            """
            INSERT INTO governance_rounds (round_number, status, trigger_source)
            VALUES (1, %s, %s) RETURNING id
            """,
            (RoundState.FROZEN.value, TRIGGER_MANUAL),
        )
        round_id = cursor.fetchone()[0]
        db.commit()
        cursor.close()

        with pytest.raises(JudgeDrawError, match="no announcement ledger index"):
            draw_judge(db, round_id, 1, client=FakeDrawClient(), sleep=lambda s: None)

    def test_missing_package_artifact_cannot_draw(self, db):
        cursor = db.cursor()
        cursor.execute(
            """
            INSERT INTO governance_rounds
                (round_number, status, trigger_source, announcement_ledger_index)
            VALUES (1, %s, %s, %s) RETURNING id
            """,
            (RoundState.ANNOUNCED.value, TRIGGER_MANUAL, ANNOUNCEMENT_LEDGER),
        )
        round_id = cursor.fetchone()[0]
        db.commit()
        cursor.close()

        with pytest.raises(JudgeDrawError, match="no frozen pool/candidates.json"):
            draw_judge(db, round_id, 1, client=FakeDrawClient(), sleep=lambda s: None)

    def test_orchestrator_runs_the_real_draw(self, db, monkeypatch):
        _seed_refresh(db)
        monkeypatch.setattr(round_package, "_build_corpus_default", _corpus)
        monkeypatch.setattr(
            round_package, "pin_package", lambda files, bundle, n: "QmWired"
        )
        announce_client = FakeAnnouncementClient(ledger_index=ANNOUNCEMENT_LEDGER)
        monkeypatch.setattr(
            announcement_service, "PFTLClient", lambda: announce_client
        )
        monkeypatch.setattr(judge_draw, "PFTLClient", lambda: FakeDrawClient())

        result = RoundOrchestrator().run_round(TRIGGER_MANUAL)

        # Freeze, announcement, and draw succeed; the round fails at the exam.
        assert result["status"] == RoundState.FAILED.value
        assert "exam" in result["error"]
        cursor = db.cursor()
        cursor.execute(
            """
            SELECT judge_hf_repo, draw_ledger_index, error_message
            FROM governance_rounds WHERE round_number = %s
            """,
            (result["round_number"],),
        )
        judge, draw_index, error_message = cursor.fetchone()
        cursor.close()
        assert judge == CHALLENGERS[0]
        assert draw_index == ANNOUNCEMENT_LEDGER + DRAW_LEDGER_OFFSET
        assert error_message.startswith("JUDGE_DRAWN:")
