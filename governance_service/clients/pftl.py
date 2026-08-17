"""PFTL blockchain client for governance on-chain memo transactions.

Adapted from dynamic-unl-scoring's PFTL client: Payment transactions with
memo attachments from the foundation publisher wallet, the wallet derived
from a seed or a hex private key (secp256k1). Submission is isolated in a
ThreadPoolExecutor so xrpl-py's internal ``asyncio.run()`` never conflicts
with an already-running event loop (manual trigger thread and scheduler
lifespan both use this path). One deliberate divergence: ``submit_memo``
also returns the validated ledger index — the governance judge-draw
procedure anchors on the announcement transaction's ledger.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

from ecpy.curves import Curve
from ecpy.keys import ECPrivateKey
from xrpl.clients import JsonRpcClient
from xrpl.models.requests import Ledger
from xrpl.models.transactions import Memo, Payment
from xrpl.transaction import autofill, submit_and_wait
from xrpl.utils import ripple_time_to_datetime, str_to_hex
from xrpl.wallet import Wallet

from governance_service.config import settings

logger = logging.getLogger(__name__)

PAYMENT_AMOUNT_DROPS = "1"


def wallet_from_hex_key(private_key_hex: str) -> Wallet:
    """Create a Wallet from a hex private key by deriving the public key."""
    private_key = private_key_hex.replace("0x", "").replace("0X", "")

    curve = Curve.get_curve("secp256k1")
    ec_private_key = ECPrivateKey(int(private_key, 16), curve)
    ec_public_key = ec_private_key.get_public_key()

    prefix = "02" if ec_public_key.W.y % 2 == 0 else "03"
    public_key = prefix + format(ec_public_key.W.x, "064x")

    return Wallet(public_key=public_key.upper(), private_key=private_key.upper())


def wallet_from_secret(secret: str) -> Wallet:
    """Derive an xrpl Wallet from a seed (s...) or a hex private key."""
    secret = secret.strip()
    if secret.startswith("s"):
        return Wallet.from_seed(secret)
    return wallet_from_hex_key(secret)


class PFTLClient:
    """Sync client for PFTL chain transactions."""

    def __init__(
        self,
        rpc_url: str | None = None,
        wallet_secret: str | None = None,
        memo_destination: str | None = None,
        network_id: int | None = None,
    ):
        self.rpc_url = rpc_url or settings.pftl_rpc_url
        self.wallet_secret = wallet_secret or settings.pftl_wallet_secret
        self.memo_destination = memo_destination or settings.pftl_memo_destination
        self.network_id = network_id or settings.pftl_network_id

        if not self.rpc_url:
            raise ValueError("PFTL_RPC_URL is required but not configured")
        if not self.wallet_secret:
            raise ValueError("PFTL_WALLET_SECRET is required but not configured")
        if not self.memo_destination:
            raise ValueError("PFTL_MEMO_DESTINATION is required but not configured")

        self._client: Optional[JsonRpcClient] = None
        self._wallet: Optional[Wallet] = None

    @property
    def client(self) -> JsonRpcClient:
        if self._client is None:
            self._client = JsonRpcClient(self.rpc_url)
        return self._client

    @property
    def wallet(self) -> Wallet:
        if self._wallet is None:
            self._wallet = wallet_from_secret(self.wallet_secret)
        return self._wallet

    @property
    def publisher_address(self) -> str:
        """Public classic (r...) address of the publisher wallet."""
        return self.wallet.classic_address

    def submit_memo(
        self,
        memo_data: str,
        memo_type: str,
    ) -> tuple[bool, str | None, int | None, str | None]:
        """Submit a Payment transaction with a memo attachment.

        Returns ``(success, tx_hash, validated_ledger_index, error)``. The
        ledger index is the validated ledger the transaction landed in —
        the anchor the frozen judge-draw procedure derives its drawing
        ledger from.
        """
        try:
            memo = Memo(
                memo_type=str_to_hex(memo_type),
                memo_data=str_to_hex(memo_data),
            )

            tx = Payment(
                account=self.wallet.classic_address,
                destination=self.memo_destination,
                amount=PAYMENT_AMOUNT_DROPS,
                network_id=self.network_id,
                memos=[memo],
            )

            rpc_client = self.client
            wallet = self.wallet

            def _execute():
                tx_autofilled = autofill(tx, rpc_client)
                return submit_and_wait(tx_autofilled, rpc_client, wallet)

            with ThreadPoolExecutor(max_workers=1) as pool:
                response = pool.submit(_execute).result()

            if response.is_successful():
                tx_hash = response.result.get("hash")
                ledger_index = response.result.get("ledger_index")
                logger.info(
                    "PFTL memo transaction successful: %s (ledger %s)",
                    tx_hash,
                    ledger_index,
                )
                return True, tx_hash, ledger_index, None

            error = response.result.get("engine_result_message", "Unknown error")
            logger.error("PFTL memo transaction failed: %s", error)
            return False, None, None, error

        except Exception as exc:
            logger.error("PFTL memo transaction error: %s", exc)
            return False, None, None, str(exc)

    def latest_validated_ledger_close_time(self) -> datetime:
        """Return the close time of the latest validated ledger.

        Consensus-agreed network time — the anchor commit/reveal windows
        are derived from. Raises RuntimeError if the RPC call fails.
        """
        response = self.client.request(Ledger(ledger_index="validated"))
        close_time = self._require_ledger(response)["close_time"]
        return ripple_time_to_datetime(int(close_time))

    def latest_validated_ledger_index(self) -> int:
        """The index of the latest validated ledger."""
        response = self.client.request(Ledger(ledger_index="validated"))
        self._require_ledger(response)
        return int(response.result["ledger_index"])

    def ledger_hash(self, ledger_index: int) -> str:
        """The hash of one validated ledger — the judge-draw randomness.

        Raises RuntimeError when the RPC call fails or the ledger is not
        validated yet; callers wait until the index is validated first.
        """
        response = self.client.request(Ledger(ledger_index=ledger_index))
        ledger = self._require_ledger(response)
        if not response.result.get("validated"):
            raise RuntimeError(f"ledger {ledger_index} is not validated yet")
        return str(ledger["ledger_hash"])

    @staticmethod
    def _require_ledger(response) -> dict:
        if not response.is_successful():
            error = response.result.get("error_message") or response.result.get(
                "error", "unknown error"
            )
            raise RuntimeError(f"ledger request failed: {error}")
        return response.result["ledger"]
