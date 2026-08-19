"""The grading stage: pairs, judge bar, redraw, grades, links, decision."""

import httpx
import pytest

from governance_service.services import exam_stage, grading_stage
from governance_service.services.decision import (
    DECISION_INCUMBENT_RETAINED,
    decide_round,
)
from governance_service.services.grading_engine import (
    JUDGE_OUTCOME_FAILED,
    JUDGE_OUTCOME_PASSED,
    GradingEngine,
)
from governance_service.services.grading_stage import (
    GradingStageError,
    run_grading,
)
from governance_service.services.orchestrator import (
    RoundAbandoned,
    RoundOrchestrator,
    RoundState,
)
from governance_service.services.runtime_manager import InfrastructureError
from tests.test_exam_engine import StubRuntime
from tests.test_exam_stage import (
    EXAMINED_CHALLENGER,
    JUDGE,
    ValidEndpoint,
    _client_factory,
    _engine,
    _seed_drawn_round,
)
from tests.test_grading_engine import JUDGE_ANSWER
from tests.test_round_package import INCUMBENT_REPO

# Draw hash "A"*64 maps to index 0 of the codepoint-sorted challengers
# ["Qwen/Qwen3-32B-FP8", "google/gemma-4-31B-it"]; the seeded judge is
# gemma, so its redraw successor is Qwen3-32B.
REDRAW_SUCCESSOR = EXAMINED_CHALLENGER


class JudgeEndpoint:
    """Schema-valid judge responses; malformed for the named judges."""

    def __init__(self, *, bad_judges: set[str] | None = None):
        self.bad_judges = bad_judges or set()
        self.calls: list[dict] = []

    def post(self, url, *, json=None, headers=None, timeout=None) -> httpx.Response:
        self.calls.append(json)
        content = (
            "not a judge document"
            if json["model"] in self.bad_judges
            else JUDGE_ANSWER
        )
        return httpx.Response(
            200,
            json={
                "id": f"chatcmpl-{len(self.calls)}",
                "choices": [
                    {"message": {"role": "assistant", "content": content}}
                ],
                "usage": {"prompt_tokens": 7, "completion_tokens": 11},
            },
        )


def _grading_engine(endpoint: JudgeEndpoint) -> GradingEngine:
    return GradingEngine(
        StubRuntime(), http_post=endpoint.post, sleep=lambda seconds: None
    )


def _examined_round(db, round_number: int = 1) -> int:
    """A round whose exam ran for real (fake boundaries) and now sits EXAMINED."""
    round_id = _seed_drawn_round(db, round_number=round_number)
    exam_stage.run_exam(
        db,
        round_id,
        round_number,
        engine=_engine(ValidEndpoint()),
        client_factory=_client_factory(),
    )
    cursor = db.cursor()
    cursor.execute(
        "UPDATE governance_rounds SET status = %s WHERE id = %s",
        (RoundState.EXAMINED.value, round_id),
    )
    db.commit()
    cursor.close()
    return round_id


def _grade_links(db, round_id: int) -> dict[str, tuple]:
    cursor = db.cursor()
    cursor.execute(
        """
        SELECT hf_repo, final_grade, grade_receipts
        FROM governance_round_exam_runs WHERE round_id = %s
        """,
        (round_id,),
    )
    rows = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
    cursor.close()
    return rows


def _grading_links(db, round_id: int) -> list[tuple]:
    cursor = db.cursor()
    cursor.execute(
        """
        SELECT l.hf_repo, l.outcome, g.status
        FROM governance_round_grading_runs l
        JOIN grading_runs g ON g.id = l.run_id
        WHERE l.round_id = %s ORDER BY l.run_id
        """,
        (round_id,),
    )
    rows = cursor.fetchall()
    cursor.close()
    return rows


def _round_judge(db, round_id: int) -> str:
    cursor = db.cursor()
    cursor.execute(
        "SELECT judge_hf_repo FROM governance_rounds WHERE id = %s", (round_id,)
    )
    judge = cursor.fetchone()[0]
    cursor.close()
    return judge


