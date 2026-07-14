"""Dtype-aware cheapest-fit GPU assignment for candidate artifacts.

A candidate must fit on exactly one GPU. The memory budget mirrors the
production SGLang profile: weights plus the KV cache for the configured
context must fit inside the same static memory fraction production serves
under, which keeps the methodology's headroom requirement with margin.
"""

from governance_service.config import settings
from governance_service.models import GpuSpec, ModelArtifact, ModelGeometry, Precision

GIB = 1024**3

# Ordered cheapest first; assignment takes the first GPU that fits.
# FP8 execution needs Ada or Hopper silicon, which excludes the A100.
GPU_TABLE = (
    GpuSpec(name="L40S", vram_gib=48, supports_fp8=True),
    GpuSpec(name="A100-80GB", vram_gib=80, supports_fp8=False),
    GpuSpec(name="H100", vram_gib=80, supports_fp8=True),
    GpuSpec(name="H200", vram_gib=141, supports_fp8=True),
)

KV_TENSORS_PER_LAYER = 2


def kv_cache_bytes(geometry: ModelGeometry, context_tokens: int) -> int:
    """KV-cache size for one sequence at the configured context length."""
    return (
        KV_TENSORS_PER_LAYER
        * geometry.num_hidden_layers
        * geometry.num_key_value_heads
        * geometry.head_dim
        * context_tokens
        * settings.kv_cache_dtype_bytes
    )


def cheapest_fit(artifact: ModelArtifact) -> GpuSpec | None:
    """The cheapest GPU the artifact fits on, or None if none fits."""
    required = artifact.weight_bytes + kv_cache_bytes(
        artifact.geometry, settings.fit_context_tokens
    )
    for gpu in GPU_TABLE:
        if artifact.precision == Precision.FP8 and not gpu.supports_fp8:
            continue
        if required <= settings.gpu_mem_fraction * gpu.vram_gib * GIB:
            return gpu
    return None
