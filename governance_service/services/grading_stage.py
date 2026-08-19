"""The grading stage: the G.4 harness running inside a governance round (G.5.9).

EXAMINED -> GRADED — the last stage of the execution pipeline, after
which a triggered round runs end to end. The stage derives
identity-blinded grading pairs from the round's linked surviving exam
answers (one canonical answer per corpus item per survivor, the current
judge's own answers excluded), runs the drawn judge through the
idempotent grading engine at the frozen repeat count, and holds the
judge to its mechanical bar: every output parses under the frozen defect
schema and every pair's repeats carry one identical hash. On a judge's
failure the frozen redraw ordering promotes the next challenger — the
promoted judge leaving the competition and its answers leaving the
pairs — and exhausting the challengers abandons the round, with the
failed judges booked into the standing blocklist as the abandonment's
only outcome. For a passing judge, every grade is computed in code — the
production answer parser, the mechanical checker, the defect-schema
parser, and the versioned grade formula — exactly the offline re-grading
chain, so any verifier reproduces identical grades from frozen material.

Grades persist per round on ``governance_round_exam_runs`` — never on
the shared exam runs, whose rows sit inside earlier rounds' published
records — and every grading run a round pays for or reuses is linked in
``governance_round_grading_runs`` with the judge's mechanical outcome,
which the redraw resume and the decision's blocklist booking read back.

Failure discipline follows the engine's two-sided taxonomy: a judge's
own failure becomes redraw evidence, while an infrastructure failure
propagates and fails the round — the manual trigger is the recovery
path, and the engine's idempotent runs plus the links make the retried
round reuse every already-paid inference when the fresh freeze
reproduces identical material.
"""

import json
import logging
from typing import Any, Callable

import httpx

from governance_service.models.runtime_profile import RuntimeProfile
from governance_service.services import decision as decision_module
from governance_service.services import orchestrator as orchestrator_module
from governance_service.services import regrading
from governance_service.services.disqualification import VERDICT_SURVIVED
from governance_service.services.exam_engine import ExamItem
from governance_service.services.exam_stage import (
    default_client,
    frozen_profile,
    load_frozen_exam_material,
)
from governance_service.services.grade_formula import (
    GRADE_FORMULA_VERSION,
    final_grade,
)
from governance_service.services.grading_engine import (
    JUDGE_OUTCOME_FAILED,
    JUDGE_OUTCOME_PASSED,
    RUN_COMPLETED,
    RUN_JUDGE_FAILED,
    VERDICT_PASS,
    GradingEngine,
    GradingPair,
    get_grading_outputs,
    judge_mechanical_verdict,
)
from governance_service.services.judge_draw import next_judge
from governance_service.services.round_package import (
    CANDIDATES_FILE_PATH,
    PARAMETERS_FILE_PATH,
    get_package_file,
)

logger = logging.getLogger(__name__)


class GradingStageError(RuntimeError):
    """The grading cannot run from the round's persisted material."""


def _load_round_state(conn, round_id: int) -> dict[str, Any]:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT judge_hf_repo, draw_ledger_hash FROM governance_rounds WHERE id = %s",
        (round_id,),
    )
    row = cursor.fetchone()
    cursor.close()
    if row is None:
        raise GradingStageError(f"Round id {round_id} does not exist")
    judge_hf_repo, draw_ledger_hash = row
    if judge_hf_repo is None or draw_ledger_hash is None:
        raise GradingStageError(
            f"Round id {round_id} has no persisted judge draw — grading "
            "cannot know who judges or how to redraw"
        )
    return {"judge_hf_repo": judge_hf_repo, "draw_ledger_hash": draw_ledger_hash}


