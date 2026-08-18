"""The decision engine: from a round's evidence to its verdict (G.5.6).

Pure arithmetic over persisted round data — no judgment call anywhere, so
any verifier recomputes the identical verdict. The rules are the
methodology's: the highest-graded surviving challenger replaces the
incumbent only when it beats it by the frozen incumbent-replacement
margin; a mechanically disqualified incumbent loses that protection and
the best surviving challenger wins outright; with no survivor at all the
incumbent keeps serving by necessity and the condition is recorded as a
production alarm. Grade ties between challengers are broken by the
round's drawing-ledger hash through the judge draw's modulo mapping —
the hash was public before any grade existed, so the tie-break was fixed
before a tie could be known, and it is recomputable from public data.

The incumbent identity and the margin come from the frozen package
artifacts, never live configuration: freeze-time truth decides the
round. Disqualified revisions are booked into the standing blocklist
with the round as their reference (failed-judge entries join when the
redraw wiring lands with the exam and grading stages).
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from governance_service.services.disqualification import VERDICT_SURVIVED
from governance_service.services.judge_draw import map_hash_to_challenger
from governance_service.services.round_package import (
    CANDIDATES_FILE_PATH,
    PARAMETERS_FILE_PATH,
    get_package_file,
)

logger = logging.getLogger(__name__)

DECISION_CHALLENGER_REPLACES = "challenger_replaces_incumbent"
DECISION_INCUMBENT_RETAINED = "incumbent_retained"
DECISION_RETAINED_BY_NECESSITY = "incumbent_retained_by_necessity"


class DecisionError(RuntimeError):
    """The verdict cannot be computed from the round's persisted data."""


@dataclass(frozen=True)
class ExaminedCandidate:
    hf_repo: str
    revision: str
    verdict: str
    final_grade: Decimal | None


@dataclass(frozen=True)
class DecisionResult:
    decision: str
    winner_hf_repo: str
    rationale: dict[str, Any]


def break_tie(tied: list[str], draw_ledger_hash: str) -> str:
    """The frozen ledger randomness picks among grade-tied challengers.

    Same mapping as the judge draw: the drawing ledger hash, a public
    number fixed before any grade existed, modulo the tied count over the
    codepoint-sorted names. No challenger is favored by its name.
    """
    return map_hash_to_challenger(draw_ledger_hash, tied)


