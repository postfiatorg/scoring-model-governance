"""Re-grade frozen material offline: judge outputs to grades with receipts.

    PYTHONPATH=. python scripts/regrade.py material.json results.json

The material file is a JSON list; each entry carries "item_id" (string),
"request" (the corpus item's frozen chat-completions request),
"answer_content" (the survivor answer's raw content), "judge_content"
(the judge output's raw content), and optionally "validator_map" (the
item's frozen identity map — omitted for constructed items, whose maps
derive synthetically). The results file records grade formula version,
per-item grades with complete receipts, and the final grade. Everything
runs offline: no database, no Modal, no network.
"""

import json
import sys

from governance_service.services.regrading import regrade_material


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: regrade.py <material.json> <results.json>")
        return 2
    material_path, results_path = sys.argv[1], sys.argv[2]

    with open(material_path, encoding="utf-8") as handle:
        entries = json.load(handle)
    try:
        results = regrade_material(entries)
    except (ValueError, KeyError) as exc:
        print(f"ERROR: {exc}")
        return 1

    with open(results_path, "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2, sort_keys=True)
        handle.write("\n")
    for item in results["items"]:
        print(f"{item['item_id']}: grade {item['grade']} (band {item['band']}, {item['condition']})")
    print(f"final grade: {results['final_grade']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
