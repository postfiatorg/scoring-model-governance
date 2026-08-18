"""The exam stage: the G.3 exam engine running inside a governance round (G.5.8).

JUDGE_DRAWN -> EXAMINED. Everything the stage consumes comes from the
round's frozen package artifacts — the corpus manifest and the frozen
edge-case payloads, the pool profiles, the repeat count — never live
state: the pool may have refreshed and the scoring history may have
advanced since the freeze, and a verifier reproduces the exam from the
published package alone. Historical corpus items are re-fetched by their
pinned CIDs and verified against the package hashes exactly as corpus
assembly verified them; constructed cases are read from the frozen
artifacts and checked against the manifest's content hashes.

Every pool member except the drawn judge — the incumbent included — sits
the exam through the idempotent exam engine, then mechanical
disqualification persists each run's verdict. The runs answering for this
round are linked in ``governance_round_exam_runs``: exam runs are
reusable across rounds and a reused terminal run keeps the ``round_id``
that paid for it, so the decision and the final record read the round's
evidence through the links, never through ``exam_runs.round_id``.

Failure discipline follows the engine's two-sided taxonomy: a
candidate's own failure is recorded as disqualification evidence and the
exam moves on, while an infrastructure failure propagates and fails the
round — the manual trigger is the recovery path, and when the retried
round's fresh freeze reproduces the identical corpus and pool, the
engine's idempotent runs plus the links reuse every already-paid
inference.
"""

import logging
from typing import Any, Callable

import httpx

from governance_service.config import settings
from governance_service.models.runtime_profile import RuntimeProfile
from governance_service.scoring import canonical_sha256
from governance_service.services import corpus as corpus_service
from governance_service.services.disqualification import (
    VERDICT_SURVIVED,
    evaluate_run,
    synthetic_validator_map,
)
from governance_service.services.exam_engine import (
    ExamEngine,
    ExamItem,
    edge_case_item_id,
    historical_item_id,
)
from governance_service.services.round_package import (
    CANDIDATES_FILE_PATH,
    CORPUS_MANIFEST_FILE_PATH,
    EDGE_CASES_DIR_PATH,
    PARAMETERS_FILE_PATH,
    get_package_file,
)

logger = logging.getLogger(__name__)


class ExamStageError(RuntimeError):
    """The exam cannot run from the round's frozen material."""


def _load_judge(conn, round_id: int) -> str:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT judge_hf_repo FROM governance_rounds WHERE id = %s",
        (round_id,),
    )
    row = cursor.fetchone()
    cursor.close()
    if row is None:
        raise ExamStageError(f"Round id {round_id} does not exist")
    if row[0] is None:
        raise ExamStageError(
            f"Round id {round_id} has no drawn judge — the exam cannot "
            "know who to exclude"
        )
    return row[0]


def _frozen_profile(entry: Any, role: str) -> RuntimeProfile:
    if not isinstance(entry, dict) or "profile" not in entry:
        raise ExamStageError(f"Frozen {role} entry carries no profile")
    profile = RuntimeProfile.model_validate(entry["profile"])
    if entry.get("profile_hash") != profile.content_hash():
        raise ExamStageError(
            f"Frozen {role} profile for {profile.hf_repo} does not match "
            "its recorded profile_hash"
        )
    return profile


def frozen_examinees(
    candidates_file: dict[str, Any], judge_hf_repo: str
) -> list[RuntimeProfile]:
    """The frozen pool minus the drawn judge, the incumbent included.

    The judge must be one of the frozen challengers — the incumbent is
    never drawn — so an unknown judge means the round's persisted draw
    and its frozen pool disagree, and the exam refuses to run.
    """
    incumbent = _frozen_profile(candidates_file.get("incumbent"), "incumbent")
    challengers_entries = candidates_file.get("challengers")
    if not isinstance(challengers_entries, list) or not challengers_entries:
        raise ExamStageError("Frozen package carries no challengers")
    challengers = [
        _frozen_profile(entry, "challenger") for entry in challengers_entries
    ]

    if judge_hf_repo == incumbent.hf_repo:
        raise ExamStageError(
            f"Drawn judge {judge_hf_repo} is the incumbent — the incumbent "
            "is never drawn"
        )
    if judge_hf_repo not in {profile.hf_repo for profile in challengers}:
        raise ExamStageError(
            f"Drawn judge {judge_hf_repo} is not a frozen challenger"
        )

    examinees = [incumbent] + [
        profile for profile in challengers if profile.hf_repo != judge_hf_repo
    ]
    examinees.sort(key=lambda profile: profile.hf_repo)
    return examinees


