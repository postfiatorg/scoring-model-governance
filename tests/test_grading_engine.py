"""The grading engine: the loop, resume, verdicts, failures, re-grading."""

import json

import httpx
import pytest

from governance_service.services import candidate_profiles, edge_cases
from governance_service.services.exam_engine import REPEAT_COUNT
from governance_service.services.grading import build_grading_request
from governance_service.services.grading_engine import (
    RULE_DETERMINISM,
    RULE_SCHEMA,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_JUDGE_FAILED,
    VERDICT_FAIL,
    VERDICT_PASS,
    GradingEngine,
    GradingPair,
    get_grading_outputs,
    judge_mechanical_verdict,
    material_hash,
)
from governance_service.services.regrading import RegradeError, regrade_material
from governance_service.services.runtime_manager import (
    CandidateDeployError,
    CandidateDeployFailure,
    EnsureResult,
    InfrastructureError,
    candidate_app_name,
)

JUDGE = candidate_profiles.CURRENT_POOL_PROFILES["Qwen/Qwen3.6-27B-FP8"]

JUDGE_ANSWER = json.dumps(
    {
        "evidence_fidelity": {"outcome": "none_found", "defects": []},
        "network_report_quality": {"outcome": "none_found", "defects": []},
        "subversion": {"outcome": "none_found", "defects": []},
    }
)


def _pairs() -> list[GradingPair]:
    built = edge_cases.build_all()
    return [
        GradingPair(
            item_id=f"edge:{name}",
            exam_request=built[name],
            answer_content=_survivor_answer(built[name]),
        )
        for name in ("all_below_cutoff", "injection_in_evidence")
    ]


def _survivor_answer(request: dict) -> str:
    payload = {
        entry["validator_id"]: {
            "score": 50,
            "consensus": 50,
            "reliability": 50,
            "software": 50,
            "diversity": 50,
            "identity": 50,
            "reasoning": "Degraded agreement dominates; no accountability signals.",
        }
        for entry in edge_cases.validator_entries(request)
    }
    payload["network_report"] = {
        "headline": "Degraded Round",
        "summary": "The candidate set shows degraded agreement throughout.",
        "categories": {
            d: {"tone": "warning", "body": "Degraded across the set."}
            for d in ("consensus", "reliability", "software", "diversity", "identity")
        },
    }
    return json.dumps(payload)


class StubRuntime:
    """A runtime manager stand-in: deploys instantly, fails on demand."""

    def __init__(self, *, ensure_error: Exception | None = None):
        self.ensure_error = ensure_error
        self.ensured: list[str] = []

    def ensure_deployed(self, profile) -> EnsureResult:
        if self.ensure_error is not None:
            raise self.ensure_error
        self.ensured.append(profile.hf_repo)
        app_name = candidate_app_name(profile.hf_repo)
        return EnsureResult(
            app_name=app_name,
            profile_hash=profile.content_hash(),
            reused=False,
            endpoint_url=f"https://test--{app_name}-serve.modal.run",
            profile_url=f"https://test--{app_name}-profile.modal.run",
        )

    def verify_warmup(self, profile, result, **kwargs) -> None:
        return None


class FakeJudgeEndpoint:
    """OpenAI-shaped judge responses; deterministic unless told otherwise."""

    def __init__(self, *, deterministic: bool = True, content: str = JUDGE_ANSWER,
                 fail_from_call: int | None = None, status_plan: list[int] | None = None):
        self.deterministic = deterministic
        self.content = content
        self.fail_from_call = fail_from_call
        self.status_plan = status_plan or []
        self.calls: list[dict] = []

    def post(self, url, *, json=None, headers=None, timeout=None) -> httpx.Response:
        self.calls.append({"url": url, "body": json})
        if self.fail_from_call is not None and len(self.calls) >= self.fail_from_call:
            raise httpx.ConnectError("connection dropped")
        if self.status_plan:
            status = self.status_plan.pop(0)
            if status != 200:
                return httpx.Response(status, text=f"upstream said {status}")
        suffix = "" if self.deterministic else f" run{len(self.calls)}"
        return httpx.Response(
            200,
            json={
                "id": f"chatcmpl-{len(self.calls)}",
                "choices": [
                    {"message": {"role": "assistant", "content": self.content + suffix}}
                ],
                "usage": {"prompt_tokens": 23, "completion_tokens": 9},
            },
        )


