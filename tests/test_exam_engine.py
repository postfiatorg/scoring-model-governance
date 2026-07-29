"""The exam execution engine: the loop, hashing, measurements, and failures."""

import hashlib
import json

import httpx
import pytest

from governance_service.scoring import canonical_json_hash
from governance_service.services import candidate_profiles, corpus
from governance_service.services.exam_engine import (
    ExamEngine,
    ExamItem,
    get_run_outputs,
    load_exam_items,
    model_response_hash,
)
from governance_service.services.runtime_manager import (
    CandidateDeployError,
    CandidateDeployFailure,
    EnsureResult,
    InfrastructureError,
    candidate_app_name,
)

INCUMBENT = candidate_profiles.CURRENT_POOL_PROFILES["Qwen/Qwen3.6-27B-FP8"]
CHALLENGER = candidate_profiles.CURRENT_POOL_PROFILES["Qwen/Qwen3-32B-FP8"]

ITEMS = [
    ExamItem(
        item_id=f"edge:{name}",
        request={
            "model": "Qwen/Qwen3.6-27B-FP8",
            "messages": [{"role": "user", "content": f"question {name}"}],
            "temperature": 0,
            "max_tokens": 64,
            "response_format": {"type": "json_object"},
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        },
    )
    for name in ("alpha", "beta")
]


class StubRuntime:
    """A runtime manager stand-in: deploys instantly, fails on demand."""

    def __init__(self, *, warmup_error: Exception | None = None,
                 ensure_error: Exception | None = None):
        self.warmup_error = warmup_error
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
        if self.warmup_error is not None:
            raise self.warmup_error


