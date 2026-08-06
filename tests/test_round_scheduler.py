"""Round scheduler behavior against a real PostgreSQL database."""

from datetime import datetime, timedelta, timezone

import pytest

from governance_service.config import settings
from governance_service.database import (
    get_db,
    release_advisory_lock,
    try_advisory_lock,
)
from governance_service.services.orchestrator import RoundState, TRIGGER_SCHEDULED
from governance_service.services.scheduler import (
    ROUND_ADVISORY_LOCK_ID,
    _advance_schedule,
    _is_round_due,
    ensure_schedule_seeded,
    reanchor_schedule,
    run_scheduler_tick,
)
from tests.test_round_orchestrator import _WalkingOrchestrator, _insert_round

CADENCE = timedelta(days=30)


@pytest.fixture(autouse=True)
def _pinned_cadence(monkeypatch):
    """Keep the suite independent of a developer .env cadence override."""
    monkeypatch.setattr(settings, "round_cadence_days", 30)


def _autocommit(db):
    db.autocommit = True
    return db


def _set_next_due(db, next_due: datetime) -> None:
    cursor = db.cursor()
    cursor.execute(
        """
        INSERT INTO governance_round_schedule (id, next_due_at)
        VALUES (1, %s)
        ON CONFLICT (id) DO UPDATE SET next_due_at = EXCLUDED.next_due_at
        """,
        (next_due,),
    )
    cursor.close()


def _get_next_due(db) -> datetime | None:
    cursor = db.cursor()
    cursor.execute("SELECT next_due_at FROM governance_round_schedule WHERE id = 1")
    row = cursor.fetchone()
    cursor.close()
    return row[0] if row else None


class TestScheduleSeeding:
    def test_fresh_install_seeds_one_cadence_out(self, db):
        conn = _autocommit(db)
        before = datetime.now(timezone.utc)

        next_due = ensure_schedule_seeded(conn)

        assert before + CADENCE <= next_due <= datetime.now(timezone.utc) + CADENCE
        assert _get_next_due(conn) == next_due

    def test_seed_derives_from_last_round(self, db):
        conn = _autocommit(db)
        started = datetime.now(timezone.utc) - timedelta(days=10)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO governance_rounds (round_number, status, trigger_source, started_at)
            VALUES (1, %s, %s, %s)
            """,
            (RoundState.COMPLETE.value, TRIGGER_SCHEDULED, started),
        )
        cursor.close()

        next_due = ensure_schedule_seeded(conn)

        assert next_due == started + CADENCE

    def test_seed_prefers_completion_time(self, db):
        conn = _autocommit(db)
        started = datetime.now(timezone.utc) - timedelta(days=10)
        completed = started + timedelta(days=2)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO governance_rounds
                (round_number, status, trigger_source, started_at, completed_at)
            VALUES (1, %s, %s, %s, %s)
            """,
            (RoundState.COMPLETE.value, TRIGGER_SCHEDULED, started, completed),
        )
        cursor.close()

        assert ensure_schedule_seeded(conn) == completed + CADENCE

    def test_existing_row_is_not_reseeded(self, db):
        conn = _autocommit(db)
        existing = datetime.now(timezone.utc) + timedelta(days=3)
        _set_next_due(conn, existing)

        assert ensure_schedule_seeded(conn) == existing


class TestDueCheck:
    def test_due_when_schedule_in_past(self, db):
        conn = _autocommit(db)
        _set_next_due(conn, datetime.now(timezone.utc) - timedelta(hours=1))

        assert _is_round_due(conn) is True

    def test_not_due_when_schedule_in_future(self, db):
        conn = _autocommit(db)
        _set_next_due(conn, datetime.now(timezone.utc) + timedelta(hours=1))

        assert _is_round_due(conn) is False

    def test_fresh_install_is_not_due(self, db):
        assert _is_round_due(_autocommit(db)) is False


class TestScheduleAdvance:
    def test_advances_by_whole_cadence_periods(self, db):
        conn = _autocommit(db)
        original = datetime.now(timezone.utc) - 3 * CADENCE - timedelta(hours=1)
        _set_next_due(conn, original)

        advanced = _advance_schedule(conn)

        assert advanced > datetime.now(timezone.utc)
        assert (advanced - original) % CADENCE == timedelta(0)
        assert advanced - datetime.now(timezone.utc) <= CADENCE
        assert _get_next_due(conn) == advanced

    def test_future_schedule_is_left_unchanged(self, db):
        conn = _autocommit(db)
        future = datetime.now(timezone.utc) + timedelta(days=3)
        _set_next_due(conn, future)

        assert _advance_schedule(conn) == future

    def test_missing_row_returns_none(self, db):
        assert _advance_schedule(_autocommit(db)) is None


