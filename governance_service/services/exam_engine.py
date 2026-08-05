"""The exam execution engine: one candidate, the whole corpus, three runs each.

This is where the harness pieces become one flow: for each candidate in
the caller's list — the engine is pool-size general; excluding a drawn
judge is round orchestration's concern — it deploys the candidate on its
pinned profile with verified warm-up (the runtime manager), adapts every
corpus item's frozen request to the candidate (the adaptation rule), and
sends each adapted request ``REPEAT_COUNT`` times through the production
scoring pattern: a direct chat-completions request with the production
per-request timeout, from which only the model's message content survives
the client boundary — the response envelope's per-call identifiers never
touch storage or hashes.

Every answer is stored with its canonical content hash, computed by the
model-response rule the scoring pipeline and validator sidecars already
agree on (``canonical_json_hash({"raw_response": content})``), so the
exam's fingerprints and the network's verification fingerprints follow
one rule. Latency and token usage are recorded for publication but never
enter any ranking.

Runs are bound to a corpus identity and are idempotent per
(candidate profile, corpus): a completed run is returned as-is, and an
interrupted or infrastructure-aborted run resumes without re-paying the
inferences it already stored. Failures compose the runtime layer's
taxonomy: infrastructure failures abort the run as retryable without
corrupting stored results; a candidate's own failure to serve is
persisted in the structured evidence shape mechanical disqualification
consumes.
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from governance_service.config import settings
from governance_service.models.runtime_profile import RuntimeProfile
from governance_service.scoring import canonical_json_hash, canonical_sha256
from governance_service.services import corpus as corpus_service
from governance_service.services.request_adaptation import adapt_request
from governance_service.services.runtime_manager import (
    CandidateDeployError,
    CandidateDeployFailure,
    ExamRuntimeManager,
    InfrastructureError,
    proxy_auth_headers,
    validate_deployable,
)

logger = logging.getLogger(__name__)

# The methodology's repeat count for the determinism check; round
# orchestration snapshots the value it froze into each round manifest.
REPEAT_COUNT = 3

# One more transport retry than production's OpenAI client default (2):
# a full-corpus exam is hours long, so a little extra patience is cheap.
INFERENCE_MAX_RETRIES = 2
INFERENCE_RETRY_DELAY_SECONDS = 5

# Transient statuses production's OpenAI client retries internally.
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
# Deterministic request rejections: the candidate cannot serve this frozen
# request, which is candidate evidence, not a platform condition.
CANDIDATE_REJECTION_STATUS_CODES = {400, 422}

EVIDENCE_BODY_EXCERPT_CHARS = 500

RUN_RUNNING = "RUNNING"
RUN_COMPLETED = "COMPLETED"
RUN_CANDIDATE_FAILED = "CANDIDATE_FAILED"
RUN_FAILED = "FAILED"

CHAT_COMPLETIONS_PATH = "/v1/chat/completions"


@dataclass(frozen=True)
class ExamItem:
    """One corpus item: a stable identifier and its frozen request."""

    item_id: str
    request: dict[str, Any]


@dataclass
class InferenceResult:
    """One answer as the production client boundary emits it."""

    content: str
    latency_seconds: float
    prompt_tokens: int | None
    completion_tokens: int | None


def model_response_hash(content: str) -> str:
    """The network's model-response fingerprint rule, reused verbatim.

    Mirrors the foundation's ``_build_raw_response`` document and the
    sidecar's ``build_model_response_document``: the canonical hash of
    ``{"raw_response": <content>}``.
    """
    return canonical_json_hash({"raw_response": content})


def corpus_items_hash(items: list[ExamItem]) -> str:
    """The corpus identity a run binds to: item ids and request hashes."""
    return canonical_sha256(
        {
            "items": sorted(
                (
                    {"item_id": item.item_id, "request_hash": canonical_json_hash(item.request)}
                    for item in items
                ),
                key=lambda entry: entry["item_id"],
            )
        }
    )


def load_exam_items(client: httpx.Client, result: "corpus_service.CorpusResult") -> list[ExamItem]:
    """The corpus as exam items: historical requests fetched and re-verified,
    constructed cases taken from the assembly's own payloads."""
    items = [
        ExamItem(
            item_id=f"round-{entry['round_number']}",
            request=corpus_service.fetch_exam_request(
                client,
                corpus_service.VerifiedHistoricalItem(
                    round_number=entry["round_number"],
                    input_package_cid=entry["input_package_cid"],
                    input_package_hash=entry["input_package_hash"],
                    input_frozen_at=entry["input_frozen_at"],
                    verified_file_count=entry["verified_file_count"],
                ),
            ),
        )
        for entry in result.manifest["historical"]
    ]
    items += [
        ExamItem(item_id=f"edge:{case_id}", request=request)
        for case_id, request in sorted(result.constructed.items())
    ]
    return items


