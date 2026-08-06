"""Governance round orchestrator — the persisted state machine for a round.

G.5.1 delivers the lifecycle backbone: the round states, their restart
classification, the persistence helpers, and the stage pipeline that the
remaining G.5 steps fill in with real behavior (G.5.2 freeze, G.5.3
announcement, G.5.4 judge draw, G.5.5 withholding and final publication,
G.5.6 decision, with the G.3 exam and G.4 grading engines wired into their
stages along the way). A stage that is not built yet raises
StageNotImplemented, so a prematurely triggered round fails explicitly
instead of faking progress.

Restart semantics follow the methodology's freeze contract: a round that
dies before its freeze completes published nothing and is abandoned by
startup cleanup, while a round past the freeze is resumable — every frozen
input is content-pinned and the exam and grading engines resume
idempotently, so a service restart never discards paid-for GPU work.
"""

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from governance_service.database import get_db

logger = logging.getLogger(__name__)


class RoundState(str, Enum):
    CREATED = "CREATED"
    FROZEN = "FROZEN"
    ANNOUNCED = "ANNOUNCED"
    JUDGE_DRAWN = "JUDGE_DRAWN"
    EXAMINED = "EXAMINED"
    GRADED = "GRADED"
    AWAITING_COMMIT_CLOSE = "AWAITING_COMMIT_CLOSE"
    DECIDED = "DECIDED"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    ABANDONED = "ABANDONED"


TERMINAL_STATES = frozenset({
    RoundState.COMPLETE,
    RoundState.FAILED,
    RoundState.ABANDONED,
})

# Before the freeze completes nothing is published, so a restart abandons
# the round instead of resuming it — re-freezing is cheap and unambiguous.
PRE_FREEZE_STATES = frozenset({RoundState.CREATED})

# Post-freeze execution states re-enter the stage pipeline on restart.
# AWAITING_COMMIT_CLOSE is excluded: a parked round is not interrupted,
# it waits for its commit window and is advanced by publish_due_rounds.
RESUMABLE_EXECUTION_STATES = frozenset({
    RoundState.FROZEN,
    RoundState.ANNOUNCED,
    RoundState.JUDGE_DRAWN,
    RoundState.EXAMINED,
    RoundState.GRADED,
})

TRIGGER_SCHEDULED = "scheduled"
TRIGGER_MANUAL = "manual"

# Stage pipelines: (entry state, handler name, state on completion).
# Execution runs in one pass until the round parks; publication resumes
# once the round's commit window has closed.
_EXECUTION_PIPELINE = (
    (RoundState.CREATED, "_freeze", RoundState.FROZEN),
    (RoundState.FROZEN, "_announce", RoundState.ANNOUNCED),
    (RoundState.ANNOUNCED, "_draw_judge", RoundState.JUDGE_DRAWN),
    (RoundState.JUDGE_DRAWN, "_run_exam", RoundState.EXAMINED),
    (RoundState.EXAMINED, "_grade", RoundState.GRADED),
    (RoundState.GRADED, "_hold_outputs", RoundState.AWAITING_COMMIT_CLOSE),
)
_PUBLICATION_PIPELINE = (
    (RoundState.AWAITING_COMMIT_CLOSE, "_decide", RoundState.DECIDED),
    (RoundState.DECIDED, "_publish_record", RoundState.COMPLETE),
)


class StageNotImplemented(RuntimeError):
    """A pipeline stage whose behavior arrives with a later milestone."""

    def __init__(self, stage: str, milestone: str):
        super().__init__(
            f"Round stage '{stage}' is not implemented yet (arrives with {milestone})"
        )


def _next_round_number(conn) -> int:
    cursor = conn.cursor()
    cursor.execute("SELECT COALESCE(MAX(round_number), 0) FROM governance_rounds")
    current_max = cursor.fetchone()[0]
    cursor.close()
    return current_max + 1