def _linked_survivors(conn, round_id: int) -> list[dict[str, Any]]:
    """The round's surviving examinees through its exam links."""
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT r.hf_repo, l.run_id, r.verdict
        FROM governance_round_exam_runs l
        JOIN exam_runs r ON r.id = l.run_id
        WHERE l.round_id = %s
        ORDER BY r.hf_repo
        """,
        (round_id,),
    )
    rows = cursor.fetchall()
    cursor.close()
    conn.rollback()
    if not rows:
        raise GradingStageError(
            f"Round id {round_id} has no linked exam runs — grading has "
            "no material"
        )
    for hf_repo, _, verdict in rows:
        if verdict is None:
            raise GradingStageError(
                f"Exam run for {hf_repo} has no verdict — the round is "
                "not fully verdicted"
            )
    return [
        {"hf_repo": hf_repo, "run_id": run_id}
        for hf_repo, run_id, verdict in rows
        if verdict == VERDICT_SURVIVED
    ]


def _canonical_answers(conn, run_id: int, item_ids: list[str]) -> dict[str, str]:
    """One answer per corpus item from a surviving run's stored outputs.

    Survivors passed the determinism rule, so every attempt of an item
    carries identical content; the first attempt is the canonical pick.
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT DISTINCT ON (item_id) item_id, raw_response
        FROM exam_outputs
        WHERE run_id = %s
        ORDER BY item_id, attempt
        """,
        (run_id,),
    )
    answers = dict(cursor.fetchall())
    cursor.close()
    conn.rollback()
    missing = [item_id for item_id in item_ids if item_id not in answers]
    if missing:
        raise GradingStageError(
            f"Exam run {run_id} has no stored answer for corpus item(s) "
            f"{', '.join(missing)}"
        )
    return answers


def build_pairs(
    items: list[ExamItem],
    answers_by_survivor: dict[str, dict[str, str]],
    excluded: set[str],
) -> list[GradingPair]:
    """The blinded grading material: every survivor answer except the
    current judge's and the failed judges' — a failed judge sits out the
    rest of the round entirely.

    Pair identity carries no candidate name — answers are addressed by
    their canonical content hash, so the judge cannot favor a model it
    recognizes and identical answers from different survivors grade once.
    """
    pairs: dict[tuple[str, str], GradingPair] = {}
    for hf_repo, answers in sorted(answers_by_survivor.items()):
        if hf_repo in excluded:
            continue
        for item in items:
            pair = GradingPair(
                item_id=item.item_id,
                exam_request=item.request,
                answer_content=answers[item.item_id],
            )
            pairs.setdefault((pair.item_id, pair.answer_hash), pair)
    return [pairs[key] for key in sorted(pairs)]


def _challenger_profiles(candidates_file: dict[str, Any]) -> dict[str, RuntimeProfile]:
    entries = candidates_file.get("challengers")
    if not isinstance(entries, list) or not entries:
        raise GradingStageError("Frozen package carries no challengers")
    profiles = [frozen_profile(entry, "challenger") for entry in entries]
    return {profile.hf_repo: profile for profile in profiles}


def _linked_grading_outcomes(conn, round_id: int) -> dict[str, str | None]:
    """Each linked judge's recorded outcome, for the redraw resume."""
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT hf_repo, outcome FROM governance_round_grading_runs
        WHERE round_id = %s
        """,
        (round_id,),
    )
    outcomes = dict(cursor.fetchall())
    cursor.close()
    conn.rollback()
    return outcomes


def _link_grading_run(
    conn, round_id: int, hf_repo: str, run_id: int, outcome: str | None
) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO governance_round_grading_runs (round_id, run_id, hf_repo, outcome)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (round_id, run_id) DO UPDATE SET outcome = EXCLUDED.outcome
        """,
        (round_id, run_id, hf_repo, outcome),
    )
    cursor.close()
    conn.commit()


def _update_judge(conn, round_id: int, judge_hf_repo: str) -> None:
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE governance_rounds SET judge_hf_repo = %s WHERE id = %s",
        (judge_hf_repo, round_id),
    )
    cursor.close()
    conn.commit()


def _judge_outcome(
    conn, run_id: int, pairs: list[GradingPair], repeats: int
) -> str:
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM grading_runs WHERE id = %s", (run_id,))
    status = cursor.fetchone()[0]
    cursor.close()
    conn.rollback()
    if status == RUN_JUDGE_FAILED:
        return JUDGE_OUTCOME_FAILED
    if status != RUN_COMPLETED:
        raise GradingStageError(
            f"Grading run {run_id} is {status} — no judge outcome exists"
        )
    verdict = judge_mechanical_verdict(conn, run_id, pairs, repeats=repeats)
    conn.rollback()
    return JUDGE_OUTCOME_PASSED if verdict["verdict"] == VERDICT_PASS else JUDGE_OUTCOME_FAILED


