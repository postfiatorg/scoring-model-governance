"""HuggingFace client for resolving mapped models to pinned artifacts.

Uses the public REST API to pin the current revision, sum the weight file
sizes, and read the model configuration the GPU-fit calculation needs.
"""

import json
import logging
import time

import httpx
from fastapi import status

from governance_service.config import settings
from governance_service.models import ModelArtifact, ModelGeometry, Precision

logger = logging.getLogger(__name__)

WEIGHT_FILE_SUFFIX = ".safetensors"
LICENSE_TAG_PREFIX = "license:"
FP8_QUANT_METHOD = "fp8"
COMPRESSED_TENSORS_QUANT_METHOD = "compressed-tensors"
DTYPE_PRECISIONS = {"bfloat16": Precision.BF16, "float16": Precision.FP16}


class HuggingFaceError(RuntimeError):
    """Raised when a model cannot be resolved to a usable artifact."""


def _get(client: httpx.Client, url: str) -> bytes:
    """GET with exponential backoff; 4xx responses fail without retry."""
    headers = {}
    if settings.hf_token:
        headers["Authorization"] = f"Bearer {settings.hf_token}"

    last_error: Exception | None = None
    for attempt in range(1, settings.http_max_retries + 1):
        try:
            response = client.get(url, headers=headers)
            # 4xx responses are deterministic and fail immediately, except
            # rate limiting, which takes the retry path.
            if (
                status.HTTP_400_BAD_REQUEST
                <= response.status_code
                < status.HTTP_500_INTERNAL_SERVER_ERROR
                and response.status_code != status.HTTP_429_TOO_MANY_REQUESTS
            ):
                raise HuggingFaceError(
                    f"HuggingFace returned {response.status_code} for {url}"
                )
            response.raise_for_status()
            return response.content
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt < settings.http_max_retries:
                delay = settings.http_retry_base_delay**attempt
                logger.warning(
                    "HuggingFace request attempt %d/%d failed: %s — retrying in %ds",
                    attempt,
                    settings.http_max_retries,
                    exc,
                    delay,
                )
                time.sleep(delay)
    raise HuggingFaceError(f"HuggingFace request failed: {url} — {last_error}")


def _parse_license(info: dict) -> str | None:
    for tag in info.get("tags", []):
        if isinstance(tag, str) and tag.startswith(LICENSE_TAG_PREFIX):
            return tag[len(LICENSE_TAG_PREFIX):]
    return None


def _compressed_tensors_precision(repo_id: str, quant_config: dict) -> Precision:
    groups = quant_config.get("config_groups") or {}
    weight_schemes = {
        (weights.get("type"), weights.get("num_bits"))
        for group in groups.values()
        if isinstance(group, dict) and isinstance(weights := group.get("weights"), dict)
    }
    if weight_schemes == {("float", 8)}:
        return Precision.FP8
    if weight_schemes == {("int", 4)}:
        return Precision.INT4
    raise HuggingFaceError(
        f"Unrecognized compressed-tensors scheme for {repo_id}: {weight_schemes}"
    )


def _parse_precision(repo_id: str, config: dict, geometry_source: dict) -> Precision:
    # Vendor configs place quantization at either nesting level (Qwen: top
    # level; Kimi: inside text_config), so both are consulted.
    quant_config = (
        config.get("quantization_config")
        or geometry_source.get("quantization_config")
        or {}
    )
    quant_method = quant_config.get("quant_method")
    if quant_method == FP8_QUANT_METHOD:
        return Precision.FP8
    if quant_method == COMPRESSED_TENSORS_QUANT_METHOD:
        return _compressed_tensors_precision(repo_id, quant_config)
    if quant_config:
        raise HuggingFaceError(
            f"Unrecognized quantization config for {repo_id}: "
            f"quant_method={quant_method!r}"
        )

    # Transformers v5 renamed torch_dtype to dtype; releases carry either.
    dtype = (
        geometry_source.get("torch_dtype")
        or geometry_source.get("dtype")
        or config.get("torch_dtype")
        or config.get("dtype")
    )
    if dtype in DTYPE_PRECISIONS:
        return DTYPE_PRECISIONS[dtype]
    raise HuggingFaceError(
        f"Cannot determine precision for {repo_id}: "
        f"quant_method={quant_method!r}, dtype={dtype!r}"
    )


def _parse_geometry(repo_id: str, source: dict) -> ModelGeometry:
    num_layers = source.get("num_hidden_layers")
    kv_heads = source.get("num_key_value_heads")
    head_dim = source.get("head_dim")
    if head_dim is None:
        hidden_size = source.get("hidden_size")
        num_heads = source.get("num_attention_heads")
        if hidden_size and num_heads:
            head_dim = hidden_size // num_heads
    if not (num_layers and kv_heads and head_dim):
        raise HuggingFaceError(
            f"Config for {repo_id} is missing attention geometry "
            f"(layers={num_layers}, kv_heads={kv_heads}, head_dim={head_dim})"
        )
    return ModelGeometry(
        num_hidden_layers=num_layers,
        num_key_value_heads=kv_heads,
        head_dim=head_dim,
    )


def fetch_artifact(
    client: httpx.Client, repo_id: str, revision: str | None = None
) -> ModelArtifact:
    """Resolve one repository to a pinned artifact with fit inputs.

    Without a revision the repository's current revision is pinned; with
    one, that exact revision is resolved (used for the incumbent, whose
    serving artifact is already pinned by the execution manifest).
    """
    info_url = f"{settings.hf_api_base_url}/api/models/{repo_id}"
    if revision is not None:
        info_url = f"{info_url}/revision/{revision}"
    info_raw = _get(client, f"{info_url}?blobs=true")
    try:
        info = json.loads(info_raw)
    except ValueError as exc:
        raise HuggingFaceError(f"Model info for {repo_id} is not JSON: {exc}") from exc

    revision = info.get("sha")
    if not revision:
        raise HuggingFaceError(f"Model info for {repo_id} has no revision sha")

    weight_bytes = sum(
        sibling.get("size") or 0
        for sibling in info.get("siblings", [])
        if sibling.get("rfilename", "").endswith(WEIGHT_FILE_SUFFIX)
    )
    if weight_bytes == 0:
        raise HuggingFaceError(f"No weight files with sizes found for {repo_id}")

    config_raw = _get(
        client, f"{settings.hf_api_base_url}/{repo_id}/resolve/{revision}/config.json"
    )
    try:
        config = json.loads(config_raw)
    except ValueError as exc:
        raise HuggingFaceError(f"config.json for {repo_id} is not JSON: {exc}") from exc

    # Multimodal releases nest the language model under text_config.
    geometry_source = config.get("text_config") or config

    return ModelArtifact(
        repo_id=repo_id,
        revision=revision,
        precision=_parse_precision(repo_id, config, geometry_source),
        weight_bytes=weight_bytes,
        geometry=_parse_geometry(repo_id, geometry_source),
        license=_parse_license(info),
        gated=bool(info.get("gated")),
    )