def _create_round(conn, round_number: int, trigger_source: str) -> int:
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO governance_rounds (round_number, status, trigger_source, started_at)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (
            round_number,
            RoundState.CREATED.value,
            trigger_source,
            datetime.now(timezone.utc),
        ),
    )
    round_id = cursor.fetchone()[0]
    conn.commit()
    cursor.close()
    logger.info(
        "Created governance round %d (id=%d, trigger=%s)",
        round_number,
        round_id,
        trigger_source,
    )
    return round_id


def _update_round(conn, round_id: int, **fields) -> None:
    if not fields:
        return
    set_clause = ", ".join(f"{k} = %s" for k in fields)
    values = list(fields.values()) + [round_id]
    cursor = conn.cursor()
    cursor.execute(
        f"UPDATE governance_rounds SET {set_clause} WHERE id = %s",
        values,
    )
    conn.commit()
    cursor.close()


def _fail_round(conn, round_id: int, error: str) -> None:
    logger.error("Governance round %d failed: %s", round_id, error)
    _update_round(
        conn,
        round_id,
        status=RoundState.FAILED.value,
        error_message=error,
        completed_at=datetime.now(timezone.utc),
    )


def get_active_round(conn) -> dict[str, Any] | None:
    """The single non-terminal round, or None.

    Governance rounds never overlap: a parked round holds the seat until
    it completes, so a new round starts only when no round is active.
    """
    terminal = tuple(s.value for s in TERMINAL_STATES)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, round_number, status FROM governance_rounds
        WHERE status NOT IN %s
        ORDER BY round_number DESC
        LIMIT 1
        """,
        (terminal,),
    )
    row = cursor.fetchone()
    cursor.close()
    if row is None:
        return None
    return {"id": row[0], "round_number": row[1], "status": row[2]}


def cleanup_interrupted_rounds(conn) -> int:
    """Abandon rounds a restart caught before their freeze completed.

    Must run under the round advisory lock: the lock serializes round
    execution, so any pre-freeze round seen here is a leftover from a
    dead process, never a live one. Returns the number abandoned.
    """
    pre_freeze = tuple(s.value for s in PRE_FREEZE_STATES)
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE governance_rounds
        SET status = %s, error_message = %s, completed_at = %s
        WHERE status IN %s
        """,
        (
            RoundState.ABANDONED.value,
            "Round abandoned — service restarted before the freeze completed",
            datetime.now(timezone.utc),
            pre_freeze,
        ),
    )
    abandoned = cursor.rowcount
    conn.commit()
    cursor.close()
    if abandoned > 0:
        logger.warning("Abandoned %d pre-freeze governance round(s)", abandoned)
    return abandoned


