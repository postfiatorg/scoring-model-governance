"""The exam stage: frozen material, examinee selection, links, verdicts."""

import json

import httpx
import pytest

from governance_service.config import settings
from governance_service.scoring import canonical_json_hash
from governance_service.services import corpus as corpus_service
from governance_service.services import edge_cases, exam_stage
from governance_service.services.corpus import CorpusResult
from governance_service.services.disqualification import (
    VERDICT_DISQUALIFIED,
    VERDICT_SURVIVED,
    synthetic_validator_map,
)
from governance_service.services.exam_engine import ExamEngine, REPEAT_COUNT
from governance_service.services.exam_stage import (
    ExamStageError,
    frozen_examinees,
    load_frozen_exam_material,
    run_exam,
)
from governance_service.services.orchestrator import (
    TRIGGER_MANUAL,
    RoundOrchestrator,
    RoundState,
)
from governance_service.services.round_package import (
    CANDIDATES_FILE_PATH,
    CORPUS_MANIFEST_FILE_PATH,
    EDGE_CASES_DIR_PATH,
    PARAMETERS_FILE_PATH,
    build_package,
    persist_package,
)
from governance_service.services.runtime_manager import InfrastructureError
from tests.test_disqualification import _valid_response
from tests.test_exam_engine import StubRuntime
from tests.test_round_package import (
    CHALLENGER_REPOS,
    FROZEN_AT,
    INCUMBENT_REPO,
    _pool,
)

JUDGE = CHALLENGER_REPOS[0]  # google/gemma-4-31B-it
EXAMINED_CHALLENGER = CHALLENGER_REPOS[1]  # Qwen/Qwen3-32B-FP8

HIST_ROUND = 42
_ALL_CASES = edge_cases.build_all()
# Shape-correct production request standing in for a historical round's
# frozen model request; max_tokens marks it distinct from any edge case.
HIST_REQUEST = {**_ALL_CASES[sorted(_ALL_CASES)[0]], "max_tokens": 777}
HIST_IDS = tuple(
    entry["validator_id"] for entry in edge_cases.validator_entries(HIST_REQUEST)
)
HIST_MAP = {
    vid: {"master_key": f"HMK-{vid}", "signing_key": f"HSK-{vid}"}
    for vid in HIST_IDS
}


def _historical_package() -> tuple[dict[str, dict], str]:
    files = {
        corpus_service.MODEL_REQUEST_FILE_PATH: HIST_REQUEST,
        corpus_service.VALIDATOR_MAP_FILE_PATH: HIST_MAP,
    }
    bundle = {
        "package_kind": "input",
        "round_number": HIST_ROUND,
        "network": settings.environment,
        "input_frozen_at": "2026-07-01T00:00:00+00:00",
        "file_hashes": {
            path: canonical_json_hash(content) for path, content in files.items()
        },
    }
    return {**files, "bundle.json": bundle}, canonical_json_hash(bundle)


def _test_corpus() -> CorpusResult:
    _, package_hash = _historical_package()
    manifest = {
        "manifest_version": 1,
        "environment": settings.environment,
        "policy": {"history_window_requested": 12, "history_rounds_found": 1},
        "historical": [
            {
                "round_number": HIST_ROUND,
                "input_package_cid": "QmHist",
                "input_package_hash": package_hash,
                "input_frozen_at": "2026-07-01T00:00:00+00:00",
                "verified_file_count": 2,
            }
        ],
        "constructed": [
            {
                "case_id": case_id,
                "catalogue_version": edge_cases.CATALOGUE_VERSION,
                "content_hash": corpus_service.canonical_sha256(request),
            }
            for case_id, request in sorted(_ALL_CASES.items())
        ],
    }
    return CorpusResult(manifest=manifest, constructed=_ALL_CASES)


def _client_factory():
    """An httpx client serving the fake historical package over HTTPS."""
    package_files, _ = _historical_package()
    prefix = f"/api/scoring/rounds/{HIST_ROUND}/input/"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith(prefix) and path[len(prefix):] in package_files:
            return httpx.Response(200, json=package_files[path[len(prefix):]])
        return httpx.Response(404)

    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


class ValidEndpoint:
    """Deterministic, parser-valid answers derived from each request."""

    def __init__(self, *, empty_for: str | None = None):
        self.calls: list[dict] = []
        self.empty_for = empty_for

    def post(self, url, *, json=None, headers=None, timeout=None) -> httpx.Response:
        self.calls.append(json)
        if self.empty_for is not None and json["model"] == self.empty_for:
            return httpx.Response(200, json={"choices": []})
        ids = tuple(
            entry["validator_id"]
            for entry in edge_cases.validator_entries(json)
        )
        return httpx.Response(
            200,
            json={
                "id": f"chatcmpl-{len(self.calls)}",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": _valid_response(ids),
                        }
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 9},
            },
        )


