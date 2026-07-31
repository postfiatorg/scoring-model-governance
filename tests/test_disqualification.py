"""Mechanical disqualification: all three rules, evidence, idempotency."""

import json

import pytest

from governance_service.services import disqualification, edge_cases
from governance_service.services.disqualification import (
    RULE_DEPLOYABILITY,
    RULE_DETERMINISM,
    RULE_FAILED,
    RULE_NOT_EVALUATED,
    RULE_PARSER,
    RULE_PASSED,
    VERDICT_DISQUALIFIED,
    VERDICT_SURVIVED,
    VerdictError,
    evaluate_run,
    synthetic_validator_map,
)
from governance_service.services.exam_engine import model_response_hash

REPEATS = 3
ITEM_IDS = ("edge:alpha", "edge:beta")
CATEGORIES = ("consensus", "reliability", "software", "diversity", "identity")


def _valid_response(validator_ids: tuple[str, ...]) -> str:
    document = {
        vid: {
            "score": 88,
            "consensus": 97,
            "reliability": 90,
            "software": 85,
            "diversity": 40,
            "identity": 75,
            "reasoning": "Near-perfect agreement; shared provider limits diversity.",
        }
        for vid in validator_ids
    }
    document["network_report"] = {
        "headline": "Strong Selected UNL",
        "summary": "The selected group shows strong agreement and current software.",
        "categories": {
            category: {"tone": "positive", "body": "Solid across the selected set."}
            for category in CATEGORIES
        },
    }
    return json.dumps(document)


VALIDATOR_IDS = ("v001", "v002")
MAPS = {
    item_id: {
        vid: {"master_key": f"MK-{vid}", "signing_key": f"SK-{vid}"}
        for vid in VALIDATOR_IDS
    }
    for item_id in ITEM_IDS
}


def _seed_run(db, status: str, candidate_failure: dict | None = None) -> int:
    cursor = db.cursor()
    cursor.execute(
        """
        INSERT INTO exam_runs (hf_repo, revision, profile_hash, corpus_hash,
                               status, candidate_failure)
        VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
        """,
        (
            "org/model",
            "a" * 40,
            "b" * 64,
            "c" * 64,
            status,
            json.dumps(candidate_failure) if candidate_failure else None,
        ),
    )
    run_id = cursor.fetchone()[0]
    cursor.close()
    db.commit()
    return run_id


def _seed_output(db, run_id: int, item_id: str, attempt: int, raw_response: str) -> None:
    cursor = db.cursor()
    cursor.execute(
        """
        INSERT INTO exam_outputs (run_id, item_id, attempt, response_hash,
                                  raw_response, latency_seconds)
        VALUES (%s, %s, %s, %s, %s, 1.0)
        """,
        (run_id, item_id, attempt, model_response_hash(raw_response), raw_response),
    )
    cursor.close()
    db.commit()


def _seed_complete_run(db, response_for: dict[str, str]) -> int:
    run_id = _seed_run(db, "COMPLETED")
    for item_id in ITEM_IDS:
        for attempt in range(1, REPEATS + 1):
            _seed_output(db, run_id, item_id, attempt, response_for[item_id])
    return run_id


def test_survival_when_all_three_rules_pass(db):
    valid = _valid_response(VALIDATOR_IDS)
    run_id = _seed_complete_run(db, {item: valid for item in ITEM_IDS})
    verdict = evaluate_run(db, run_id, MAPS, repeats=REPEATS)

    assert verdict["verdict"] == VERDICT_SURVIVED
    assert all(
        verdict["rules"][rule]["outcome"] == RULE_PASSED
        for rule in (RULE_DEPLOYABILITY, RULE_DETERMINISM, RULE_PARSER)
    )
    assert verdict["items_evaluated"] == 2
    cursor = db.cursor()
    cursor.execute("SELECT verdict FROM exam_runs WHERE id = %s", (run_id,))
    assert cursor.fetchone()[0] == VERDICT_SURVIVED
    cursor.close()


def test_unparseable_answers_disqualify_with_recorded_errors(db):
    valid = _valid_response(VALIDATOR_IDS)
    run_id = _seed_complete_run(
        db, {ITEM_IDS[0]: valid, ITEM_IDS[1]: "sorry, I cannot answer in JSON"}
    )
    verdict = evaluate_run(db, run_id, MAPS, repeats=REPEATS)

    assert verdict["verdict"] == VERDICT_DISQUALIFIED
    parser = verdict["rules"][RULE_PARSER]
    assert parser["outcome"] == RULE_FAILED
    assert len(parser["failures"]) == REPEATS
    assert parser["failures"][0]["item_id"] == ITEM_IDS[1]
    assert parser["failures"][0]["errors"]
    assert verdict["rules"][RULE_DETERMINISM]["outcome"] == RULE_PASSED


