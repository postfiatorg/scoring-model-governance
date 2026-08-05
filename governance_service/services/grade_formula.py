"""Grade formula v1: from the two defect lists to the grade.

The last stage of the G.4 checker/judge/formula split. The mechanical
checker (G.4.3) and the drawn judge (G.4.2) each produce a defect list
for one (corpus item, survivor answer) pair; this module turns them
into the per-item grade — banded 0-100 in multiples of 5 — and a
survivor's final grade, the unweighted mean on the 0-100 scale with
one decimal, the resolution the incumbent-replacement margin compares.

Everything here is a pure function of its inputs, and every grade is
emitted with receipts: the aggregated defects, each one's
classification, and the band decision. The thresholds are named,
versioned constants; changing one is a new formula version published
per round, never a silent edit. The band table and placement procedure
are the grading prompt v1 band rules carried into code (see
``docs/GradeFormulaV1.md`` for what changed in the translation).
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Sequence

from governance_service.services.checker import CheckerDefect
from governance_service.services.grading import SECTION_KINDS, JudgeOutput

GRADE_FORMULA_VERSION = 1

GRADE_STEP = 5
# A merged defect touching at least this many validators is systemic.
SYSTEMIC_VALIDATOR_THRESHOLD = 3
# A single merged evidence defect covering at least this fraction of
# the validator set triggers the 20-35 band's "evidence contradicted or
# ignored across the set" condition. Only the evidence-fidelity kinds
# qualify: a set-wide formatting or ordering defect is systemic, not an
# across-the-set evidence failure.
ACROSS_SET_FRACTION = Decimal("0.5")
ACROSS_SET_KINDS = SECTION_KINDS["evidence_fidelity"]

KIND_SUBVERSION = SECTION_KINDS["subversion"][0]

CLASS_LOCALIZED = "localized"
CLASS_SYSTEMIC = "systemic"

# Judge defects carry their section as the dimension analog; checker
# structural defects carry no dimension and group under their own label.
JUDGE_SECTIONS = tuple(SECTION_KINDS)
DIMENSION_STRUCTURAL = "structural"


class GradeFormulaError(ValueError):
    """Raised on inputs the formula cannot grade — never a defect."""


@dataclass(frozen=True)
class AggregatedDefect:
    """One distinct defect after merging: the unit band selection counts.

    ``sources`` is how many reported defects merged into it — receipts
    for the dedup, never a factor in the grade.
    """

    kind: str
    dimension: str
    validator_ids: tuple[str, ...]
    classification: str
    sources: int


@dataclass(frozen=True)
class ItemGradeResult:
    """One per-item grade with its receipts — including the validator
    count the across-the-set decision was made against, so the result
    is verifiable without re-opening the corpus item."""

    grade: int
    band: tuple[int, int]
    condition: str
    systemic_count: int
    localized_count: int
    validator_count: int
    defects: tuple[AggregatedDefect, ...]


def _merge(
    checker_defects: Sequence[CheckerDefect],
    judge_output: JudgeOutput,
) -> tuple[AggregatedDefect, ...]:
    """The distinct defects: same kind and dimension merge into one,
    counting each underlying defect once however many findings mention
    it. Checker and judge kinds are exclusive by construction, so the
    two lists concatenate without same-defect reconciliation."""
    buckets: dict[tuple[str, str], tuple[set[str], int]] = {}

    def _add(kind: str, dimension: str, validator_ids: tuple[str, ...]) -> None:
        ids, sources = buckets.setdefault((kind, dimension), (set(), 0))
        ids.update(validator_ids)
        buckets[(kind, dimension)] = (ids, sources + 1)

    for defect in checker_defects:
        _add(defect.kind, defect.dimension or DIMENSION_STRUCTURAL, defect.validator_ids)
    for section in JUDGE_SECTIONS:
        for defect in getattr(judge_output, section).defects:
            _add(defect.kind, section, defect.validator_ids)

    aggregated = []
    for (kind, dimension), (ids, sources) in buckets.items():
        classification = (
            CLASS_SYSTEMIC
            if len(ids) >= SYSTEMIC_VALIDATOR_THRESHOLD
            else CLASS_LOCALIZED
        )
        aggregated.append(
            AggregatedDefect(
                kind=kind,
                dimension=dimension,
                validator_ids=tuple(sorted(ids)),
                classification=classification,
                sources=sources,
            )
        )
    return tuple(sorted(aggregated, key=lambda d: (d.dimension, d.kind)))


def _across_set(defects: tuple[AggregatedDefect, ...], validator_count: int) -> bool:
    if validator_count <= 0:
        return False
    return any(
        defect.kind in ACROSS_SET_KINDS
        and Decimal(len(defect.validator_ids)) / Decimal(validator_count)
        >= ACROSS_SET_FRACTION
        for defect in defects
    )


def compute_item_grade(
    checker_defects: Sequence[CheckerDefect],
    judge_output: JudgeOutput,
    validator_count: int,
) -> ItemGradeResult:
    """The per-item grade for one (corpus item, survivor answer) pair.

    Band selection is a count, not an impression: aggregate, classify,
    count, select the lowest applicable band, anchor at its top for the
    minimum count of the condition met, step down one multiple of 5 per
    additional distinct defect, floor at the band's bottom. When more
    than one of the selected band's conditions is met, the lowest
    resulting grade stands.
    """
    if validator_count < 0:
        raise GradeFormulaError("validator_count must be non-negative")

    defects = _merge(checker_defects, judge_output)
    systemic = sum(1 for d in defects if d.classification == CLASS_SYSTEMIC)
    localized = len(defects) - systemic
    total = len(defects)
    subversion = any(d.kind == KIND_SUBVERSION for d in defects)
    across_set = _across_set(defects, validator_count)

    # (band bottom, band top, condition label, minimum count) for every
    # satisfied condition, in band order: the lowest applicable band
    # wins, and within it the lowest resulting grade stands.
    conditions: list[tuple[int, int, str, int]] = []
    if subversion:
        conditions.append((0, 15, "subversion", 1))
    if systemic >= 3:
        conditions.append((20, 35, "three_or_more_systemic", 3))
    if across_set:
        conditions.append((20, 35, "defect_across_the_set", 1))
    if systemic == 2:
        conditions.append((40, 55, "exactly_two_systemic", 2))
    if systemic == 1:
        conditions.append((60, 75, "exactly_one_systemic", 1))
    if systemic == 0 and localized >= 3:
        conditions.append((60, 75, "three_or_more_localized", 3))
    if systemic == 0 and 1 <= localized <= 2:
        conditions.append((80, 90, "one_or_two_localized", 1))

    if not conditions:
        if total:
            raise GradeFormulaError("Defects present but no band condition met")
        return ItemGradeResult(
            grade=100,
            band=(100, 100),
            condition="no_defects",
            systemic_count=0,
            localized_count=0,
            validator_count=validator_count,
            defects=(),
        )

    lowest_bottom = min(bottom for bottom, _, _, _ in conditions)
    selected = [c for c in conditions if c[0] == lowest_bottom]
    grade, condition = min(
        (max(bottom, top - GRADE_STEP * (total - minimum)), label)
        for bottom, top, label, minimum in selected
    )

    return ItemGradeResult(
        grade=grade,
        band=(selected[0][0], selected[0][1]),
        condition=condition,
        systemic_count=systemic,
        localized_count=localized,
        validator_count=validator_count,
        defects=defects,
    )


def final_grade(item_grades: Sequence[int]) -> Decimal:
    """A survivor's final grade: the unweighted mean of its per-item
    grades, 0-100 with one decimal — the resolution the incumbent
    margin compares."""
    if not item_grades:
        raise GradeFormulaError("final_grade requires at least one item grade")
    for grade in item_grades:
        if (
            isinstance(grade, bool)
            or not isinstance(grade, int)
            or not 0 <= grade <= 100
            or grade % GRADE_STEP
        ):
            raise GradeFormulaError(
                f"Item grade {grade!r} is not an integer 0-100 in multiples of {GRADE_STEP}"
            )
    mean = Decimal(sum(item_grades)) / Decimal(len(item_grades))
    return mean.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
