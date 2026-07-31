"""Vendored foundation scoring code and its governance-side adaptations.

Mirrors the validator-scoring-sidecar vendoring pattern: ``_vendor_source/``
holds byte-identical upstream copies pinned by content hash, and adapted
runnable modules live alongside. The pins are asserted against the on-disk
copies by the provenance tests and against the upstream repository by
scripts/check_vendor_freshness.py.
"""

from governance_service.scoring.hashing import (
    CanonicalHashError,
    canonical_json_bytes,
    canonical_json_hash,
    canonical_sha256,
    is_sha256_hex,
)
from governance_service.scoring.parser import (
    DIMENSIONAL_FIELDS,
    ScoringResult,
    ValidatorScore,
    parse_response,
)
from governance_service.scoring.pins import (
    SUPPORTED_COMMIT_REVEAL_CONTENT_HASHES,
    SUPPORTED_PARSER_CONTENT_HASHES,
)

__all__ = [
    "CanonicalHashError",
    "DIMENSIONAL_FIELDS",
    "ScoringResult",
    "ValidatorScore",
    "canonical_json_bytes",
    "canonical_json_hash",
    "canonical_sha256",
    "is_sha256_hex",
    "parse_response",
    "SUPPORTED_COMMIT_REVEAL_CONTENT_HASHES",
    "SUPPORTED_PARSER_CONTENT_HASHES",
]