class ExamEngine:
    """Runs candidates through the corpus; every boundary is injectable."""

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

    # -- the flow ------------------------------------------------------------

    def examine(
        self,
        connection,
        profiles: list[RuntimeProfile],
        items: list[ExamItem],
        *,
        repeats: int = REPEAT_COUNT,
    ) -> list[int]:
        """Examine every candidate sequentially; returns the run ids.

        Idempotent per (profile, corpus): a COMPLETED run is returned
        without re-running, and an interrupted or infrastructure-aborted
        run resumes, skipping the inferences it already stored. A
        candidate's own failure marks its run CANDIDATE_FAILED with the
        structured evidence and the exam moves on; an infrastructure
        failure marks the current run FAILED and re-raises — the caller
        retries the whole operation once the platform recovers.
        """
        for profile in profiles:
            validate_deployable(profile)
        corpus_hash = corpus_items_hash(items)

        run_ids = []
        for profile in profiles:
            run_id, already_complete = self._get_or_resume_run(
                connection, profile, corpus_hash
            )
            run_ids.append(run_id)
            if already_complete:
                logger.info(
                    "Run %d for %s already terminal — reusing stored outputs",
                    run_id,
                    profile.hf_repo,
                )
                continue
            try:
                self._examine_candidate(connection, run_id, profile, items, repeats)
            except CandidateDeployError as exc:
                self._finish_run(
                    connection,
                    run_id,
                    RUN_CANDIDATE_FAILED,
                    candidate_failure=exc.failure,
                )
                logger.warning("Candidate %s failed its exam: %s", profile.hf_repo, exc)
            except InfrastructureError as exc:
                self._finish_run(connection, run_id, RUN_FAILED, error_message=str(exc))
                raise
        return run_ids

    def _examine_candidate(
        self,
        connection,
        run_id: int,
        profile: RuntimeProfile,
        items: list[ExamItem],
        repeats: int,
    ) -> None:
        ensured = self._runtime.ensure_deployed(profile)
        self._runtime.verify_warmup(profile, ensured)
        existing = self._existing_attempts(connection, run_id)

        for item in items:
            adapted = adapt_request(item.request, profile)
            for attempt in range(1, repeats + 1):
                if (item.item_id, attempt) in existing:
                    continue
                result = self._infer(profile, ensured.endpoint_url, adapted)
                self._store_output(connection, run_id, item.item_id, attempt, result)

        self._finish_run(connection, run_id, RUN_COMPLETED)

    # -- inference ------------------------------------------------------------

    def _infer(
        self, profile: RuntimeProfile, endpoint_url: str, request: dict[str, Any]
    ) -> InferenceResult:
        return run_inference(
            profile,
            endpoint_url,
            request,
            http_post=self._http_post,
            sleep=self._sleep,
        )

    # -- persistence ------------------------------------------------------------

    def _get_or_resume_run(
        self, connection, profile: RuntimeProfile, corpus_hash: str
    ) -> tuple[int, bool]:
        """(run id, already-terminal) for this profile and corpus.

        COMPLETED and CANDIDATE_FAILED runs are terminal — re-examining
        the same profile on the same frozen corpus would re-pay for
        outputs determinism already fixed. RUNNING (crash) and FAILED
        (infrastructure abort) runs are resumed: flipped back to RUNNING
        so ``_existing_attempts`` skips what they already stored.
        """
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT id, status FROM exam_runs
            WHERE hf_repo = %s AND profile_hash = %s AND corpus_hash = %s
            ORDER BY id DESC LIMIT 1
            """,
            (profile.hf_repo, profile.content_hash(), corpus_hash),
        )
        row = cursor.fetchone()
        if row is not None:
            run_id, status = row
            if status in (RUN_COMPLETED, RUN_CANDIDATE_FAILED):
                cursor.close()
                return run_id, True
            cursor.execute(
                """
                UPDATE exam_runs
                SET status = %s, error_message = NULL, completed_at = NULL
                WHERE id = %s
                """,
                (RUN_RUNNING, run_id),
            )
            cursor.close()
            connection.commit()
            return run_id, False

        cursor.execute(
            """
            INSERT INTO exam_runs (hf_repo, revision, profile_hash, corpus_hash, status)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
            """,
            (
                profile.hf_repo,
                profile.revision,
                profile.content_hash(),
                corpus_hash,
                RUN_RUNNING,
            ),
        )
        run_id = cursor.fetchone()[0]
        cursor.close()
        connection.commit()
        return run_id, False

    def _existing_attempts(self, connection, run_id: int) -> set[tuple[str, int]]:
        """Attempts already stored — a resumed run skips what it paid for."""
        cursor = connection.cursor()
        cursor.execute(
            "SELECT item_id, attempt FROM exam_outputs WHERE run_id = %s", (run_id,)
        )
        rows = {(item_id, attempt) for item_id, attempt in cursor.fetchall()}
        cursor.close()
        return rows

    def _store_output(
        self,
        connection,
        run_id: int,
        item_id: str,
        attempt: int,
        result: InferenceResult,
    ) -> None:
        cursor = connection.cursor()
        cursor.execute(
            """
            INSERT INTO exam_outputs (
                run_id, item_id, attempt, response_hash, raw_response,
                latency_seconds, prompt_tokens, completion_tokens
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run_id,
                item_id,
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
        candidate_failure: CandidateDeployFailure | None = None,
        error_message: str | None = None,
    ) -> None:
        cursor = connection.cursor()
        cursor.execute(
            """
            UPDATE exam_runs
            SET status = %s, candidate_failure = %s, error_message = %s,
                completed_at = NOW()
            WHERE id = %s
            """,
            (
                status,
                json.dumps(candidate_failure.as_dict()) if candidate_failure else None,
                error_message,
                run_id,
            ),
        )
        cursor.close()
        connection.commit()