def _exhausted(
    conn, round_id: int, round_number: int, failed: set[str]
) -> RuntimeError:
    """The exhaustion exit: book the failed judges, then hand back the
    abandonment for the caller to raise.

    The methodology makes those blocklist entries an exhausted round's
    only outcome, and the abandonment closes the round terminally — so
    booking must happen before the raise, on every path that can reach
    exhaustion, the resume path included. Booking reads the round's
    committed grading links and is idempotent, so a crash between it and
    the abandonment replays safely.
    """
    decision_module.book_failed_judges(conn, round_number, round_id)
    # The decision commits its own transaction; here the abandonment
    # handler's rollback would otherwise discard the bookings.
    conn.commit()
    return orchestrator_module.RoundAbandoned(
        "Judge redraw exhausted the frozen challengers — failed "
        f"judges: {', '.join(sorted(failed))}"
    )


def _compute_grades(
    conn,
    grading_run_id: int,
    items: list[ExamItem],
    validator_maps: dict[str, dict[str, dict[str, str]]],
    survivors: list[dict[str, Any]],
    answers_by_survivor: dict[str, dict[str, str]],
    excluded: set[str],
) -> dict[str, dict[str, Any]]:
    """Every survivor's final grade from the judge's stored outputs.

    Pure recomputation over persisted material — the offline re-grading
    chain, keyed to the judge's canonical output per (item, answer hash).
    """
    outputs = get_grading_outputs(conn, grading_run_id)
    conn.rollback()
    judge_content: dict[tuple[str, str], str] = {}
    for output in outputs:
        judge_content.setdefault(
            (output["item_id"], output["answer_hash"]), output["raw_response"]
        )

    grades: dict[str, dict[str, Any]] = {}
    for survivor in survivors:
        hf_repo = survivor["hf_repo"]
        if hf_repo in excluded:
            continue
        item_results = []
        for item in items:
            answer = answers_by_survivor[hf_repo][item.item_id]
            pair = GradingPair(
                item_id=item.item_id,
                exam_request=item.request,
                answer_content=answer,
            )
            content = judge_content.get((item.item_id, pair.answer_hash))
            if content is None:
                raise GradingStageError(
                    f"Grading run {grading_run_id} has no stored judge output "
                    f"for {item.item_id} / {pair.answer_hash}"
                )
            item_results.append(
                regrading.regrade_item(
                    item.item_id,
                    item.request,
                    answer,
                    content,
                    validator_maps[item.item_id],
                )
            )
        grades[hf_repo] = {
            "final_grade": str(final_grade([r["grade"] for r in item_results])),
            "receipts": {
                "grade_formula_version": GRADE_FORMULA_VERSION,
                "items": item_results,
            },
        }
    return grades


def _persist_grades(
    conn, round_id: int, grades: dict[str, dict[str, Any]]
) -> None:
    cursor = conn.cursor()
    for hf_repo, grade in grades.items():
        cursor.execute(
            """
            UPDATE governance_round_exam_runs
            SET final_grade = %s, grade_receipts = %s
            WHERE round_id = %s AND hf_repo = %s
            """,
            (
                grade["final_grade"],
                json.dumps(grade["receipts"], sort_keys=True),
                round_id,
                hf_repo,
            ),
        )
        if cursor.rowcount != 1:
            cursor.close()
            raise GradingStageError(
                f"Round id {round_id} has no exam link for graded survivor "
                f"{hf_repo}"
            )
    cursor.close()
    conn.commit()