def _engine(endpoint: ValidEndpoint | None = None, runtime: StubRuntime | None = None) -> ExamEngine:
    return ExamEngine(
        runtime or StubRuntime(),
        http_post=(endpoint or ValidEndpoint()).post,
        sleep=lambda seconds: None,
    )


def _seed_drawn_round(
    db, round_number: int = 1, judge: str | None = JUDGE, with_package: bool = True
) -> int:
    cursor = db.cursor()
    cursor.execute(
        """
        INSERT INTO governance_rounds
            (round_number, status, trigger_source, announcement_ledger_index,
             judge_hf_repo, draw_ledger_index, draw_ledger_hash,
             commit_closes_at)
        VALUES (%s, %s, %s, 5000000, %s, 5000010, %s, NOW() + INTERVAL '1 day')
        RETURNING id
        """,
        (round_number, RoundState.JUDGE_DRAWN.value, TRIGGER_MANUAL, judge, "A" * 64),
    )
    round_id = cursor.fetchone()[0]
    db.commit()
    cursor.close()
    if with_package:
        files, bundle = build_package(round_number, _test_corpus(), _pool(), FROZEN_AT)
        persist_package(db, round_id, files, bundle, "QmPkg", FROZEN_AT)
    return round_id


def _tamper_artifact(db, round_id: int, path: str, content: dict) -> None:
    cursor = db.cursor()
    cursor.execute(
        """
        UPDATE governance_round_artifacts SET content = %s
        WHERE round_id = %s AND path = %s
        """,
        (json.dumps(content), round_id, path),
    )
    assert cursor.rowcount == 1
    db.commit()
    cursor.close()


def _artifact(db, round_id: int, path: str) -> dict:
    cursor = db.cursor()
    cursor.execute(
        "SELECT content FROM governance_round_artifacts WHERE round_id = %s AND path = %s",
        (round_id, path),
    )
    content = cursor.fetchone()[0]
    cursor.close()
    return content


def _links(db, round_id: int) -> dict[str, int]:
    cursor = db.cursor()
    cursor.execute(
        "SELECT hf_repo, run_id FROM governance_round_exam_runs WHERE round_id = %s",
        (round_id,),
    )
    links = dict(cursor.fetchall())
    cursor.close()
    return links


def _runs(db) -> list[dict]:
    cursor = db.cursor()
    cursor.execute(
        """
        SELECT id, hf_repo, status, round_id, verdict
        FROM exam_runs ORDER BY id
        """
    )
    rows = [
        {"id": r[0], "hf_repo": r[1], "status": r[2], "round_id": r[3], "verdict": r[4]}
        for r in cursor.fetchall()
    ]
    cursor.close()
    return rows


class TestFrozenExaminees:
    def _candidates_file(self, db) -> tuple[dict, int]:
        round_id = _seed_drawn_round(db)
        return _artifact(db, round_id, CANDIDATES_FILE_PATH), round_id

    def test_judge_is_excluded_and_incumbent_included(self, db):
        candidates_file, _ = self._candidates_file(db)

        examinees = frozen_examinees(candidates_file, JUDGE)

        assert [p.hf_repo for p in examinees] == sorted(
            [INCUMBENT_REPO, EXAMINED_CHALLENGER]
        )

    def test_incumbent_judge_is_refused(self, db):
        candidates_file, _ = self._candidates_file(db)

        with pytest.raises(ExamStageError, match="never drawn"):
            frozen_examinees(candidates_file, INCUMBENT_REPO)

    def test_unknown_judge_is_refused(self, db):
        candidates_file, _ = self._candidates_file(db)

        with pytest.raises(ExamStageError, match="not a frozen challenger"):
            frozen_examinees(candidates_file, "org/not-in-pool")

    def test_tampered_profile_hash_fails_closed(self, db):
        candidates_file, _ = self._candidates_file(db)
        candidates_file["challengers"][0]["profile_hash"] = "0" * 64

        with pytest.raises(ExamStageError, match="profile_hash"):
            frozen_examinees(candidates_file, JUDGE)