class TestGrading:
    def test_grades_survivors_under_the_drawn_judge(self, db):
        round_id = _examined_round(db)
        endpoint = JudgeEndpoint()

        result = run_grading(
            db, round_id, 1,
            engine=_grading_engine(endpoint),
            client_factory=_client_factory(),
        )

        assert result["judge_hf_repo"] == JUDGE
        assert result["redraws"] == 0
        assert sorted(result["grades"]) == sorted(
            [INCUMBENT_REPO, EXAMINED_CHALLENGER]
        )
        grades = _grade_links(db, round_id)
        for hf_repo in (INCUMBENT_REPO, EXAMINED_CHALLENGER):
            final_grade, receipts = grades[hf_repo]
            assert final_grade is not None
            assert receipts["grade_formula_version"] == 1
            assert len(receipts["items"]) == 7
        assert JUDGE not in grades
        assert _grading_links(db, round_id) == [(JUDGE, JUDGE_OUTCOME_PASSED, "COMPLETED")]
        # Both survivors answered identically item-for-item, so their
        # deduped pairs grade once and their final grades must agree.
        assert grades[INCUMBENT_REPO][0] == grades[EXAMINED_CHALLENGER][0]

    def test_judge_failure_applies_the_frozen_redraw_ordering(self, db):
        round_id = _examined_round(db)
        endpoint = JudgeEndpoint(bad_judges={JUDGE})

        result = run_grading(
            db, round_id, 1,
            engine=_grading_engine(endpoint),
            client_factory=_client_factory(),
        )

        assert result["judge_hf_repo"] == REDRAW_SUCCESSOR
        assert result["redraws"] == 1
        assert _round_judge(db, round_id) == REDRAW_SUCCESSOR
        # The promoted judge leaves the competition: only the incumbent
        # is graded, and the failed initial judge never sat the exam.
        assert sorted(result["grades"]) == [INCUMBENT_REPO]
        grades = _grade_links(db, round_id)
        assert grades[INCUMBENT_REPO][0] is not None
        assert grades[REDRAW_SUCCESSOR][0] is None
        assert _grading_links(db, round_id) == [
            (JUDGE, JUDGE_OUTCOME_FAILED, "COMPLETED"),
            (REDRAW_SUCCESSOR, JUDGE_OUTCOME_PASSED, "COMPLETED"),
        ]

    def test_exhaustion_abandons_and_books_the_failed_judges(self, db):
        round_id = _examined_round(db)
        endpoint = JudgeEndpoint(bad_judges={JUDGE, REDRAW_SUCCESSOR})

        with pytest.raises(RoundAbandoned, match="exhausted"):
            run_grading(
                db, round_id, 1,
                engine=_grading_engine(endpoint),
                client_factory=_client_factory(),
            )

        outcomes = {row[0]: row[1] for row in _grading_links(db, round_id)}
        assert outcomes == {
            JUDGE: JUDGE_OUTCOME_FAILED,
            REDRAW_SUCCESSOR: JUDGE_OUTCOME_FAILED,
        }
        cursor = db.cursor()
        cursor.execute("SELECT hf_repo, reason FROM blocklist ORDER BY hf_repo")
        booked = cursor.fetchall()
        cursor.close()
        assert [row[0] for row in booked] == sorted([JUDGE, REDRAW_SUCCESSOR])
        assert all("judge mechanical bar" in row[1] for row in booked)

    def test_resume_after_exhaustion_crash_still_books_failed_judges(self, db):
        round_id = _examined_round(db)
        endpoint = JudgeEndpoint(bad_judges={JUDGE, REDRAW_SUCCESSOR})
        with pytest.raises(RoundAbandoned):
            run_grading(
                db, round_id, 1,
                engine=_grading_engine(endpoint),
                client_factory=_client_factory(),
            )
        # Simulate a crash between the last FAILED link committing and the
        # booking: the entries are lost and the judge still names a failed
        # challenger. The resumed exhaustion must book them again.
        cursor = db.cursor()
        cursor.execute("DELETE FROM blocklist")
        cursor.execute(
            "UPDATE governance_rounds SET judge_hf_repo = %s WHERE id = %s",
            (JUDGE, round_id),
        )
        db.commit()
        cursor.close()

        resumed_endpoint = JudgeEndpoint()
        with pytest.raises(RoundAbandoned, match="exhausted"):
            run_grading(
                db, round_id, 1,
                engine=_grading_engine(resumed_endpoint),
                client_factory=_client_factory(),
            )

        assert resumed_endpoint.calls == []
        cursor = db.cursor()
        cursor.execute("SELECT hf_repo FROM blocklist ORDER BY hf_repo")
        booked = [row[0] for row in cursor.fetchall()]
        cursor.close()
        assert booked == sorted([JUDGE, REDRAW_SUCCESSOR])

    def test_rerun_reuses_stored_judge_outputs_and_grades(self, db):
        round_id = _examined_round(db)
        run_grading(
            db, round_id, 1,
            engine=_grading_engine(JudgeEndpoint()),
            client_factory=_client_factory(),
        )
        first_grades = _grade_links(db, round_id)

        second_endpoint = JudgeEndpoint()
        result = run_grading(
            db, round_id, 1,
            engine=_grading_engine(second_endpoint),
            client_factory=_client_factory(),
        )

        assert second_endpoint.calls == []
        assert result["judge_hf_repo"] == JUDGE
        assert _grade_links(db, round_id) == first_grades

    def test_resume_recovers_the_redraw_from_linked_outcomes(self, db):
        round_id = _examined_round(db)
        run_grading(
            db, round_id, 1,
            engine=_grading_engine(JudgeEndpoint(bad_judges={JUDGE})),
            client_factory=_client_factory(),
        )
        # Simulate a crash between linking the failure and persisting the
        # promoted judge: the round still names the failed judge.
        cursor = db.cursor()
        cursor.execute(
            "UPDATE governance_rounds SET judge_hf_repo = %s WHERE id = %s",
            (JUDGE, round_id),
        )
        db.commit()
        cursor.close()

        endpoint = JudgeEndpoint()
        result = run_grading(
            db, round_id, 1,
            engine=_grading_engine(endpoint),
            client_factory=_client_factory(),
        )

        assert endpoint.calls == []
        assert result["judge_hf_repo"] == REDRAW_SUCCESSOR
        assert _round_judge(db, round_id) == REDRAW_SUCCESSOR

    def test_no_gradable_survivors_skips_grading(self, db):
        round_id = _examined_round(db)
        cursor = db.cursor()
        cursor.execute("UPDATE exam_runs SET verdict = 'DISQUALIFIED'")
        db.commit()
        cursor.close()
        endpoint = JudgeEndpoint()

        result = run_grading(
            db, round_id, 1,
            engine=_grading_engine(endpoint),
            client_factory=_client_factory(),
        )

        assert result["graded"] == 0
        assert endpoint.calls == []
        assert _grading_links(db, round_id) == []

    def test_infrastructure_failure_propagates(self, db):
        round_id = _examined_round(db)
        engine = GradingEngine(
            StubRuntime(ensure_error=InfrastructureError("quota exhausted")),
            http_post=JudgeEndpoint().post,
            sleep=lambda seconds: None,
        )

        with pytest.raises(InfrastructureError):
            run_grading(
                db, round_id, 1,
                engine=engine,
                client_factory=_client_factory(),
            )

        assert _grading_links(db, round_id) == []

    def test_missing_package_fails_closed(self, db):
        round_id = _seed_drawn_round(db, with_package=False)

        with pytest.raises(GradingStageError, match="pool/candidates.json"):
            run_grading(db, round_id, 1)

    def test_missing_judge_fails_closed(self, db):
        round_id = _seed_drawn_round(db, judge=None)

        with pytest.raises(GradingStageError, match="no persisted judge draw"):
            run_grading(db, round_id, 1)


