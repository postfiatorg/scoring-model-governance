"""Provenance guards for the vendored dynamic-unl-scoring code."""

import hashlib
from pathlib import Path

import pytest

from governance_service.scoring import (
    SUPPORTED_COMMIT_REVEAL_CONTENT_HASHES,
    CanonicalHashError,
    canonical_json_bytes,
    canonical_sha256,
    is_sha256_hex,
)

VENDOR_DIR = (
    Path(__file__).resolve().parent.parent
    / "governance_service"
    / "scoring"
    / "_vendor_source"
)
HASHING_PATH = VENDOR_DIR.parent / "hashing.py"

CANONICAL_DUMPS_LINE = 'json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)'


def test_vendored_commit_reveal_matches_pin():
    digest = hashlib.sha256((VENDOR_DIR / "commit_reveal.py").read_bytes()).hexdigest()
    assert digest in SUPPORTED_COMMIT_REVEAL_CONTENT_HASHES


def test_adapted_hashing_uses_the_vendored_canonical_rule():
    vendored = (VENDOR_DIR / "commit_reveal.py").read_text(encoding="utf-8")
    adapted = HASHING_PATH.read_text(encoding="utf-8")
    assert CANONICAL_DUMPS_LINE in vendored
    assert CANONICAL_DUMPS_LINE in adapted


def test_canonical_json_bytes_reference_vector():
    payload = {"b": [1, 2], "a": 1, "c": "x", "d": None}
    assert canonical_json_bytes(payload) == b'{"a":1,"b":[1,2],"c":"x","d":null}'
    assert (
        canonical_sha256(payload)
        == "786f5cde16f5c2785718fe3544bcc0fc8adc2ba51afd54d4690eb8fe51f7c78f"
    )


def test_canonical_json_bytes_rejects_non_mapping():
    with pytest.raises(CanonicalHashError):
        canonical_json_bytes([1, 2, 3])


def test_is_sha256_hex():
    assert is_sha256_hex("a" * 64)
    assert not is_sha256_hex("A" * 64)
    assert not is_sha256_hex("a" * 63)
    assert not is_sha256_hex(None)
