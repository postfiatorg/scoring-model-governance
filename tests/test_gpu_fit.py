"""Cheapest-fit GPU assignment arithmetic with the incumbent's real numbers."""

from governance_service.models import ModelArtifact, ModelGeometry, Precision
from governance_service.services import gpu_fit

INCUMBENT_WEIGHT_BYTES = 30_866_866_928
INCUMBENT_GEOMETRY = ModelGeometry(
    num_hidden_layers=64, num_key_value_heads=4, head_dim=256
)
# 2 tensors * 64 layers * 4 kv heads * 256 head dim * 32768 tokens * 2 bytes
INCUMBENT_KV_BYTES = 8_589_934_592


def _artifact(weight_bytes: int, precision: Precision, geometry=INCUMBENT_GEOMETRY):
    return ModelArtifact(
        repo_id="test/model",
        revision="0" * 40,
        precision=precision,
        weight_bytes=weight_bytes,
        geometry=geometry,
        license=None,
        gated=False,
    )


def test_kv_cache_bytes_exact():
    assert (
        gpu_fit.kv_cache_bytes(INCUMBENT_GEOMETRY, 32768) == INCUMBENT_KV_BYTES
    )


def test_incumbent_assigns_h100():
    # Weights + KV (39.5 GB) exceed the L40S budget (0.75 * 48 GiB) and the
    # A100 is excluded for FP8, so the cheapest fit is the H100 — exactly
    # the GPU production serves the incumbent on.
    artifact = _artifact(INCUMBENT_WEIGHT_BYTES, Precision.FP8)
    gpu = gpu_fit.cheapest_fit(artifact)
    assert gpu is not None and gpu.name == "H100"


def test_bf16_artifact_of_same_size_assigns_a100():
    artifact = _artifact(INCUMBENT_WEIGHT_BYTES, Precision.BF16)
    gpu = gpu_fit.cheapest_fit(artifact)
    assert gpu is not None and gpu.name == "A100-80GB"


def test_small_fp8_artifact_assigns_l40s():
    artifact = _artifact(20_000_000_000, Precision.FP8)
    gpu = gpu_fit.cheapest_fit(artifact)
    assert gpu is not None and gpu.name == "L40S"


def test_giant_model_fits_nothing():
    geometry = ModelGeometry(num_hidden_layers=61, num_key_value_heads=1, head_dim=512)
    artifact = _artifact(864_721_029_744, Precision.FP8, geometry)
    assert gpu_fit.cheapest_fit(artifact) is None


def test_gpu_table_is_ordered_cheapest_first():
    assert [gpu.name for gpu in gpu_fit.GPU_TABLE] == [
        "L40S",
        "A100-80GB",
        "H100",
        "H200",
    ]