def run_grading(
    conn,
    round_id: int,
    round_number: int,
    *,
    engine: GradingEngine | None = None,
    client_factory: Callable[[], httpx.Client] | None = None,
) -> dict[str, Any]:
    """Grade the round's survivors under the drawn judge, redrawing as needed.

    Safe to re-run: the engine reuses terminal runs, linked outcomes
    replay the redraw history without re-judging, and the grade
    computation is a pure function of stored material.
    """
    state = _load_round_state(conn, round_id)
    candidates_file = get_package_file(conn, round_number, CANDIDATES_FILE_PATH)
    if candidates_file is None:
        raise GradingStageError(
            f"Round {round_number} has no frozen {CANDIDATES_FILE_PATH} artifact"
        )
    parameters = get_package_file(conn, round_number, PARAMETERS_FILE_PATH)
    if parameters is None:
        raise GradingStageError(
            f"Round {round_number} has no frozen {PARAMETERS_FILE_PATH} artifact"
        )
    repeats = parameters.get("repeat_count")
    if not isinstance(repeats, int) or isinstance(repeats, bool) or repeats < 1:
        raise GradingStageError(
            f"Frozen repeat_count {repeats!r} is not a positive integer"
        )
    challengers = _challenger_profiles(candidates_file)

    with (client_factory or default_client)() as client:
        items, validator_maps = load_frozen_exam_material(
            conn, round_number, client
        )
    survivors = _linked_survivors(conn, round_id)
    answers_by_survivor = {
        survivor["hf_repo"]: _canonical_answers(
            conn, survivor["run_id"], [item.item_id for item in items]
        )
        for survivor in survivors
    }

    # Resume: judges already determined FAILED stay failed; the frozen
    # ordering then lands on the same judge a crashed run was grading.
    failed = {
        hf_repo
        for hf_repo, outcome in _linked_grading_outcomes(conn, round_id).items()
        if outcome == JUDGE_OUTCOME_FAILED
    }
    judge_hf_repo = state["judge_hf_repo"]
    if judge_hf_repo in failed:
        judge_hf_repo = next_judge(
            state["draw_ledger_hash"], sorted(challengers), failed
        )
        if judge_hf_repo is None:
            raise _exhausted(conn, round_id, round_number, failed)
        _update_judge(conn, round_id, judge_hf_repo)

    grading_engine = engine or GradingEngine()
    while True:
        if judge_hf_repo not in challengers:
            raise GradingStageError(
                f"Judge {judge_hf_repo} is not a frozen challenger"
            )
        judge_profile = challengers[judge_hf_repo]
        pairs = build_pairs(items, answers_by_survivor, failed | {judge_hf_repo})
        if not pairs:
            # No survivor outside the judge: nothing is gradable, and the
            # decision's no-survivor rules take it from here.
            logger.warning(
                "Round %d has no gradable survivor answers — skipping grading",
                round_number,
            )
            return {
                "judge_hf_repo": judge_hf_repo,
                "graded": 0,
                "redraws": len(failed),
                "grades": {},
            }

        run_id = grading_engine.grade(
            conn, judge_profile, pairs, repeats=repeats, round_id=round_id
        )
        outcome = _judge_outcome(conn, run_id, pairs, repeats)
        _link_grading_run(conn, round_id, judge_hf_repo, run_id, outcome)
        if outcome == JUDGE_OUTCOME_PASSED:
            break

        logger.warning(
            "Round %d judge %s failed its mechanical bar — applying the "
            "frozen redraw ordering",
            round_number,
            judge_hf_repo,
        )
        failed.add(judge_hf_repo)
        judge_hf_repo = next_judge(
            state["draw_ledger_hash"], sorted(challengers), failed
        )
        if judge_hf_repo is None:
            raise _exhausted(conn, round_id, round_number, failed)
        _update_judge(conn, round_id, judge_hf_repo)

    grades = _compute_grades(
        conn,
        run_id,
        items,
        validator_maps,
        survivors,
        answers_by_survivor,
        failed | {judge_hf_repo},
    )
    _persist_grades(conn, round_id, grades)
    logger.info(
        "Round %d graded: judge %s over %d survivor(s), %d redraw(s)",
        round_number,
        judge_hf_repo,
        len(grades),
        len(failed),
    )
    return {
        "judge_hf_repo": judge_hf_repo,
        "graded": len(grades),
        "redraws": len(failed),
        "grades": {repo: grade["final_grade"] for repo, grade in grades.items()},
    }
