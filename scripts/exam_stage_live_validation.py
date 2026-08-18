"""Small live validation of the wired exam stage on the real workspace.

Fabricates one governance round in the local development database with a
genuinely persisted frozen package — a two-item corpus (the newest
completed scoring round of the configured environment, fetched and
hash-verified live, plus one constructed edge case) and a deliberately
minimal pool: the examined candidate as incumbent and a never-deployed
stand-in challenger fixed as the drawn judge. Then runs the real
``exam_stage.run_exam`` twice: the first pass deploys the candidate on
Modal, examines the frozen corpus, links the runs, and persists the
mechanical verdicts; the second pass must reuse every stored inference.
The stage does not re-check freeze eligibility (that is the freeze's
job), which is what makes the one-examinee pool a valid fragment.

    PYTHONPATH=. python scripts/exam_stage_live_validation.py "Qwen/Qwen3.6-27B-FP8" out.json

Requires a Modal CLI login, MODAL_KEY / MODAL_SECRET in the environment,
network access to the environment's scoring service, and the local
development database (docker compose up -d postgres).
"""

import json
import sys
import time
from datetime import datetime, timezone

import httpx

from governance_service.clients import scoring_api
from governance_service.config import settings
from governance_service.database import get_db, init_db_if_needed
from governance_service.scoring import canonical_sha256
from governance_service.services import corpus as corpus_service
from governance_service.services import edge_cases, exam_stage
from governance_service.services.candidate_profiles import CURRENT_POOL_PROFILES
from governance_service.services.exam_engine import get_run_outputs
from governance_service.services.round_package import (
    FrozenPool,
    build_package,
    persist_package,
)
from governance_service.services.runtime_manager import ExamRuntimeManager

VALIDATION_CASE = "all_below_cutoff"
VALIDATION_CID = "unpinned-local-validation"


def _validation_corpus(client: httpx.Client) -> corpus_service.CorpusResult:
    """One real historical round plus one constructed case."""
    rounds = scoring_api.list_rounds(client, limit=corpus_service.ROUNDS_PAGE_LIMIT)
    selected = corpus_service.select_history_rounds(rounds, 1)
    if not selected:
        raise RuntimeError(
            f"{settings.scoring_api_base_url} has no completed round with a "
            "frozen input package"
        )
    historical = [corpus_service.verify_input_package(client, selected[0])]
    constructed = {VALIDATION_CASE: edge_cases.build_all()[VALIDATION_CASE]}

    manifest = {
        "manifest_version": corpus_service.MANIFEST_VERSION,
        "environment": settings.environment,
        "policy": {
            "history_window_requested": 1,
            "history_rounds_found": len(historical),
            "catalogue_version": edge_cases.CATALOGUE_VERSION,
            "note": "exam-stage live validation fragment",
        },
        "historical": [
            {
                "round_number": item.round_number,
                "input_package_cid": item.input_package_cid,
                "input_package_hash": item.input_package_hash,
                "input_frozen_at": item.input_frozen_at,
                "verified_file_count": item.verified_file_count,
            }
            for item in historical
        ],
        "constructed_template": {
            "source_round": edge_cases.TEMPLATE_SOURCE_ROUND,
            "source_cid": edge_cases.TEMPLATE_SOURCE_CID,
        },
        "constructed": [
            {
                "case_id": case_id,
                "catalogue_version": edge_cases.CATALOGUE_VERSION,
                "content_hash": canonical_sha256(request),
            }
            for case_id, request in sorted(constructed.items())
        ],
    }
    return corpus_service.CorpusResult(manifest=manifest, constructed=constructed)


def _seed_round(conn, corpus: corpus_service.CorpusResult, pool: FrozenPool, judge: str) -> tuple[int, int]:
    cursor = conn.cursor()
    cursor.execute("SELECT COALESCE(MAX(round_number), 0) + 1 FROM governance_rounds")
    round_number = cursor.fetchone()[0]
    cursor.execute(
        """
        INSERT INTO governance_rounds
            (round_number, status, trigger_source, announcement_ledger_index,
             judge_hf_repo, draw_ledger_index, draw_ledger_hash)
        VALUES (%s, 'JUDGE_DRAWN', 'manual', 0, %s, 0, %s)
        RETURNING id
        """,
        (round_number, judge, "0" * 64),
    )
    round_id = cursor.fetchone()[0]
    conn.commit()
    cursor.close()

    frozen_at = datetime.now(timezone.utc)
    files, bundle = build_package(round_number, corpus, pool, frozen_at)
    persist_package(conn, round_id, files, bundle, VALIDATION_CID, frozen_at)
    return round_id, round_number