def test_hash_divergent_runs_disqualify(db):
    valid = _valid_response(VALIDATOR_IDS)
    run_id = _seed_run(db, "COMPLETED")
    for attempt in range(1, REPEATS + 1):
        _seed_output(db, run_id, ITEM_IDS[0], attempt, valid)
        divergent = _valid_response(VALIDATOR_IDS)[:-1] + " "  # same-ish, different bytes
        _seed_output(db, run_id, ITEM_IDS[1], attempt, divergent + str(attempt))
    verdict = evaluate_run(db, run_id, MAPS, repeats=REPEATS)

    assert verdict["verdict"] == VERDICT_DISQUALIFIED
    determinism = verdict["rules"][RULE_DETERMINISM]
    assert determinism["outcome"] == RULE_FAILED
    assert determinism["failures"][0]["item_id"] == ITEM_IDS[1]
    assert len(determinism["failures"][0]["distinct_hashes"]) == REPEATS


def test_missing_attempts_fail_determinism(db):
    valid = _valid_response(VALIDATOR_IDS)
    run_id = _seed_run(db, "COMPLETED")
    for item_id in ITEM_IDS:
        for attempt in range(1, REPEATS + 1):
            _seed_output(db, run_id, item_id, attempt, valid)
    cursor = db.cursor()
    cursor.execute(
        "DELETE FROM exam_outputs WHERE run_id = %s AND item_id = %s AND attempt = 3",
        (run_id, ITEM_IDS[0]),
    )
    cursor.close()
    db.commit()

    verdict = evaluate_run(db, run_id, MAPS, repeats=REPEATS)
    determinism = verdict["rules"][RULE_DETERMINISM]
    assert verdict["verdict"] == VERDICT_DISQUALIFIED
    assert determinism["failures"][0] == {
        "item_id": ITEM_IDS[0],
        "attempts_stored": 2,
        "distinct_hashes": [model_response_hash(valid)],
    }


def test_failed_deployment_disqualifies_without_evaluating_the_rest(db):
    failure = {
        "hf_repo": "org/model",
        "revision": "a" * 40,
        "profile_hash": "b" * 64,
        "stage": "serve",
        "detail": "did not become healthy",
    }
    run_id = _seed_run(db, "CANDIDATE_FAILED", candidate_failure=failure)
    verdict = evaluate_run(db, run_id, MAPS, repeats=REPEATS)

    assert verdict["verdict"] == VERDICT_DISQUALIFIED
    assert verdict["rules"][RULE_DEPLOYABILITY]["outcome"] == RULE_FAILED
    assert verdict["rules"][RULE_DEPLOYABILITY]["failures"] == [failure]
    assert verdict["rules"][RULE_DETERMINISM]["outcome"] == RULE_NOT_EVALUATED
    assert verdict["rules"][RULE_PARSER]["outcome"] == RULE_NOT_EVALUATED


def test_recomputation_is_idempotent(db):
    valid = _valid_response(VALIDATOR_IDS)
    run_id = _seed_complete_run(db, {item: valid for item in ITEM_IDS})
    evaluate_run(db, run_id, MAPS, repeats=REPEATS)

    cursor = db.cursor()
    cursor.execute("SELECT verdict, verdict_evidence FROM exam_runs WHERE id = %s", (run_id,))
    first_verdict, first_evidence = cursor.fetchone()
    evaluate_run(db, run_id, MAPS, repeats=REPEATS)
    cursor.execute("SELECT verdict, verdict_evidence FROM exam_runs WHERE id = %s", (run_id,))
    second_verdict, second_evidence = cursor.fetchone()
    cursor.close()

    assert first_verdict == second_verdict == VERDICT_SURVIVED
    assert first_evidence == second_evidence


def test_non_terminal_runs_are_not_evaluable(db):
    run_id = _seed_run(db, "RUNNING")
    with pytest.raises(VerdictError, match="terminal"):
        evaluate_run(db, run_id, MAPS, repeats=REPEATS)


def test_missing_validator_map_is_an_operator_error(db):
    valid = _valid_response(VALIDATOR_IDS)
    run_id = _seed_complete_run(db, {item: valid for item in ITEM_IDS})
    with pytest.raises(VerdictError, match="No validator map"):
        evaluate_run(db, run_id, {ITEM_IDS[0]: MAPS[ITEM_IDS[0]]}, repeats=REPEATS)


def test_synthetic_map_derives_from_the_request_itself():
    request = edge_cases.build_all_below_cutoff()
    mapping = synthetic_validator_map(request)
    assert set(mapping) == {f"v{i:03d}" for i in range(1, 7)}
    assert mapping["v001"] == {
        "master_key": "SYNTHETIC-v001",
        "signing_key": "SYNTHETIC-v001-SK",
    }


def test_expected_item_with_no_stored_outputs_fails_determinism(db):
    valid = _valid_response(VALIDATOR_IDS)
    run_id = _seed_run(db, "COMPLETED")
    for attempt in range(1, REPEATS + 1):
        _seed_output(db, run_id, ITEM_IDS[0], attempt, valid)
    # ITEM_IDS[1] is in the maps (the frozen corpus) but has zero rows.
    verdict = evaluate_run(db, run_id, MAPS, repeats=REPEATS)
    assert verdict["verdict"] == VERDICT_DISQUALIFIED
    determinism = verdict["rules"][RULE_DETERMINISM]
    assert determinism["failures"] == [
        {"item_id": ITEM_IDS[1], "attempts_stored": 0, "distinct_hashes": []}
    ]
