"""Canonical hash rules, adapted from the vendored commit_reveal.py.

These functions must stay byte-for-byte equivalent in behavior to
``canonical_json_bytes`` / ``canonical_sha256`` / ``is_sha256_hex`` in
``_vendor_source/commit_reveal.py``: every hash this service checks —
``input_package_hash`` over ``bundle.json`` and the per-file entries in
``file_hashes`` — was produced by the foundation with exactly these rules.
The adaptation exists because the vendored module imports xrpl, which this
service does not depend on; the provenance tests hold both files together.
"""

import hashlib
import json
import re
from typing import Any, Mapping

_LOWER_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class CanonicalHashError(ValueError):
    """Raised when a payload cannot be canonically hashed."""


def canonical_json_bytes(data: Mapping[str, Any]) -> bytes:
    """Return protocol canonical JSON bytes for one JSON object."""
    if not isinstance(data, Mapping):
        raise CanonicalHashError("canonical payload must be a JSON object")
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return canonical.encode("utf-8")


def canonical_sha256(data: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(data)).hexdigest()


def canonical_json_hash(data: Any) -> str:
    """The package file-hash rule: any JSON value, not just objects.

    Matches the foundation's ``_content_hash`` (ipfs_publisher.py) and the
    sidecar's ``canonical_json_hash`` (input_package.py): raw evidence files
    such as ``raw/crawl_probes.json`` are JSON arrays, so this rule has no
    object requirement, unlike the protocol-payload rule above.
    """
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_sha256_hex(value: Any) -> bool:
    return isinstance(value, str) and bool(_LOWER_SHA256_RE.fullmatch(value))