def _abandon_round(conn, round_id: int) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE governance_rounds
        SET status = 'ABANDONED',
            error_message = 'exam-stage live validation round',
            completed_at = NOW()
        WHERE id = %s
        """,
        (round_id,),
    )
    conn.commit()
    cursor.close()


def _output_count(conn) -> int:
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM exam_outputs")
    count = cursor.fetchone()[0]
    cursor.close()
    conn.rollback()
    return count


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in CURRENT_POOL_PROFILES:
        known = ", ".join(sorted(CURRENT_POOL_PROFILES))
        print(
            "usage: exam_stage_live_validation.py <hf_repo> <output.json>\n"
            f"known repos: {known}"
        )
        return 2
    hf_repo, output_path = sys.argv[1], sys.argv[2]

    examinee = CURRENT_POOL_PROFILES[hf_repo]
    judge_repo = next(repo for repo in sorted(CURRENT_POOL_PROFILES) if repo != hf_repo)
    pool = FrozenPool(
        refresh_id=0,
        incumbent=examinee,
        challengers=[CURRENT_POOL_PROFILES[judge_repo]],
    )

    init_db_if_needed()
    connection = get_db()
    runtime = ExamRuntimeManager()

    started = time.monotonic()
    document: dict = {
        "examinee": hf_repo,
        "revision": examinee.revision,
        "judge_stand_in": judge_repo,
        "scoring_api": settings.scoring_api_base_url,
    }
    round_id = None
    try:
        with httpx.Client(timeout=settings.http_timeout_seconds) as client:
            corpus = _validation_corpus(client)
        document["corpus"] = {
            "historical_round": corpus.manifest["historical"][0]["round_number"],
            "input_package_cid": corpus.manifest["historical"][0]["input_package_cid"],
            "constructed_case": VALIDATION_CASE,
        }
        round_id, round_number = _seed_round(
            connection, corpus, pool, judge_repo
        )
        document["round"] = {"id": round_id, "round_number": round_number}

        first = exam_stage.run_exam(connection, round_id, round_number)
        document["first_pass"] = first
        outputs_after_first = _output_count(connection)

        second = exam_stage.run_exam(connection, round_id, round_number)
        document["second_pass"] = second
        document["rerun_reused_all_inferences"] = (
            _output_count(connection) == outputs_after_first
        )

        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT l.hf_repo, l.run_id, r.status, r.verdict
            FROM governance_round_exam_runs l
            JOIN exam_runs r ON r.id = l.run_id
            WHERE l.round_id = %s
            """,
            (round_id,),
        )
        links = cursor.fetchall()
        cursor.close()
        document["links"] = [
            {"hf_repo": repo, "run_id": run_id, "status": status, "verdict": verdict}
            for repo, run_id, status, verdict in links
        ]

        document["items"] = {}
        deterministic = True
        for entry in document["links"]:
            for row in get_run_outputs(connection, entry["run_id"]):
                item = document["items"].setdefault(
                    row["item_id"], {"attempts": 0, "response_hashes": set()}
                )
                item["attempts"] += 1
                item["response_hashes"].add(row["response_hash"])
        for item in document["items"].values():
            item["distinct_hashes"] = len(item["response_hashes"])
            item["response_hashes"] = sorted(item["response_hashes"])
            deterministic = deterministic and item["distinct_hashes"] == 1
        document["bit_identical_across_runs"] = deterministic
        document["status"] = "ok"
    except Exception as exc:
        document["status"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            document["torn_down_app"] = runtime.teardown(hf_repo)
        except Exception as exc:
            document["teardown_error"] = str(exc)
        if round_id is not None:
            _abandon_round(connection, round_id)
        document["total_seconds"] = round(time.monotonic() - started, 1)
        connection.close()

    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0 if document.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
