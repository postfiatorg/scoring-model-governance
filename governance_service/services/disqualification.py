"""Mechanical disqualification: the methodology's three pass/fail rules.

The automatic marking after an exam: pure, deterministic, idempotent
computation over what the exam engine stored — no model, no judgment, no
network; the caller supplies every frozen validator map. The three rules
(docs/Methodology.md §4):

- **parser_validity** — every stored answer parses with the unmodified
  production response parser (vendored, pinned, drift-checked), using the
  corpus item's validator identity map;
- **bit_identical_runs** — all repeat runs of every corpus item carry one
  identical canonical response hash;
- **deployed_and_served** — the candidate deployed and served on its
  pinned profile, already decided by the run's terminal status and the
  structured serve-failure evidence the runtime layer recorded.

The verdict and its per-rule evidence persist on the exam run itself
(the publication-state pattern); recomputing always overwrites with the
identical result. Booking disqualified revisions into the standing
blocklist is round orchestration's responsibility, never this module's.
"""

import json
import logging
from typing import Any

from governance_service.scoring import parse_response
from governance_service.services.edge_cases import EdgeCaseTemplateError, validator_entries
from governance_service.services.exam_engine import (
    RUN_CANDIDATE_FAILED,
    RUN_COMPLETED,
    get_run_outputs,
)

logger = logging.getLogger(__name__)

VERDICT_SURVIVED = "SURVIVED"
VERDICT_DISQUALIFIED = "DISQUALIFIED"

RULE_PARSER = "parser_validity"
RULE_DETERMINISM = "bit_identical_runs"
RULE_DEPLOYABILITY = "deployed_and_served"

RULE_PASSED = "PASSED"
RULE_FAILED = "FAILED"
RULE_NOT_EVALUATED = "NOT_EVALUATED"

MAX_RECORDED_ERRORS = 5

SYNTHETIC_KEY_PREFIX = "SYNTHETIC"


class VerdictError(ValueError):
    """Raised when a run is not in an evaluable (terminal) state."""


def synthetic_validator_map(request: dict[str, Any]) -> dict[str, dict[str, str]]:
    """A deterministic identity map for a constructed edge-case request.

    Constructed rounds have no real validators, so their map derives
    directly from the validator ids embedded in the request — the same
    shape production's ``inputs/validator_map.json`` carries.
    """
    try:
        validators = validator_entries(request)
    except EdgeCaseTemplateError as exc:
        raise VerdictError(str(exc)) from exc
    return {
        entry["validator_id"]: {
            "master_key": f"{SYNTHETIC_KEY_PREFIX}-{entry['validator_id']}",
            "signing_key": f"{SYNTHETIC_KEY_PREFIX}-{entry['validator_id']}-SK",
        }
        for entry in validators
    }


def evaluate_run(
    connection,
    run_id: int,
    validator_maps: dict[str, dict[str, dict[str, str]]],
    *,
    repeats: int,
) -> dict[str, Any]:
    """Apply the three rules to one terminal run and persist the verdict.

    ``validator_maps`` maps every corpus item id to its identity map —
    historical items from their frozen packages, constructed items from
    ``synthetic_validator_map``. Returns the persisted verdict document.
    """
    run = _load_run(connection, run_id)
    outputs = get_run_outputs(connection, run_id)

    deployability = _check_deployability(run)
    if deployability["outcome"] == RULE_FAILED:
        determinism = {"outcome": RULE_NOT_EVALUATED, "failures": []}
        parser = {"outcome": RULE_NOT_EVALUATED, "failures": []}
    else:
        determinism = _check_determinism(outputs, repeats, set(validator_maps))
        parser = _check_parser(outputs, validator_maps)

    rules = {
        RULE_DEPLOYABILITY: deployability,
        RULE_DETERMINISM: determinism,
        RULE_PARSER: parser,
    }
    survived = all(rule["outcome"] == RULE_PASSED for rule in rules.values())
    verdict = VERDICT_SURVIVED if survived else VERDICT_DISQUALIFIED

    evidence = {
        "rules": rules,
        "hf_repo": run["hf_repo"],
        "revision": run["revision"],
        "profile_hash": run["profile_hash"],
        "corpus_hash": run["corpus_hash"],
        "items_evaluated": len({row["item_id"] for row in outputs}),
        "repeats_required": repeats,
    }
    _persist_verdict(connection, run_id, verdict, evidence)
    logger.info("Run %d verdict: %s", run_id, verdict)
    return {"run_id": run_id, "verdict": verdict, **evidence}


def _load_run(connection, run_id: int) -> dict[str, Any]:
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT hf_repo, revision, profile_hash, corpus_hash, status, candidate_failure
        FROM exam_runs WHERE id = %s
        """,
        (run_id,),
    )
    row = cursor.fetchone()
    cursor.close()
    if row is None:
        raise VerdictError(f"Exam run {run_id} does not exist")
    run = {
        "hf_repo": row[0],
        "revision": row[1],
        "profile_hash": row[2],
        "corpus_hash": row[3],
        "status": row[4],
        "candidate_failure": row[5],
    }
    if run["status"] not in (RUN_COMPLETED, RUN_CANDIDATE_FAILED):
        raise VerdictError(
            f"Exam run {run_id} is {run['status']} — only terminal runs are evaluable"
        )
    return run


def _check_deployability(run: dict[str, Any]) -> dict[str, Any]:
    if run["status"] == RUN_COMPLETED:
        return {"outcome": RULE_PASSED, "failures": []}
    return {
        "outcome": RULE_FAILED,
        "failures": [run["candidate_failure"] or {"detail": "run did not complete"}],
    }


def _check_determinism(
    outputs: list[dict[str, Any]], repeats: int, expected_items: set[str]
) -> dict[str, Any]:
    by_item: dict[str, list[str]] = {item_id: [] for item_id in expected_items}
    for row in outputs:
        by_item.setdefault(row["item_id"], []).append(row["response_hash"])

    failures = []
    for item_id, hashes in sorted(by_item.items()):
        distinct = sorted(set(hashes))
        if len(hashes) != repeats or len(distinct) != 1:
            # An expected item with zero stored rows fails here too
            # (attempts_stored 0, no hashes) — absence must never survive.
            failures.append(
                {
                    "item_id": item_id,
                    "attempts_stored": len(hashes),
                    "distinct_hashes": distinct,
                }
            )
    outcome = RULE_PASSED if not failures else RULE_FAILED
    return {"outcome": outcome, "failures": failures}


def _check_parser(
    outputs: list[dict[str, Any]],
    validator_maps: dict[str, dict[str, dict[str, str]]],
) -> dict[str, Any]:
    failures = []
    for row in outputs:
        item_id = row["item_id"]
        if item_id not in validator_maps:
            raise VerdictError(f"No validator map supplied for corpus item {item_id}")
        result = parse_response(row["raw_response"], validator_maps[item_id])
        if not result.complete or result.errors:
            failures.append(
                {
                    "item_id": item_id,
                    "attempt": row["attempt"],
                    "errors": result.errors[:MAX_RECORDED_ERRORS],
                }
            )
    outcome = RULE_PASSED if not failures else RULE_FAILED
    return {"outcome": outcome, "failures": failures}


def _persist_verdict(
    connection, run_id: int, verdict: str, evidence: dict[str, Any]
) -> None:
    cursor = connection.cursor()
    cursor.execute(
        """
        UPDATE exam_runs
        SET verdict = %s, verdict_evidence = %s, verdict_at = NOW()
        WHERE id = %s
        """,
        (verdict, json.dumps(evidence, sort_keys=True), run_id),
    )
    cursor.close()
    connection.commit()
