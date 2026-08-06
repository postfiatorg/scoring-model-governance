"""Automated governance round scheduler.

Background task that checks whether a new governance round is due based
on the persisted `governance_round_schedule.next_due_at` timestamp. The
timestamp advances by whole cadence periods at scheduled round start, so
a round's own duration never shifts the schedule and a failed round
consumes its slot — the admin manual trigger is the recovery path. Uses
a PostgreSQL advisory lock to prevent concurrent rounds.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from governance_service.config import settings
from governance_service.database import get_db, release_advisory_lock, try_advisory_lock
from governance_service.services.orchestrator import (
    TRIGGER_SCHEDULED,
    RoundOrchestrator,
    cleanup_interrupted_rounds,
    get_active_round,
)

logger = logging.getLogger(__name__)

ROUND_ADVISORY_LOCK_ID = 99201


def ensure_schedule_seeded(conn) -> datetime:
    """Return next_due_at, seeding the row if missing.

    Seed = COALESCE(completed_at, started_at) of the newest round +
    cadence; with no rounds at all, now + cadence. Unlike the scoring
    scheduler, a fresh install does not fire immediately: governance has
    no pre-schedule legacy to match, and the first round of a freshly
    deployed service should be a deliberate admin trigger, not a deploy
    side effect. Requires an autocommit connection; callers must hold
    advisory lock 99201, which serializes seeding against reanchor writes.
    """
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT next_due_at FROM governance_round_schedule WHERE id = 1")
        row = cursor.fetchone()
        if row is not None:
            return row[0]

        cursor.execute(
            """
            SELECT COALESCE(completed_at, started_at)
            FROM governance_rounds
            ORDER BY round_number DESC
            LIMIT 1
            """
        )
        last_row = cursor.fetchone()
        cadence = timedelta(days=settings.round_cadence_days)

        if last_row is None:
            next_due = datetime.now(timezone.utc) + cadence
            logger.info(
                "No previous governance round — seeding schedule one cadence out: "
                "next due %s",
                next_due.isoformat(),
            )
        else:
            next_due = last_row[0] + cadence
            logger.info(
                "Seeding schedule from last round %s + %.1fd cadence: next due %s",
                last_row[0].isoformat(),
                settings.round_cadence_days,
                next_due.isoformat(),
            )

        cursor.execute(
            """
            INSERT INTO governance_round_schedule (id, next_due_at)
            VALUES (1, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (next_due,),
        )
        return next_due
    finally:
        cursor.close()


def _is_round_due(conn) -> bool:
    """Check whether the persisted schedule says a round is due."""
    next_due = ensure_schedule_seeded(conn)
    now = datetime.now(timezone.utc)

    if now >= next_due:
        logger.info("Governance round is due: next was due %s", next_due.isoformat())
        return True

    logger.debug(
        "Governance round not yet due: next at %s (%s remaining)",
        next_due.isoformat(),
        next_due - now,
    )
    return False


def _advance_schedule(conn) -> datetime | None:
    """Advance next_due_at by whole cadence periods until it is in the future.

    Called at scheduled round start so a failed round still consumes its
    slot; consuming every missed period yields at most one catch-up round
    after an outage. Requires an autocommit connection and advisory lock
    99201.
    """
    cadence = timedelta(days=settings.round_cadence_days)
    if cadence <= timedelta(0):
        raise ValueError(f"Cannot advance schedule: cadence {cadence} is not positive")

    cursor = conn.cursor()
    try:
        cursor.execute("SELECT next_due_at FROM governance_round_schedule WHERE id = 1")
        row = cursor.fetchone()
        if row is None:
            logger.error(
                "Cannot advance schedule: governance_round_schedule row is missing"
            )
            return None

        next_due = row[0]
        now = datetime.now(timezone.utc)
        while next_due <= now:
            next_due += cadence

        cursor.execute(
            "UPDATE governance_round_schedule SET next_due_at = %s WHERE id = 1",
            (next_due,),
        )
        logger.info("Schedule advanced: next governance round due %s", next_due.isoformat())
        return next_due
    finally:
        cursor.close()


def reanchor_schedule(conn) -> datetime:
    """Reset the schedule so the next round is due one cadence from now.

    Upserts, so a manual trigger during the pre-seed window (fresh deploy,
    startup delay still running) creates the row directly. Requires an
    autocommit connection and advisory lock 99201.
    """
    next_due = datetime.now(timezone.utc) + timedelta(days=settings.round_cadence_days)
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO governance_round_schedule (id, next_due_at)
            VALUES (1, %s)
            ON CONFLICT (id) DO UPDATE SET next_due_at = EXCLUDED.next_due_at
            """,
            (next_due,),
        )
    finally:
        cursor.close()
    logger.info("Schedule reanchored: next governance round due %s", next_due.isoformat())
    return next_due


def run_scheduler_tick(orchestrator: RoundOrchestrator) -> None:
    """One scheduler pass: cleanup, resume, publish due rounds, start if due.

    Skips silently when another process holds the round advisory lock —
    a round is executing there and this tick has nothing safe to do.
    """
    conn = get_db()
    lock_acquired = False
    try:
        conn.autocommit = True
        if not try_advisory_lock(conn, ROUND_ADVISORY_LOCK_ID):
            logger.info("Round advisory lock held — a round is in progress, skipping")
            return
        lock_acquired = True

        cleanup_interrupted_rounds(conn)

        resumed = orchestrator.resume_rounds()
        if resumed:
            logger.info("Resumed governance rounds: %s", resumed)

        published = orchestrator.publish_due_rounds()
        if published:
            logger.info("Published held governance rounds: %s", published)

        if _is_round_due(conn):
            active = get_active_round(conn)
            if active is not None:
                # The slot stays unconsumed: the due check re-fires each tick
                # and the scheduled round starts once the active one ends.
                logger.info(
                    "Governance round %d is still %s — delaying the scheduled round",
                    active["round_number"],
                    active["status"],
                )
            else:
                logger.info("Triggering scheduled governance round")
                _advance_schedule(conn)
                result = orchestrator.run_round(TRIGGER_SCHEDULED)
                if result["started"]:
                    logger.info(
                        "Scheduled governance round finished: status=%s, round_number=%s",
                        result.get("status"),
                        result.get("round_number"),
                    )
                else:
                    logger.warning(
                        "Scheduled governance round declined: %s",
                        result.get("reason"),
                    )
    finally:
        if lock_acquired:
            try:
                release_advisory_lock(conn, ROUND_ADVISORY_LOCK_ID)
            except Exception:
                logger.exception("Failed to release round advisory lock")
        conn.close()


async def scheduler_loop(orchestrator: RoundOrchestrator | None = None):
    """Background loop that triggers governance rounds on schedule.

    Waits for a startup delay, then runs a scheduler tick every
    `scheduler_check_interval_seconds`.
    """
    startup_delay = settings.scheduler_startup_delay_seconds
    check_interval = settings.scheduler_check_interval_seconds

    logger.info(
        "Governance scheduler starting — %ds startup delay, %ds check interval, "
        "%.1fd cadence",
        startup_delay,
        check_interval,
        settings.round_cadence_days,
    )

    await asyncio.sleep(startup_delay)

    if orchestrator is None:
        orchestrator = RoundOrchestrator()

    while True:
        try:
            await asyncio.to_thread(run_scheduler_tick, orchestrator)
        except Exception:
            logger.exception("Scheduler error during round check/execution")

        await asyncio.sleep(check_interval)
