"""The mechanical grading checker: closed-form defects in code.

Under the G.4 checker/judge/formula split this module owns every defect
with a computable right answer, applied to one (corpus item, parsed
candidate answer) pair: identical-evidence sub-score divergence,
ordering violations where strictly better evidence scored strictly
worse, the item's scoring-prompt version's numeric rules, and the
structural checks. The judge is never asked about these kinds, so the
checker's and the judge's defect lists cannot overlap.

Grading is instruction-relative: the checker enforces only rules the
corpus item's scoring-prompt version actually states. Each version's
crisp rules live as one hand-curated row in
``governance_service/scoring_rules.yaml`` — keyed by the SHA-256 of the
exact scoring-instructions text embedded in the frozen request, so era
resolution can neither drift nor be spoofed by metadata. The rules are
fail-closed twice over: an item whose instructions match no row is a
hard error, and a row feature that no validator entry carries is a
hard error (a wrong field name must never make every validator look
identical). Prose rules — guide bands, "where appropriate" penalties —
are deliberately absent here: reconcilability judgment is the judge's,
per the division of labor in ``docs/MechanicalGradingChecker.md``.
"""

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from governance_service.scoring.parser import DIMENSIONAL_FIELDS, ScoringResult
from governance_service.services.edge_cases import validator_entries

RULES_PATH = Path(__file__).resolve().parents[1] / "scoring_rules.yaml"

DIMENSIONS = tuple(DIMENSIONAL_FIELDS)

KIND_INCONSISTENT = "inconsistent_scores"
KIND_ORDERING = "ordering_violation"
KIND_CEILING = "ceiling_exceeded"
KIND_BANDING = "banding_violation"
KIND_MISSING = "missing_validator"
KIND_INVENTED = "invented_validator"

# Window features compare (score higher-better, missed lower-better) for
# ordering and the full window object for equality.
WINDOW_FEATURES = ("agreement_1h", "agreement_24h", "agreement_30d")
# Plain evidence fields, equality-only.
FIELD_FEATURES = (
    "domain",
    "domain_verified",
    "identity",
    "server_version",
    "base_fee",
)
# Derived features: the counts compare lower-better for ordering;
# endpoint_resolved is equality-only.
DERIVED_FEATURES = (
    "country_peer_count",
    "asn_peer_count",
    "concentration_country_count",
    "concentration_family_count",
    "endpoint_resolved",
)
ORDERABLE_FEATURES = WINDOW_FEATURES + (
    "country_peer_count",
    "asn_peer_count",
    "concentration_country_count",
    "concentration_family_count",
)
KNOWN_FEATURES = WINDOW_FEATURES + FIELD_FEATURES + DERIVED_FEATURES

CEILING_NONE = "none"
CEILING_WORST_WINDOW_FLOOR = "worst_window_floor"

CONCENTRATION_MARKER = "NETWORK CONCENTRATION:"
UNRESOLVED_FAMILY = "unknown"

# The vendored production parser reports invented entries with this
# stable, pinned prefix; the checker turns it into a structural defect.
PARSER_UNEXPECTED_PREFIX = "Unexpected entries: "


class CheckerError(ValueError):
    """Raised when the checker cannot run at all — a malformed rules
    table, request, or answer pairing. Never a candidate defect."""


class UnknownScoringVersionError(CheckerError):
    """Raised when a corpus item's scoring instructions match no rules
    row. Fail-closed: the row must be curated before the item is
    checkable."""


@dataclass(frozen=True)
class CheckerDefect:
    """One mechanical defect, in the judge defect objects' shape: the
    kind, the validators it concerns, and a self-verifiable detail
    string carrying the numbers behind it."""

    kind: str
    dimension: str | None
    validator_ids: tuple[str, ...]
    details: str


