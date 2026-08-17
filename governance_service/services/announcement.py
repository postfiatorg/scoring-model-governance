"""Governance on-chain publishing: memo formats and the announce stage (G.5.3).

The announcement makes a frozen round public and datable: a memo from the
foundation publisher wallet carries the package identity and the absolute
commit/reveal windows, derived at emission with the same discipline
scoring rounds use (anchored at validated-ledger close time, the commit
window never opening before the freeze — adapted from the scoring
service's commit_reveal module). The announcement transaction's validated
ledger index is persisted as the anchor the frozen judge-draw procedure
builds on, and its commit-close timestamp is the value the withheld
publication release gates on.

The memo formats live in their own versioned governance namespace,
mirroring the scoring protocol's conventions. The round-close receipt
format is defined here and emitted at G.5.5 with the final record.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from governance_service.clients.pftl import PFTLClient
from governance_service.config import settings
from governance_service.scoring import canonical_json_bytes, is_sha256_hex

logger = logging.getLogger(__name__)

GOVERNANCE_PROTOCOL_VERSION = 1
_SUFFIX = f"v{GOVERNANCE_PROTOCOL_VERSION}"
ROUND_ANNOUNCEMENT_TYPE = f"pf_governance_round_announcement_{_SUFFIX}"
ROUND_RECEIPT_TYPE = f"pf_governance_round_receipt_{_SUFFIX}"

# The transaction's MemoType carries the type discriminator, so payloads
# carry no type field — same convention as the scoring announcements.
ANNOUNCEMENT_PAYLOAD_FIELDS = (
    "protocol_version",
    "network",
    "round_number",
    "package_cid",
    "package_hash",
    "commit_opens_at",
    "commit_closes_at",
    "reveal_opens_at",
    "reveal_closes_at",
)
RECEIPT_PAYLOAD_FIELDS = (
    "protocol_version",
    "network",
    "round_number",
    "package_hash",
    "final_record_cid",
)


class AnnouncementError(RuntimeError):
    """The announcement could not be built, submitted, or anchored."""


@dataclass(frozen=True)
class GovernanceRoundAnnouncement:
    protocol_version: int
    network: str
    round_number: int
    package_cid: str
    package_hash: str
    commit_opens_at: datetime
    commit_closes_at: datetime
    reveal_opens_at: datetime
    reveal_closes_at: datetime


def compute_round_windows(
    *,
    frozen_at: datetime,
    anchor: datetime,
    commit_window: timedelta,
    reveal_window: timedelta,
) -> tuple[datetime, datetime, datetime, datetime]:
    """Derive ordered commit/reveal windows for the announcement.

    Windows anchor at emission time so verifiers receive the full window
    even when the freeze happened earlier; the commit window never opens
    before the freeze. Adapted from the scoring commit_reveal rule;
    returns UTC timestamps so the payload's canonical bytes never depend
    on a session or deployment timezone.
    """
    if frozen_at.tzinfo is None or anchor.tzinfo is None:
        raise AnnouncementError("window inputs must be timezone-aware")
    frozen_at = frozen_at.astimezone(timezone.utc)
    anchor = anchor.astimezone(timezone.utc)
    if commit_window <= timedelta(0):
        raise AnnouncementError("commit_window must be positive")
    if reveal_window <= timedelta(0):
        raise AnnouncementError("reveal_window must be positive")
    commit_opens_at = max(anchor, frozen_at)
    commit_closes_at = commit_opens_at + commit_window
    reveal_opens_at = commit_closes_at
    reveal_closes_at = reveal_opens_at + reveal_window
    return commit_opens_at, commit_closes_at, reveal_opens_at, reveal_closes_at


def build_announcement(
    *,
    network: str,
    round_number: int,
    package_cid: str,
    package_hash: str,
    commit_opens_at: datetime,
    commit_closes_at: datetime,
    reveal_opens_at: datetime,
    reveal_closes_at: datetime,
) -> GovernanceRoundAnnouncement:
    """Validate inputs and assemble the announcement."""
    if not isinstance(round_number, int) or round_number < 1:
        raise AnnouncementError(f"round_number must be a positive int: {round_number!r}")
    if not network:
        raise AnnouncementError("network must not be empty")
    if not package_cid:
        raise AnnouncementError("package_cid must not be empty")
    if not is_sha256_hex(package_hash):
        raise AnnouncementError(f"package_hash is not a sha256 hex: {package_hash!r}")
    if not (commit_opens_at < commit_closes_at <= reveal_opens_at < reveal_closes_at):
        raise AnnouncementError("announcement windows are not ordered")
    return GovernanceRoundAnnouncement(
        protocol_version=GOVERNANCE_PROTOCOL_VERSION,
        network=network,
        round_number=round_number,
        package_cid=package_cid,
        package_hash=package_hash,
        commit_opens_at=commit_opens_at,
        commit_closes_at=commit_closes_at,
        reveal_opens_at=reveal_opens_at,
        reveal_closes_at=reveal_closes_at,
    )


def announcement_payload(announcement: GovernanceRoundAnnouncement) -> dict[str, Any]:
    """The canonical on-chain MemoData payload for an announcement."""
    return {
        "protocol_version": announcement.protocol_version,
        "network": announcement.network,
        "round_number": announcement.round_number,
        "package_cid": announcement.package_cid,
        "package_hash": announcement.package_hash,
        "commit_opens_at": announcement.commit_opens_at.isoformat(),
        "commit_closes_at": announcement.commit_closes_at.isoformat(),
        "reveal_opens_at": announcement.reveal_opens_at.isoformat(),
        "reveal_closes_at": announcement.reveal_closes_at.isoformat(),
    }


def receipt_payload(
    *,
    network: str,
    round_number: int,
    package_hash: str,
    final_record_cid: str,
) -> dict[str, Any]:
    """The canonical round-close receipt payload, emitted at G.5.5."""
    if not isinstance(round_number, int) or round_number < 1:
        raise AnnouncementError(f"round_number must be a positive int: {round_number!r}")
    if not network:
        raise AnnouncementError("network must not be empty")
    if not is_sha256_hex(package_hash):
        raise AnnouncementError(f"package_hash is not a sha256 hex: {package_hash!r}")
    if not final_record_cid:
        raise AnnouncementError("final_record_cid must not be empty")
    return {
        "protocol_version": GOVERNANCE_PROTOCOL_VERSION,
        "network": network,
        "round_number": round_number,
        "package_hash": package_hash,
        "final_record_cid": final_record_cid,
    }


def _load_round_state(
    conn, round_id: int
) -> tuple[str, str, datetime, str | None, int | None, datetime | None]:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT package_cid, package_hash, frozen_at,
               announcement_tx_hash, announcement_ledger_index, commit_closes_at
        FROM governance_rounds
        WHERE id = %s
        """,
        (round_id,),
    )
    row = cursor.fetchone()
    cursor.close()
    conn.rollback()
    if row is None or not all(row[:3]):
        raise AnnouncementError(
            f"Round id {round_id} has no frozen package identity to announce"
        )
    return row