class FakeEndpoint:
    """OpenAI-shaped responses; deterministic unless told otherwise."""

    def __init__(self, *, deterministic: bool = True, empty: bool = False,
                 status_plan: list[int] | None = None, fail_from_call: int | None = None):
        self.deterministic = deterministic
        self.empty = empty
        self.status_plan = status_plan or []
        self.fail_from_call = fail_from_call
        self.healed = False
        self.calls: list[dict] = []

    def post(self, url, *, json=None, headers=None, timeout=None) -> httpx.Response:
        self.calls.append({"url": url, "body": json})
        if (
            self.fail_from_call is not None
            and not self.healed
            and len(self.calls) >= self.fail_from_call
        ):
            raise httpx.ConnectError("connection dropped")
        if self.status_plan:
            status = self.status_plan.pop(0)
            if status != 200:
                return httpx.Response(status, text=f"upstream said {status}")
        if self.empty:
            return httpx.Response(200, json={"choices": []})
        question = json["messages"][-1]["content"]
        suffix = "" if self.deterministic else f" run{len(self.calls)}"
        return httpx.Response(
            200,
            json={
                "id": f"chatcmpl-{len(self.calls)}",
                "created": 1000000 + len(self.calls),
                "choices": [
                    {"message": {"role": "assistant", "content": f"answer to {question}{suffix}"}}
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            },
        )


def _engine(runtime: StubRuntime, endpoint: FakeEndpoint) -> ExamEngine:
    return ExamEngine(runtime, http_post=endpoint.post, sleep=lambda seconds: None)


def test_model_response_hash_matches_the_network_rule():
    text = '{"v001": {"score": 91}}'
    expected = hashlib.sha256(
        json.dumps({"raw_response": text}, sort_keys=True, separators=(",", ":"), default=str)
        .encode("utf-8")
    ).hexdigest()
    assert model_response_hash(text) == expected


def test_three_runs_per_item_with_identical_hashes(db):
    endpoint = FakeEndpoint()
    engine = _engine(StubRuntime(), endpoint)
    run_ids = engine.examine(db, [INCUMBENT], ITEMS)

    assert len(run_ids) == 1
    outputs = get_run_outputs(db, run_ids[0])
    assert len(outputs) == 6
    by_item: dict[str, set[str]] = {}
    for row in outputs:
        by_item.setdefault(row["item_id"], set()).add(row["response_hash"])
        assert row["latency_seconds"] >= 0
        assert row["prompt_tokens"] == 11
        assert row["completion_tokens"] == 7
    assert all(len(hashes) == 1 for hashes in by_item.values())

    sent_models = {call["body"]["model"] for call in endpoint.calls}
    assert sent_models == {INCUMBENT.hf_repo}
    assert all(
        call["body"]["chat_template_kwargs"] == {"enable_thinking": False}
        for call in endpoint.calls
    )

    cursor = db.cursor()
    cursor.execute("SELECT status FROM exam_runs WHERE id = %s", (run_ids[0],))
    assert cursor.fetchone()[0] == "COMPLETED"
    cursor.close()


def test_sequential_multi_candidate(db):
    endpoint = FakeEndpoint()
    runtime = StubRuntime()
    run_ids = _engine(runtime, endpoint).examine(db, [INCUMBENT, CHALLENGER], ITEMS)
    assert len(run_ids) == 2
    assert runtime.ensured == [INCUMBENT.hf_repo, CHALLENGER.hf_repo]
    assert len(endpoint.calls) == 12
    assert {call["body"]["model"] for call in endpoint.calls[6:]} == {CHALLENGER.hf_repo}


def test_candidate_failure_records_evidence_and_continues(db):
    failure = CandidateDeployFailure(
        hf_repo=INCUMBENT.hf_repo,
        revision=INCUMBENT.revision,
        profile_hash=INCUMBENT.content_hash(),
        stage="serve",
        detail="did not become healthy",
    )
    endpoint = FakeEndpoint()

    class OneBadRuntime(StubRuntime):
        def verify_warmup(self, profile, result, **kwargs):
            if profile.hf_repo == INCUMBENT.hf_repo:
                raise CandidateDeployError(failure)

    run_ids = _engine(OneBadRuntime(), endpoint).examine(db, [INCUMBENT, CHALLENGER], ITEMS)

    cursor = db.cursor()
    cursor.execute(
        "SELECT status, candidate_failure FROM exam_runs WHERE id = %s", (run_ids[0],)
    )
    status, evidence = cursor.fetchone()
    assert status == "CANDIDATE_FAILED"
    assert evidence["hf_repo"] == INCUMBENT.hf_repo
    assert evidence["stage"] == "serve"
    cursor.execute("SELECT status FROM exam_runs WHERE id = %s", (run_ids[1],))
    assert cursor.fetchone()[0] == "COMPLETED"
    cursor.close()


def test_infrastructure_failure_aborts_without_outputs(db):
    endpoint = FakeEndpoint()
    runtime = StubRuntime(ensure_error=InfrastructureError("quota exhausted"))
    with pytest.raises(InfrastructureError):
        _engine(runtime, endpoint).examine(db, [INCUMBENT], ITEMS)
    cursor = db.cursor()
    cursor.execute("SELECT status, error_message FROM exam_runs")
    status, error_message = cursor.fetchone()
    assert status == "FAILED"
    assert "quota" in error_message
    cursor.execute("SELECT COUNT(*) FROM exam_outputs")
    assert cursor.fetchone()[0] == 0
    cursor.close()


def test_missing_message_content_is_candidate_evidence(db):
    endpoint = FakeEndpoint(empty=True)
    run_ids = _engine(StubRuntime(), endpoint).examine(db, [INCUMBENT], ITEMS)
    cursor = db.cursor()
    cursor.execute(
        "SELECT status, candidate_failure FROM exam_runs WHERE id = %s", (run_ids[0],)
    )
    status, evidence = cursor.fetchone()
    assert status == "CANDIDATE_FAILED"
    assert "without message content" in evidence["detail"]
    cursor.close()


def test_interrupted_run_resumes_without_repaying_inferences(db):
    endpoint = FakeEndpoint()
    engine = _engine(StubRuntime(), endpoint)
    first = engine.examine(db, [INCUMBENT], ITEMS)[0]
    calls_after_first = len(endpoint.calls)

    # Simulate a crash mid-run: reopen the run and drop one stored attempt.
    cursor = db.cursor()
    cursor.execute("UPDATE exam_runs SET status = 'RUNNING' WHERE id = %s", (first,))
    cursor.execute(
        "DELETE FROM exam_outputs WHERE run_id = %s AND item_id = %s AND attempt = 3",
        (first, ITEMS[1].item_id),
    )
    cursor.close()
    db.commit()

    resumed = engine.examine(db, [INCUMBENT], ITEMS)[0]
    assert resumed == first
    assert len(endpoint.calls) == calls_after_first + 1
    assert len(get_run_outputs(db, first)) == 6


def test_nondeterminism_is_recorded_faithfully(db):
    endpoint = FakeEndpoint(deterministic=False)
    run_ids = _engine(StubRuntime(), endpoint).examine(db, [INCUMBENT], ITEMS)
    outputs = get_run_outputs(db, run_ids[0])
    hashes = {row["response_hash"] for row in outputs if row["item_id"] == ITEMS[0].item_id}
    assert len(hashes) == 3


def test_completed_run_is_idempotent_per_profile_and_corpus(db):
    endpoint = FakeEndpoint()
    engine = _engine(StubRuntime(), endpoint)
    first = engine.examine(db, [INCUMBENT], ITEMS)[0]
    calls = len(endpoint.calls)
    second = engine.examine(db, [INCUMBENT], ITEMS)[0]
    assert second == first
    assert len(endpoint.calls) == calls


def test_infrastructure_abort_mid_exam_resumes_without_repaying(db):
    endpoint = FakeEndpoint(fail_from_call=4)
    engine = _engine(StubRuntime(), endpoint)
    from governance_service.services.runtime_manager import InfrastructureError

    with pytest.raises(InfrastructureError):
        engine.examine(db, [INCUMBENT], ITEMS)
    cursor = db.cursor()
    cursor.execute("SELECT id, status FROM exam_runs")
    run_id, status = cursor.fetchone()
    cursor.close()
    assert status == "FAILED"
    stored_before = len(get_run_outputs(db, run_id))
    assert stored_before == 3

    endpoint.healed = True
    successful_before = 3
    resumed = engine.examine(db, [INCUMBENT], ITEMS)[0]
    assert resumed == run_id
    assert len(get_run_outputs(db, run_id)) == 6
    new_successful_calls = len(
        [c for c in endpoint.calls]
    )
    # only the three missing inferences were re-paid after healing
    assert len(get_run_outputs(db, run_id)) - stored_before == 3


def test_retryable_status_is_retried(db):
    endpoint = FakeEndpoint(status_plan=[503])
    slept = []
    engine = ExamEngine(
        StubRuntime(), http_post=endpoint.post, sleep=lambda s: slept.append(s)
    )
    run_id = engine.examine(db, [INCUMBENT], ITEMS)[0]
    assert len(get_run_outputs(db, run_id)) == 6
    assert slept, "a retry backoff sleep must have happened"


def test_deterministic_rejection_is_candidate_evidence(db):
    endpoint = FakeEndpoint(status_plan=[400])
    run_id = _engine(StubRuntime(), endpoint).examine(db, [INCUMBENT], ITEMS)[0]
    cursor = db.cursor()
    cursor.execute(
        "SELECT status, candidate_failure FROM exam_runs WHERE id = %s", (run_id,)
    )
    status, evidence = cursor.fetchone()
    cursor.close()
    assert status == "CANDIDATE_FAILED"
    assert "HTTP 400" in evidence["detail"]
    assert "upstream said 400" in evidence["detail"]


def _corpus_result_with_one_round() -> tuple[corpus.CorpusResult, dict, dict]:
    request = {"model": "test", "messages": [], "extra_body": {}}
    files = {
        "inputs/model_request.json": request,
        "inputs/validator_map.json": {"v001": {"master_key": "nH..."}},
    }
    bundle = {
        "bundle_version": 1,
        "package_kind": "input",
        "round_kind": "normal",
        "network": "devnet",
        "round_number": 42,
        "input_frozen_at": "2026-07-01T00:00:00+00:00",
        "file_hashes": {path: canonical_json_hash(body) for path, body in files.items()},
    }
    result = corpus.CorpusResult(
        manifest={
            "historical": [
                {
                    "round_number": 42,
                    "input_package_cid": "QmTest42",
                    "input_package_hash": canonical_json_hash(bundle),
                    "input_frozen_at": bundle["input_frozen_at"],
                    "verified_file_count": 1,
                }
            ]
        },
        constructed={"alpha_case": {"model": "test", "messages": [], "extra_body": {}}},
    )
    return result, bundle, files


def _corpus_client(bundle: dict, files: dict) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/input/bundle.json"):
            return httpx.Response(200, json=bundle)
        for file_path, body in files.items():
            if path.endswith(f"/input/{file_path}"):
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": "missing"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_load_exam_items_verifies_and_orders():
    result, bundle, files = _corpus_result_with_one_round()
    with _corpus_client(bundle, files) as client:
        items = load_exam_items(client, result)
    assert [item.item_id for item in items] == ["round-42", "edge:alpha_case"]
    assert items[0].request == files["inputs/model_request.json"]


def test_load_exam_items_rejects_tampered_request():
    result, bundle, files = _corpus_result_with_one_round()
    files["inputs/model_request.json"] = {"model": "tampered", "messages": [], "extra_body": {}}
    with _corpus_client(bundle, files) as client:
        with pytest.raises(corpus.CorpusVerificationError, match="model request hash"):
            load_exam_items(client, result)


def test_load_exam_items_rejects_missing_request_entry():
    result, bundle, files = _corpus_result_with_one_round()
    del bundle["file_hashes"]["inputs/model_request.json"]
    result.manifest["historical"][0]["input_package_hash"] = canonical_json_hash(bundle)
    with _corpus_client(bundle, files) as client:
        with pytest.raises(corpus.CorpusVerificationError, match="no inputs/model_request"):
            load_exam_items(client, result)
