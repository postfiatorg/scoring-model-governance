"""Governance round memos on the PFT Ledger (G.5.3).

Publishes two on-chain receipts per round: one when the round freezes
(the frozen-input package's hash is now fixed) and one when the round
completes (the final decision is public). Both are best-effort and
never block round progression — a missing PFTL configuration or a
ledger submission failure is logged and the stage simply completes
without a memo, mirroring this repo's own existing
``record_publisher.py`` philosophy of "publication failure is a
visible state, never an exception into the flow."

The frozen/complete status strings are duplicated here rather than
imported from ``orchestrator.RoundState`` to avoid a circular import
(``orchestrator.py`` imports this module). ``TestStatusConstantsMatchRoundState``
in the test suite imports both and asserts equality, so any future
drift between the two is caught by CI rather than silently.
"""

import logging

from governance_service.clients.pftl import PFTLClient
from governance_service.config import settings
from governance_service.scoring import canonical_json_bytes

logger = logging.getLogger(__name__)

GOVERNANCE_ROUND_FROZEN_MEMO_TYPE = "pf_governance_round_frozen_v1"
GOVERNANCE_ROUND_COMPLETE_MEMO_TYPE = "pf_governance_round_complete_v1"

# Duplicated from orchestrator.RoundState on purpose — see module docstring.
FROZEN_STATUS = "FROZEN"
COMPLETE_STATUS = "COMPLETE"


def build_memo_payload(round_id: int, status: str, package_hash: str) -> dict:
    """The deterministic {round_id, status, package_hash} memo payload."""
    return {"round_id": round_id, "status": status, "package_hash": package_hash}


def _load_package_hash(conn, round_id: int) -> str | None:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT package_hash FROM governance_rounds WHERE id = %s", (round_id,)
    )
    row = cursor.fetchone()
    cursor.close()
    return row[0] if row is not None else None


class RoundMemoPublisher:
    """Publishes the frozen-round and complete-round memos, best-effort."""

    def __init__(self, pftl_client: PFTLClient | None = None):
        self._client: PFTLClient | None = pftl_client

    def _get_client(self) -> PFTLClient | None:
        if self._client is not None:
            return self._client
        if not settings.pftl_enabled:
            return None
        try:
            self._client = PFTLClient()
        except Exception:
            logger.exception(
                "Governance round memo publisher: PFTL client init failed"
            )
            return None
        return self._client

    def _publish(
        self, round_id: int, status: str, package_hash: str, memo_type: str
    ) -> str | None:
        client = self._get_client()
        if client is None:
            logger.info(
                "PFTL not configured — skipping %s memo for governance round %d",
                status,
                round_id,
            )
            return None

        payload = build_memo_payload(round_id, status, package_hash)
        memo_data = canonical_json_bytes(payload).decode("utf-8")

        success, tx_hash, error = client.submit_memo(memo_data, memo_type=memo_type)
        if success:
            logger.info(
                "Governance round %d memo published (%s): tx=%s",
                round_id,
                status,
                tx_hash,
            )
            return tx_hash

        logger.error(
            "Governance round %d memo failed (%s): %s", round_id, status, error
        )
        return None

    def publish_round_frozen(self, conn, round_id: int) -> str | None:
        """Best-effort announcement memo for a round that just froze."""
        package_hash = _load_package_hash(conn, round_id)
        if package_hash is None:
            logger.warning(
                "Governance round %d has no package_hash yet — skipping frozen memo",
                round_id,
            )
            return None
        return self._publish(
            round_id, FROZEN_STATUS, package_hash, GOVERNANCE_ROUND_FROZEN_MEMO_TYPE
        )

    def publish_round_complete(self, conn, round_id: int) -> str | None:
        """Best-effort completion receipt memo for a round that just decided."""
        package_hash = _load_package_hash(conn, round_id)
        if package_hash is None:
            logger.warning(
                "Governance round %d has no package_hash — skipping complete memo",
                round_id,
            )
            return None
        return self._publish(
            round_id,
            COMPLETE_STATUS,
            package_hash,
            GOVERNANCE_ROUND_COMPLETE_MEMO_TYPE,
        )
