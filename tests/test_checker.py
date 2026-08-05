"""The mechanical grading checker: rules table, era resolution, primitives."""

import hashlib
import json

import pytest
import yaml

from governance_service.scoring.parser import parse_response
from governance_service.services import edge_cases
from governance_service.services.checker import (
    KIND_BANDING,
    KIND_CEILING,
    KIND_INCONSISTENT,
    KIND_INVENTED,
    KIND_MISSING,
    KIND_ORDERING,
    CheckerError,
    UnknownScoringVersionError,
    check_answer,
    load_rules,
    resolve_version,
)
from governance_service.services.disqualification import synthetic_validator_map

DIMENSIONS = ("consensus", "reliability", "software", "diversity", "identity")
TEST_INSTRUCTIONS = "TEST SCORING INSTRUCTIONS for checker unit tests."


def _template() -> dict:
    return json.loads(edge_cases.TEMPLATE_PATH.read_text(encoding="utf-8"))


def _validator(
    vid,
    score=0.9999,
    missed=0,
    country="Germany",
    family="OVH",
    domain=None,
    version="1.0.4",
):
    window = {"score": score, "total": 10000, "missed": missed}
    return {
        "validator_id": vid,
        "domain": domain,
        "domain_verified": bool(domain),
        "agreement_1h": dict(window),
        "agreement_24h": dict(window),
        "agreement_30d": dict(window),
        "server_version": version,
        "base_fee": 10,
        "asn": {"as_name": family} if family else None,
        "geolocation": {"country": country} if country else None,
        "identity": None,
        "provider_family": family or "unknown",
    }


def _concentration(validators) -> dict:
    families: dict[str, int] = {}
    countries: dict[str, int] = {}
    unresolved = 0
    for entry in validators:
        family = entry.get("provider_family")
        if family and family != "unknown":
            families[family] = families.get(family, 0) + 1
        else:
            unresolved += 1
        country = (entry.get("geolocation") or {}).get("country")
        if country:
            countries[country] = countries.get(country, 0) + 1
    return {
        "provider_families": [
            {"family": name, "validators": count}
            for name, count in sorted(families.items(), key=lambda i: (-i[1], i[0]))
        ],
        "countries": [
            {"country": name, "validators": count}
            for name, count in sorted(countries.items(), key=lambda i: (-i[1], i[0]))
        ],
        "unresolved_endpoints": unresolved,
    }


def _request(validators, concentration=None, instructions=TEST_INSTRUCTIONS) -> dict:
    blocks = ""
    if concentration is not None:
        blocks += "NETWORK CONCENTRATION:\n" + json.dumps(concentration) + "\n\n"
    blocks += "VALIDATOR DATA:\n" + json.dumps(validators)
    return {
        "model": "test/model",
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": f"Score every validator.\n\n{blocks}\n\nJSON only."},
        ],
    }


def _rules_file(tmp_path, instructions=TEST_INSTRUCTIONS, **row):
    digest = hashlib.sha256(instructions.encode("utf-8")).hexdigest()
    data = {
        "vtest": {
            "instructions_sha256": row.pop("instructions_sha256", digest),
            "equality": row.pop("equality", {}),
            "ordering": row.pop("ordering", {}),
            "consensus_ceiling": row.pop("consensus_ceiling", "none"),
            "multiples_of_5": row.pop("multiples_of_5", []),
        }
    }
    assert not row
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _answer(request, overrides=None, drop=(), extra=None) -> str:
    payload: dict = {}
    for entry in edge_cases.validator_entries(request):
        vid = entry["validator_id"]
        if vid in drop:
            continue
        scores = {
            "score": 80,
            "consensus": 90,
            "reliability": 90,
            "software": 90,
            "diversity": 50,
            "identity": 50,
            "reasoning": "Strong agreement is the main positive; diversity is limited.",
        }
        scores.update((overrides or {}).get(vid, {}))
        payload[vid] = scores
    payload.update(extra or {})
    payload["network_report"] = {
        "headline": "Round headline",
        "summary": "Round summary for the selected set.",
        "categories": {
            d: {"tone": "neutral", "body": "Dimension body."} for d in DIMENSIONS
        },
    }
    return json.dumps(payload)


def _check(request, rules_path, overrides=None, drop=(), extra=None):
    validator_map = synthetic_validator_map(request)
    result = parse_response(_answer(request, overrides, drop, extra), validator_map)
    return check_answer(request, result, validator_map, rules_path)


# -- era resolution ----------------------------------------------------------


def test_shipped_rules_cover_the_vendored_real_round():
    rows = load_rules()
    assert {row.version for row in rows.values()} == {"v5", "v8", "v9"}
    assert resolve_version(_template()).version == "v5"


