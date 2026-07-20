"""Pool rule evaluation and standing-blocklist loading."""

import pytest

from governance_service.config import MODEL_BLOCKLIST_PATH
from governance_service.models import BlocklistEntry, CandidateDescriptor
from governance_service.services.pool_refresh import (
    RULE_BLOCKLISTED,
    RULE_FAMILY_DEDUPLICATED,
    RULE_IS_INCUMBENT,
    RULE_NO_SINGLE_GPU_FIT,
    RULE_VARIANT_INELIGIBLE,
    BlocklistError,
    evaluate_release,
    load_blocklist,
)

INCUMBENT_REPO = "Qwen/Qwen3.6-27B-FP8"
RELEASE = "2026_06_25"


def descriptor(
    key: str,
    family: str,
    global_average: float,
    precision: str = "fp8",
    assigned_gpu: str | None = "H100",
    hf_repo: str | None = None,
    revision: str | None = None,
    thinking: str = "hybrid",
) -> CandidateDescriptor:
    return CandidateDescriptor(
        livebench_key=key,
        display_name=key.title(),
        organization=family.title(),
        family=family,
        thinking=thinking,
        global_average=global_average,
        category_averages={"reasoning": global_average},
        hf_repo=hf_repo or f"{family}/{key}",
        revision=revision or f"rev-{key}",
        precision=precision,
        weight_bytes=30_000_000_000,
        license="apache-2.0",
        gated=False,
        assigned_gpu=assigned_gpu,
        release=RELEASE,
    )


def outcomes(evaluations) -> dict[str, tuple[bool, str | None]]:
    return {
        e.descriptor.livebench_key: (e.in_pool, e.exclusion_rule)
        for e in evaluations
    }


def test_variant_ineligible_precision_is_excluded():
    evaluations = evaluate_release(
        [descriptor("kimi", "kimi", 70.0, precision="int4")], [], INCUMBENT_REPO
    )
    assert outcomes(evaluations)["kimi"] == (False, RULE_VARIANT_INELIGIBLE)


def test_full_precision_variants_are_eligible():
    evaluations = evaluate_release(
        [
            descriptor("model-bf16", "a", 70.0, precision="bf16"),
            descriptor("model-fp16", "b", 69.0, precision="fp16"),
        ],
        [],
        INCUMBENT_REPO,
    )
    assert all(evaluation.in_pool for evaluation in evaluations)


def test_no_single_gpu_fit_is_excluded():
    evaluations = evaluate_release(
        [descriptor("huge", "big", 80.0, assigned_gpu=None)], [], INCUMBENT_REPO
    )
    assert outcomes(evaluations)["huge"] == (False, RULE_NO_SINGLE_GPU_FIT)


def test_family_deduplication_keeps_best_ranked():
    evaluations = evaluate_release(
        [
            descriptor("glm-best", "glm", 75.0),
            descriptor("glm-worse", "glm", 70.0),
        ],
        [],
        INCUMBENT_REPO,
    )
    results = outcomes(evaluations)
    assert results["glm-best"] == (True, None)
    assert results["glm-worse"] == (False, RULE_FAMILY_DEDUPLICATED)


def test_incumbent_leaderboard_entry_never_competes():
    evaluations = evaluate_release(
        [descriptor("qwen3.6-27b", "qwen", 64.0, hf_repo=INCUMBENT_REPO)],
        [],
        INCUMBENT_REPO,
    )
    evaluation = evaluations[0]
    assert evaluation.is_incumbent
    assert not evaluation.in_pool
    assert evaluation.exclusion_rule == RULE_IS_INCUMBENT


def test_incumbent_family_successor_takes_the_family_slot():
    evaluations = evaluate_release(
        [
            descriptor("qwen4", "qwen", 80.0),
            descriptor("qwen3.6-27b", "qwen", 64.0, hf_repo=INCUMBENT_REPO),
        ],
        [],
        INCUMBENT_REPO,
    )
    results = outcomes(evaluations)
    assert results["qwen4"] == (True, None)
    assert results["qwen3.6-27b"] == (False, RULE_IS_INCUMBENT)


def test_blocked_revision_passes_slot_to_family_sibling():
    blocklist = [
        BlocklistEntry(
            hf_repo="glm/glm-best",
            revision="rev-glm-best",
            reason="Non-deterministic outputs across repeated runs.",
            round_reference="round-0001",
        )
    ]
    evaluations = evaluate_release(
        [
            descriptor("glm-best", "glm", 75.0),
            descriptor("glm-sibling", "glm", 71.0),
        ],
        blocklist,
        INCUMBENT_REPO,
    )
    results = outcomes(evaluations)
    assert results["glm-best"] == (False, RULE_BLOCKLISTED)
    assert results["glm-sibling"] == (True, None)


def test_new_revision_of_blocked_model_is_a_new_candidate():
    blocklist = [
        BlocklistEntry(
            hf_repo="glm/glm-best",
            revision="older-blocked-revision",
            reason="Failed to deploy on its pinned profile.",
            round_reference="round-0001",
        )
    ]
    evaluations = evaluate_release(
        [descriptor("glm-best", "glm", 75.0)], blocklist, INCUMBENT_REPO
    )
    assert outcomes(evaluations)["glm-best"] == (True, None)


def test_repo_blocklist_file_is_valid_and_empty():
    assert load_blocklist(MODEL_BLOCKLIST_PATH) == []


def test_blocklist_entries_parse(tmp_path):
    path = tmp_path / "blocklist.yaml"
    path.write_text(
        "- hf_repo: org/model\n"
        "  revision: abc123\n"
        "  reason: Outputs failed the production parser.\n"
        "  round_reference: round-0001\n",
        encoding="utf-8",
    )
    entries = load_blocklist(path)
    assert len(entries) == 1
    assert entries[0].hf_repo == "org/model"
    assert entries[0].revision == "abc123"


def test_malformed_blocklist_rejected(tmp_path):
    path = tmp_path / "blocklist.yaml"

    path.write_text("key: value\n", encoding="utf-8")
    with pytest.raises(BlocklistError, match="must be a list"):
        load_blocklist(path)

    path.write_text("- not-a-mapping\n", encoding="utf-8")
    with pytest.raises(BlocklistError, match="must be a mapping"):
        load_blocklist(path)

    path.write_text("- hf_repo: org/model\n  revision: abc\n", encoding="utf-8")
    with pytest.raises(BlocklistError, match="is invalid"):
        load_blocklist(path)