def announce_round(
    conn,
    round_id: int,
    round_number: int,
    *,
    client: PFTLClient | None = None,
    fallback_now: datetime | None = None,
) -> dict[str, Any]:
    """Submit the round announcement and persist its on-chain identity.

    Windows anchor at the validated-ledger close time (`fallback_now`, or
    service UTC time, only when the ledger read fails). Re-run-safe: a
    round that already carries an announcement returns its persisted
    identity instead of submitting a second memo — a resumed FROZEN round
    must never move the public judge-draw anchor. Raises
    AnnouncementError on any failure; the orchestrator's stage discipline
    records it on the FAILED round.
    """
    (
        package_cid,
        package_hash,
        frozen_at,
        existing_tx_hash,
        existing_ledger_index,
        existing_commit_closes,
    ) = _load_round_state(conn, round_id)
    if existing_tx_hash is not None:
        logger.info(
            "Round %d already announced (tx %s) — keeping the existing anchor",
            round_number,
            existing_tx_hash,
        )
        return {
            "announcement_tx_hash": existing_tx_hash,
            "announcement_ledger_index": existing_ledger_index,
            "commit_closes_at": existing_commit_closes,
        }
    pftl = client or PFTLClient()

    try:
        anchor = pftl.latest_validated_ledger_close_time()
    except Exception as exc:
        anchor = fallback_now or datetime.now(timezone.utc)
        logger.warning(
            "Could not read validated-ledger close time for announcement; "
            "falling back to service UTC time: %s",
            exc,
        )

    commit_opens, commit_closes, reveal_opens, reveal_closes = compute_round_windows(
        frozen_at=frozen_at,
        anchor=anchor,
        commit_window=timedelta(seconds=settings.round_commit_window_seconds),
        reveal_window=timedelta(seconds=settings.round_reveal_window_seconds),
    )
    announcement = build_announcement(
        network=settings.environment,
        round_number=round_number,
        package_cid=package_cid,
        package_hash=package_hash,
        commit_opens_at=commit_opens,
        commit_closes_at=commit_closes,
        reveal_opens_at=reveal_opens,
        reveal_closes_at=reveal_closes,
    )
    memo_data = canonical_json_bytes(announcement_payload(announcement)).decode("utf-8")

    success, tx_hash, ledger_index, error = pftl.submit_memo(
        memo_data, memo_type=ROUND_ANNOUNCEMENT_TYPE
    )
    if not success:
        raise AnnouncementError(f"Announcement memo submission failed: {error}")
    if ledger_index is None:
        raise AnnouncementError(
            f"Announcement {tx_hash} validated without a ledger index — "
            "the judge-draw anchor cannot be derived"
        )

    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE governance_rounds
        SET announcement_tx_hash = %s, announcement_ledger_index = %s,
            commit_opens_at = %s, commit_closes_at = %s,
            reveal_opens_at = %s, reveal_closes_at = %s
        WHERE id = %s
        """,
        (tx_hash, ledger_index, commit_opens, commit_closes, reveal_opens, reveal_closes, round_id),
    )
    conn.commit()
    cursor.close()

    logger.info(
        "Round %d announced: tx %s in ledger %d, commit closes %s",
        round_number,
        tx_hash,
        ledger_index,
        commit_closes.isoformat(),
    )
    return {
        "announcement_tx_hash": tx_hash,
        "announcement_ledger_index": ledger_index,
        "commit_closes_at": commit_closes,
    }