class TestFrozenMaterial:
    def test_items_and_maps_rebuild_from_the_frozen_package(self, db):
        round_id = _seed_drawn_round(db)

        with _client_factory()() as client:
            items, maps = load_frozen_exam_material(db, 1, client)

        expected_ids = [f"round-{HIST_ROUND}"] + [
            f"edge:{case_id}" for case_id in sorted(_ALL_CASES)
        ]
        assert [item.item_id for item in items] == expected_ids
        assert items[0].request == HIST_REQUEST
        assert maps[f"round-{HIST_ROUND}"] == HIST_MAP
        for case_id, request in _ALL_CASES.items():
            item = next(i for i in items if i.item_id == f"edge:{case_id}")
            assert item.request == _artifact(
                db, round_id, f"{EDGE_CASES_DIR_PATH}/{case_id}.json"
            )
            assert maps[f"edge:{case_id}"] == synthetic_validator_map(request)

    def test_tampered_edge_case_fails_closed(self, db):
        round_id = _seed_drawn_round(db)
        case_id = sorted(_ALL_CASES)[0]
        _tamper_artifact(
            db,
            round_id,
            f"{EDGE_CASES_DIR_PATH}/{case_id}.json",
            {"model": "tampered"},
        )

        with _client_factory()() as client:
            with pytest.raises(ExamStageError, match="content hash"):
                load_frozen_exam_material(db, 1, client)

    def test_drifted_historical_package_fails_closed(self, db):
        _seed_drawn_round(db)
        package_files, _ = _historical_package()
        drifted = {
            **package_files,
            corpus_service.MODEL_REQUEST_FILE_PATH: {"model": "drifted"},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path.rsplit("/input/", 1)[-1]
            gateway_path = request.url.path.split("/QmHist/")[-1]
            for candidate in (path, gateway_path):
                if candidate in drifted:
                    return httpx.Response(200, json=drifted[candidate])
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(
            corpus_service.CorpusVerificationError, match="hash mismatch"
        ):
            load_frozen_exam_material(db, 1, client)
        client.close()


class TestRunExam:
    def test_examines_the_frozen_pool_minus_the_judge(self, db):
        round_id = _seed_drawn_round(db)
        endpoint = ValidEndpoint()

        result = run_exam(
            db,
            round_id,
            1,
            engine=_engine(endpoint),
            client_factory=_client_factory(),
        )

        assert result["examinees"] == 2
        assert result["survived"] == 2
        assert result["verdicts"] == {
            INCUMBENT_REPO: VERDICT_SURVIVED,
            EXAMINED_CHALLENGER: VERDICT_SURVIVED,
        }
        runs = _runs(db)
        assert {run["hf_repo"] for run in runs} == {
            INCUMBENT_REPO,
            EXAMINED_CHALLENGER,
        }
        assert all(run["round_id"] == round_id for run in runs)
        assert all(run["verdict"] == VERDICT_SURVIVED for run in runs)
        items = 1 + len(_ALL_CASES)
        assert len(endpoint.calls) == 2 * items * REPEAT_COUNT
        assert _links(db, round_id) == {
            run["hf_repo"]: run["id"] for run in runs
        }

    def test_rerun_reuses_stored_runs_without_new_inference(self, db):
        round_id = _seed_drawn_round(db)
        run_exam(
            db, round_id, 1,
            engine=_engine(ValidEndpoint()),
            client_factory=_client_factory(),
        )
        first_links = _links(db, round_id)

        second_endpoint = ValidEndpoint()
        run_exam(
            db, round_id, 1,
            engine=_engine(second_endpoint),
            client_factory=_client_factory(),
        )

        assert second_endpoint.calls == []
        assert _links(db, round_id) == first_links
        assert len(_runs(db)) == 2

    def test_reused_run_from_an_earlier_round_is_linked_not_reattributed(self, db):
        first_round = _seed_drawn_round(db, round_number=1)
        run_exam(
            db, first_round, 1,
            engine=_engine(ValidEndpoint()),
            client_factory=_client_factory(),
        )
        cursor = db.cursor()
        cursor.execute(
            "UPDATE governance_rounds SET status = %s WHERE id = %s",
            (RoundState.FAILED.value, first_round),
        )
        db.commit()
        cursor.close()

        cursor = db.cursor()
        cursor.execute("SELECT id, verdict_at FROM exam_runs ORDER BY id")
        first_verdicted_at = dict(cursor.fetchall())
        cursor.close()

        second_round = _seed_drawn_round(db, round_number=2)
        endpoint = ValidEndpoint()
        result = run_exam(
            db, second_round, 2,
            engine=_engine(endpoint),
            client_factory=_client_factory(),
        )

        assert endpoint.calls == []
        assert result["survived"] == 2
        assert _links(db, second_round) == _links(db, first_round)
        # Attribution never moves: the runs keep the round that paid.
        assert all(run["round_id"] == first_round for run in _runs(db))
        # And the shared rows are never re-verdicted: the earlier round's
        # record is re-assembled live from them, so its bytes must hold.
        cursor = db.cursor()
        cursor.execute("SELECT id, verdict_at FROM exam_runs ORDER BY id")
        assert dict(cursor.fetchall()) == first_verdicted_at
        cursor.close()

    def test_candidate_failure_is_disqualification_evidence(self, db):
        round_id = _seed_drawn_round(db)
        endpoint = ValidEndpoint(empty_for=EXAMINED_CHALLENGER)

        result = run_exam(
            db, round_id, 1,
            engine=_engine(endpoint),
            client_factory=_client_factory(),
        )

        assert result["survived"] == 1
        assert result["verdicts"][EXAMINED_CHALLENGER] == VERDICT_DISQUALIFIED
        failed = next(
            run for run in _runs(db) if run["hf_repo"] == EXAMINED_CHALLENGER
        )
        assert failed["status"] == "CANDIDATE_FAILED"
        assert failed["verdict"] == VERDICT_DISQUALIFIED
        assert EXAMINED_CHALLENGER in _links(db, round_id)

    def test_infrastructure_failure_propagates_without_links(self, db):
        round_id = _seed_drawn_round(db)
        engine = ExamEngine(
            StubRuntime(ensure_error=InfrastructureError("quota exhausted")),
            http_post=ValidEndpoint().post,
            sleep=lambda seconds: None,
        )

        with pytest.raises(InfrastructureError):
            run_exam(
                db, round_id, 1,
                engine=engine,
                client_factory=_client_factory(),
            )

        assert _links(db, round_id) == {}

    def test_repeat_count_comes_from_the_frozen_parameters(self, db):
        round_id = _seed_drawn_round(db)
        parameters = _artifact(db, round_id, PARAMETERS_FILE_PATH)
        parameters["repeat_count"] = 2
        _tamper_artifact(db, round_id, PARAMETERS_FILE_PATH, parameters)
        endpoint = ValidEndpoint()

        run_exam(
            db, round_id, 1,
            engine=_engine(endpoint),
            client_factory=_client_factory(),
        )

        items = 1 + len(_ALL_CASES)
        assert len(endpoint.calls) == 2 * items * 2
        cursor = db.cursor()
        cursor.execute("SELECT verdict_evidence FROM exam_runs LIMIT 1")
        evidence = cursor.fetchone()[0]
        cursor.close()
        assert evidence["repeats_required"] == 2

    def test_missing_judge_fails_closed(self, db):
        round_id = _seed_drawn_round(db, judge=None)

        with pytest.raises(ExamStageError, match="no drawn judge"):
            run_exam(db, round_id, 1, engine=_engine())

    def test_missing_package_fails_closed(self, db):
        round_id = _seed_drawn_round(db, with_package=False)

        with pytest.raises(ExamStageError, match="pool/candidates.json"):
            run_exam(db, round_id, 1, engine=_engine())

    def test_missing_corpus_manifest_fails_closed(self, db):
        round_id = _seed_drawn_round(db)
        cursor = db.cursor()
        cursor.execute(
            "DELETE FROM governance_round_artifacts WHERE round_id = %s AND path = %s",
            (round_id, CORPUS_MANIFEST_FILE_PATH),
        )
        db.commit()
        cursor.close()

        with pytest.raises(ExamStageError, match="corpus/manifest.json"):
            run_exam(
                db, round_id, 1,
                engine=_engine(),
                client_factory=_client_factory(),
            )


class TestPipelineWiring:
    def test_resumed_round_runs_the_real_exam_stage(self, db, monkeypatch):
        round_id = _seed_drawn_round(db)
        endpoint = ValidEndpoint()
        monkeypatch.setattr(
            exam_stage, "ExamEngine", lambda: _engine(endpoint)
        )
        monkeypatch.setattr(exam_stage, "default_client", _client_factory())
        graded_rounds = []
        monkeypatch.setattr(
            "governance_service.services.grading_stage.run_grading",
            lambda conn, rid, rnum: graded_rounds.append(rid),
        )

        results = RoundOrchestrator().resume_rounds()

        # The exam ran and persisted; the round then went on through the
        # (stubbed) grading stage and parked for its commit window.
        assert len(results) == 1
        assert results[0]["status"] == RoundState.AWAITING_COMMIT_CLOSE.value
        assert graded_rounds == [round_id]
        runs = _runs(db)
        assert {run["hf_repo"] for run in runs} == {
            INCUMBENT_REPO,
            EXAMINED_CHALLENGER,
        }
        assert all(run["verdict"] == VERDICT_SURVIVED for run in runs)
        assert set(_links(db, round_id)) == {INCUMBENT_REPO, EXAMINED_CHALLENGER}
