"""The decision engine: the verdict matrix, tie-break, blocklist, wiring."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from governance_service.services import final_publication, round_package
from governance_service.services.decision import (
    DECISION_CHALLENGER_REPLACES,
    DECISION_INCUMBENT_RETAINED,
    DECISION_RETAINED_BY_NECESSITY,
    DecisionError,
    ExaminedCandidate,
    break_tie,
    decide,
    decide_round,
)
from governance_service.services.orchestrator import (
    TRIGGER_MANUAL,
    RoundOrchestrator,
    RoundState,
)
from governance_service.services.pool_refresh import (
    RULE_BLOCKLISTED,
    RULE_IS_INCUMBENT,
    evaluate_release,
    load_table_blocklist,
)
from tests.test_final_publication import FakeReceiptClient
from tests.test_pool_rules import descriptor
from tests.test_round_package import _corpus, _pool, FROZEN_AT, INCUMBENT_REPO

MARGIN = Decimal(5)
EVEN_HASH = "A" * 63 + "0"
ODD_HASH = "A" * 63 + "1"

CHALLENGER_A = "Qwen/Qwen3-32B-FP8"
CHALLENGER_B = "google/gemma-4-31B-it"


def _candidate(
    hf_repo: str,
    grade: str | None,
    *,
    verdict: str = "SURVIVED",
    revision: str = "aa" * 20,
) -> ExaminedCandidate:
    return ExaminedCandidate(
        hf_repo=hf_repo,
        revision=revision,
        verdict=verdict,
        final_grade=Decimal(grade) if grade is not None else None,
    )


def _decide(candidates, *, judge=None, draw_hash=EVEN_HASH):
    return decide(
        incumbent_hf_repo=INCUMBENT_REPO,
        candidates=candidates,
        margin=MARGIN,
        draw_ledger_hash=draw_hash,
        judge_hf_repo=judge,
    )


class TestDecisionMatrix:
    def test_margin_met_replaces_the_incumbent(self):
        result = _decide(
            [_candidate(INCUMBENT_REPO, "82.0"), _candidate(CHALLENGER_A, "87.0")]
        )
        # Exactly the margin counts as beating it by the margin.
        assert result.decision == DECISION_CHALLENGER_REPLACES
        assert result.winner_hf_repo == CHALLENGER_A
        assert result.rationale["margin_arithmetic"]["difference"] == "5.0"

    def test_below_margin_retains_the_incumbent(self):
        result = _decide(
            [_candidate(INCUMBENT_REPO, "82.0"), _candidate(CHALLENGER_A, "86.9")]
        )
        assert result.decision == DECISION_INCUMBENT_RETAINED
        assert result.winner_hf_repo == INCUMBENT_REPO

    def test_disqualified_incumbent_loses_the_margin_protection(self):
        result = _decide(
            [
                _candidate(INCUMBENT_REPO, None, verdict="DISQUALIFIED"),
                _candidate(CHALLENGER_A, "70.0"),
            ]
        )
        assert result.decision == DECISION_CHALLENGER_REPLACES
        assert result.winner_hf_repo == CHALLENGER_A

    def test_no_survivor_at_all_retains_by_necessity(self):
        result = _decide(
            [
                _candidate(INCUMBENT_REPO, None, verdict="DISQUALIFIED"),
                _candidate(CHALLENGER_A, None, verdict="DISQUALIFIED"),
            ]
        )
        assert result.decision == DECISION_RETAINED_BY_NECESSITY
        assert result.winner_hf_repo == INCUMBENT_REPO
        assert "production alarm" in result.rationale["reason"]

    def test_healthy_incumbent_with_no_survivors_is_retained(self):
        result = _decide(
            [
                _candidate(INCUMBENT_REPO, "82.0"),
                _candidate(CHALLENGER_A, None, verdict="DISQUALIFIED"),
            ]
        )
        assert result.decision == DECISION_INCUMBENT_RETAINED

    def test_judge_is_excluded_from_the_competition(self):
        result = _decide(
            [
                _candidate(INCUMBENT_REPO, "82.0"),
                _candidate(CHALLENGER_A, "99.0"),
                _candidate(CHALLENGER_B, "84.0"),
            ],
            judge=CHALLENGER_A,
        )
        # The judge's 99.0 cannot win; the remaining challenger is under margin.
        assert result.decision == DECISION_INCUMBENT_RETAINED

    def test_ungraded_survivor_is_an_error(self):
        with pytest.raises(DecisionError, match="not fully graded"):
            _decide(
                [_candidate(INCUMBENT_REPO, "82.0"), _candidate(CHALLENGER_A, None)]
            )

    def test_missing_incumbent_run_is_an_error(self):
        # Missing evidence is not a disqualification: only a mechanically
        # disqualified incumbent loses the margin protection.
        with pytest.raises(DecisionError, match="evidence is incomplete"):
            _decide([_candidate(CHALLENGER_A, "99.0")])


class TestTieBreak:
    def test_ledger_hash_picks_among_tied_challengers(self):
        tied = sorted([CHALLENGER_A, CHALLENGER_B])
        assert break_tie(tied, EVEN_HASH) == tied[0]
        assert break_tie(tied, ODD_HASH) == tied[1]

    def test_tied_top_grades_use_the_draw_hash(self):
        candidates = [
            _candidate(INCUMBENT_REPO, "80.0"),
            _candidate(CHALLENGER_A, "90.0"),
            _candidate(CHALLENGER_B, "90.0"),
        ]
        even = _decide(candidates, draw_hash=EVEN_HASH)
        odd = _decide(candidates, draw_hash=ODD_HASH)

        tied_sorted = sorted([CHALLENGER_A, CHALLENGER_B])
        assert even.winner_hf_repo == tied_sorted[0]
        assert odd.winner_hf_repo == tied_sorted[1]
        assert even.rationale["tie_break"]["tied"] == tied_sorted


def _decidable_round(db, round_number: int = 1) -> int:
    """An AWAITING_COMMIT_CLOSE round with frozen artifacts and graded runs."""
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    cursor = db.cursor()
    cursor.execute(
        """
        INSERT INTO governance_rounds
            (round_number, status, trigger_source, package_cid, package_hash,
             frozen_at, announcement_tx_hash, announcement_ledger_index,
             commit_opens_at, commit_closes_at, judge_hf_repo,
             draw_ledger_index, draw_ledger_hash)
        VALUES (%s, %s, %s, 'QmPackage', %s, %s, 'TXHASH123', 5000000,
                %s, %s, %s, 5000010, %s)
        RETURNING id
        """,
        (
            round_number,
            RoundState.AWAITING_COMMIT_CLOSE.value,
            TRIGGER_MANUAL,
            "ab" * 32,
            past - timedelta(days=2),
            past - timedelta(days=2),
            past,
            CHALLENGER_B,
            EVEN_HASH,
        ),
    )
    round_id = cursor.fetchone()[0]
    db.commit()
    cursor.close()
    files, bundle = round_package.build_package(round_number, _corpus(), _pool(), FROZEN_AT)
    round_package.persist_package(db, round_id, files, bundle, "QmPackage", FROZEN_AT)
    return round_id


def _seed_graded_run(
    db, round_id: int, hf_repo: str, grade: str | None, verdict: str = "SURVIVED",
    revision: str = "aa" * 20,
) -> None:
    cursor = db.cursor()
    cursor.execute(
        """
        INSERT INTO exam_runs
            (hf_repo, revision, profile_hash, corpus_hash, status, round_id,
             verdict)
        VALUES (%s, %s, 'p1', 'c1', 'COMPLETED', %s, %s)
        RETURNING id
        """,
        (hf_repo, revision, round_id, verdict),
    )
    run_id = cursor.fetchone()[0]
    cursor.execute(
        """
        INSERT INTO governance_round_exam_runs (round_id, hf_repo, run_id, final_grade)
        VALUES (%s, %s, %s, %s)
        """,
        (round_id, hf_repo, run_id, grade),
    )
    db.commit()
    cursor.close()


class TestDecideRound:
    def test_decides_and_persists_from_round_evidence(self, db):
        round_id = _decidable_round(db)
        _seed_graded_run(db, round_id, INCUMBENT_REPO, "82.0")
        _seed_graded_run(db, round_id, CHALLENGER_A, "88.5", revision="bb" * 20)

        result = decide_round(db, round_id, 1)

        assert result == {
            "decision": DECISION_CHALLENGER_REPLACES,
            "winner_hf_repo": CHALLENGER_A,
        }
        cursor = db.cursor()
        cursor.execute(
            """
            SELECT decision, winner_hf_repo, decision_rationale, decided_at
            FROM governance_rounds WHERE id = %s
            """,
            (round_id,),
        )
        decision_value, winner, rationale, decided_at = cursor.fetchone()
        cursor.close()
        assert (decision_value, winner) == (DECISION_CHALLENGER_REPLACES, CHALLENGER_A)
        assert rationale["margin_arithmetic"]["difference"] == "6.5"
        assert decided_at is not None

    def test_margin_comes_from_the_frozen_parameters(self, db):
        round_id = _decidable_round(db)
        # Doctor the frozen margin to 7 so the outcome can only come from
        # the artifact, never from a module constant.
        cursor = db.cursor()
        cursor.execute(
            """
            UPDATE governance_round_artifacts
            SET content = jsonb_set(content, '{incumbent_margin_points}', '7')
            WHERE round_id = %s AND path = 'round/parameters.json'
            """,
            (round_id,),
        )
        db.commit()
        cursor.close()
        _seed_graded_run(db, round_id, INCUMBENT_REPO, "82.0")
        _seed_graded_run(db, round_id, CHALLENGER_A, "88.5", revision="bb" * 20)

        result = decide_round(db, round_id, 1)

        # +6.5 replaces under the standing 5 but not under the frozen 7.
        assert result["decision"] == DECISION_INCUMBENT_RETAINED

    def test_disqualified_runs_are_booked_into_the_blocklist(self, db):
        round_id = _decidable_round(db)
        _seed_graded_run(db, round_id, INCUMBENT_REPO, "82.0")
        _seed_graded_run(
            db, round_id, CHALLENGER_A, None, verdict="DISQUALIFIED",
            revision="bb" * 20,
        )

        decide_round(db, round_id, 1)

        entries = load_table_blocklist(db)
        assert [(e.hf_repo, e.revision) for e in entries] == [
            (CHALLENGER_A, "bb" * 20)
        ]
        assert "governance round 1" in entries[0].round_reference
        # The round-booked entry — present only in the table, never in the
        # curated file — excludes that revision at the next refresh.
        evaluations = evaluate_release(
            [
                descriptor(
                    "qwen3-32b", "qwen3", 70.0,
                    hf_repo=CHALLENGER_A, revision="bb" * 20,
                )
            ],
            entries,
            INCUMBENT_REPO,
        )
        assert evaluations[0].in_pool is False
        assert evaluations[0].exclusion_rule == RULE_BLOCKLISTED

    def test_rerun_keeps_the_verdict_and_duplicates_nothing(self, db):
        round_id = _decidable_round(db)
        _seed_graded_run(db, round_id, INCUMBENT_REPO, "82.0")
        _seed_graded_run(
            db, round_id, CHALLENGER_A, "88.5", verdict="DISQUALIFIED",
            revision="bb" * 20,
        )
        first = decide_round(db, round_id, 1)

        second = decide_round(db, round_id, 1)

        assert second == first
        assert len(load_table_blocklist(db)) == 1

    def test_incomplete_grading_is_an_error(self, db):
        round_id = _decidable_round(db)
        _seed_graded_run(db, round_id, INCUMBENT_REPO, "82.0")
        _seed_graded_run(db, round_id, CHALLENGER_A, None, revision="bb" * 20)

        with pytest.raises(DecisionError, match="not fully graded"):
            decide_round(db, round_id, 1)

    def test_no_candidates_is_an_error(self, db):
        round_id = _decidable_round(db)

        with pytest.raises(DecisionError, match="no examined candidates"):
            decide_round(db, round_id, 1)

    def test_missing_verdict_is_an_error(self, db):
        round_id = _decidable_round(db)
        _seed_graded_run(db, round_id, INCUMBENT_REPO, "82.0")
        _seed_graded_run(
            db, round_id, CHALLENGER_A, None, verdict=None, revision="bb" * 20
        )

        with pytest.raises(DecisionError, match="not fully verdicted"):
            decide_round(db, round_id, 1)
        assert load_table_blocklist(db) == []

    def test_disqualified_incumbent_is_booked_and_still_seated_next_refresh(self, db):
        round_id = _decidable_round(db)
        _seed_graded_run(
            db, round_id, INCUMBENT_REPO, None, verdict="DISQUALIFIED",
            revision="cc" * 20,
        )
        _seed_graded_run(db, round_id, CHALLENGER_A, "70.0", revision="bb" * 20)

        result = decide_round(db, round_id, 1)

        assert result["decision"] == DECISION_CHALLENGER_REPLACES
        entries = load_table_blocklist(db)
        assert (INCUMBENT_REPO, "cc" * 20) in [
            (e.hf_repo, e.revision) for e in entries
        ]
        # The blocklisted incumbent still sits in the next pool by right —
        # its entry takes effect only once it is replaced.
        evaluations = evaluate_release(
            [
                descriptor(
                    "qwen36-27b", "qwen", 70.0,
                    hf_repo=INCUMBENT_REPO, revision="cc" * 20,
                )
            ],
            entries,
            INCUMBENT_REPO,
        )
        assert evaluations[0].in_pool is False
        assert evaluations[0].exclusion_rule == RULE_IS_INCUMBENT


class TestPipelineWiring:
    def test_parked_round_decides_and_publishes(self, db, monkeypatch):
        round_id = _decidable_round(db)
        _seed_graded_run(db, round_id, INCUMBENT_REPO, "82.0")
        _seed_graded_run(db, round_id, CHALLENGER_A, "88.5", revision="bb" * 20)

        monkeypatch.setattr(
            final_publication, "PFTLClient", lambda: FakeReceiptClient()
        )
        monkeypatch.setattr(
            final_publication.round_package,
            "pin_package",
            lambda files, bundle, name: "QmDecided",
        )

        results = RoundOrchestrator().publish_due_rounds()

        assert results[0]["status"] == RoundState.COMPLETE.value
        cursor = db.cursor()
        cursor.execute(
            "SELECT decision, winner_hf_repo, status FROM governance_rounds WHERE id = %s",
            (round_id,),
        )
        decision_value, winner, status = cursor.fetchone()
        cursor.close()
        assert decision_value == DECISION_CHALLENGER_REPLACES
        assert winner == CHALLENGER_A
        assert status == RoundState.COMPLETE.value

    def test_decision_joins_the_final_record(self, db):
        round_id = _decidable_round(db)
        _seed_graded_run(db, round_id, INCUMBENT_REPO, "82.0")
        _seed_graded_run(db, round_id, CHALLENGER_A, "88.5", revision="bb" * 20)
        decide_round(db, round_id, 1)

        files, _ = final_publication.build_final_record(db, round_id, 1)

        assert files["round.json"]["decision"] == DECISION_CHALLENGER_REPLACES
        assert files["round.json"]["winner_hf_repo"] == CHALLENGER_A
        assert files["round.json"]["decision_rationale"]["reason"]
