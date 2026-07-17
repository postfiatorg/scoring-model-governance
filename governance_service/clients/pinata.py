"""Pinata client for secondary replication of pinned snapshot CIDs.

Asks Pinata to pin an existing CID by fetching it from the IPFS network,
so refresh snapshots survive even if the primary node disappears — and
uploads the bytes directly as the write fallback when the primary node
pin fails, so a primary outage does not abort record publication. Mirrors
the dynamic-unl-scoring replication client.
"""

import json
import logging
import time

import httpx

from governance_service.config import settings

logger = logging.getLogger(__name__)

PINATA_API_BASE = "https://api.pinata.cloud"
PIN_FILE_URL = f"{PINATA_API_BASE}/pinning/pinFileToIPFS"

# Common wrapper folder so Pinata always pins a directory, mirroring the
# primary node's wrap-with-directory; files resolve at <root_cid>/<path>.
_PIN_DIRECTORY_WRAPPER = "bundle"


class PinataClient:
    """Replicates existing IPFS pins to Pinata."""

    def __init__(self, api_key: str | None = None, api_secret: str | None = None):
        self._api_key = api_key or settings.pinata_api_key
        self._api_secret = api_secret or settings.pinata_api_secret
        if not (self._api_key and self._api_secret):
            raise ValueError("Pinata credentials are required but not configured")

    @property
    def _headers(self) -> dict:
        return {
            "pinata_api_key": self._api_key,
            "pinata_secret_api_key": self._api_secret,
        }

    def pin_by_cid(self, cid: str, name: str | None = None) -> bool:
        """Ask Pinata to pin an existing CID from the IPFS network.

        Returns True when the pin request was accepted; replication is
        best-effort and never fails the caller's flow.
        """
        url = f"{PINATA_API_BASE}/pinning/pinByHash"
        payload: dict = {"hashToPin": cid}
        if name:
            payload["pinataMetadata"] = {"name": name}

        for attempt in range(1, settings.http_max_retries + 1):
            try:
                with httpx.Client(timeout=settings.http_timeout_seconds) as client:
                    response = client.post(url, headers=self._headers, json=payload)
                    response.raise_for_status()
                logger.info("Pinata replication requested for CID %s", cid)
                return True
            except httpx.HTTPError as exc:
                if attempt == settings.http_max_retries:
                    logger.error(
                        "Pinata pin_by_cid failed after %d attempts: %s",
                        settings.http_max_retries,
                        exc,
                    )
                    return False
                delay = settings.http_retry_base_delay**attempt
                logger.warning(
                    "Pinata pin_by_cid attempt %d/%d failed: %s — retrying in %ds",
                    attempt,
                    settings.http_max_retries,
                    exc,
                    delay,
                )
                time.sleep(delay)

        return False

    def pin_directory(self, files: dict[str, bytes], name: str | None = None) -> str | None:
        """Upload a directory of files directly to Pinata and return its root CID.

        Unlike pin-by-CID — which copies a CID that already exists on the
        network — this uploads the bytes, so Pinata adds the content
        itself. It is the write fallback for when the primary IPFS node
        pin fails. The returned CID may differ from the primary node's
        for the same bytes; integrity is anchored by the recorded content
        hashes, not the CID, so the two are interchangeable.

        Args:
            files: Mapping of relative paths to file contents.
            name: Optional human-readable name for the pin in Pinata's dashboard.

        Returns:
            Root CID of the uploaded directory, or None if all attempts fail.
        """
        if not files:
            logger.error("Cannot upload empty directory to Pinata")
            return None

        data = {"pinataMetadata": json.dumps({"name": name})} if name else {}

        for attempt in range(1, settings.http_max_retries + 1):
            try:
                multipart_files = [
                    ("file", (f"{_PIN_DIRECTORY_WRAPPER}/{path}", content))
                    for path, content in files.items()
                ]
                with httpx.Client(timeout=settings.http_timeout_seconds) as client:
                    response = client.post(
                        PIN_FILE_URL,
                        files=multipart_files,
                        data=data,
                        headers=self._headers,
                    )
                    response.raise_for_status()
                    root_cid = response.json().get("IpfsHash")

                if not root_cid:
                    logger.error("Pinata upload response missing IpfsHash")
                    return None

                logger.info(
                    "Pinata direct upload pinned %d files — root CID: %s",
                    len(files),
                    root_cid,
                )
                return root_cid

            except (httpx.HTTPError, ValueError) as exc:
                if attempt == settings.http_max_retries:
                    logger.warning(
                        "Pinata pin_directory failed after %d attempts: %s",
                        settings.http_max_retries,
                        exc,
                    )
                    return None
                delay = settings.http_retry_base_delay**attempt
                logger.warning(
                    "Pinata pin_directory attempt %d/%d failed: %s — retrying in %ds",
                    attempt,
                    settings.http_max_retries,
                    exc,
                    delay,
                )
                time.sleep(delay)

        return None