def _engine(runtime: StubRuntime, endpoint: FakeJudgeEndpoint) -> GradingEngine:
    return GradingEngine(runtime, http_post=endpoint.post, sleep=lambda seconds: None)


# -- the flow ----------------------------------------------------------------


def test_three_repeats_per_pair_with_identical_hashes(db):
    endpoint = FakeJudgeEndpoint()
    pairs = _pairs()
    run_id = _engine(StubRuntime(), endpoint).grade(db, JUDGE, pairs)

    outputs = get_grading_outputs(db, run_id)
    assert len(outputs) == len(pairs) * REPEAT_COUNT
    by_pair: dict[tuple[str, str], set[str]] = {}
    for row in outputs:
        by_pair.setdefault((row["item_id"], row["answer_hash"]), set()).add(
            row["response_hash"]
        )
        assert row["prompt_tokens"] == 23
        assert row["completion_tokens"] == 9
    assert all(len(hashes) == 1 for hashes in by_pair.values())

    cursor = db.cursor()
    cursor.execute("SELECT status FROM grading_runs WHERE id = %s", (run_id,))
    assert cursor.fetchone()[0] == RUN_COMPLETED
    cursor.close()


def test_requests_are_the_frozen_derivation(db):
    endpoint = FakeJudgeEndpoint()
    pairs = _pairs()
    _engine(StubRuntime(), endpoint).grade(db, JUDGE, pairs)
    expected = build_grading_request(pairs[0].exam_request, pairs[0].answer_content, JUDGE)
    first_body = endpoint.calls[0]["body"]
    assert first_body["messages"] == expected["messages"]
    assert first_body["max_tokens"] == expected["max_tokens"]
    assert first_body["temperature"] == 0


def test_completed_runs_are_idempotent(db):
    endpoint = FakeJudgeEndpoint()
    engine = _engine(StubRuntime(), endpoint)
    pairs = _pairs()
    first = engine.grade(db, JUDGE, pairs)
    calls_after_first = len(endpoint.calls)
    second = engine.grade(db, JUDGE, pairs)
    assert first == second
    assert len(endpoint.calls) == calls_after_first


def test_interrupted_runs_resume_without_repaying(db):
    pairs = _pairs()
    failing = FakeJudgeEndpoint(fail_from_call=3)
    engine = _engine(StubRuntime(), failing)
    with pytest.raises(InfrastructureError):
        engine.grade(db, JUDGE, pairs)

    healed = FakeJudgeEndpoint()
    resumed = _engine(StubRuntime(), healed).grade(db, JUDGE, pairs)
    outputs = get_grading_outputs(db, resumed)
    assert len(outputs) == len(pairs) * REPEAT_COUNT
    # Two inferences were stored before the failure; only the rest re-ran.
    assert len(healed.calls) == len(pairs) * REPEAT_COUNT - 2


def test_material_identity_binds_answers(db):
    pairs = _pairs()
    changed = [
        GradingPair(
            item_id=pairs[0].item_id,
            exam_request=pairs[0].exam_request,
            answer_content=pairs[0].answer_content.replace('"score": 50', '"score": 45', 1),
        ),
        pairs[1],
    ]
    assert material_hash(pairs) != material_hash(changed)


def test_empty_and_duplicate_pairs_are_rejected(db):
    engine = _engine(StubRuntime(), FakeJudgeEndpoint())
    with pytest.raises(ValueError):
        engine.grade(db, JUDGE, [])
    pairs = _pairs()
    with pytest.raises(ValueError):
        engine.grade(db, JUDGE, [pairs[0], pairs[0]])


