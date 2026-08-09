"""Strict parsing and canonical hashing for final candidate grades.

The drawn judge never emits a grade.  Grades are produced by
``grade_formula.final_grade`` and cross the governance-round boundary as
canonical one-decimal strings.  This module validates that boundary and builds
a versioned, order-independent SHA-256 commitment over candidate grades.

Only Python's standard library is used.  The canonical preimage is UTF-8 JSON
with no insignificant whitespace; see ``canonical_grade_bytes`` for the exact
contract.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

GRADE_SET_SCHEMA = "postfiat.governance.candidate-grades.v1"

# ASCII digits are intentional: Unicode digit classes would admit visually
# confusable representations that different implementations may serialize
# differently.  Leading zeroes, signs, exponents, and surrounding whitespace
# are not part of the canonical grade syntax.
_GRADE_PATTERN = re.compile(r"(?:0|[1-9][0-9]?)\.[0-9]|100\.0")


class GradeParseError(ValueError):
    """Raised when a value is not a canonical one-decimal grade string."""


class GradeCommitmentError(ValueError):
    """Raised when candidate-grade pairs cannot form an unambiguous set."""


def parse_grade(value: Any) -> Decimal:
    """Parse one canonical grade in the inclusive range 0.0 through 100.0.

    The input must be a string matching the canonical syntax exactly.  In
    particular, integers, floats, signs, exponents, leading zeroes, extra
    decimal places, whitespace, and non-ASCII digits are rejected.  Returning
    :class:`~decimal.Decimal` avoids binary floating-point round-off.
    """

    if not isinstance(value, str) or _GRADE_PATTERN.fullmatch(value) is None:
        raise GradeParseError(
            "grade must be a canonical string from 0.0 through 100.0 "
            "with exactly one decimal place"
        )
    return Decimal(value)


def _canonical_candidate_id(value: Any, index: int) -> str:
    if not isinstance(value, str) or not value:
        raise GradeCommitmentError(
            f"pair {index} candidate_id must be a non-empty string"
        )

    # NFC gives canonically equivalent Unicode identifiers one byte encoding.
    candidate_id = unicodedata.normalize("NFC", value)
    try:
        candidate_id.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise GradeCommitmentError(
            f"pair {index} candidate_id is not valid UTF-8 text"
        ) from exc
    return candidate_id


def _canonical_rows(pairs: Iterable[tuple[str, str]]) -> list[list[str]]:
    try:
        iterator = iter(pairs)
    except TypeError as exc:
        raise GradeCommitmentError("pairs must be an iterable of two-item pairs") from exc

    rows: list[list[str]] = []
    seen_candidate_ids: set[str] = set()
    for index, pair in enumerate(iterator):
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise GradeCommitmentError(
                f"pair {index} must be a two-item (candidate_id, grade) pair"
            )

        candidate_id = _canonical_candidate_id(pair[0], index)
        if candidate_id in seen_candidate_ids:
            raise GradeCommitmentError(
                f"pair {index} repeats candidate_id {candidate_id!r}"
            )
        seen_candidate_ids.add(candidate_id)

        # Formatting the parsed Decimal makes the normalization explicit and
        # keeps the serialized grade a JSON string, never a lossy JSON number.
        grade = format(parse_grade(pair[1]), ".1f")
        rows.append([candidate_id, grade])

    rows.sort(key=lambda row: row[0])
    return rows


def canonical_grade_bytes(pairs: Iterable[tuple[str, str]]) -> bytes:
    """Return the exact versioned UTF-8 preimage for candidate grades.

    Contract, in order:

    1. validate every grade with :func:`parse_grade`;
    2. normalize candidate ids to Unicode NFC and reject repeated ids;
    3. sort rows by candidate id in Unicode code-point order;
    4. encode ``{"grades":[[id, grade], ...], "schema": ...}`` as JSON with
       keys sorted, no optional whitespace, non-ASCII text preserved, and no
       trailing newline; and
    5. encode that JSON as UTF-8.

    The schema tag domain-separates this digest and provides an explicit
    version boundary for any future serialization change.
    """

    payload = {"schema": GRADE_SET_SCHEMA, "grades": _canonical_rows(pairs)}
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return canonical.encode("utf-8")


def stable_grade_hash(pairs: Iterable[tuple[str, str]]) -> str:
    """Return the lowercase SHA-256 hex digest of canonical candidate grades."""

    return hashlib.sha256(canonical_grade_bytes(pairs)).hexdigest()