def decide(
    *,
    incumbent_hf_repo: str,
    candidates: list[ExaminedCandidate],
    margin: Decimal,
    draw_ledger_hash: str,
    judge_hf_repo: str | None,
) -> DecisionResult:
    """The pure verdict from one round's examined candidates.

    ``candidates`` are the round's examined pool members. The drawn judge
    never competes; a redraw-judge's exam run stays in the record but is
    excluded here.
    """
    competing = [c for c in candidates if c.hf_repo != judge_hf_repo]
    incumbent = next(
        (c for c in competing if c.hf_repo == incumbent_hf_repo), None
    )
    if incumbent is None:
        # A missing run is missing evidence, not a disqualification: only
        # a mechanically disqualified incumbent loses margin protection.
        raise DecisionError(
            f"Incumbent {incumbent_hf_repo} has no exam run — the round's "
            "evidence is incomplete"
        )
    challengers = [c for c in competing if c.hf_repo != incumbent_hf_repo]
    survivors = [c for c in challengers if c.verdict == VERDICT_SURVIVED]
    for candidate in survivors:
        if candidate.final_grade is None:
            raise DecisionError(
                f"Survivor {candidate.hf_repo} has no final grade — the "
                "round is not fully graded"
            )

    incumbent_healthy = incumbent.verdict == VERDICT_SURVIVED
    if incumbent_healthy and incumbent.final_grade is None:
        raise DecisionError(
            f"Incumbent {incumbent_hf_repo} survived without a final grade — "
            "the round is not fully graded"
        )

    rationale: dict[str, Any] = {
        "incumbent": incumbent_hf_repo,
        "incumbent_verdict": incumbent.verdict,
        "margin": str(margin),
        "judge": judge_hf_repo,
        "grades": {
            c.hf_repo: str(c.final_grade) if c.final_grade is not None else None
            for c in competing
        },
    }

    if not survivors:
        if incumbent_healthy:
            rationale["reason"] = (
                "No challenger survived mechanical disqualification; the "
                "incumbent keeps its seat."
            )
            return DecisionResult(
                DECISION_INCUMBENT_RETAINED, incumbent_hf_repo, rationale
            )
        rationale["reason"] = (
            "The incumbent was mechanically disqualified and no challenger "
            "survived either: the incumbent keeps serving by necessity. "
            "This is a production alarm — the live scorer no longer upholds "
            "the determinism discipline verification depends on."
        )
        logger.error(
            "Governance decision: incumbent retained by necessity — no "
            "surviving candidate at all"
        )
        return DecisionResult(
            DECISION_RETAINED_BY_NECESSITY, incumbent_hf_repo, rationale
        )

    top_grade = max(c.final_grade for c in survivors)
    tied = [c.hf_repo for c in survivors if c.final_grade == top_grade]
    best = tied[0] if len(tied) == 1 else break_tie(tied, draw_ledger_hash)
    if len(tied) > 1:
        rationale["tie_break"] = {
            "tied": sorted(tied),
            "draw_ledger_hash": draw_ledger_hash,
            "picked": best,
        }

    if not incumbent_healthy:
        rationale["reason"] = (
            "The incumbent was mechanically disqualified, so the margin does "
            "not protect it: the highest-graded surviving challenger wins "
            "outright."
        )
        return DecisionResult(DECISION_CHALLENGER_REPLACES, best, rationale)

    difference = top_grade - incumbent.final_grade
    rationale["margin_arithmetic"] = {
        "best_challenger": best,
        "challenger_grade": str(top_grade),
        "incumbent_grade": str(incumbent.final_grade),
        "difference": str(difference),
    }
    if difference >= margin:
        rationale["reason"] = (
            f"{best} beats the incumbent by {difference} points, meeting the "
            f"frozen {margin}-point replacement margin."
        )
        return DecisionResult(DECISION_CHALLENGER_REPLACES, best, rationale)

    lead = (
        f"leads by {difference}"
        if difference >= 0
        else f"trails by {abs(difference)}"
    )
    rationale["reason"] = (
        f"The best challenger {lead} points, below the frozen "
        f"{margin}-point replacement margin: the incumbent keeps its seat."
    )
    return DecisionResult(DECISION_INCUMBENT_RETAINED, incumbent_hf_repo, rationale)


def _load_round(conn, round_id: int) -> dict[str, Any]:
    columns = ("decision", "winner_hf_repo", "judge_hf_repo", "draw_ledger_hash")
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT {', '.join(columns)} FROM governance_rounds WHERE id = %s",
        (round_id,),
    )
    row = cursor.fetchone()
    cursor.close()
    conn.rollback()
    if row is None:
        raise DecisionError(f"Round id {round_id} does not exist")
    return dict(zip(columns, row))


def _load_candidates(conn, round_id: int) -> list[ExaminedCandidate]:
    # Through the round's exam links, not exam_runs.round_id: a reused
    # terminal run keeps the round that paid for it, but it answers for
    # this round through governance_round_exam_runs.
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT r.hf_repo, r.revision, r.verdict, r.final_grade
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
    seen: set[str] = set()
    candidates = []
    for hf_repo, revision, verdict, final_grade in rows:
        if verdict is None:
            # No verdict means disqualification never evaluated this run;
            # deciding over it would turn missing evidence into permanent
            # blocklist punishment. Fail closed like every other gap.
            raise DecisionError(
                f"Exam run for {hf_repo} has no verdict — the round is "
                "not fully verdicted"
            )
        if hf_repo in seen:
            raise DecisionError(
                f"Round carries more than one exam run for {hf_repo} — "
                "the evidence is ambiguous"
            )
        seen.add(hf_repo)
        candidates.append(
            ExaminedCandidate(
                hf_repo=hf_repo,
                revision=revision,
                verdict=verdict,
                final_grade=final_grade,
            )
        )
    return candidates