class RoundOrchestrator:
    """Drives governance rounds through the stage pipelines."""

    def run_round(self, trigger_source: str) -> dict[str, Any]:
        """Start a new round and advance it until it parks, completes, or fails.

        Callers must hold the round advisory lock. Returns without starting
        when another round is still active — rounds never overlap.
        """
        conn = get_db()
        try:
            cleanup_interrupted_rounds(conn)
            active = get_active_round(conn)
            if active is not None:
                logger.info(
                    "Governance round %d is still %s — not starting a new round",
                    active["round_number"],
                    active["status"],
                )
                return {
                    "started": False,
                    "reason": "round_in_progress",
                    "active_round_number": active["round_number"],
                    "active_status": active["status"],
                }

            round_number = _next_round_number(conn)
            round_id = _create_round(conn, round_number, trigger_source)
            result = self._advance(
                conn, round_id, round_number, RoundState.CREATED, _EXECUTION_PIPELINE
            )
            result["started"] = True
            return result
        finally:
            conn.close()

    def resume_rounds(self) -> list[dict[str, Any]]:
        """Re-enter the execution pipeline for every interrupted post-freeze round.

        Callers must hold the round advisory lock. Parked rounds are not
        touched here — publish_due_rounds owns them.
        """
        conn = get_db()
        try:
            resumable = tuple(s.value for s in RESUMABLE_EXECUTION_STATES)
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, round_number, status FROM governance_rounds
                WHERE status IN %s
                ORDER BY round_number
                """,
                (resumable,),
            )
            rows = cursor.fetchall()
            cursor.close()

            results = []
            for round_id, round_number, status in rows:
                logger.info(
                    "Resuming governance round %d from %s", round_number, status
                )
                results.append(
                    self._advance(
                        conn,
                        round_id,
                        round_number,
                        RoundState(status),
                        _EXECUTION_PIPELINE,
                    )
                )
            return results
        finally:
            conn.close()

    def publish_due_rounds(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Advance every held round whose commit window has closed.

        The hold is fail-closed: a parked round with no recorded
        commit_closes_at is never released automatically. Also resumes
        rounds interrupted mid-publication (DECIDED). Callers must hold
        the round advisory lock.
        """
        moment = now or datetime.now(timezone.utc)
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, round_number, status FROM governance_rounds
                WHERE (
                    status = %s
                    AND commit_closes_at IS NOT NULL
                    AND commit_closes_at <= %s
                )
                OR status = %s
                ORDER BY round_number
                """,
                (
                    RoundState.AWAITING_COMMIT_CLOSE.value,
                    moment,
                    RoundState.DECIDED.value,
                ),
            )
            rows = cursor.fetchall()
            cursor.close()

            results = []
            for round_id, round_number, status in rows:
                logger.info(
                    "Publishing governance round %d from %s", round_number, status
                )
                results.append(
                    self._advance(
                        conn,
                        round_id,
                        round_number,
                        RoundState(status),
                        _PUBLICATION_PIPELINE,
                    )
                )
            return results
        finally:
            conn.close()

    def _advance(
        self,
        conn,
        round_id: int,
        round_number: int,
        from_state: RoundState,
        pipeline: tuple,
    ) -> dict[str, Any]:
        """Walk a pipeline from the given state until it ends, parks, or fails."""
        result: dict[str, Any] = {"round_id": round_id, "round_number": round_number}
        state = from_state
        round_ctx = {"id": round_id, "round_number": round_number}

        for entry_state, handler_name, next_state in pipeline:
            if entry_state != state:
                continue
            try:
                getattr(self, handler_name)(conn, round_ctx)
            except Exception as exc:
                _fail_round(conn, round_id, f"{entry_state.value}: {exc}")
                result["status"] = RoundState.FAILED.value
                result["error"] = str(exc)
                return result
            state = next_state
            fields: dict[str, Any] = {"status": state.value}
            if state in TERMINAL_STATES:
                fields["completed_at"] = datetime.now(timezone.utc)
            _update_round(conn, round_id, **fields)

        result["status"] = state.value
        return result

    # -- stage handlers -------------------------------------------------------
    # Each remaining G.5 step replaces one of these with the real behavior;
    # the pipeline, persistence, and failure discipline stay as they are.
    # Handlers must be safe to re-run: a crash between a handler finishing
    # and its status write re-enters the same handler on resume.

    def _freeze(self, conn, round_ctx) -> None:
        raise StageNotImplemented("freeze", "G.5.2")

    def _announce(self, conn, round_ctx) -> None:
        raise StageNotImplemented("announcement", "G.5.3")

    def _draw_judge(self, conn, round_ctx) -> None:
        raise StageNotImplemented("judge draw", "G.5.4")

    def _run_exam(self, conn, round_ctx) -> None:
        raise StageNotImplemented("exam", "a later G.5 step")

    def _grade(self, conn, round_ctx) -> None:
        raise StageNotImplemented("grading", "a later G.5 step")

    def _hold_outputs(self, conn, round_ctx) -> None:
        raise StageNotImplemented("output withholding", "G.5.5")

    def _decide(self, conn, round_ctx) -> None:
        raise StageNotImplemented("decision", "G.5.6")

    def _publish_record(self, conn, round_ctx) -> None:
        raise StageNotImplemented("final publication", "G.5.5")