def test_shipped_rows_carry_the_curated_rules_exactly():
    """The rows are protocol artifacts: silently dropping a rule must
    fail here, exactly like a prompt clause dropping its pin."""
    rows = {row.version: row for row in load_rules().values()}
    windows = ("agreement_1h", "agreement_24h", "agreement_30d")

    v5 = rows["v5"]
    assert (v5.equality, v5.ordering, v5.consensus_ceiling, v5.multiples_of_5) == (
        {},
        {},
        "none",
        (),
    )

    v8 = rows["v8"]
    assert v8.equality == {
        "consensus": windows,
        "software": ("server_version", "base_fee"),
        "diversity": ("country_peer_count", "asn_peer_count"),
        "identity": ("domain", "domain_verified", "identity"),
    }
    assert v8.ordering == {
        "consensus": windows,
        "diversity": ("country_peer_count", "asn_peer_count"),
    }
    assert (v8.consensus_ceiling, v8.multiples_of_5) == ("none", ())

    v9 = rows["v9"]
    assert v9.equality == {
        "consensus": windows,
        "reliability": windows,
        "software": ("server_version", "base_fee"),
        "diversity": ("concentration_country_count", "concentration_family_count"),
        "identity": ("domain", "domain_verified", "identity"),
    }
    assert v9.ordering == {
        "consensus": windows,
        "reliability": windows,
        "diversity": ("concentration_country_count", "concentration_family_count"),
    }
    assert v9.consensus_ceiling == "worst_window_floor"
    assert v9.multiples_of_5 == ("reliability", "software", "diversity", "identity")


def test_unknown_instructions_fail_closed():
    request = _template()
    request["messages"][0]["content"] += "\nrevised"
    with pytest.raises(UnknownScoringVersionError):
        resolve_version(request)


# -- the real frozen-round fixture (v5: structural checks only) --------------


def test_clean_answer_on_the_real_round_has_no_defects():
    request = _template()
    validator_map = synthetic_validator_map(request)
    result = parse_response(_answer(request), validator_map)
    assert check_answer(request, result, validator_map) == ()


def test_v5_reports_no_equality_or_ordering_defects():
    """v5 states no equality or ordering rule, so divergent identical-
    evidence scores are the judge's reconcilability question, never a
    mechanical defect — instruction-relative curation in action."""
    request = _template()
    ids = [e["validator_id"] for e in edge_cases.validator_entries(request)]
    overrides = {ids[0]: {"reliability": 95}, ids[1]: {"reliability": 20}}
    validator_map = synthetic_validator_map(request)
    result = parse_response(_answer(request, overrides), validator_map)
    assert check_answer(request, result, validator_map) == ()


def test_missing_and_invented_validators_on_the_real_round():
    request = _template()
    ids = [e["validator_id"] for e in edge_cases.validator_entries(request)]
    validator_map = synthetic_validator_map(request)
    invented = {
        "v999": {
            "score": 80,
            "consensus": 90,
            "reliability": 90,
            "software": 90,
            "diversity": 50,
            "identity": 50,
            "reasoning": "Invented entry.",
        }
    }
    result = parse_response(
        _answer(request, drop=(ids[0],), extra=invented), validator_map
    )
    defects = check_answer(request, result, validator_map)
    kinds = {d.kind: d for d in defects}
    assert set(kinds) == {KIND_MISSING, KIND_INVENTED}
    assert kinds[KIND_MISSING].validator_ids == (ids[0],)
    assert kinds[KIND_INVENTED].validator_ids == ("v999",)


# -- equality ----------------------------------------------------------------


def test_identical_evidence_divergence_is_a_defect(tmp_path):
    rules = _rules_file(
        tmp_path,
        equality={"reliability": ["agreement_1h", "agreement_24h", "agreement_30d"]},
    )
    validators = [_validator("v001"), _validator("v002"), _validator("v003", score=0.9)]
    request = _request(validators)
    defects = _check(request, rules, overrides={"v001": {"reliability": 70}})
    assert len(defects) == 1
    defect = defects[0]
    assert defect.kind == KIND_INCONSISTENT
    assert defect.dimension == "reliability"
    assert defect.validator_ids == ("v001", "v002")
    assert "70" in defect.details and "90" in defect.details


def test_identical_evidence_with_identical_scores_is_clean(tmp_path):
    rules = _rules_file(
        tmp_path,
        equality={"reliability": ["agreement_1h", "agreement_24h", "agreement_30d"]},
    )
    request = _request([_validator("v001"), _validator("v002")])
    assert _check(request, rules) == ()