def _book_blocklist(conn, round_number: int, candidates: list[ExaminedCandidate]) -> list[dict]:
    """Book every disqualified revision, append-only like the file sync.

    Booked at the decision, which today is the round's point of no
    return. If a post-decision abandon path ever lands (the G.6
    divergence halt), it must un-book these rows — the methodology says
    an abandoned round adds no candidate entries.
    """
    entries = []
    cursor = conn.cursor()
    for candidate in candidates:
        if candidate.verdict == VERDICT_SURVIVED:
            continue
        reason = (
            f"Mechanically disqualified in governance round {round_number}"
        )
        round_reference = f"governance round {round_number}"
        cursor.execute(
            """
            INSERT INTO blocklist (hf_repo, revision, reason, round_reference)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (hf_repo, revision) DO NOTHING
            """,
            (candidate.hf_repo, candidate.revision, reason, round_reference),
        )
        entries.append(
            {
                "hf_repo": candidate.hf_repo,
                "revision": candidate.revision,
                "reason": reason,
            }
        )
    cursor.close()
    return entries


def decide_round(conn, round_id: int, round_number: int) -> dict[str, Any]:
    """Compute and persist the round's verdict from its persisted evidence.

    Re-run-safe: a round that already carries a decision returns it
    unchanged, and the verdict itself is deterministic anyway. Raises
    DecisionError when the evidence is incomplete; the orchestrator's
    publication discipline retries on the next tick.
    """
    round_state = _load_round(conn, round_id)
    if round_state["decision"] is not None:
        logger.info(
            "Round %d already decided (%s) — keeping the verdict",
            round_number,
            round_state["decision"],
        )
        return {
            "decision": round_state["decision"],
            "winner_hf_repo": round_state["winner_hf_repo"],
        }
    if round_state["draw_ledger_hash"] is None:
        raise DecisionError(
            f"Round id {round_id} has no drawing-ledger hash to break ties with"
        )

    candidates_file = get_package_file(conn, round_number, CANDIDATES_FILE_PATH)
    parameters = get_package_file(conn, round_number, PARAMETERS_FILE_PATH)
    conn.rollback()
    if candidates_file is None or parameters is None:
        raise DecisionError(
            f"Round {round_number} has no frozen pool or parameters artifacts"
        )
    incumbent_hf_repo = candidates_file["incumbent"]["profile"]["hf_repo"]
    margin = Decimal(str(parameters["incumbent_margin_points"]))

    candidates = _load_candidates(conn, round_id)
    if not candidates:
        raise DecisionError(
            f"Round {round_number} has no examined candidates to decide over"
        )

    result = decide(
        incumbent_hf_repo=incumbent_hf_repo,
        candidates=candidates,
        margin=margin,
        draw_ledger_hash=round_state["draw_ledger_hash"],
        judge_hf_repo=round_state["judge_hf_repo"],
    )
    blocklist_entries = _book_blocklist(conn, round_number, candidates)
    rationale = dict(result.rationale)
    if blocklist_entries:
        rationale["blocklist_entries"] = blocklist_entries

    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE governance_rounds
        SET decision = %s, winner_hf_repo = %s, decision_rationale = %s,
            decided_at = %s
        WHERE id = %s
        """,
        (
            result.decision,
            result.winner_hf_repo,
            json.dumps(rationale, sort_keys=True),
            datetime.now(timezone.utc),
            round_id,
        ),
    )
    conn.commit()
    cursor.close()

    logger.info(
        "Round %d decided: %s (winner %s)",
        round_number,
        result.decision,
        result.winner_hf_repo,
    )
    return {"decision": result.decision, "winner_hf_repo": result.winner_hf_repo}
