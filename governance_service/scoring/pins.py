"""Content-hash pins for the vendored dynamic-unl-scoring copies.

Stdlib-only on purpose: the vendor-freshness workflow imports this module
with a bare Python interpreter, so it must never pull the package's
runtime dependencies.
"""

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
