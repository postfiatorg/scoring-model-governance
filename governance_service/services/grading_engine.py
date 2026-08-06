"""Deterministic judge execution: one judge, every grading pair, three runs.

The execution half of the G.4 split's judge: for one drawn judge — the
engine is judge-general; which judge was drawn is round orchestration's
concern — it deploys the judge on its pinned profile with verified
warm-up (the same runtime manager candidates use, because judges are
pool members), builds one grading request per (corpus item, survivor
answer) pair with the frozen derivation, and sends each request
``REPEAT_COUNT`` times through the production scoring pattern. Only the
message content survives the client boundary, stored with the canonical
content hash the exam pipeline uses, so bit-identical repeats are
checkable at the byte level.

Runs are bound to a material identity and are idempotent per
(judge profile, material): a completed run is returned as-is, and an
interrupted or infrastructure-aborted run resumes without re-paying the
inferences it already stored. Failures keep the exam engine's two-sided
taxonomy: infrastructure failures abort the run as retryable; the
judge's own failure to deploy or serve is persisted as the structured
evidence the judge-redraw rule (round orchestration, G.5) consumes.
The post-run mechanical bar — every stored output parses under the
judge defect schema and every pair's repeats carry one identical hash —
is computed by :func:`judge_mechanical_verdict` over stored rows.
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from governance_service.models.runtime_profile import RuntimeProfile
from governance_service.scoring import canonical_json_hash, canonical_sha256
from governance_service.services.exam_engine import (
    REPEAT_COUNT,
    InferenceResult,
    default_http_post,
    model_response_hash,
    run_inference,
)
from governance_service.services.grading import (
    JudgeOutputError,
    build_grading_request,
    parse_judge_output,
)
from governance_service.services.runtime_manager import (
    CandidateDeployError,
    CandidateDeployFailure,
    ExamRuntimeManager,
    InfrastructureError,
    validate_deployable,
)

logger = logging.getLogger(__name__)

RUN_RUNNING = "RUNNING"
RUN_COMPLETED = "COMPLETED"
RUN_JUDGE_FAILED = "JUDGE_FAILED"
RUN_FAILED = "FAILED"

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"

RULE_SCHEMA = "defect_schema_validity"
RULE_DETERMINISM = "repeat_determinism"


@dataclass(frozen=True)
class GradingPair:
    """One grading unit: a corpus item's frozen request and one survivor
    answer's content. The candidate's identity is deliberately absent —
    the pair is addressed by the answer's canonical hash."""

    item_id: str
    exam_request: dict[str, Any]
    answer_content: str

    @property
    def answer_hash(self) -> str:
        return model_response_hash(self.answer_content)


def material_hash(pairs: list[GradingPair]) -> str:
    """The material identity a grading run binds to: item ids, frozen
    request hashes, and answer hashes."""
    return canonical_sha256(
        {
            "pairs": sorted(
                (
                    {
                        "item_id": pair.item_id,
                        "request_hash": canonical_json_hash(pair.exam_request),
                        "answer_hash": pair.answer_hash,
                    }
                    for pair in pairs
                ),
                key=lambda entry: (entry["item_id"], entry["answer_hash"]),
            )
        }
    )


