"""Precision and geometry parsing across real vendor config shapes."""

import json
from pathlib import Path

import pytest

from governance_service.clients import huggingface
from governance_service.clients.huggingface import HuggingFaceError
from governance_service.models import Precision

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _precision(config: dict) -> Precision:
    geometry_source = config.get("text_config") or config
    return huggingface._parse_precision("test/repo", config, geometry_source)


def test_fp8_quant_method_detected():
    config = _load("hf_qwen3.6-27b-fp8_config.json")
    assert _precision(config) == Precision.FP8


def test_native_fp8_release_detected():
    config = _load("hf_deepseek-v4-pro_config.json")
    assert _precision(config) == Precision.FP8


def test_compressed_tensors_int4_detected():
    # Kimi K2.7 Code: transformers v5 'dtype' naming, nested text_config,
    # compressed-tensors pack-quantized int4 weights.
    config = _load("hf_kimi-k2.7-code_config.json")
    assert _precision(config) == Precision.INT4


def test_compressed_tensors_fp8_scheme_detected():
    config = {
        "quantization_config": {
            "quant_method": "compressed-tensors",
            "config_groups": {
                "group_0": {"weights": {"type": "float", "num_bits": 8}}
            },
        },
        "num_hidden_layers": 1,
    }
    assert _precision(config) == Precision.FP8


def test_plain_dtypes_detected():
    assert _precision({"torch_dtype": "bfloat16"}) == Precision.BF16
    assert _precision({"dtype": "bfloat16"}) == Precision.BF16
    assert _precision({"torch_dtype": "float16"}) == Precision.FP16


def test_unknown_quant_method_raises():
    with pytest.raises(HuggingFaceError, match="Unrecognized quantization config"):
        _precision({"quantization_config": {"quant_method": "awq"}})


def test_quant_config_without_method_raises():
    with pytest.raises(HuggingFaceError, match="Unrecognized quantization config"):
        _precision({"quantization_config": {"bits": 4}, "torch_dtype": "bfloat16"})


def test_undetectable_precision_raises():
    with pytest.raises(HuggingFaceError, match="Cannot determine precision"):
        _precision({"model_type": "mystery"})


def test_4xx_fails_immediately_without_retry():
    import httpx

    requests_seen = []

    def handler(request):
        requests_seen.append(request)
        return httpx.Response(404, content=b"missing")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HuggingFaceError, match="returned 404"):
            huggingface.fetch_artifact(client, "test/missing")
    assert len(requests_seen) == 1


def test_rate_limit_is_retried(monkeypatch):
    import httpx

    from governance_service.config import settings

    monkeypatch.setattr(settings, "http_retry_base_delay", 0)
    requests_seen = []

    def handler(request):
        requests_seen.append(request)
        if len(requests_seen) < 3:
            return httpx.Response(429, content=b"rate limited")
        return httpx.Response(200, content=b"ok")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert huggingface._get(client, "https://huggingface.co/api/models/x") == b"ok"
    assert len(requests_seen) == 3


def test_geometry_head_dim_fallback():
    geometry = huggingface._parse_geometry(
        "test/repo",
        {
            "num_hidden_layers": 61,
            "num_key_value_heads": 64,
            "hidden_size": 7168,
            "num_attention_heads": 64,
        },
    )
    assert geometry.head_dim == 112


def test_missing_geometry_raises():
    with pytest.raises(HuggingFaceError, match="missing attention geometry"):
        huggingface._parse_geometry("test/repo", {"num_hidden_layers": 2})
