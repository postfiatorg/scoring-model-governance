"""Small live validation of the exam execution engine on the real workspace.

Runs a fragment of the exam for real: two constructed edge-case items,
three runs each, against one deployed pool candidate, through the same
engine round orchestration will call — then tears the app down. Produces
the first real-world data point on cross-run bit-identical determinism.

    PYTHONPATH=. python scripts/exam_live_validation.py "Qwen/Qwen3.6-27B-FP8" out.json

Requires a Modal CLI login, MODAL_KEY / MODAL_SECRET in the environment,
and the local development database (docker compose up -d postgres).
"""

import json
import sys
import time

from governance_service.database import get_db, init_db_if_needed
from governance_service.services import edge_cases
from governance_service.services.candidate_profiles import CURRENT_POOL_PROFILES
from governance_service.services.exam_engine import ExamEngine, ExamItem, get_run_outputs
from governance_service.services.runtime_manager import ExamRuntimeManager

VALIDATION_CASES = ("all_below_cutoff", "injection_in_evidence")


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in CURRENT_POOL_PROFILES:
        known = ", ".join(sorted(CURRENT_POOL_PROFILES))
        print(f"usage: exam_live_validation.py <hf_repo> <output.json>\nknown repos: {known}")
        return 2
    hf_repo, output_path = sys.argv[1], sys.argv[2]

    profile = CURRENT_POOL_PROFILES[hf_repo]
    built = edge_cases.build_all()
    items = [ExamItem(item_id=f"edge:{name}", request=built[name]) for name in VALIDATION_CASES]

    init_db_if_needed()
    connection = get_db()
    runtime = ExamRuntimeManager()
    engine = ExamEngine(runtime)

    started = time.monotonic()
    document: dict = {"hf_repo": hf_repo, "revision": profile.revision, "items": {}}
    try:
        run_id = engine.examine(connection, [profile], items)[0]
        cursor = connection.cursor()
        cursor.execute("SELECT status, candidate_failure FROM exam_runs WHERE id = %s", (run_id,))
        run_status, candidate_failure = cursor.fetchone()
        cursor.close()
        if run_status != "COMPLETED":
            document["run_id"] = run_id
            document["status"] = f"run {run_status}: {candidate_failure}"
            raise SystemExit  # falls through to finally for teardown
        outputs = get_run_outputs(connection, run_id)
        deterministic = True
        for item in items:
            rows = [r for r in outputs if r["item_id"] == item.item_id]
            hashes = sorted({r["response_hash"] for r in rows})
            deterministic = deterministic and len(hashes) == 1
            document["items"][item.item_id] = {
                "attempts": len(rows),
                "distinct_hashes": len(hashes),
                "response_hashes": hashes,
                "latencies_seconds": [round(r["latency_seconds"], 1) for r in rows],
                "completion_tokens": [r["completion_tokens"] for r in rows],
            }
        document["run_id"] = run_id
        document["bit_identical_across_runs"] = deterministic
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
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0 if document.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