def get_run_outputs(connection, run_id: int) -> list[dict[str, Any]]:
    """One run's stored outputs, ordered for comparison and publication."""
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT item_id, attempt, response_hash, raw_response, latency_seconds,
               prompt_tokens, completion_tokens
        FROM exam_outputs
        WHERE run_id = %s
        ORDER BY item_id, attempt
        """,
        (run_id,),
    )
    outputs = [
        {
            "item_id": row[0],
            "attempt": row[1],
            "response_hash": row[2],
            "raw_response": row[3],
            "latency_seconds": row[4],
            "prompt_tokens": row[5],
            "completion_tokens": row[6],
        }
        for row in cursor.fetchall()
    ]
    cursor.close()
    return outputs


def run_inference(
    profile: RuntimeProfile,
    endpoint_url: str,
    request: dict[str, Any],
    *,
    http_post: Callable[..., httpx.Response],
    sleep: Callable[[float], None],
) -> InferenceResult:
    """One inference through the production scoring pattern, shared by the
    exam and grading engines: retries on transport and transient statuses,
    a deterministic rejection is the model's own serve failure."""
    url = f"{endpoint_url.rstrip('/')}{CHAT_COMPLETIONS_PATH}"
    body = {
        key: request[key]
        for key in (
            "model",
            "messages",
            "temperature",
            "max_tokens",
            "response_format",
        )
        if key in request
    }
    # extra_body carries the chat-template settings; the OpenAI SDK
    # merges it into the top level, and so does the raw POST body.
    body.update(request.get("extra_body") or {})

    last_error: str | None = None
    for attempt in range(1, INFERENCE_MAX_RETRIES + 2):
        started = time.monotonic()
        try:
            response = http_post(
                url,
                json=body,
                headers=proxy_auth_headers(),
                timeout=settings.exam_request_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            last_error = str(exc)
            if attempt <= INFERENCE_MAX_RETRIES:
                sleep(INFERENCE_RETRY_DELAY_SECONDS * attempt)
            continue
        latency = time.monotonic() - started

        if response.status_code == 200:
            return extract_content(profile, response, latency)
        excerpt = response.text[:EVIDENCE_BODY_EXCERPT_CHARS]
        if response.status_code in RETRYABLE_STATUS_CODES:
            last_error = f"HTTP {response.status_code}: {excerpt}"
            if attempt <= INFERENCE_MAX_RETRIES:
                sleep(INFERENCE_RETRY_DELAY_SECONDS * attempt)
            continue
        if response.status_code in CANDIDATE_REJECTION_STATUS_CODES:
            raise CandidateDeployError(
                CandidateDeployFailure(
                    hf_repo=profile.hf_repo,
                    revision=profile.revision or "",
                    profile_hash=profile.content_hash(),
                    stage="serve",
                    detail=(
                        f"Endpoint rejected the frozen request with HTTP "
                        f"{response.status_code}: {excerpt}"
                    ),
                )
            )
        raise InfrastructureError(
            f"Inference request returned HTTP {response.status_code}: {excerpt}"
        )

    raise InfrastructureError(
        f"Inference request failed after retries: {last_error}"
    )


def extract_content(
    profile: RuntimeProfile, response: httpx.Response, latency: float
) -> InferenceResult:
    """The production client boundary: only the message content survives."""
    try:
        payload = response.json()
        choices = payload.get("choices") or []
        content = choices[0]["message"]["content"] if choices else None
    except (ValueError, KeyError, IndexError, TypeError):
        content = None
    if not isinstance(content, str):
        raise CandidateDeployError(
            CandidateDeployFailure(
                hf_repo=profile.hf_repo,
                revision=profile.revision or "",
                profile_hash=profile.content_hash(),
                stage="serve",
                detail=(
                    "Endpoint returned a response without message content: "
                    f"{response.text[:EVIDENCE_BODY_EXCERPT_CHARS]}"
                ),
            )
        )
    usage = payload.get("usage") or {}
    return InferenceResult(
        content=content,
        latency_seconds=latency,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
    )


_shared_client: httpx.Client | None = None


def default_http_post(url: str, **kwargs) -> httpx.Response:
    """One shared client, no redirects — a POST must never degrade to GET."""
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.Client(follow_redirects=False)
    return _shared_client.post(url, **kwargs)