def test_judge_failed_runs_are_terminal(db):
    failure = CandidateDeployFailure(
        hf_repo=JUDGE.hf_repo,
        revision=JUDGE.revision or "",
        profile_hash=JUDGE.content_hash(),
        stage="deploy",
        detail="image build failed",
    )
    runtime = StubRuntime(ensure_error=CandidateDeployError(failure))
    pairs = _pairs()
    first = _engine(runtime, FakeJudgeEndpoint()).grade(db, JUDGE, pairs)

    healthy = StubRuntime()
    second = _engine(healthy, FakeJudgeEndpoint()).grade(db, JUDGE, pairs)
    assert first == second
    assert healthy.ensured == []


# -- failures ----------------------------------------------------------------


def test_judge_deploy_failure_is_recorded_not_raised(db):
    failure = CandidateDeployFailure(
        hf_repo=JUDGE.hf_repo,
        revision=JUDGE.revision or "",
        profile_hash=JUDGE.content_hash(),
        stage="deploy",
        detail="image build failed",
    )
    runtime = StubRuntime(ensure_error=CandidateDeployError(failure))
    run_id = _engine(runtime, FakeJudgeEndpoint()).grade(db, JUDGE, _pairs())

    cursor = db.cursor()
    cursor.execute("SELECT status, judge_failure FROM grading_runs WHERE id = %s", (run_id,))
    status, judge_failure = cursor.fetchone()
    cursor.close()
    assert status == RUN_JUDGE_FAILED
    assert judge_failure["stage"] == "deploy"


def test_infrastructure_failure_marks_failed_and_reraises(db):
    runtime = StubRuntime(ensure_error=InfrastructureError("quota exhausted"))
    with pytest.raises(InfrastructureError):
        _engine(runtime, FakeJudgeEndpoint()).grade(db, JUDGE, _pairs())

    cursor = db.cursor()
    cursor.execute("SELECT status FROM grading_runs ORDER BY id DESC LIMIT 1")
    assert cursor.fetchone()[0] == RUN_FAILED
    cursor.close()


# -- the mechanical verdict --------------------------------------------------


def test_verdict_passes_on_deterministic_schema_valid_outputs(db):
    pairs = _pairs()
    run_id = _engine(StubRuntime(), FakeJudgeEndpoint()).grade(db, JUDGE, pairs)
    verdict = judge_mechanical_verdict(db, run_id, pairs)
    assert verdict["verdict"] == VERDICT_PASS
    assert verdict["rules"][RULE_SCHEMA]["outcome"] == VERDICT_PASS
    assert verdict["rules"][RULE_DETERMINISM]["outcome"] == VERDICT_PASS


def test_verdict_fails_on_nondeterministic_repeats(db):
    endpoint = FakeJudgeEndpoint(deterministic=False)
    pairs = _pairs()
    run_id = _engine(StubRuntime(), endpoint).grade(db, JUDGE, pairs)
    verdict = judge_mechanical_verdict(db, run_id, pairs)
    assert verdict["verdict"] == VERDICT_FAIL
    assert verdict["rules"][RULE_DETERMINISM]["outcome"] == VERDICT_FAIL
    assert verdict["rules"][RULE_DETERMINISM]["errors"]


def test_verdict_fails_on_schema_invalid_outputs(db):
    endpoint = FakeJudgeEndpoint(content='{"not": "the schema"}')
    pairs = _pairs()
    run_id = _engine(StubRuntime(), endpoint).grade(db, JUDGE, pairs)
    verdict = judge_mechanical_verdict(db, run_id, pairs)
    assert verdict["verdict"] == VERDICT_FAIL
    assert verdict["rules"][RULE_SCHEMA]["outcome"] == VERDICT_FAIL
    assert verdict["rules"][RULE_SCHEMA]["errors"][0]["attempt"] == 1