@dataclass(frozen=True)
class VersionRules:
    """One curated row: the crisp rules one scoring-prompt version states."""

    version: str
    instructions_sha256: str
    equality: dict[str, tuple[str, ...]]
    ordering: dict[str, tuple[str, ...]]
    consensus_ceiling: str
    multiples_of_5: tuple[str, ...]


def instructions_sha256(request: dict[str, Any]) -> str:
    """The era key: SHA-256 of the exact embedded instructions text."""
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages:
        raise CheckerError("Request carries no messages")
    system = messages[0]
    if not isinstance(system, dict) or system.get("role") != "system":
        raise CheckerError("Request message 0 must be the system message")
    content = system.get("content")
    if not isinstance(content, str) or not content:
        raise CheckerError("Request system message must carry non-empty content")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@lru_cache(maxsize=None)
def load_rules(path: Path = RULES_PATH) -> dict[str, VersionRules]:
    """The rules table, validated row by row and keyed by instructions hash."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw:
        raise CheckerError(f"{path.name} must be a non-empty mapping")
    rows: dict[str, VersionRules] = {}
    for version, row in raw.items():
        if not isinstance(row, dict):
            raise CheckerError(f"{path.name}: {version} must be a mapping")
        digest = row.get("instructions_sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise CheckerError(
                f"{path.name}: {version}.instructions_sha256 must be a "
                f"64-character SHA-256 hex digest"
            )
        for section in ("equality", "ordering"):
            for dimension, features in (row.get(section) or {}).items():
                if dimension not in DIMENSIONS:
                    raise CheckerError(
                        f"{path.name}: {version}.{section} names unknown "
                        f"dimension {dimension!r}"
                    )
                for feature in features:
                    if feature not in KNOWN_FEATURES:
                        raise CheckerError(
                            f"{path.name}: {version}.{section}.{dimension} "
                            f"names unknown feature {feature!r}"
                        )
                if section == "ordering" and not any(
                    feature in ORDERABLE_FEATURES for feature in features
                ):
                    raise CheckerError(
                        f"{path.name}: {version}.ordering.{dimension} has no "
                        f"orderable feature and could never establish dominance"
                    )
        ceiling = row.get("consensus_ceiling", CEILING_NONE)
        if ceiling not in (CEILING_NONE, CEILING_WORST_WINDOW_FLOOR):
            raise CheckerError(
                f"{path.name}: {version}.consensus_ceiling must be "
                f"{CEILING_NONE!r} or {CEILING_WORST_WINDOW_FLOOR!r}"
            )
        banded = tuple(row.get("multiples_of_5") or ())
        for dimension in banded:
            if dimension not in DIMENSIONS:
                raise CheckerError(
                    f"{path.name}: {version}.multiples_of_5 names unknown "
                    f"dimension {dimension!r}"
                )
        if digest in rows:
            raise CheckerError(f"{path.name}: duplicate instructions hash {digest}")
        rows[digest] = VersionRules(
            version=str(version),
            instructions_sha256=digest,
            equality={
                dimension: tuple(features)
                for dimension, features in (row.get("equality") or {}).items()
            },
            ordering={
                dimension: tuple(features)
                for dimension, features in (row.get("ordering") or {}).items()
            },
            consensus_ceiling=ceiling,
            multiples_of_5=banded,
        )
    return rows


def resolve_version(request: dict[str, Any], path: Path = RULES_PATH) -> VersionRules:
    """The rules row for one corpus item, fail-closed on unknown eras."""
    digest = instructions_sha256(request)
    rules = load_rules(path).get(digest)
    if rules is None:
        raise UnknownScoringVersionError(
            f"Scoring instructions hash {digest} matches no rules row; "
            f"curate the version's row before checking this item"
        )
    return rules


def _concentration_counts(request: dict[str, Any]) -> dict[str, dict[str, int]]:
    """The NETWORK CONCENTRATION block's counts, when the era carries one."""
    content = next(
        (m["content"] for m in request.get("messages", []) if m.get("role") == "user"),
        "",
    )
    marker_at = content.find(CONCENTRATION_MARKER)
    if marker_at < 0:
        raise CheckerError(
            "Rules require concentration counts but the request carries no "
            f"{CONCENTRATION_MARKER!r} block"
        )
    try:
        block, _ = json.JSONDecoder().raw_decode(
            content[marker_at + len(CONCENTRATION_MARKER) :].lstrip()
        )
    except ValueError as exc:
        raise CheckerError(f"Concentration block is not valid JSON: {exc}") from exc
    if not isinstance(block, dict):
        raise CheckerError("Concentration block must be a JSON object")
    return block


