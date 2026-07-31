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

# sha256 of governance_service/scoring/_vendor_source/commit_reveal.py,
# byte-identical to scoring_service/services/commit_reveal.py upstream.
SUPPORTED_COMMIT_REVEAL_CONTENT_HASHES: frozenset[str] = frozenset(
    {"5ce025098523557a2d02f828e00bfa1e82ddc6323cff5af3f9f8a4bc04c65049"}
)

# sha256 of governance_service/scoring/_vendor_source/response_parser.py,
# byte-identical to scoring_service/services/response_parser.py upstream
# and to the validator sidecar's pinned copy.
SUPPORTED_PARSER_CONTENT_HASHES: frozenset[str] = frozenset(
    {"1eeeed7bee91d2e6e95039018074c5e30ba3e92dffaa16257e6e5dbd07a2f7f7"}
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
