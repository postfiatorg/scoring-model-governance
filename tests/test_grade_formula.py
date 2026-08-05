"""Grade formula v1: aggregation, classification, the band table, the mean."""

from decimal import Decimal

import pytest

from governance_service.services.checker import CheckerDefect
from governance_service.services.grade_formula import (
    ACROSS_SET_FRACTION,
    CLASS_LOCALIZED,
    CLASS_SYSTEMIC,
    GRADE_FORMULA_VERSION,
    SYSTEMIC_VALIDATOR_THRESHOLD,
    GradeFormulaError,
    compute_item_grade,
    final_grade,
)
from governance_service.services.grading import (
    JudgeDefect,
    JudgeOutput,
    SectionFinding,
)

VALIDATOR_COUNT = 10


def _checker_defect(kind="inconsistent_scores", dimension="reliability", ids=("v001", "v002")):
    return CheckerDefect(
        kind=kind,
        dimension=dimension,
        validator_ids=tuple(ids),
        details="details",
    )


def _judge_defect(kind="false_claim", ids=("v001",)):
    return JudgeDefect(
        kind=kind,
        validator_ids=tuple(ids),
        quote="quoted claim",
        explanation="the evidence contradicts it",
    )


def _judge_output(fidelity=(), report=(), subversion=()):
    def _section(defects):
        return SectionFinding(
            outcome="defects_found" if defects else "none_found",
            defects=tuple(defects),
        )

    return JudgeOutput(
        evidence_fidelity=_section(fidelity),
        network_report_quality=_section(report),
        subversion=_section(subversion),
    )


def _grade(checker=(), judge=None, validator_count=VALIDATOR_COUNT):
    return compute_item_grade(checker, judge or _judge_output(), validator_count)


# -- constants ---------------------------------------------------------------


def test_declared_constants():
    assert GRADE_FORMULA_VERSION == 1
    assert SYSTEMIC_VALIDATOR_THRESHOLD == 3
    assert ACROSS_SET_FRACTION == Decimal("0.5")


# -- aggregation and classification ------------------------------------------


def test_same_kind_same_dimension_defects_merge_once():
    checker = [
        _checker_defect(ids=("v001", "v002")),
        _checker_defect(ids=("v002", "v003")),
    ]
    result = _grade(checker)
    assert len(result.defects) == 1
    merged = result.defects[0]
    assert merged.validator_ids == ("v001", "v002", "v003")
    assert merged.sources == 2
    assert merged.classification == CLASS_SYSTEMIC


def test_different_kinds_never_merge():
    checker = [
        _checker_defect(kind="inconsistent_scores", ids=("v001",)),
        _checker_defect(kind="ordering_violation", ids=("v001", "v002")),
    ]
    result = _grade(checker)
    assert len(result.defects) == 2
    assert all(d.classification == CLASS_LOCALIZED for d in result.defects)


def test_judge_and_checker_lists_concatenate():
    result = _grade(
        [_checker_defect(ids=("v001",))],
        _judge_output(fidelity=[_judge_defect(ids=("v002",))]),
    )
    assert len(result.defects) == 2
    assert result.localized_count == 2


def test_systemic_threshold_is_exactly_three_validators():
    two = _grade([_checker_defect(ids=("v001", "v002"))])
    three = _grade([_checker_defect(ids=("v001", "v002", "v003"))])
    assert two.defects[0].classification == CLASS_LOCALIZED
    assert three.defects[0].classification == CLASS_SYSTEMIC


# -- the band table ----------------------------------------------------------


def test_no_defects_grades_100_flat():
    result = _grade()
    assert result.grade == 100
    assert result.condition == "no_defects"
    assert result.defects == ()


def test_one_localized_defect_grades_90():
    result = _grade([_checker_defect(ids=("v001",))])
    assert (result.grade, result.band) == (90, (80, 90))


def test_two_localized_defects_grade_85():
    checker = [
        _checker_defect(ids=("v001",)),
        _checker_defect(kind="ordering_violation", ids=("v002", "v003")),
    ]
    assert _grade(checker).grade == 85


def test_three_localized_defects_enter_60_75_at_the_top():
    checker = [
        _checker_defect(ids=("v001",)),
        _checker_defect(kind="ordering_violation", ids=("v002",)),
        _checker_defect(kind="banding_violation", dimension="identity", ids=("v003",)),
    ]
    result = _grade(checker)
    assert (result.grade, result.band) == (75, (60, 75))
    assert result.condition == "three_or_more_localized"


