"""Exam corpus assembly: verified history references plus the constructed catalogue.

One assembly pass selects the newest completed scoring rounds under the
configured history window, fetches each round's frozen input package
(scoring service HTTPS first, public IPFS gateway second — the sidecar's
source order), verifies every file against the package's recorded canonical
hashes, and binds the verified references together with the edge-case
catalogue into a corpus manifest. Historical packages are referenced by
their existing CIDs and hashes, never re-pinned or copied; constructed
cases are new artifacts identified by their canonical content hashes.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from governance_service.clients import scoring_api
from governance_service.config import settings
from governance_service.scoring import (
    canonical_json_hash,
    canonical_sha256,
    is_sha256_hex,
)
from governance_service.services import edge_cases

logger = logging.getLogger(__name__)

MANIFEST_VERSION = 1
BUNDLE_FILE_PATH = "bundle.json"
MODEL_REQUEST_FILE_PATH = "inputs/model_request.json"
INPUT_PACKAGE_KIND = "input"

# The rounds endpoint's own page-size cap (scoring_service/api/scoring.py).
ROUNDS_PAGE_LIMIT = 100

_PACKAGE_PATH_RE = re.compile(r"[A-Za-z0-9._/-]+")

# Environments whose bundles must name the same network; "local" builds
# against whichever scoring service the operator points at.
NETWORK_CHECKED_ENVIRONMENTS = {"devnet", "testnet"}


class CorpusVerificationError(RuntimeError):
    """Raised when a historical package fails hash or shape verification."""


@dataclass
class VerifiedHistoricalItem:
    """One historical round admitted to the corpus, by reference only."""

    round_number: int
    input_package_cid: str
    input_package_hash: str
    input_frozen_at: str
    verified_file_count: int


@dataclass
class CorpusResult:
    """One corpus assembly: the manifest and the constructed case payloads."""

    manifest: dict[str, Any]
    constructed: dict[str, dict[str, Any]]


def select_history_rounds(rounds: list[dict], window: int) -> list[dict]:
    """The newest completed rounds carrying a frozen input package.

    Takes up to ``window`` rounds and fewer when the environment's history
    is shorter — a short history is a smaller corpus, never an error.
    """
    eligible = [
        r
        for r in rounds
        if r.get("status") == "COMPLETE"
        and r.get("input_package_cid")
        and r.get("input_package_hash")
    ]
    eligible.sort(key=lambda r: r["round_number"], reverse=True)
    return eligible[:window]


def _validate_package_path(path: Any) -> str:
    if (
        not isinstance(path, str)
        or not path
        or not _PACKAGE_PATH_RE.fullmatch(path)
        or path.startswith("/")
        or any(part in ("", ".", "..") for part in path.split("/"))
    ):
        raise CorpusVerificationError(f"Invalid package file path: {path!r}")
    return path


def _fetch_package_file(
    client: httpx.Client, round_number: int, cid: str, file_path: str
) -> Any:
    try:
        return scoring_api.fetch_input_file(client, round_number, file_path)
    except scoring_api.ScoringServiceError as exc:
        logger.info(
            "HTTPS fetch failed for round %d %s (%s) — trying the IPFS gateway",
            round_number,
            file_path,
            exc,
        )
        return scoring_api.fetch_gateway_file(client, cid, file_path)


def _parse_bundle(bundle: Any, round_row: dict) -> dict[str, str]:
    if not isinstance(bundle, dict):
        raise CorpusVerificationError("bundle.json is not a JSON object")
    if bundle.get("package_kind") != INPUT_PACKAGE_KIND:
        raise CorpusVerificationError(
            f"Package kind is {bundle.get('package_kind')!r}, expected input"
        )
    if bundle.get("round_number") != round_row["round_number"]:
        raise CorpusVerificationError(
            f"Bundle round {bundle.get('round_number')} does not match "
            f"round {round_row['round_number']}"
        )
    if (
        settings.environment in NETWORK_CHECKED_ENVIRONMENTS
        and bundle.get("network") != settings.environment
    ):
        raise CorpusVerificationError(
            f"Bundle network {bundle.get('network')!r} does not match "
            f"environment {settings.environment!r}"
        )

    file_hashes = bundle.get("file_hashes")
    if not isinstance(file_hashes, dict) or not file_hashes:
        raise CorpusVerificationError("bundle.json has no file_hashes")
    if BUNDLE_FILE_PATH in file_hashes:
        raise CorpusVerificationError("file_hashes must not list bundle.json itself")
    for path, digest in file_hashes.items():
        _validate_package_path(path)
        if not is_sha256_hex(digest):
            raise CorpusVerificationError(f"file_hashes[{path!r}] is not a sha256 hex")
    return file_hashes


def verify_input_package(client: httpx.Client, round_row: dict) -> VerifiedHistoricalItem:
    """Fetch one round's package and verify every file against its recorded hashes.

    The package boundary rule is the foundation's own: ``input_package_hash``
    is the canonical sha256 of ``bundle.json``, and every file listed in the
    bundle's ``file_hashes`` must canonically hash to its recorded value.
    """
    round_number = round_row["round_number"]
    cid = round_row["input_package_cid"]
    expected_hash = round_row["input_package_hash"]

    bundle = _fetch_package_file(client, round_number, cid, BUNDLE_FILE_PATH)
    actual_hash = canonical_json_hash(bundle)
    if actual_hash != expected_hash:
        raise CorpusVerificationError(
            f"Round {round_number} bundle hash mismatch: "
            f"expected {expected_hash}, computed {actual_hash}"
        )

    file_hashes = _parse_bundle(bundle, round_row)
    for file_path, expected_file_hash in file_hashes.items():
        content = _fetch_package_file(client, round_number, cid, file_path)
        actual_file_hash = canonical_json_hash(content)
        if actual_file_hash != expected_file_hash:
            raise CorpusVerificationError(
                f"Round {round_number} file {file_path} hash mismatch: "
                f"expected {expected_file_hash}, computed {actual_file_hash}"
            )

    if "input_frozen_at" not in bundle:
        raise CorpusVerificationError(f"Round {round_number} bundle has no input_frozen_at")
    return VerifiedHistoricalItem(
        round_number=round_number,
        input_package_cid=cid,
        input_package_hash=expected_hash,
        input_frozen_at=str(bundle["input_frozen_at"]),
        verified_file_count=len(file_hashes),
    )


def _fetch_rounds_until_window(client: httpx.Client, window: int) -> list[dict]:
    """Page through the rounds endpoint until the window fills or history ends."""
    rounds: list[dict] = []
    offset = 0
    while True:
        page = scoring_api.list_rounds(client, limit=ROUNDS_PAGE_LIMIT, offset=offset)
        rounds.extend(page)
        if len(select_history_rounds(rounds, window)) >= window:
            break
        if len(page) < ROUNDS_PAGE_LIMIT:
            break
        offset += ROUNDS_PAGE_LIMIT
    return rounds



def fetch_exam_request(
    client: httpx.Client, item: VerifiedHistoricalItem
) -> dict[str, Any]:
    """One historical item's frozen model request, hash-verified on fetch.

    The exam engine consumes requests, not references; this re-fetches the
    package boundary (bundle hash against ``input_package_hash``) and the
    request file (against its ``file_hashes`` entry) so an exam can never
    run on bytes that drifted since corpus assembly.
    """
    bundle = _fetch_package_file(
        client, item.round_number, item.input_package_cid, BUNDLE_FILE_PATH
    )
    if canonical_json_hash(bundle) != item.input_package_hash:
        raise CorpusVerificationError(
            f"Round {item.round_number} bundle hash changed since corpus assembly"
        )
    file_hashes = _parse_bundle(
        bundle,
        {"round_number": item.round_number},
    )
    expected = file_hashes.get(MODEL_REQUEST_FILE_PATH)
    if expected is None:
        raise CorpusVerificationError(
            f"Round {item.round_number} package has no {MODEL_REQUEST_FILE_PATH}"
        )
    request = _fetch_package_file(
        client, item.round_number, item.input_package_cid, MODEL_REQUEST_FILE_PATH
    )
    if canonical_json_hash(request) != expected:
        raise CorpusVerificationError(
            f"Round {item.round_number} model request hash mismatch"
        )
    return request


def build_corpus(client: httpx.Client) -> CorpusResult:
    """One full corpus assembly under the configured policy."""
    window = settings.corpus_history_window
    rounds = _fetch_rounds_until_window(client, window)
    selected = select_history_rounds(rounds, window)
    logger.info(
        "Corpus history: %d of %d requested rounds available",
        len(selected),
        window,
    )

    historical = [verify_input_package(client, round_row) for round_row in selected]
    constructed = edge_cases.build_all()

    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "environment": settings.environment,
        "policy": {
            "history_window_requested": window,
            "history_rounds_found": len(historical),
            "catalogue_version": edge_cases.CATALOGUE_VERSION,
        },
        "historical": [
            {
                "round_number": item.round_number,
                "input_package_cid": item.input_package_cid,
                "input_package_hash": item.input_package_hash,
                "input_frozen_at": item.input_frozen_at,
                "verified_file_count": item.verified_file_count,
            }
            for item in historical
        ],
        "constructed_template": {
            "source_round": edge_cases.TEMPLATE_SOURCE_ROUND,
            "source_cid": edge_cases.TEMPLATE_SOURCE_CID,
        },
        "constructed": [
            {
                "case_id": case_id,
                "catalogue_version": edge_cases.CATALOGUE_VERSION,
                "content_hash": canonical_sha256(request),
            }
            for case_id, request in sorted(constructed.items())
        ],
    }
    return CorpusResult(manifest=manifest, constructed=constructed)