# -- ordering ----------------------------------------------------------------


def test_better_evidence_scored_worse_is_a_defect(tmp_path):
    rules = _rules_file(
        tmp_path,
        ordering={"consensus": ["agreement_1h", "agreement_24h", "agreement_30d"]},
    )
    validators = [_validator("v001", score=0.9999), _validator("v002", score=0.9)]
    request = _request(validators)
    defects = _check(
        request, rules, overrides={"v001": {"consensus": 80}, "v002": {"consensus": 95}}
    )
    assert [d.kind for d in defects] == [KIND_ORDERING]
    assert defects[0].validator_ids == ("v001", "v002")


def test_tie_at_the_scale_top_is_never_a_defect(tmp_path):
    rules = _rules_file(
        tmp_path,
        ordering={"consensus": ["agreement_1h", "agreement_24h", "agreement_30d"]},
    )
    validators = [_validator("v001", score=1.0), _validator("v002", score=0.9999)]
    request = _request(validators)
    defects = _check(
        request,
        rules,
        overrides={"v001": {"consensus": 100}, "v002": {"consensus": 100}},
    )
    assert defects == ()


# -- ceiling and forced ties -------------------------------------------------


def test_consensus_above_the_worst_window_ceiling_is_a_defect(tmp_path):
    rules = _rules_file(tmp_path, consensus_ceiling="worst_window_floor")
    request = _request([_validator("v001", score=0.9991)])
    defects = _check(request, rules, overrides={"v001": {"consensus": 100}})
    assert [d.kind for d in defects] == [KIND_CEILING]
    assert "99" in defects[0].details


def test_ceiling_uses_exact_decimal_arithmetic(tmp_path):
    """0.29 * 100 is 28.999... in binary floats; the ceiling must still
    be 29, never a wrongly tightened 28."""
    rules = _rules_file(tmp_path, consensus_ceiling="worst_window_floor")
    request = _request([_validator("v001", score=0.29)])
    assert _check(request, rules, overrides={"v001": {"consensus": 29}}) == ()
    defects = _check(request, rules, overrides={"v001": {"consensus": 30}})
    assert [d.kind for d in defects] == [KIND_CEILING]


def test_shared_ceiling_tie_is_the_cap_working(tmp_path):
    """Both worst windows floor to 99: the forced tie at the shared
    ceiling is legitimate, never an ordering violation."""
    rules = _rules_file(
        tmp_path,
        ordering={"consensus": ["agreement_1h", "agreement_24h", "agreement_30d"]},
        consensus_ceiling="worst_window_floor",
    )
    validators = [_validator("v001", score=0.9999), _validator("v002", score=0.9991)]
    request = _request(validators)
    defects = _check(
        request,
        rules,
        overrides={"v001": {"consensus": 99}, "v002": {"consensus": 99}},
    )
    assert defects == ()


def test_tie_below_the_ceiling_is_still_a_violation(tmp_path):
    rules = _rules_file(
        tmp_path,
        ordering={"consensus": ["agreement_1h", "agreement_24h", "agreement_30d"]},
        consensus_ceiling="worst_window_floor",
    )
    validators = [_validator("v001", score=0.9999), _validator("v002", score=0.9991)]
    request = _request(validators)
    defects = _check(
        request,
        rules,
        overrides={"v001": {"consensus": 95}, "v002": {"consensus": 95}},
    )
    assert [d.kind for d in defects] == [KIND_ORDERING]


# -- numeric rules -----------------------------------------------------------


def test_banding_violations_name_every_offender(tmp_path):
    rules = _rules_file(tmp_path, multiples_of_5=["reliability", "identity"])
    request = _request([_validator("v001"), _validator("v002")])
    defects = _check(
        request,
        rules,
        overrides={"v001": {"reliability": 87}, "v002": {"identity": 52}},
    )
    assert [d.kind for d in defects] == [KIND_BANDING, KIND_BANDING]
    by_dimension = {d.dimension: d for d in defects}
    assert by_dimension["reliability"].validator_ids == ("v001",)
    assert "87" in by_dimension["reliability"].details
    assert by_dimension["identity"].validator_ids == ("v002",)


# -- concentration-driven diversity ------------------------------------------


def test_less_concentrated_validator_scored_worse_is_a_defect(tmp_path):
    rules = _rules_file(
        tmp_path,
        ordering={
            "diversity": [
                "concentration_country_count",
                "concentration_family_count",
            ]
        },
    )
    validators = [
        _validator("v001", country="Iceland", family="RareHost"),
        _validator("v002", country="Germany", family="OVH"),
        _validator("v003", country="Germany", family="OVH"),
    ]
    request = _request(validators, concentration=_concentration(validators))
    defects = _check(
        request,
        rules,
        overrides={"v001": {"diversity": 40}, "v002": {"diversity": 60}, "v003": {"diversity": 35}},
    )
    assert [d.kind for d in defects] == [KIND_ORDERING]
    assert defects[0].validator_ids == ("v001", "v002")