def test_one_systemic_defect_grades_75():
    result = _grade([_checker_defect(ids=("v001", "v002", "v003"))])
    assert (result.grade, result.band) == (75, (60, 75))
    assert result.condition == "exactly_one_systemic"


def test_one_systemic_plus_localized_steps_down():
    checker = [
        _checker_defect(ids=("v001", "v002", "v003")),
        _checker_defect(kind="ordering_violation", ids=("v004",)),
        _checker_defect(kind="banding_violation", dimension="software", ids=("v005",)),
    ]
    assert _grade(checker).grade == 65


def test_two_systemic_defects_grade_55():
    checker = [
        _checker_defect(ids=("v001", "v002", "v003")),
        _checker_defect(kind="ordering_violation", ids=("v004", "v005", "v006")),
    ]
    result = _grade(checker)
    assert (result.grade, result.band) == (55, (40, 55))


def test_two_systemic_plus_localized_step_down_to_the_40_55_floor():
    base = [
        _checker_defect(ids=("v001", "v002", "v003")),
        _checker_defect(kind="ordering_violation", ids=("v004", "v005", "v006")),
    ]
    extra = [
        _checker_defect(kind=k, dimension=d, ids=("v007",))
        for k, d in (
            ("banding_violation", "software"),
            ("banding_violation", "identity"),
            ("ceiling_exceeded", "consensus"),
            ("inconsistent_scores", "software"),
        )
    ]
    assert _grade(base + extra[:1]).grade == 50
    assert _grade(base + extra[:2]).grade == 45
    assert _grade(base + extra).grade == 40


def test_judge_defects_of_the_same_kind_merge():
    judge = _judge_output(
        fidelity=[
            _judge_defect(ids=("v001",)),
            _judge_defect(ids=("v002",)),
        ]
    )
    result = _grade((), judge)
    assert len(result.defects) == 1
    assert result.defects[0].validator_ids == ("v001", "v002")
    assert result.defects[0].classification == CLASS_LOCALIZED


def test_fidelity_kinds_stay_distinct_despite_sharing_a_section():
    judge = _judge_output(
        fidelity=[
            _judge_defect(kind="false_claim", ids=("v001",)),
            _judge_defect(kind="ignored_evidence", ids=("v001",)),
        ]
    )
    assert len(_grade((), judge).defects) == 2


def test_three_systemic_defects_grade_35():
    checker = [
        _checker_defect(ids=("v001", "v002", "v003")),
        _checker_defect(kind="ordering_violation", ids=("v004", "v005", "v006")),
        _checker_defect(kind="banding_violation", dimension="diversity", ids=("v007", "v008", "v009")),
    ]
    result = _grade(checker)
    assert (result.grade, result.band) == (35, (20, 35))


def test_step_down_never_leaves_the_band():
    checker = [
        _checker_defect(kind=k, dimension=d, ids=("v001",))
        for k, d in (
            ("inconsistent_scores", "reliability"),
            ("inconsistent_scores", "software"),
            ("ordering_violation", "consensus"),
            ("ordering_violation", "diversity"),
            ("banding_violation", "software"),
            ("banding_violation", "identity"),
            ("ceiling_exceeded", "consensus"),
        )
    ]
    result = _grade(checker)
    assert result.localized_count == 7
    assert result.band == (60, 75)
    assert result.grade == 60


# -- the across-the-set trigger ----------------------------------------------


def test_evidence_defect_covering_half_the_set_forces_20_35():
    judge = _judge_output(
        fidelity=[_judge_defect(ids=tuple(f"v{i:03d}" for i in range(1, 6)))]
    )
    result = _grade((), judge, validator_count=10)
    assert result.band == (20, 35)
    assert result.condition == "defect_across_the_set"
    assert result.grade == 35


def test_just_below_half_does_not_trigger():
    judge = _judge_output(
        fidelity=[_judge_defect(ids=tuple(f"v{i:03d}" for i in range(1, 5)))]
    )
    result = _grade((), judge, validator_count=10)
    assert result.band == (60, 75)


def test_non_evidence_kinds_never_trigger_across_the_set():
    """A set-wide formatting defect is one systemic defect on the normal
    ladder, never an across-the-set evidence failure."""
    result = _grade(
        [_checker_defect(kind="banding_violation", ids=tuple(f"v{i:03d}" for i in range(1, 11)))],
        validator_count=10,
    )
    assert result.band == (60, 75)
    assert result.condition == "exactly_one_systemic"