def _window_value(entry: dict[str, Any], feature: str) -> Any:
    window = entry.get(feature)
    if not isinstance(window, dict) or "score" not in window or "missed" not in window:
        return None
    return window


def _entry_country(entry: dict[str, Any]) -> str | None:
    """The validator's country: nested under geolocation in production
    entries, flat in older material."""
    geolocation = entry.get("geolocation")
    if isinstance(geolocation, dict):
        return geolocation.get("country")
    country = entry.get("country")
    return country if isinstance(country, str) else None


def _entry_asn_key(entry: dict[str, Any]) -> str | None:
    """The provider identity behind the asn field: the AS name in
    production's nested object, the raw value in older material."""
    asn = entry.get("asn")
    if isinstance(asn, dict):
        return asn.get("as_name")
    return asn if isinstance(asn, str) else None


def _count_lookup(block: list[Any], key_name: str) -> dict[str, int]:
    """A name -> validators lookup from the concentration block's
    list-of-objects shape."""
    counts: dict[str, int] = {}
    for item in block:
        if not isinstance(item, dict) or key_name not in item:
            raise CheckerError(
                f"Concentration block entries must carry {key_name!r} and "
                f"'validators'"
            )
        counts[item[key_name]] = item["validators"]
    return counts


def _features(
    request: dict[str, Any],
    entries: list[dict[str, Any]],
    needed: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    """Per-validator feature values for one dimension's rule; a feature a
    validator does not carry resolves to None and excludes it from that
    dimension's comparisons."""
    derived_needed = [f for f in needed if f in DERIVED_FEATURES]
    country_counts: dict[str, int] = {}
    asn_counts: dict[str, int] = {}
    concentration_countries: dict[str, int] = {}
    concentration_families: dict[str, int] = {}
    if "country_peer_count" in derived_needed or "asn_peer_count" in derived_needed:
        for entry in entries:
            country = _entry_country(entry)
            if country:
                country_counts[country] = country_counts.get(country, 0) + 1
            asn_key = _entry_asn_key(entry)
            if asn_key:
                asn_counts[asn_key] = asn_counts.get(asn_key, 0) + 1
    if (
        "concentration_country_count" in derived_needed
        or "concentration_family_count" in derived_needed
    ):
        block = _concentration_counts(request)
        concentration_countries = _count_lookup(
            block.get("countries", []), "country"
        )
        concentration_families = _count_lookup(
            block.get("provider_families", []), "family"
        )

    values: dict[str, dict[str, Any]] = {}
    for entry in entries:
        row: dict[str, Any] = {}
        for feature in needed:
            if feature in WINDOW_FEATURES:
                row[feature] = _window_value(entry, feature)
            elif feature in FIELD_FEATURES:
                row[feature] = entry.get(feature)
            elif feature == "country_peer_count":
                country = _entry_country(entry)
                row[feature] = country_counts.get(country) if country else None
            elif feature == "asn_peer_count":
                asn_key = _entry_asn_key(entry)
                row[feature] = asn_counts.get(asn_key) if asn_key else None
            elif feature == "concentration_country_count":
                country = _entry_country(entry)
                row[feature] = (
                    concentration_countries.get(country) if country else None
                )
            elif feature == "concentration_family_count":
                family = entry.get("provider_family")
                row[feature] = (
                    concentration_families.get(family)
                    if family and family != UNRESOLVED_FAMILY
                    else None
                )
            elif feature == "endpoint_resolved":
                family = entry.get("provider_family")
                row[feature] = bool(family and family != UNRESOLVED_FAMILY)
        values[entry["validator_id"]] = row
    return values


def _guard_features_present(
    dimension: str, needed: tuple[str, ...], values: dict[str, dict[str, Any]]
) -> None:
    """A feature absent from every entry means a mis-curated row — without
    this guard it would silently make all validators identical."""
    for feature in needed:
        if all(row.get(feature) is None for row in values.values()):
            raise CheckerError(
                f"Rules feature {feature!r} for dimension {dimension!r} is "
                f"absent from every validator entry; the rules row does not "
                f"match this request's evidence format"
            )


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _dominates(a: dict[str, Any], b: dict[str, Any], needed: tuple[str, ...]) -> bool:
    """Whether evidence a is strictly better than evidence b: no compared
    feature worse, at least one strictly better."""
    strictly_better = False
    for feature in needed:
        va, vb = a[feature], b[feature]
        if feature in WINDOW_FEATURES:
            if va["score"] < vb["score"] or va["missed"] > vb["missed"]:
                return False
            if va["score"] > vb["score"] or va["missed"] < vb["missed"]:
                strictly_better = True
        elif feature in (
            "country_peer_count",
            "asn_peer_count",
            "concentration_country_count",
            "concentration_family_count",
        ):
            if va > vb:
                return False
            if va < vb:
                strictly_better = True
        else:
            if va != vb:
                return False
    return strictly_better


def _ceiling(entry_features: dict[str, Any]) -> int | None:
    windows = [entry_features.get(f) for f in WINDOW_FEATURES]
    if any(w is None for w in windows):
        return None
    # Exact decimal arithmetic: binary floats make 0.29 * 100 land just
    # below 29, which would floor to a wrongly tightened ceiling.
    return int(min(Decimal(str(w["score"])) for w in windows) * 100)


def check_answer(
    request: dict[str, Any],
    result: ScoringResult,
    validator_map: dict[str, dict[str, str]],
    rules_path: Path = RULES_PATH,
) -> tuple[CheckerDefect, ...]:
    """Every mechanical defect in one (corpus item, parsed answer) pair.

    Pure and deterministic: the same request, parsed result, and
    validator map always produce the identical defect tuple, ordered by
    (kind, dimension, validators). The answer must come from the
    production parser (disqualification guarantees it parses); the map
    is the item's frozen validator identity map.
    """
    rules = resolve_version(request, rules_path)
    entries = validator_entries(request)
    expected_ids = [e["validator_id"] for e in entries]
    if len(set(expected_ids)) != len(expected_ids):
        raise CheckerError("Request carries duplicate validator ids")

    key_to_id = {
        identity["master_key"]: validator_id
        for validator_id, identity in validator_map.items()
    }
    scores: dict[str, Any] = {}
    for validator_score in result.validator_scores:
        validator_id = key_to_id.get(validator_score.master_key)
        if validator_id is None:
            raise CheckerError(
                f"Parsed score carries master key {validator_score.master_key!r} "
                f"absent from the validator map"
            )
        scores[validator_id] = validator_score

    defects: list[CheckerDefect] = []

    missing = sorted(set(expected_ids) - set(scores))
    if missing:
        defects.append(
            CheckerDefect(
                kind=KIND_MISSING,
                dimension=None,
                validator_ids=tuple(missing),
                details=(
                    "Input validators absent from the parsed answer: "
                    + ", ".join(missing)
                ),
            )
        )
    for error in result.errors:
        if error.startswith(PARSER_UNEXPECTED_PREFIX):
            invented = tuple(error[len(PARSER_UNEXPECTED_PREFIX) :].split(", "))
            defects.append(
                CheckerDefect(
                    kind=KIND_INVENTED,
                    dimension=None,
                    validator_ids=invented,
                    details=(
                        "Answer entries matching no input validator: "
                        + ", ".join(invented)
                    ),
                )
            )

    for dimension in rules.multiples_of_5:
        offenders = sorted(
            validator_id
            for validator_id, score in scores.items()
            if getattr(score, dimension) % 5
        )
        if offenders:
            values = ", ".join(
                f"{v}={getattr(scores[v], dimension)}" for v in offenders
            )
            defects.append(
                CheckerDefect(
                    kind=KIND_BANDING,
                    dimension=dimension,
                    validator_ids=tuple(offenders),
                    details=(
                        f"The instructions require {dimension} sub-scores in "
                        f"multiples of 5; violated by {values}"
                    ),
                )
            )

    ceilings: dict[str, int] = {}
    if rules.consensus_ceiling == CEILING_WORST_WINDOW_FLOOR:
        window_values = _features(request, entries, WINDOW_FEATURES)
        _guard_features_present("consensus", WINDOW_FEATURES, window_values)
        for validator_id, score in sorted(scores.items()):
            ceiling = _ceiling(window_values.get(validator_id, {}))
            if ceiling is None:
                continue
            ceilings[validator_id] = ceiling
            if score.consensus > ceiling:
                defects.append(
                    CheckerDefect(
                        kind=KIND_CEILING,
                        dimension="consensus",
                        validator_ids=(validator_id,),
                        details=(
                            f"consensus {score.consensus} exceeds the "
                            f"worst-window ceiling {ceiling}"
                        ),
                    )
                )

    for dimension, needed in sorted(rules.equality.items()):
        values = _features(request, entries, needed)
        _guard_features_present(dimension, needed, values)
        groups: dict[str, list[str]] = {}
        for validator_id in sorted(scores):
            row = values.get(validator_id)
            if row is None or any(row[f] is None for f in needed):
                continue
            groups.setdefault(_canonical(row), []).append(validator_id)
        for members in groups.values():
            sub_scores = {v: getattr(scores[v], dimension) for v in members}
            if len(set(sub_scores.values())) > 1:
                values_text = ", ".join(f"{v}={s}" for v, s in sub_scores.items())
                defects.append(
                    CheckerDefect(
                        kind=KIND_INCONSISTENT,
                        dimension=dimension,
                        validator_ids=tuple(members),
                        details=(
                            f"Identical {dimension} evidence with divergent "
                            f"sub-scores: {values_text}"
                        ),
                    )
                )

    for dimension, needed in sorted(rules.ordering.items()):
        values = _features(request, entries, needed)
        _guard_features_present(dimension, needed, values)
        comparable = [
            validator_id
            for validator_id in sorted(scores)
            if validator_id in values
            and all(values[validator_id][f] is not None for f in needed)
        ]
        for better in comparable:
            for worse in comparable:
                if better == worse:
                    continue
                if not _dominates(values[better], values[worse], needed):
                    continue
                score_better = getattr(scores[better], dimension)
                score_worse = getattr(scores[worse], dimension)
                cap = 100
                if dimension == "consensus" and better in ceilings:
                    cap = ceilings[better]
                # A tie is the cap working, never a violation, only when
                # the better validator cannot legally score higher.
                if score_better < score_worse or (
                    score_better == score_worse and score_better < cap
                ):
                    defects.append(
                        CheckerDefect(
                            kind=KIND_ORDERING,
                            dimension=dimension,
                            validator_ids=(better, worse),
                            details=(
                                f"{better} has strictly better {dimension} "
                                f"evidence than {worse} but scores "
                                f"{score_better} versus {score_worse}"
                            ),
                        )
                    )

    return tuple(
        sorted(
            defects,
            key=lambda d: (d.kind, d.dimension or "", d.validator_ids),
        )
    )