def test_equal_peer_counts_with_divergent_diversity_is_a_defect(tmp_path):
    rules = _rules_file(
        tmp_path,
        equality={"diversity": ["country_peer_count", "asn_peer_count"]},
    )
    validators = [
        _validator("v001", country="Germany", family="OVH"),
        _validator("v002", country="Germany", family="OVH"),
    ]
    request = _request(validators)
    defects = _check(request, rules, overrides={"v001": {"diversity": 70}})
    assert [d.kind for d in defects] == [KIND_INCONSISTENT]
    assert defects[0].dimension == "diversity"
    assert defects[0].validator_ids == ("v001", "v002")


def test_concentration_rules_without_the_block_fail_closed(tmp_path):
    rules = _rules_file(
        tmp_path,
        equality={"diversity": ["concentration_country_count"]},
    )
    request = _request([_validator("v001"), _validator("v002")])
    with pytest.raises(CheckerError):
        _check(request, rules)


# -- fail-closed guards ------------------------------------------------------


def test_feature_absent_from_every_entry_fails_closed(tmp_path):
    """A projection field no entry carries must be a hard error — it
    would otherwise make every validator look identical."""
    rules = _rules_file(tmp_path, equality={"software": ["server_version", "base_fee"]})
    validators = [
        {k: v for k, v in _validator(vid).items() if k not in ("server_version", "base_fee")}
        for vid in ("v001", "v002")
    ]
    request = _request(validators)
    with pytest.raises(CheckerError):
        _check(request, rules)


def test_null_evidence_is_excluded_not_compared(tmp_path):
    rules = _rules_file(
        tmp_path,
        equality={"diversity": ["country_peer_count", "asn_peer_count"]},
        ordering={"diversity": ["country_peer_count", "asn_peer_count"]},
    )
    validators = [
        _validator("v001", country="Germany", family="OVH"),
        _validator("v002", country="Germany", family="OVH"),
        _validator("v003", country=None, family=None),
    ]
    request = _request(validators)
    defects = _check(request, rules, overrides={"v003": {"diversity": 95}})
    assert defects == ()


@pytest.mark.parametrize(
    "row",
    [
        {"equality": {"charisma": ["domain"]}},
        {"equality": {"identity": ["shoe_size"]}},
        {"ordering": {"identity": ["shoe_size"]}},
        {"consensus_ceiling": "vibes"},
        {"multiples_of_5": ["charisma"]},
        {"instructions_sha256": "tooshort"},
    ],
)
def test_malformed_rules_rows_are_rejected(tmp_path, row):
    path = _rules_file(tmp_path, **row)
    request = _request([_validator("v001")])
    with pytest.raises(CheckerError):
        _check(request, path)


def test_duplicate_instruction_hashes_are_rejected(tmp_path):
    digest = hashlib.sha256(TEST_INSTRUCTIONS.encode("utf-8")).hexdigest()
    path = tmp_path / "rules.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "va": {"instructions_sha256": digest},
                "vb": {"instructions_sha256": digest},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CheckerError):
        _check(_request([_validator("v001")]), path)


def test_ordering_row_without_an_orderable_feature_is_rejected(tmp_path):
    path = _rules_file(tmp_path, ordering={"identity": ["domain", "domain_verified"]})
    with pytest.raises(CheckerError):
        _check(_request([_validator("v001")]), path)


# -- determinism -------------------------------------------------------------


def test_defects_are_pure_and_deterministically_ordered(tmp_path):
    rules = _rules_file(
        tmp_path,
        equality={"reliability": ["agreement_1h", "agreement_24h", "agreement_30d"]},
        ordering={"consensus": ["agreement_1h", "agreement_24h", "agreement_30d"]},
        multiples_of_5=["identity"],
        consensus_ceiling="worst_window_floor",
    )
    validators = [
        _validator("v001", score=0.9999),
        _validator("v002", score=0.9999),
        _validator("v003", score=0.9),
    ]
    request = _request(validators)
    overrides = {
        "v001": {"reliability": 70, "consensus": 80, "identity": 52},
        "v003": {"consensus": 95},
    }
    first = _check(request, rules, overrides=overrides)
    second = _check(request, rules, overrides=overrides)
    assert first == second
    assert [d.kind for d in first] == sorted(d.kind for d in first)
    assert len(first) >= 3