def load_frozen_exam_material(
    conn,
    round_number: int,
    client: httpx.Client,
) -> tuple[list[ExamItem], dict[str, dict[str, dict[str, str]]]]:
    """The frozen corpus as exam items plus each item's validator map.

    Historical items are re-fetched by the manifest's pinned CIDs and
    verified against the recorded package hashes; their validator maps
    come from the same frozen packages. Constructed cases are read from
    the round's own artifacts, checked against the manifest's content
    hashes, and derive synthetic maps from their own validator ids.
    """
    manifest = get_package_file(conn, round_number, CORPUS_MANIFEST_FILE_PATH)
    if manifest is None:
        raise ExamStageError(
            f"Round {round_number} has no frozen {CORPUS_MANIFEST_FILE_PATH} artifact"
        )

    constructed: list[tuple[str, dict[str, Any]]] = []
    for entry in sorted(
        manifest.get("constructed", []), key=lambda case: case["case_id"]
    ):
        case_id = entry["case_id"]
        request = get_package_file(
            conn, round_number, f"{EDGE_CASES_DIR_PATH}/{case_id}.json"
        )
        if request is None:
            raise ExamStageError(
                f"Round {round_number} has no frozen edge-case artifact "
                f"for {case_id}"
            )
        if canonical_sha256(request) != entry.get("content_hash"):
            raise ExamStageError(
                f"Frozen edge case {case_id} does not match the manifest's "
                "content hash"
            )
        constructed.append((case_id, request))

    # The artifact reads are done; the historical fetches below go over
    # the network, so do not sit on an idle-in-transaction connection.
    conn.rollback()

    items: list[ExamItem] = []
    validator_maps: dict[str, dict[str, dict[str, str]]] = {}

    for entry in manifest.get("historical", []):
        files = corpus_service.fetch_package_files(
            client,
            corpus_service.VerifiedHistoricalItem(
                round_number=entry["round_number"],
                input_package_cid=entry["input_package_cid"],
                input_package_hash=entry["input_package_hash"],
                input_frozen_at=entry["input_frozen_at"],
                verified_file_count=entry["verified_file_count"],
            ),
            (
                corpus_service.MODEL_REQUEST_FILE_PATH,
                corpus_service.VALIDATOR_MAP_FILE_PATH,
            ),
        )
        item_id = historical_item_id(entry["round_number"])
        items.append(
            ExamItem(
                item_id=item_id,
                request=files[corpus_service.MODEL_REQUEST_FILE_PATH],
            )
        )
        validator_maps[item_id] = files[corpus_service.VALIDATOR_MAP_FILE_PATH]

    for case_id, request in constructed:
        item_id = edge_case_item_id(case_id)
        items.append(ExamItem(item_id=item_id, request=request))
        validator_maps[item_id] = synthetic_validator_map(request)

    if not items:
        raise ExamStageError(f"Round {round_number} froze an empty corpus")
    return items, validator_maps


def _link_round_runs(
    conn, round_id: int, links: list[tuple[str, int]]
) -> None:
    cursor = conn.cursor()
    for hf_repo, run_id in links:
        cursor.execute(
            """
            INSERT INTO governance_round_exam_runs (round_id, hf_repo, run_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (round_id, hf_repo) DO UPDATE SET run_id = EXCLUDED.run_id
            """,
            (round_id, hf_repo, run_id),
        )
    cursor.close()
    conn.commit()


def _existing_verdict(conn, run_id: int, repeats: int) -> str | None:
    """A verdict already persisted for this run under the same repeat count.

    A run shared with an earlier round may sit inside that round's
    published record, which is re-assembled live from these rows — so an
    existing verdict is reused, never re-persisted: recomputation is
    deterministic anyway, and rewriting it would change the earlier
    record's bytes (at minimum ``verdict_at``).
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT verdict, verdict_evidence->>'repeats_required'
        FROM exam_runs WHERE id = %s
        """,
        (run_id,),
    )
    row = cursor.fetchone()
    cursor.close()
    conn.rollback()
    if row is None or row[0] is None:
        return None
    if row[1] != str(repeats):
        return None
    return row[0]


def _default_client() -> httpx.Client:
    return httpx.Client(timeout=settings.http_timeout_seconds)


def run_exam(
    conn,
    round_id: int,
    round_number: int,
    *,
    engine: ExamEngine | None = None,
    client_factory: Callable[[], httpx.Client] | None = None,
) -> dict[str, Any]:
    """Examine the round's frozen pool and persist verdicts and links.

    Safe to re-run: the engine reuses terminal runs and resumes
    interrupted ones without re-paying stored inferences, the links
    upsert, and disqualification recomputes identical verdicts.
    """
    judge_hf_repo = _load_judge(conn, round_id)
    candidates_file = get_package_file(conn, round_number, CANDIDATES_FILE_PATH)
    if candidates_file is None:
        raise ExamStageError(
            f"Round {round_number} has no frozen {CANDIDATES_FILE_PATH} artifact"
        )
    parameters = get_package_file(conn, round_number, PARAMETERS_FILE_PATH)
    if parameters is None:
        raise ExamStageError(
            f"Round {round_number} has no frozen {PARAMETERS_FILE_PATH} artifact"
        )
    repeats = parameters.get("repeat_count")
    if not isinstance(repeats, int) or isinstance(repeats, bool) or repeats < 1:
        raise ExamStageError(
            f"Frozen repeat_count {repeats!r} is not a positive integer"
        )

    examinees = frozen_examinees(candidates_file, judge_hf_repo)
    with (client_factory or _default_client)() as client:
        items, validator_maps = load_frozen_exam_material(
            conn, round_number, client
        )

    logger.info(
        "Round %d exam: %d examinees (judge %s excluded), %d corpus items, "
        "%d repeats",
        round_number,
        len(examinees),
        judge_hf_repo,
        len(items),
        repeats,
    )

    exam_engine = engine or ExamEngine()
    run_ids = exam_engine.examine(
        conn, examinees, items, repeats=repeats, round_id=round_id
    )
    _link_round_runs(
        conn,
        round_id,
        [
            (profile.hf_repo, run_id)
            for profile, run_id in zip(examinees, run_ids)
        ],
    )

    verdicts = {}
    for profile, run_id in zip(examinees, run_ids):
        verdict = _existing_verdict(conn, run_id, repeats)
        if verdict is None:
            verdict = evaluate_run(conn, run_id, validator_maps, repeats=repeats)[
                "verdict"
            ]
        verdicts[profile.hf_repo] = verdict

    survived = sum(1 for v in verdicts.values() if v == VERDICT_SURVIVED)
    logger.info(
        "Round %d exam complete: %d of %d examinees survived",
        round_number,
        survived,
        len(examinees),
    )
    return {
        "examinees": len(examinees),
        "survived": survived,
        "verdicts": verdicts,
    }
