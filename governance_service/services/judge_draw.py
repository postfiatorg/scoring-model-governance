"""The judge draw: ledger randomness onto the frozen challengers (G.5.4).

Implements exactly the draw procedure the round package froze at G.5.2:
the drawing ledger is the first validated ledger whose index is at least
the announcement transaction's validated ledger index plus the frozen
offset; its hash, read as a big-endian integer, modulo the challenger
count indexes the challengers sorted ascending by hf_repo in Unicode
codepoint order. The challenger list comes from the frozen package
artifacts, never the live pool — freeze-time membership is authoritative,
and the incumbent is excluded by construction because the package
separates it from the challengers.

The draw is a pure function of public data (the on-chain announcement
plus the frozen package), so any verifier recomputes the identical judge
and a resumed round cannot land on a different one. The deterministic
redraw ordering lives here too; the stages that detect a judge's
mechanical failure invoke it when they land.
"""

import logging
import time
from typing import Any, Callable, Collection

from governance_service.clients.pftl import PFTLClient
from governance_service.services.round_package import (
    CANDIDATES_FILE_PATH,
    DRAW_LEDGER_OFFSET,
    get_package_file,
)

logger = logging.getLogger(__name__)

# Operational bounds for waiting on the drawing ledger, which by
# construction validates well under a minute after the announcement.
DRAW_WAIT_TIMEOUT_SECONDS = 300
DRAW_POLL_INTERVAL_SECONDS = 5


class JudgeDrawError(RuntimeError):
    """The draw could not be computed from the round's public data."""


def drawing_ledger_index(announcement_ledger_index: int) -> int:
    """The frozen rule: the announcement's validated ledger plus the offset."""
    return announcement_ledger_index + DRAW_LEDGER_OFFSET


def sorted_challengers(candidates_file: dict[str, Any]) -> list[str]:
    """The frozen challenger identifiers in Unicode codepoint order."""
    entries = candidates_file.get("challengers")
    if not isinstance(entries, list) or not entries:
        raise JudgeDrawError("Frozen package carries no challengers")
    repos = []
    for entry in entries:
        repo = (entry.get("profile") or {}).get("hf_repo")
        if not repo:
            raise JudgeDrawError("Frozen challenger entry has no hf_repo")
        repos.append(repo)
    return sorted(repos)


def map_hash_to_challenger(ledger_hash: str, challengers: list[str]) -> str:
    """The frozen mapping: hash as a big-endian integer modulo the count."""
    if not challengers:
        raise JudgeDrawError("Cannot draw a judge from zero challengers")
    try:
        draw_value = int(ledger_hash, 16)
    except (TypeError, ValueError) as exc:
        raise JudgeDrawError(f"Drawing ledger hash is not hex: {ledger_hash!r}") from exc
    ordered = sorted(challengers)
    return ordered[draw_value % len(ordered)]


def next_judge(
    ledger_hash: str,
    challengers: list[str],
    failed: Collection[str] = (),
) -> str | None:
    """The frozen redraw ordering: cyclic successor, skipping failed judges.

    Returns the first challenger in draw order that has not failed, or
    None when the challengers are exhausted — the round is abandoned.
    """
    ordered = sorted(challengers)
    first = ordered.index(map_hash_to_challenger(ledger_hash, ordered))
    for step in range(len(ordered)):
        candidate = ordered[(first + step) % len(ordered)]
        if candidate not in failed:
            return candidate
    return None


def _load_draw_state(
    conn, round_id: int
) -> tuple[int | None, str | None, int | None, str | None]:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT announcement_ledger_index, judge_hf_repo,
               draw_ledger_index, draw_ledger_hash
        FROM governance_rounds
        WHERE id = %s
        """,
        (round_id,),
    )
    row = cursor.fetchone()
    cursor.close()
    conn.rollback()
    if row is None:
        raise JudgeDrawError(f"Round id {round_id} does not exist")
    return row


def draw_judge(
    conn,
    round_id: int,
    round_number: int,
    *,
    client: PFTLClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Compute and persist the round's judge from the drawing ledger.

    Waits within a bounded window for the drawing ledger to validate.
    Re-run-safe two ways: a round with a persisted draw returns it
    without touching the chain (resilient to later ledger pruning), and
    a recomputation is deterministic by construction anyway. Raises
    JudgeDrawError on draw failures; configuration and RPC errors
    propagate as their own types — the orchestrator's stage discipline
    records any of them on the FAILED round.
    """
    (
        announcement_ledger_index,
        existing_judge,
        existing_draw_index,
        existing_draw_hash,
    ) = _load_draw_state(conn, round_id)
    if existing_judge is not None:
        logger.info(
            "Round %d already drew its judge (%s) — keeping it",
            round_number,
            existing_judge,
        )
        return {
            "judge_hf_repo": existing_judge,
            "draw_ledger_index": existing_draw_index,
            "draw_ledger_hash": existing_draw_hash,
        }
    if announcement_ledger_index is None:
        raise JudgeDrawError(
            f"Round id {round_id} has no announcement ledger index to draw from"
        )

    candidates_file = get_package_file(conn, round_number, CANDIDATES_FILE_PATH)
    # The artifact read is done; the wait loop below can run minutes, so
    # do not sit on an idle-in-transaction connection through it.
    conn.rollback()
    if candidates_file is None:
        raise JudgeDrawError(
            f"Round {round_number} has no frozen {CANDIDATES_FILE_PATH} artifact"
        )
    challengers = sorted_challengers(candidates_file)

    pftl = client or PFTLClient()
    target = drawing_ledger_index(announcement_ledger_index)
    attempts = max(1, DRAW_WAIT_TIMEOUT_SECONDS // DRAW_POLL_INTERVAL_SECONDS)
    for attempt in range(attempts):
        if pftl.latest_validated_ledger_index() >= target:
            break
        if attempt == attempts - 1:
            raise JudgeDrawError(
                f"Drawing ledger {target} did not validate within "
                f"{DRAW_WAIT_TIMEOUT_SECONDS}s"
            )
        sleep(DRAW_POLL_INTERVAL_SECONDS)

    ledger_hash = pftl.ledger_hash(target)
    judge = map_hash_to_challenger(ledger_hash, challengers)

    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE governance_rounds
        SET judge_hf_repo = %s, draw_ledger_index = %s, draw_ledger_hash = %s
        WHERE id = %s
        """,
        (judge, target, ledger_hash, round_id),
    )
    conn.commit()
    cursor.close()

    logger.info(
        "Round %d drew judge %s from ledger %d (%s)",
        round_number,
        judge,
        target,
        ledger_hash,
    )
    return {
        "judge_hf_repo": judge,
        "draw_ledger_index": target,
        "draw_ledger_hash": ledger_hash,
    }
