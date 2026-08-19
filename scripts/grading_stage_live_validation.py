"""Small live validation of the wired grading stage on the real workspace.

Reuses the exam-stage validation's fabricated-round shape: a local round
with a genuinely persisted frozen package over a two-item corpus (the
newest completed scoring round, fetched and hash-verified live, plus one
constructed edge case) and a minimal pool — the examined candidate as
incumbent and one challenger fixed as the drawn judge. The real
``exam_stage.run_exam`` produces the survivor answers (reusing stored
inference when the corpus is unchanged), then the real
``grading_stage.run_grading`` runs twice: the first pass deploys the
judge on Modal, grades every blinded pair at the frozen repeat count,
holds the judge to its mechanical bar, and persists the survivor's final
grade with receipts on the round's exam link; the second pass must reuse
every stored judge inference. Both apps are torn down at the end.

    PYTHONPATH=. python scripts/grading_stage_live_validation.py \
        "Qwen/Qwen3.6-27B-FP8" "google/gemma-4-31B-it" out.json

Requires a Modal CLI login, MODAL_KEY / MODAL_SECRET in the environment,
network access to the environment's scoring service, and the local
development database (docker compose up -d postgres).
"""

import json
import sys
import time

import httpx

from governance_service.config import settings
from governance_service.database import get_db, init_db_if_needed
from governance_service.services import exam_stage, grading_stage
from governance_service.services.candidate_profiles import CURRENT_POOL_PROFILES
from governance_service.services.grading_engine import get_grading_outputs
from governance_service.services.round_package import FrozenPool
from governance_service.services.runtime_manager import ExamRuntimeManager
from scripts.exam_stage_live_validation import (
    _abandon_round,
    _seed_round,
    _validation_corpus,
)


def _grading_state(conn, round_id: int) -> dict:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT l.hf_repo, l.run_id, l.outcome, g.status
        FROM governance_round_grading_runs l
        JOIN grading_runs g ON g.id = l.run_id
        WHERE l.round_id = %s ORDER BY l.run_id
        """,
        (round_id,),
    )
    links = [
        {"hf_repo": row[0], "run_id": row[1], "outcome": row[2], "status": row[3]}
        for row in cursor.fetchall()
    ]
    cursor.execute(
        """
        SELECT hf_repo, final_grade, grade_receipts
        FROM governance_round_exam_runs
        WHERE round_id = %s AND final_grade IS NOT NULL
        ORDER BY hf_repo
        """,
        (round_id,),
    )
    grades = [
        {
            "hf_repo": row[0],
            "final_grade": str(row[1]),
            "item_grades": [
                {
                    "item_id": item["item_id"],
                    "grade": item["grade"],
                    "condition": item["condition"],
                    "defects": len(item["defects"]),
                }
                for item in row[2]["items"]
            ],
        }
        for row in cursor.fetchall()
    ]
    cursor.close()
    conn.rollback()
    return {"links": links, "grades": grades}


def _judge_output_count(conn) -> int:
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM grading_outputs")
    count = cursor.fetchone()[0]
    cursor.close()
    conn.rollback()
    return count


def main() -> int:
    if (
        len(sys.argv) != 4
        or sys.argv[1] not in CURRENT_POOL_PROFILES
        or sys.argv[2] not in CURRENT_POOL_PROFILES
        or sys.argv[1] == sys.argv[2]
    ):
        known = ", ".join(sorted(CURRENT_POOL_PROFILES))
        print(
            "usage: grading_stage_live_validation.py <examinee> <judge> <output.json>\n"
            f"known repos: {known}"
        )
        return 2
    examinee_repo, judge_repo, output_path = sys.argv[1], sys.argv[2], sys.argv[3]

    examinee = CURRENT_POOL_PROFILES[examinee_repo]
    judge = CURRENT_POOL_PROFILES[judge_repo]
    pool = FrozenPool(refresh_id=0, incumbent=examinee, challengers=[judge])

    init_db_if_needed()
    connection = get_db()
    runtime = ExamRuntimeManager()

    started = time.monotonic()
    document: dict = {
        "examinee": examinee_repo,
        "judge": judge_repo,
        "judge_revision": judge.revision,
        "scoring_api": settings.scoring_api_base_url,
    }
    round_id = None
    try:
        with httpx.Client(timeout=settings.http_timeout_seconds) as client:
            corpus = _validation_corpus(client)
        document["corpus"] = {
            "historical_round": corpus.manifest["historical"][0]["round_number"],
            "constructed_case": corpus.manifest["constructed"][0]["case_id"],
        }
        round_id, round_number = _seed_round(connection, corpus, pool, judge_repo)
        document["round"] = {"id": round_id, "round_number": round_number}

        document["exam"] = exam_stage.run_exam(connection, round_id, round_number)

        first = grading_stage.run_grading(connection, round_id, round_number)
        document["first_pass"] = first
        outputs_after_first = _judge_output_count(connection)

        second = grading_stage.run_grading(connection, round_id, round_number)
        document["second_pass"] = second
        document["rerun_reused_all_judge_inferences"] = (
            _judge_output_count(connection) == outputs_after_first
        )

        state = _grading_state(connection, round_id)
        document["grading_links"] = state["links"]
        document["grades"] = state["grades"]

        determinism = []
        for link in state["links"]:
            outputs = get_grading_outputs(connection, link["run_id"])
            connection.rollback()
            by_pair: dict = {}
            for output in outputs:
                by_pair.setdefault(
                    (output["item_id"], output["answer_hash"]), set()
                ).add(output["response_hash"])
            determinism.append(
                {
                    "run_id": link["run_id"],
                    "pairs": len(by_pair),
                    "attempts": len(outputs),
                    "bit_identical": all(len(v) == 1 for v in by_pair.values()),
                    "response_hashes": sorted(
                        hash for v in by_pair.values() for hash in v
                    ),
                }
            )
        document["judge_determinism"] = determinism
        document["status"] = "ok"
    except Exception as exc:
        document["status"] = f"{type(exc).__name__}: {exc}"
    finally:
        for repo in (examinee_repo, judge_repo):
            try:
                document.setdefault("torn_down_apps", []).append(
                    runtime.teardown(repo)
                )
            except Exception as exc:
                document.setdefault("teardown_errors", []).append(str(exc))
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