# -- subversion --------------------------------------------------------------


def test_subversion_forces_the_bottom_band():
    judge = _judge_output(subversion=[_judge_defect(kind="subversion", ids=())])
    result = _grade((), judge)
    assert (result.grade, result.band) == (15, (0, 15))
    assert result.condition == "subversion"


def test_subversion_steps_down_with_additional_defects_to_the_floor():
    judge = _judge_output(
        fidelity=[_judge_defect(ids=("v001",))],
        subversion=[_judge_defect(kind="subversion", ids=())],
    )
    assert _grade((), judge).grade == 10

    checker = [
        _checker_defect(ids=("v001",)),
        _checker_defect(kind="ordering_violation", ids=("v002",)),
        _checker_defect(kind="banding_violation", dimension="software", ids=("v003",)),
        _checker_defect(kind="ceiling_exceeded", dimension="consensus", ids=("v004",)),
    ]
    result = _grade(checker, _judge_output(subversion=[_judge_defect(kind="subversion", ids=())]))
    assert result.grade == 0


def test_subversion_outranks_an_otherwise_clean_answer_with_systemics():
    checker = [_checker_defect(ids=("v001", "v002", "v003"))]
    judge = _judge_output(subversion=[_judge_defect(kind="subversion", ids=())])
    result = _grade(checker, judge)
    assert result.band == (0, 15)


# -- precedence and placement ------------------------------------------------


def test_lowest_applicable_band_wins():
    """Two systemics alone select 40-55, but one of them is an evidence
    defect across half the set — the lower band wins."""
    checker = [_checker_defect(ids=("v001", "v002", "v003"))]
    judge = _judge_output(
        fidelity=[_judge_defect(ids=tuple(f"v{i:03d}" for i in range(4, 9)))]
    )
    result = _grade(checker, judge, validator_count=10)
    assert result.systemic_count == 2
    assert result.band == (20, 35)
    assert result.grade == 30


def test_within_band_the_lowest_satisfied_condition_stands():
    """One systemic plus three localized meets 60-75 twice; the systemic
    condition's anchor yields the lower grade and stands."""
    checker = [
        _checker_defect(ids=("v001", "v002", "v003")),
        _checker_defect(kind="ordering_violation", ids=("v004",)),
        _checker_defect(kind="banding_violation", dimension="software", ids=("v005",)),
        _checker_defect(kind="ceiling_exceeded", dimension="consensus", ids=("v006",)),
    ]
    result = _grade(checker)
    assert result.grade == 60
    assert result.condition == "exactly_one_systemic"


# -- receipts and purity -----------------------------------------------------


def test_result_carries_full_receipts():
    checker = [_checker_defect(ids=("v001", "v002", "v003"))]
    judge = _judge_output(fidelity=[_judge_defect(ids=("v004",))])
    result = _grade(checker, judge)
    assert result.systemic_count == 1
    assert result.localized_count == 1
    assert {d.kind for d in result.defects} == {"inconsistent_scores", "false_claim"}
    assert all(d.classification in (CLASS_LOCALIZED, CLASS_SYSTEMIC) for d in result.defects)


def test_formula_is_pure():
    checker = [_checker_defect(ids=("v001", "v002"))]
    judge = _judge_output(fidelity=[_judge_defect(ids=("v003",))])
    assert _grade(checker, judge) == _grade(checker, judge)


def test_negative_validator_count_is_rejected():
    with pytest.raises(GradeFormulaError):
        _grade(validator_count=-1)


# -- the final grade ---------------------------------------------------------


def test_final_grade_is_the_one_decimal_mean():
    assert final_grade([90, 85, 75]) == Decimal("83.3")
    assert final_grade([100]) == Decimal("100.0")
    assert final_grade([0, 15]) == Decimal("7.5")


def test_final_grade_rounds_half_up():
    assert final_grade([90, 85]) == Decimal("87.5")
    assert final_grade([85, 90, 90]) == Decimal("88.3")
    # 86.25 rounds up to 86.3; ROUND_HALF_EVEN would give 86.2.
    assert final_grade([90, 85, 85, 85]) == Decimal("86.3")


def test_final_grade_rejects_bad_inputs():
    for bad in ([], [90, 101], [90, 85.5], [90, 87], [True, False]):
        with pytest.raises(GradeFormulaError):
            final_grade(bad)