def test_verdict_refuses_non_completed_runs(db):
    """A fail-open verdict at a governance gate is the wrong failure
    direction: anything but COMPLETED is refused, never passed."""
    failure = CandidateDeployFailure(
        hf_repo=JUDGE.hf_repo,
        revision=JUDGE.revision or "",
        profile_hash=JUDGE.content_hash(),
        stage="deploy",
        detail="image build failed",
    )
    runtime = StubRuntime(ensure_error=CandidateDeployError(failure))
    pairs = _pairs()
    run_id = _engine(runtime, FakeJudgeEndpoint()).grade(db, JUDGE, pairs)
    with pytest.raises(ValueError):
        judge_mechanical_verdict(db, run_id, pairs)
    with pytest.raises(ValueError):
        judge_mechanical_verdict(db, run_id + 1000, pairs)


def test_verdict_fails_pairs_absent_from_storage(db):
    """Expected pairs are seeded from the material, never from what
    happens to be stored — absence must never survive."""
    pairs = _pairs()
    run_id = _engine(StubRuntime(), FakeJudgeEndpoint()).grade(db, JUDGE, pairs)
    cursor = db.cursor()
    cursor.execute(
        "DELETE FROM grading_outputs WHERE run_id = %s AND item_id = %s",
        (run_id, pairs[0].item_id),
    )
    db.commit()
    cursor.close()
    verdict = judge_mechanical_verdict(db, run_id, pairs)
    assert verdict["verdict"] == VERDICT_FAIL
    errors = verdict["rules"][RULE_DETERMINISM]["errors"]
    assert any(e["item_id"] == pairs[0].item_id and e["attempts"] == 0 for e in errors)


# -- offline re-grading ------------------------------------------------------


def test_regrade_material_produces_grades_with_receipts():
    pairs = _pairs()
    material = [
        {
            "item_id": pair.item_id,
            "request": pair.exam_request,
            "answer_content": pair.answer_content,
            "judge_content": JUDGE_ANSWER,
        }
        for pair in pairs
    ]
    results = regrade_material(material)
    assert results["grade_formula_version"] == 1
    assert len(results["items"]) == len(pairs)
    for item in results["items"]:
        assert 0 <= item["grade"] <= 100
        assert item["validator_count"] > 0
        assert "defects" in item
    assert "." in results["final_grade"]


def test_regrade_is_reproducible():
    pairs = _pairs()
    material = [
        {
            "item_id": pairs[0].item_id,
            "request": pairs[0].exam_request,
            "answer_content": pairs[0].answer_content,
            "judge_content": JUDGE_ANSWER,
        }
    ]
    assert regrade_material(material) == regrade_material(material)


def test_regrade_rejects_unparseable_answers():
    pairs = _pairs()
    with pytest.raises(RegradeError):
        regrade_material(
            [
                {
                    "item_id": pairs[0].item_id,
                    "request": pairs[0].exam_request,
                    "answer_content": "not json at all",
                    "judge_content": JUDGE_ANSWER,
                }
            ]
        )


def test_regrade_rejects_empty_material():
    with pytest.raises(RegradeError):
        regrade_material([])


# -- governance round linkage ------------------------------------------------


def _governance_round(db, round_number: int = 1) -> int:
    cursor = db.cursor()
    cursor.execute(
        """
        INSERT INTO governance_rounds (round_number, status, trigger_source)
        VALUES (%s, 'EXAMINED', 'scheduled')
        RETURNING id
        """,
        (round_number,),
    )
    round_id = cursor.fetchone()[0]
    db.commit()
    cursor.close()
    return round_id


def _run_round_id(db, run_id: int) -> int | None:
    cursor = db.cursor()
    cursor.execute("SELECT round_id FROM grading_runs WHERE id = %s", (run_id,))
    round_id = cursor.fetchone()[0]
    cursor.close()
    return round_id


def test_round_link_is_recorded_and_terminal_runs_keep_theirs(db):
    round_id = _governance_round(db)
    run_id = _engine(StubRuntime(), FakeJudgeEndpoint()).grade(
        db, JUDGE, _pairs(), round_id=round_id
    )
    assert _run_round_id(db, run_id) == round_id

    other_round = _governance_round(db, round_number=2)
    reused = _engine(StubRuntime(), FakeJudgeEndpoint()).grade(
        db, JUDGE, _pairs(), round_id=other_round
    )
    assert reused == run_id
    assert _run_round_id(db, run_id) == round_id
