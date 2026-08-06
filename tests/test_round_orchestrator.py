"""Round state machine behavior against a real PostgreSQL database."""

from datetime import datetime, timedelta, timezone

from governance_service.services.orchestrator import (
    TRIGGER_MANUAL,
    TRIGGER_SCHEDULED,
    RoundOrchestrator,
    RoundState,
    cleanup_interrupted_rounds,
    get_active_round,
)


def _insert_round(
    db,
    round_number: int,
    status: RoundState,
    commit_closes_at: datetime | None = None,
) -> int:
    cursor = db.cursor()
    cursor.execute(
        """
        INSERT INTO governance_rounds
            (round_number, status, trigger_source, commit_closes_at)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (round_number, status.value, TRIGGER_SCHEDULED, commit_closes_at),
    )
    round_id = cursor.fetchone()[0]
    db.commit()
    cursor.close()
    return round_id


def _round_row(db, round_id: int) -> dict:
    cursor = db.cursor()
    cursor.execute(
        """
        SELECT status, trigger_source, error_message, completed_at
        FROM governance_rounds WHERE id = %s
        """,
        (round_id,),
    )
    status, trigger_source, error_message, completed_at = cursor.fetchone()
    cursor.close()
    return {
        "status": status,
        "trigger_source": trigger_source,
        "error_message": error_message,
        "completed_at": completed_at,
    }


class _WalkingOrchestrator(RoundOrchestrator):
    """Every stage succeeds and records its call order."""

    def __init__(self):
        self.calls = []

    def _freeze(self, conn, round_ctx):
        self.calls.append("freeze")

    def _announce(self, conn, round_ctx):
        self.calls.append("announce")

    def _draw_judge(self, conn, round_ctx):
        self.calls.append("draw_judge")

    def _run_exam(self, conn, round_ctx):
        self.calls.append("run_exam")

    def _grade(self, conn, round_ctx):
        self.calls.append("grade")

    def _hold_outputs(self, conn, round_ctx):
        self.calls.append("hold_outputs")

    def _decide(self, conn, round_ctx):
        self.calls.append("decide")

    def _publish_record(self, conn, round_ctx):
        self.calls.append("publish_record")


class TestLifecycleProgression:
    def test_run_round_walks_execution_pipeline_and_parks(self, db):
        orchestrator = _WalkingOrchestrator()

        result = orchestrator.run_round(TRIGGER_SCHEDULED)

        assert result["started"] is True
        assert result["round_number"] == 1
        assert result["status"] == RoundState.AWAITING_COMMIT_CLOSE.value
        assert orchestrator.calls == [
            "freeze",
            "announce",
            "draw_judge",
            "run_exam",
            "grade",
            "hold_outputs",
        ]
        row = _round_row(db, result["round_id"])
        assert row["status"] == RoundState.AWAITING_COMMIT_CLOSE.value
        assert row["completed_at"] is None

    def test_round_numbers_increment(self, db):
        _insert_round(db, 1, RoundState.COMPLETE)
        orchestrator = _WalkingOrchestrator()

        result = orchestrator.run_round(TRIGGER_SCHEDULED)

        assert result["round_number"] == 2

    def test_trigger_source_is_recorded(self, db):
        orchestrator = _WalkingOrchestrator()

        result = orchestrator.run_round(TRIGGER_MANUAL)

        assert _round_row(db, result["round_id"])["trigger_source"] == TRIGGER_MANUAL

    def test_unbuilt_stages_fail_the_round_explicitly(self, db):
        class _FreezeOnly(RoundOrchestrator):
            def _freeze(self, conn, round_ctx):
                pass

        result = _FreezeOnly().run_round(TRIGGER_SCHEDULED)

        assert result["status"] == RoundState.FAILED.value
        row = _round_row(db, result["round_id"])
        assert row["status"] == RoundState.FAILED.value
        assert "not implemented" in row["error_message"]
        assert "announcement" in row["error_message"]
        assert row["completed_at"] is not None

    def test_stage_failure_marks_round_failed_with_stage_prefix(self, db):
        class _FailsAtDraw(_WalkingOrchestrator):
            def _draw_judge(self, conn, round_ctx):
                raise RuntimeError("ledger unavailable")

        orchestrator = _FailsAtDraw()
        result = orchestrator.run_round(TRIGGER_SCHEDULED)

        assert result["status"] == RoundState.FAILED.value
        row = _round_row(db, result["round_id"])
        assert row["error_message"] == "ANNOUNCED: ledger unavailable"
        assert orchestrator.calls == ["freeze", "announce"]


class TestActiveRoundRule:
    def test_second_round_is_not_started_while_one_is_active(self, db):
        _insert_round(db, 1, RoundState.EXAMINED)
        orchestrator = _WalkingOrchestrator()

        result = orchestrator.run_round(TRIGGER_SCHEDULED)

        assert result == {
            "started": False,
            "reason": "round_in_progress",
            "active_round_number": 1,
            "active_status": RoundState.EXAMINED.value,
        }
        assert orchestrator.calls == []

    def test_parked_round_counts_as_active(self, db):
        _insert_round(db, 1, RoundState.AWAITING_COMMIT_CLOSE)

        result = _WalkingOrchestrator().run_round(TRIGGER_SCHEDULED)

        assert result["started"] is False

    def test_terminal_rounds_do_not_block(self, db):
        _insert_round(db, 1, RoundState.FAILED)
        _insert_round(db, 2, RoundState.ABANDONED)
        _insert_round(db, 3, RoundState.COMPLETE)

        result = _WalkingOrchestrator().run_round(TRIGGER_SCHEDULED)

        assert result["started"] is True

    def test_get_active_round_returns_none_when_all_terminal(self, db):
        _insert_round(db, 1, RoundState.COMPLETE)

        assert get_active_round(db) is None


class TestRestartClassification:
    def test_pre_freeze_round_is_abandoned(self, db):
        round_id = _insert_round(db, 1, RoundState.CREATED)

        assert cleanup_interrupted_rounds(db) == 1

        row = _round_row(db, round_id)
        assert row["status"] == RoundState.ABANDONED.value
        assert "before the freeze completed" in row["error_message"]
        assert row["completed_at"] is not None

    def test_post_freeze_rounds_survive_cleanup(self, db):
        surviving = [
            RoundState.FROZEN,
            RoundState.ANNOUNCED,
            RoundState.JUDGE_DRAWN,
            RoundState.EXAMINED,
            RoundState.GRADED,
            RoundState.AWAITING_COMMIT_CLOSE,
            RoundState.DECIDED,
            RoundState.COMPLETE,
            RoundState.FAILED,
            RoundState.ABANDONED,
        ]
        ids = {
            state: _insert_round(db, number, state)
            for number, state in enumerate(surviving, start=1)
        }

        assert cleanup_interrupted_rounds(db) == 0

        for state, round_id in ids.items():
            assert _round_row(db, round_id)["status"] == state.value

    def test_resume_continues_from_persisted_state(self, db):
        round_id = _insert_round(db, 1, RoundState.ANNOUNCED)
        orchestrator = _WalkingOrchestrator()

        results = orchestrator.resume_rounds()

        assert len(results) == 1
        assert results[0]["status"] == RoundState.AWAITING_COMMIT_CLOSE.value
        assert orchestrator.calls == ["draw_judge", "run_exam", "grade", "hold_outputs"]
        assert (
            _round_row(db, round_id)["status"]
            == RoundState.AWAITING_COMMIT_CLOSE.value
        )

    def test_resume_ignores_parked_and_terminal_rounds(self, db):
        _insert_round(db, 1, RoundState.AWAITING_COMMIT_CLOSE)
        _insert_round(db, 2, RoundState.COMPLETE)
        orchestrator = _WalkingOrchestrator()

        assert orchestrator.resume_rounds() == []
        assert orchestrator.calls == []


class TestWithheldPublication:
    def test_parked_round_publishes_after_commit_close(self, db):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        round_id = _insert_round(
            db, 1, RoundState.AWAITING_COMMIT_CLOSE, commit_closes_at=past
        )
        orchestrator = _WalkingOrchestrator()

        results = orchestrator.publish_due_rounds()

        assert len(results) == 1
        assert results[0]["status"] == RoundState.COMPLETE.value
        assert orchestrator.calls == ["decide", "publish_record"]
        row = _round_row(db, round_id)
        assert row["status"] == RoundState.COMPLETE.value
        assert row["completed_at"] is not None

    def test_hold_is_fail_closed_without_commit_close_timestamp(self, db):
        _insert_round(db, 1, RoundState.AWAITING_COMMIT_CLOSE, commit_closes_at=None)
        orchestrator = _WalkingOrchestrator()

        assert orchestrator.publish_due_rounds() == []
        assert orchestrator.calls == []

    def test_hold_is_kept_until_the_window_closes(self, db):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        _insert_round(
            db, 1, RoundState.AWAITING_COMMIT_CLOSE, commit_closes_at=future
        )
        orchestrator = _WalkingOrchestrator()

        assert orchestrator.publish_due_rounds() == []

    def test_interrupted_publication_resumes_from_decided(self, db):
        round_id = _insert_round(db, 1, RoundState.DECIDED)
        orchestrator = _WalkingOrchestrator()

        results = orchestrator.publish_due_rounds()

        assert len(results) == 1
        assert orchestrator.calls == ["publish_record"]
        assert _round_row(db, round_id)["status"] == RoundState.COMPLETE.value