class GradingEngine:
    """Runs one judge through the grading pairs; every boundary is injectable."""

    def __init__(
        self,
        runtime: ExamRuntimeManager | None = None,
        *,
        http_post: Callable[..., httpx.Response] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._runtime = runtime or ExamRuntimeManager()
        self._http_post = http_post or default_http_post
        self._sleep = sleep

    def grade(
        self,
        connection,
        judge: RuntimeProfile,
        pairs: list[GradingPair],
        *,
        repeats: int = REPEAT_COUNT,
        round_id: int | None = None,
    ) -> int:
        """Grade every pair with this judge; returns the run id.

        Idempotent per (judge profile, material): a COMPLETED or
        JUDGE_FAILED run is returned without re-running, and an
        interrupted or infrastructure-aborted run resumes, skipping the
        inferences it already stored. The judge's own failure marks the
        run JUDGE_FAILED with the structured evidence; an infrastructure
        failure marks it FAILED and re-raises for the caller to retry
        once the platform recovers.

        A round_id links the run to its governance round; a reused
        terminal run keeps whatever round produced it.
        """
        if not pairs:
            raise ValueError("Grading requires at least one pair")
        identities = [(pair.item_id, pair.answer_hash) for pair in pairs]
        if len(set(identities)) != len(identities):
            raise ValueError(
                "Grading pairs must be unique per (item_id, answer content)"
            )
        validate_deployable(judge)
        run_id, already_terminal = self._get_or_resume_run(
            connection, judge, material_hash(pairs), round_id
        )
        if already_terminal:
            logger.info(
                "Grading run %d for %s already terminal — reusing stored outputs",
                run_id,
                judge.hf_repo,
            )
            return run_id
        try:
            self._grade_pairs(connection, run_id, judge, pairs, repeats)
        except CandidateDeployError as exc:
            self._finish_run(connection, run_id, RUN_JUDGE_FAILED, judge_failure=exc.failure)
            logger.warning("Judge %s failed grading: %s", judge.hf_repo, exc)
        except InfrastructureError as exc:
            self._finish_run(connection, run_id, RUN_FAILED, error_message=str(exc))
            raise
        return run_id

    def _grade_pairs(
        self,
        connection,
        run_id: int,
        judge: RuntimeProfile,
        pairs: list[GradingPair],
        repeats: int,
    ) -> None:
        ensured = self._runtime.ensure_deployed(judge)
        self._runtime.verify_warmup(judge, ensured)
        existing = self._existing_attempts(connection, run_id)

        for pair in pairs:
            request = build_grading_request(pair.exam_request, pair.answer_content, judge)
            for attempt in range(1, repeats + 1):
                if (pair.item_id, pair.answer_hash, attempt) in existing:
                    continue
                result = run_inference(
                    judge,
                    ensured.endpoint_url,
                    request,
                    http_post=self._http_post,
                    sleep=self._sleep,
                )
                self._store_output(connection, run_id, pair, attempt, result)

        self._finish_run(connection, run_id, RUN_COMPLETED)

    # -- persistence ---------------------------------------------------------

    def _get_or_resume_run(
        self,
        connection,
        judge: RuntimeProfile,
        material: str,
        round_id: int | None = None,
    ) -> tuple[int, bool]:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT id, status FROM grading_runs
            WHERE hf_repo = %s AND profile_hash = %s AND material_hash = %s
            ORDER BY id DESC LIMIT 1
            """,
            (judge.hf_repo, judge.content_hash(), material),
        )
        row = cursor.fetchone()
        if row is not None:
            run_id, status = row
            if status in (RUN_COMPLETED, RUN_JUDGE_FAILED):
                cursor.close()
                return run_id, True
            cursor.execute(
                """
                UPDATE grading_runs
                SET status = %s, error_message = NULL, completed_at = NULL,
                    round_id = COALESCE(round_id, %s)
                WHERE id = %s
                """,
                (RUN_RUNNING, round_id, run_id),
            )
            cursor.close()
            connection.commit()
            return run_id, False

        cursor.execute(
            """
            INSERT INTO grading_runs (hf_repo, revision, profile_hash, material_hash, status, round_id)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (
                judge.hf_repo,
                judge.revision,
                judge.content_hash(),
                material,
                RUN_RUNNING,
                round_id,
            ),
        )
        run_id = cursor.fetchone()[0]
        cursor.close()
        connection.commit()
        return run_id, False

    def _existing_attempts(self, connection, run_id: int) -> set[tuple[str, str, int]]:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT item_id, answer_hash, attempt FROM grading_outputs WHERE run_id = %s",
            (run_id,),
        )
        rows = {tuple(row) for row in cursor.fetchall()}
        cursor.close()
        return rows

    def _store_output(
        self,
        connection,
        run_id: int,
        pair: GradingPair,
        attempt: int,
        result: InferenceResult,
    ) -> None:
        cursor = connection.cursor()
        cursor.execute(
            """
            INSERT INTO grading_outputs (
                run_id, item_id, answer_hash, attempt, response_hash,
                raw_response, latency_seconds, prompt_tokens, completion_tokens
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run_id,
                pair.item_id,
                pair.answer_hash,
                attempt,
                model_response_hash(result.content),
                result.content,
                result.latency_seconds,
                result.prompt_tokens,
                result.completion_tokens,
            ),
        )
        cursor.close()
        connection.commit()

    def _finish_run(
        self,
        connection,
        run_id: int,
        status: str,
        *,
        judge_failure: CandidateDeployFailure | None = None,
        error_message: str | None = None,
    ) -> None:
        cursor = connection.cursor()
        cursor.execute(
            """
            UPDATE grading_runs
            SET status = %s, judge_failure = %s, error_message = %s,
                completed_at = NOW()
            WHERE id = %s
            """,
            (
                status,
                json.dumps(judge_failure.as_dict()) if judge_failure else None,
                error_message,
                run_id,
            ),
        )
        cursor.close()
        connection.commit()


def get_grading_outputs(connection, run_id: int) -> list[dict[str, Any]]:
    """One grading run's stored outputs, ordered for comparison."""
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT item_id, answer_hash, attempt, response_hash, raw_response,
               latency_seconds, prompt_tokens, completion_tokens
        FROM grading_outputs
        WHERE run_id = %s
        ORDER BY item_id, answer_hash, attempt
        """,
        (run_id,),
    )
    outputs = [
        {
            "item_id": row[0],
            "answer_hash": row[1],
            "attempt": row[2],
            "response_hash": row[3],
            "raw_response": row[4],
            "latency_seconds": row[5],
            "prompt_tokens": row[6],
            "completion_tokens": row[7],
        }
        for row in cursor.fetchall()
    ]
    cursor.close()
    return outputs


def judge_mechanical_verdict(
    connection,
    run_id: int,
    pairs: list[GradingPair],
    *,
    repeats: int = REPEAT_COUNT,
) -> dict[str, Any]:
    """The judge's mechanical bar over one run's stored rows.

    Two rules, computed pure and idempotent: every stored output parses
    under the frozen judge defect schema, and every expected pair —
    seeded from the caller's material, never from what happens to be
    stored, so absence must never survive — carries exactly ``repeats``
    attempts with one identical response hash. Only a COMPLETED run is
    judgeable; anything else is refused rather than passed fail-open.
    The verdict is evidence for the redraw rule — acting on it is round
    orchestration's concern, never this layer's.
    """
    cursor = connection.cursor()
    cursor.execute("SELECT status FROM grading_runs WHERE id = %s", (run_id,))
    row = cursor.fetchone()
    cursor.close()
    if row is None:
        raise ValueError(f"Grading run {run_id} does not exist")
    if row[0] != RUN_COMPLETED:
        raise ValueError(
            f"Grading run {run_id} is {row[0]}, not {RUN_COMPLETED} — "
            f"a non-completed run has no mechanical verdict"
        )
    outputs = get_grading_outputs(connection, run_id)

    schema_errors: list[dict[str, Any]] = []
    for output in outputs:
        try:
            parse_judge_output(output["raw_response"])
        except JudgeOutputError as exc:
            schema_errors.append(
                {
                    "item_id": output["item_id"],
                    "answer_hash": output["answer_hash"],
                    "attempt": output["attempt"],
                    "error": str(exc),
                }
            )

    determinism_errors: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {
        (pair.item_id, pair.answer_hash): [] for pair in pairs
    }
    for output in outputs:
        grouped.setdefault((output["item_id"], output["answer_hash"]), []).append(output)
    for (item_id, answer_hash), rows in sorted(grouped.items()):
        hashes = sorted({row["response_hash"] for row in rows})
        if len(rows) != repeats or len(hashes) != 1:
            determinism_errors.append(
                {
                    "item_id": item_id,
                    "answer_hash": answer_hash,
                    "attempts": len(rows),
                    "distinct_hashes": hashes,
                }
            )

    rules = {
        RULE_SCHEMA: {
            "outcome": VERDICT_FAIL if schema_errors else VERDICT_PASS,
            "errors": schema_errors,
        },
        RULE_DETERMINISM: {
            "outcome": VERDICT_FAIL if determinism_errors else VERDICT_PASS,
            "errors": determinism_errors,
        },
    }
    failed = any(rule["outcome"] == VERDICT_FAIL for rule in rules.values())
    return {
        "verdict": VERDICT_FAIL if failed else VERDICT_PASS,
        "rules": rules,
    }
