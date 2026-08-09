"""Tests for strict final-grade parsing and stable candidate-grade hashes."""

from decimal import Decimal

import pytest

from governance_service.services.grade_commitment import (
    GRADE_SET_SCHEMA,
    GradeCommitmentError,
    GradeParseError,
    canonical_grade_bytes,
    parse_grade,
    stable_grade_hash,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0.0", Decimal("0.0")),
        ("0.1", Decimal("0.1")),
        ("9.9", Decimal("9.9")),
        ("10.0", Decimal("10.0")),
        ("42.5", Decimal("42.5")),
        ("99.9", Decimal("99.9")),
        ("100.0", Decimal("100.0")),
    ],
)
def test_parse_grade_accepts_canonical_values(raw, expected):
    parsed = parse_grade(raw)

    assert parsed == expected
    assert isinstance(parsed, Decimal)


@pytest.mark.parametrize("raw", ["0", "42", "100"])
def test_parse_grade_rejects_integers(raw):
    with pytest.raises(GradeParseError):
        parse_grade(raw)


@pytest.mark.parametrize("raw", ["0.00", "42.50", "99.99", "100.00"])
def test_parse_grade_rejects_extra_decimal_places(raw):
    with pytest.raises(GradeParseError):
        parse_grade(raw)


@pytest.mark.parametrize("raw", ["-0.1", "-1.0", "100.1", "101.0", "999.9"])
def test_parse_grade_rejects_out_of_range_values(raw):
    with pytest.raises(GradeParseError):
        parse_grade(raw)


@pytest.mark.parametrize(
    "raw",
    ["ten", "NaN", "Infinity", "1e1", "1-2", "0.0..1.0", "١.٠"],
)
def test_parse_grade_rejects_text_ranges_and_non_ascii_digits(raw):
    with pytest.raises(GradeParseError):
        parse_grade(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        ".0",
        "0.",
        "00.0",
        "01.0",
        "+1.0",
        " 1.0",
        "1.0 ",
        "1.0\n",
        None,
        True,
        1,
        1.0,
        Decimal("1.0"),
        b"1.0",
    ],
)
def test_parse_grade_rejects_malformed_or_non_string_input(raw):
    with pytest.raises(GradeParseError):
        parse_grade(raw)


def test_canonical_bytes_are_exact_versioned_utf8_json():
    pairs = [("model-z", "91.7"), ("model-a", "83.3")]

    assert canonical_grade_bytes(pairs) == (
        b'{"grades":[["model-a","83.3"],["model-z","91.7"]],'
        b'"schema":"postfiat.governance.candidate-grades.v1"}'
    )
    assert GRADE_SET_SCHEMA.encode("ascii") in canonical_grade_bytes(pairs)


def test_opposite_orders_have_the_same_known_sha256():
    forward = [("model-a", "83.3"), ("model-z", "91.7")]
    reverse = [("model-z", "91.7"), ("model-a", "83.3")]
    expected = "4ee92c01a5772d23fa3d7df35dd83177042f4113cd821aea70c9090fcaec2c0c"

    assert stable_grade_hash(forward) == expected
    assert stable_grade_hash(reverse) == expected


def test_hash_changes_when_a_candidate_or_grade_changes():
    baseline = [("model-a", "83.3"), ("model-z", "91.7")]

    assert stable_grade_hash(baseline) != stable_grade_hash(
        [("model-a", "83.4"), ("model-z", "91.7")]
    )
    assert stable_grade_hash(baseline) != stable_grade_hash(
        [("model-b", "83.3"), ("model-z", "91.7")]
    )


def test_generator_input_is_supported_without_changing_the_hash():
    pairs = [("model-z", "91.7"), ("model-a", "83.3")]

    assert stable_grade_hash(pair for pair in pairs) == stable_grade_hash(pairs)


def test_unicode_candidate_ids_use_nfc_and_utf8():
    composed = [("caf\u00e9", "50.0")]
    decomposed = [("cafe\u0301", "50.0")]

    assert canonical_grade_bytes(composed) == canonical_grade_bytes(decomposed)
    assert b"caf\xc3\xa9" in canonical_grade_bytes(composed)


@pytest.mark.parametrize(
    "pairs",
    [
        None,
        [None],
        ["ab"],
        [("candidate",)],
        [("candidate", "1.0", "extra")],
        [(None, "1.0")],
        [("", "1.0")],
        [("candidate", 1.0)],
        [("candidate", "1")],
    ],
)
def test_canonicalization_rejects_malformed_pairs(pairs):
    with pytest.raises((GradeCommitmentError, GradeParseError)):
        canonical_grade_bytes(pairs)


def test_canonicalization_rejects_non_utf8_candidate_id():
    with pytest.raises(GradeCommitmentError, match="UTF-8"):
        canonical_grade_bytes([("bad\ud800id", "1.0")])


@pytest.mark.parametrize(
    "pairs",
    [
        [("candidate", "1.0"), ("candidate", "1.0")],
        [("candidate", "1.0"), ("candidate", "2.0")],
        [("caf\u00e9", "1.0"), ("cafe\u0301", "2.0")],
    ],
)
def test_canonicalization_rejects_duplicate_candidate_ids(pairs):
    with pytest.raises(GradeCommitmentError, match="repeats candidate_id"):
        canonical_grade_bytes(pairs)


def test_empty_grade_set_has_a_stable_domain_separated_hash():
    canonical = canonical_grade_bytes([])

    assert canonical == (
        b'{"grades":[],"schema":"postfiat.governance.candidate-grades.v1"}'
    )
    assert stable_grade_hash([]) == stable_grade_hash(())
