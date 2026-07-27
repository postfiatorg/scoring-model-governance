"""Client for the environment's own scoring service and the IPFS gateway.

Enumerates scoring rounds and fetches frozen input package files, first
through the scoring service's HTTPS artifact path and then through the
public IPFS gateway — the same source order the validator sidecar uses.
"""

import json
import time
from typing import Any

import httpx

from governance_service.config import settings


class ScoringServiceError(RuntimeError):
    """Raised when the scoring service cannot be read."""


class ScoringServiceNotFoundError(ScoringServiceError):
    """Raised when the scoring service reports a missing resource."""


class IPFSGatewayError(RuntimeError):
    """Raised when the IPFS gateway cannot serve a package file."""


def _get_json(
    client: httpx.Client,
    url: str,
    error_cls: type[RuntimeError],
    not_found_cls: type[RuntimeError],
) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, settings.http_max_retries + 1):
        try:
            response = client.get(url)
            if response.status_code == 404:
                raise not_found_cls(f"Resource not found: {url}")
            response.raise_for_status()
            return json.loads(response.content)
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < settings.http_max_retries:
                time.sleep(settings.http_retry_base_delay**attempt)
    raise error_cls(f"Request failed: {url} — {last_error}")


def list_rounds(client: httpx.Client, limit: int, offset: int = 0) -> list[dict]:
    """Newest-first scoring rounds from the public rounds endpoint."""
    url = f"{settings.scoring_api_base_url}/api/scoring/rounds?limit={limit}&offset={offset}"
    payload = _get_json(client, url, ScoringServiceError, ScoringServiceNotFoundError)
    if not isinstance(payload, dict) or not isinstance(payload.get("rounds"), list):
        raise ScoringServiceError(f"Unexpected rounds response shape from {url}")
    return payload["rounds"]


def fetch_input_file(client: httpx.Client, round_number: int, file_path: str) -> Any:
    """One frozen input package file via the scoring service HTTPS path."""
    url = (
        f"{settings.scoring_api_base_url}/api/scoring/rounds/"
        f"{round_number}/input/{file_path}"
    )
    return _get_json(client, url, ScoringServiceError, ScoringServiceNotFoundError)


def fetch_gateway_file(client: httpx.Client, cid: str, file_path: str) -> Any:
    """One input package file via the public IPFS gateway fallback."""
    url = f"{settings.ipfs_gateway_url}/{cid}/{file_path}"
    return _get_json(client, url, IPFSGatewayError, IPFSGatewayError)
