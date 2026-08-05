"""Offline re-grading: frozen material to grades, no judge required live.

The chain that keeps every past round re-gradable under any judge: from
one corpus item's frozen request, one survivor answer, and one judge
output — all stored material — it runs the production answer parser,
the mechanical grading checker, the judge defect schema parser, and
grade formula v1 into a per-item grade with complete receipts, and the
final grade over a material set. Pure functions of their inputs: anyone
re-running this chain on the same frozen material reproduces every
grade bit-for-bit, so judge rotation never erases cross-round
comparability.
"""

from dataclasses import asdict
from typing import Any

from governance_service.scoring import parse_response
from governance_service.services.checker import check_answer
from governance_service.services.disqualification import synthetic_validator_map
from governance_service.services.edge_cases import validator_entries
from governance_service.services.grade_formula import (
    GRADE_FORMULA_VERSION,
    compute_item_grade,
    final_grade,
)
from governance_service.services.grading import JudgeOutputError, parse_judge_output


class RegradeError(ValueError):
    """Raised when material cannot be re-graded — never a defect."""


def regrade_item(
    item_id: str,
    request: dict[str, Any],
    answer_content: str,
    judge_content: str,
    validator_map: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """One (corpus item, survivor answer, judge output) triple to a grade.

    Historical items pass their frozen validator identity map;
    constructed items derive the synthetic map from their own validator
    ids, exactly as disqualification does.
    """
    identity_map = validator_map or synthetic_validator_map(request)
    scoring_result = parse_response(answer_content, identity_map)
    if not scoring_result.complete:
        raise RegradeError(
            f"{item_id}: answer does not parse completely under the production "
            f"parser ({'; '.join(scoring_result.errors)}) — disqualification "
            f"material is not gradable"
        )
    checker_defects = check_answer(request, scoring_result, identity_map)
    try:
        judge_output = parse_judge_output(judge_content)
    except JudgeOutputError as exc:
        raise RegradeError(
            f"{item_id}: judge output violates the defect schema: {exc}"
        ) from exc
    result = compute_item_grade(
        checker_defects,
        judge_output,
        validator_count=len(validator_entries(request)),
    )
    return {"item_id": item_id, **asdict(result)}


def regrade_material(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """A material set to per-item grades and the final grade.

    Each entry carries ``item_id``, ``request``, ``answer_content``,
    ``judge_content``, and optionally ``validator_map``.
    """
    if not entries:
        raise RegradeError("Material carries no entries")
    items = [
        regrade_item(
            entry["item_id"],
            entry["request"],
            entry["answer_content"],
            entry["judge_content"],
            entry.get("validator_map"),
        )
        for entry in entries
    ]
    return {
        "grade_formula_version": GRADE_FORMULA_VERSION,
        "items": items,
        "final_grade": str(final_grade([item["grade"] for item in items])),
    }