class TestDecisionIntegration:
    def _decided(self, db, endpoint: JudgeEndpoint) -> tuple[int, dict]:
        round_id = _examined_round(db)
        run_grading(
            db, round_id, 1,
            engine=_grading_engine(endpoint),
            client_factory=_client_factory(),
        )
        return round_id, decide_round(db, round_id, 1)

    def test_decision_reads_the_link_grades(self, db):
        round_id, result = self._decided(db, JudgeEndpoint())

        # Identical answers grade identically, so the challenger cannot
        # clear the frozen margin and the incumbent keeps its seat.
        assert result["decision"] == DECISION_INCUMBENT_RETAINED
        cursor = db.cursor()
        cursor.execute(
            "SELECT decision_rationale FROM governance_rounds WHERE id = %s",
            (round_id,),
        )
        rationale = cursor.fetchone()[0]
        cursor.close()
        assert rationale["margin_arithmetic"]["difference"] == "0.0"
        assert rationale["failed_judges"] == []

    def test_failed_judges_are_excluded_and_booked_at_decision(self, db):
        round_id, result = self._decided(db, JudgeEndpoint(bad_judges={JUDGE}))

        assert result["decision"] == DECISION_INCUMBENT_RETAINED
        cursor = db.cursor()
        cursor.execute(
            "SELECT decision_rationale FROM governance_rounds WHERE id = %s",
            (round_id,),
        )
        rationale = cursor.fetchone()[0]
        cursor.execute("SELECT hf_repo, reason FROM blocklist")
        booked = cursor.fetchall()
        cursor.close()
        assert rationale["failed_judges"] == [JUDGE]
        assert rationale["judge"] == REDRAW_SUCCESSOR
        assert JUDGE not in rationale["grades"]
        assert booked == [
            (JUDGE, "Failed the judge mechanical bar in governance round 1")
        ]


class TestPipelineWiring:
    def test_examined_round_grades_and_parks(self, db, monkeypatch):
        round_id = _examined_round(db)
        endpoint = JudgeEndpoint()
        monkeypatch.setattr(
            grading_stage, "GradingEngine", lambda: _grading_engine(endpoint)
        )
        monkeypatch.setattr(grading_stage, "default_client", _client_factory())

        results = RoundOrchestrator().resume_rounds()

        assert len(results) == 1
        assert results[0]["status"] == RoundState.AWAITING_COMMIT_CLOSE.value
        grades = _grade_links(db, round_id)
        assert grades[INCUMBENT_REPO][0] is not None

    def test_exhausted_round_is_abandoned_by_the_pipeline(self, db, monkeypatch):
        round_id = _examined_round(db)
        endpoint = JudgeEndpoint(bad_judges={JUDGE, REDRAW_SUCCESSOR})
        monkeypatch.setattr(
            grading_stage, "GradingEngine", lambda: _grading_engine(endpoint)
        )
        monkeypatch.setattr(grading_stage, "default_client", _client_factory())

        results = RoundOrchestrator().resume_rounds()

        assert len(results) == 1
        assert results[0]["status"] == RoundState.ABANDONED.value
        cursor = db.cursor()
        cursor.execute(
            "SELECT status, error_message FROM governance_rounds WHERE id = %s",
            (round_id,),
        )
        status, error_message = cursor.fetchone()
        cursor.close()
        assert status == RoundState.ABANDONED.value
        assert "exhausted" in error_message
