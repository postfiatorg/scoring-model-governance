"""Small live validation of the grading pipeline on the real workspace.

Exercises the whole G.4 chain for real against one deployed pool model:
runs the two-item exam fragment through the exam engine to produce real
survivor answers, grades each (item, answer) pair three times with the
same deployed model acting as judge — deploying once serves both roles,
which is validation economy, not protocol (a drawn judge never grades
its own exam in a round) — applies the judge mechanical verdict to the
stored rows, then re-grades the material offline through the checker
and grade formula into end-to-end grades, and tears the app down.

    PYTHONPATH=. python scripts/grading_live_validation.py "Qwen/Qwen3.6-27B-FP8" out.json

Requires a Modal CLI login, MODAL_KEY / MODAL_SECRET in the environment,
and the local development database (docker compose up -d postgres).
"""

import json
import sys
import time

from governance_service.database import get_db, init_db_if_needed
from governance_service.services import edge_cases
from governance_service.services.candidate_profiles import CURRENT_POOL_PROFILES
from governance_service.services.exam_engine import (
    REPEAT_COUNT,
    ExamEngine,
    ExamItem,
    get_run_outputs,
)
from governance_service.services.grading_engine import (
    RUN_COMPLETED,
    GradingEngine,
    GradingPair,
    get_grading_outputs,
    judge_mechanical_verdict,
)
from governance_service.services.regrading import regrade_material
from governance_service.services.runtime_manager import ExamRuntimeManager

VALIDATION_CASES = ("all_below_cutoff", "injection_in_evidence")


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in CURRENT_POOL_PROFILES:
        known = ", ".join(sorted(CURRENT_POOL_PROFILES))
        print(f"usage: grading_live_validation.py <hf_repo> <output.json>\nknown repos: {known}")
        return 2
    hf_repo, output_path = sys.argv[1], sys.argv[2]

    profile = CURRENT_POOL_PROFILES[hf_repo]
    built = edge_cases.build_all()
    items = [ExamItem(item_id=f"edge:{name}", request=built[name]) for name in VALIDATION_CASES]

    init_db_if_needed()
    connection = get_db()
    runtime = ExamRuntimeManager()

    started = time.monotonic()
    document: dict = {"hf_repo": hf_repo, "revision": profile.revision, "pairs": {}}
    try:
        exam_run_id = ExamEngine(runtime).examine(connection, [profile], items)[0]
        exam_outputs = get_run_outputs(connection, exam_run_id)
        document["exam_run_id"] = exam_run_id

        pairs = []
        for item in items:
            rows = [r for r in exam_outputs if r["item_id"] == item.item_id]
            if len({r["response_hash"] for r in rows}) != 1:
                raise RuntimeError(f"{item.item_id}: exam answers not bit-identical")
            pairs.append(
                GradingPair(
                    item_id=item.item_id,
                    exam_request=item.request,
                    answer_content=rows[0]["raw_response"],
                )
            )

        grading_run_id = GradingEngine(runtime).grade(connection, profile, pairs)
        document["grading_run_id"] = grading_run_id
        cursor = connection.cursor()
        cursor.execute("SELECT status, judge_failure FROM grading_runs WHERE id = %s", (grading_run_id,))
        run_status, judge_failure = cursor.fetchone()
        cursor.close()
        if run_status != RUN_COMPLETED:
            document["status"] = f"grading run {run_status}: {judge_failure}"
            raise SystemExit  # falls through to finally for teardown

        grading_outputs = get_grading_outputs(connection, grading_run_id)
        for pair in pairs:
            rows = [
                r
                for r in grading_outputs
                if r["item_id"] == pair.item_id and r["answer_hash"] == pair.answer_hash
            ]
            hashes = sorted({r["response_hash"] for r in rows})
            document["pairs"][pair.item_id] = {
                "answer_hash": pair.answer_hash,
                "attempts": len(rows),
                "distinct_hashes": len(hashes),
                "response_hashes": hashes,
                "latencies_seconds": [round(r["latency_seconds"], 1) for r in rows],
                "completion_tokens": [r["completion_tokens"] for r in rows],
            }

        verdict = judge_mechanical_verdict(connection, grading_run_id, pairs, repeats=REPEAT_COUNT)
        document["judge_mechanical_verdict"] = {
            "verdict": verdict["verdict"],
            "rules": {name: rule["outcome"] for name, rule in verdict["rules"].items()},
        }

        material = [
            {
                "item_id": pair.item_id,
                "request": pair.exam_request,
                "answer_content": pair.answer_content,
                "judge_content": next(
                    r["raw_response"]
                    for r in grading_outputs
                    if r["item_id"] == pair.item_id and r["answer_hash"] == pair.answer_hash
                ),
            }
            for pair in pairs
        ]
        document["regrading"] = regrade_material(material)
        document["status"] = "ok"
    except SystemExit:
        pass
    except Exception as exc:
        document["status"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            document["torn_down_app"] = runtime.teardown(hf_repo)
        except Exception as exc:
            document["teardown_error"] = str(exc)
        document["total_seconds"] = round(time.monotonic() - started, 1)
        connection.close()

    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps({k: v for k, v in document.items() if k != "regrading"}, indent=2, sort_keys=True))
    if document.get("status") == "ok":
        for item in document["regrading"]["items"]:
            print(f"{item['item_id']}: grade {item['grade']} ({item['condition']})")
        print(f"final grade: {document['regrading']['final_grade']}")
    return 0 if document.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