class TestReanchor:
    def test_reanchor_resets_to_one_cadence_from_now(self, db):
        conn = _autocommit(db)
        _set_next_due(conn, datetime.now(timezone.utc) - timedelta(days=5))
        before = datetime.now(timezone.utc)

        next_due = reanchor_schedule(conn)

        assert before + CADENCE <= next_due <= datetime.now(timezone.utc) + CADENCE
        assert _get_next_due(conn) == next_due

    def test_reanchor_upserts_before_seeding(self, db):
        conn = _autocommit(db)

        next_due = reanchor_schedule(conn)

        assert _get_next_due(conn) == next_due


class TestAdvisoryLock:
    def test_lock_is_exclusive_across_connections(self, db):
        conn = _autocommit(db)
        other = get_db()
        other.autocommit = True
        try:
            assert try_advisory_lock(conn, ROUND_ADVISORY_LOCK_ID) is True
            assert try_advisory_lock(other, ROUND_ADVISORY_LOCK_ID) is False

            release_advisory_lock(conn, ROUND_ADVISORY_LOCK_ID)

            assert try_advisory_lock(other, ROUND_ADVISORY_LOCK_ID) is True
            release_advisory_lock(other, ROUND_ADVISORY_LOCK_ID)
        finally:
            other.close()


class TestSchedulerTick:
    def test_due_tick_starts_round_and_consumes_slot(self, db):
        conn = _autocommit(db)
        _set_next_due(conn, datetime.now(timezone.utc) - timedelta(hours=1))
        orchestrator = _WalkingOrchestrator()

        run_scheduler_tick(orchestrator)

        assert orchestrator.calls[0] == "freeze"
        cursor = conn.cursor()
        cursor.execute(
            "SELECT trigger_source, status FROM governance_rounds WHERE round_number = 1"
        )
        trigger_source, status = cursor.fetchone()
        cursor.close()
        assert trigger_source == TRIGGER_SCHEDULED
        assert status == RoundState.AWAITING_COMMIT_CLOSE.value
        assert _get_next_due(conn) > datetime.now(timezone.utc)

    def test_tick_without_due_schedule_starts_nothing(self, db):
        conn = _autocommit(db)
        _set_next_due(conn, datetime.now(timezone.utc) + timedelta(days=3))
        orchestrator = _WalkingOrchestrator()

        run_scheduler_tick(orchestrator)

        assert orchestrator.calls == []

    def test_tick_skips_when_lock_held_elsewhere(self, db):
        conn = _autocommit(db)
        _set_next_due(conn, datetime.now(timezone.utc) - timedelta(hours=1))
        holder = get_db()
        holder.autocommit = True
        try:
            assert try_advisory_lock(holder, ROUND_ADVISORY_LOCK_ID) is True
            orchestrator = _WalkingOrchestrator()

            run_scheduler_tick(orchestrator)

            assert orchestrator.calls == []
            release_advisory_lock(holder, ROUND_ADVISORY_LOCK_ID)
        finally:
            holder.close()

    def test_tick_abandons_interrupted_pre_freeze_round(self, db):
        conn = _autocommit(db)
        _set_next_due(conn, datetime.now(timezone.utc) + timedelta(days=3))
        round_id = _insert_round(conn, 1, RoundState.CREATED)

        run_scheduler_tick(_WalkingOrchestrator())

        cursor = conn.cursor()
        cursor.execute(
            "SELECT status FROM governance_rounds WHERE id = %s", (round_id,)
        )
        assert cursor.fetchone()[0] == RoundState.ABANDONED.value
        cursor.close()

    def test_tick_resumes_interrupted_round_instead_of_starting_new(self, db):
        conn = _autocommit(db)
        _set_next_due(conn, datetime.now(timezone.utc) - timedelta(hours=1))
        _insert_round(conn, 1, RoundState.GRADED)
        orchestrator = _WalkingOrchestrator()

        run_scheduler_tick(orchestrator)

        assert orchestrator.calls == ["hold_outputs"]
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM governance_rounds")
        assert cursor.fetchone()[0] == 1
        cursor.close()

    def test_due_slot_is_not_consumed_while_a_round_is_active(self, db):
        conn = _autocommit(db)
        overdue = datetime.now(timezone.utc) - timedelta(hours=1)
        _set_next_due(conn, overdue)
        _insert_round(db, 1, RoundState.AWAITING_COMMIT_CLOSE)
        orchestrator = _WalkingOrchestrator()

        run_scheduler_tick(orchestrator)

        assert orchestrator.calls == []
        assert _get_next_due(conn) == overdue
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM governance_rounds")
        assert cursor.fetchone()[0] == 1
        cursor.close()

    def test_tick_publishes_due_parked_round(self, db):
        conn = _autocommit(db)
        _set_next_due(conn, datetime.now(timezone.utc) + timedelta(days=3))
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        round_id = _insert_round(
            conn, 1, RoundState.AWAITING_COMMIT_CLOSE, commit_closes_at=past
        )
        orchestrator = _WalkingOrchestrator()

        run_scheduler_tick(orchestrator)

        assert orchestrator.calls == ["decide", "publish_record"]
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status FROM governance_rounds WHERE id = %s", (round_id,)
        )
        assert cursor.fetchone()[0] == RoundState.COMPLETE.value
        cursor.close()
