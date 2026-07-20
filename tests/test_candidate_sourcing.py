"""Candidate sourcing pipeline against mocked HTTP with real fixture data."""

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from governance_service import freshness
from governance_service.config import MODEL_MAPPING_PATH
from governance_service.models import SourcingReport, ThinkingMode
from governance_service.services.candidate_sourcing import (
    MappingError,
    load_mapping,
    source_candidates,
)

FIXTURES = Path(__file__).parent / "fixtures"
RELEASE = "2026_06_25"

QWEN_INFO = json.loads((FIXTURES / "hf_qwen3.6-27b-fp8_info.json").read_text())
DEEPSEEK_INFO = json.loads((FIXTURES / "hf_deepseek-v4-pro_info.json").read_text())

ROUTES = {
    "https://api.github.com/repos/livebench/livebench.github.io/contents/public": "github_contents_public.json",
    f"https://livebench.ai/table_{RELEASE}.csv": f"table_{RELEASE}.csv",
    f"https://livebench.ai/categories_{RELEASE}.json": f"categories_{RELEASE}.json",
    "https://raw.githubusercontent.com/livebench/livebench.github.io/main/src/Table/modelLinks.js": "modelLinks.js",
    "https://huggingface.co/api/models/Qwen/Qwen3.6-27B-FP8?blobs=true": "hf_qwen3.6-27b-fp8_info.json",
    f"https://huggingface.co/Qwen/Qwen3.6-27B-FP8/resolve/{QWEN_INFO['sha']}/config.json": "hf_qwen3.6-27b-fp8_config.json",
    "https://huggingface.co/api/models/deepseek-ai/DeepSeek-V4-Pro?blobs=true": "hf_deepseek-v4-pro_info.json",
    f"https://huggingface.co/deepseek-ai/DeepSeek-V4-Pro/resolve/{DEEPSEEK_INFO['sha']}/config.json": "hf_deepseek-v4-pro_config.json",
}

TEST_MAPPING = """\
qwen3.6-27b:
  hf_repo: Qwen/Qwen3.6-27B-FP8
  family: qwen
  thinking: hybrid
deepseek-v4-pro:
  hf_repo: deepseek-ai/DeepSeek-V4-Pro
  family: deepseek
  thinking: unknown
kimi-k2.6-thinking:
  skip_reason: No public weight repository identified.
"""


def _handler(request: httpx.Request) -> httpx.Response:
    fixture = ROUTES.get(str(request.url))
    if fixture is None:
        return httpx.Response(404, content=b"not found")
    return httpx.Response(200, content=(FIXTURES / fixture).read_bytes())


@pytest.fixture()
def mock_client():
    with httpx.Client(transport=httpx.MockTransport(_handler)) as client:
        yield client


@pytest.fixture()
def test_mapping_path(tmp_path):
    path = tmp_path / "model_mapping.yaml"
    path.write_text(TEST_MAPPING, encoding="utf-8")
    return path


def test_full_sourcing_pass(mock_client, test_mapping_path):
    report = source_candidates(mock_client, mapping_path=test_mapping_path)

    assert report.release == RELEASE
    by_key = {candidate.livebench_key: candidate for candidate in report.candidates}

    qwen = by_key["qwen3.6-27b"]
    assert qwen.hf_repo == "Qwen/Qwen3.6-27B-FP8"
    assert qwen.revision == QWEN_INFO["sha"]
    assert qwen.precision.value == "fp8"
    assert qwen.license == "apache-2.0"
    assert qwen.gated is False
    assert qwen.assigned_gpu == "H100"
    assert qwen.thinking is ThinkingMode.HYBRID
    assert round(qwen.global_average, 2) == 64.03

    deepseek = by_key["deepseek-v4-pro"]
    assert deepseek.assigned_gpu is None
    assert deepseek.weight_bytes == 864_721_029_744

    assert set(report.unmapped) == {
        "glm-5.2",
        "kimi-k2.7-code",
        "deepseek-v4-flash",
    }
    assert report.skipped == {
        "kimi-k2.6-thinking": "No public weight repository identified."
    }


def test_snapshots_hash_the_exact_fetched_bytes(mock_client, test_mapping_path):
    report = source_candidates(mock_client, mapping_path=test_mapping_path)
    by_name = {snap.name: snap for snap in report.snapshots}
    for name, fixture in [
        (f"table_{RELEASE}.csv", f"table_{RELEASE}.csv"),
        (f"categories_{RELEASE}.json", f"categories_{RELEASE}.json"),
        ("modelLinks.js", "modelLinks.js"),
    ]:
        raw = (FIXTURES / fixture).read_bytes()
        assert by_name[name].sha256 == hashlib.sha256(raw).hexdigest()


def test_explicit_release_skips_discovery(test_mapping_path):
    routes = {url: f for url, f in ROUTES.items() if "api.github.com" not in url}

    def handler(request):
        fixture = routes.get(str(request.url))
        if fixture is None:
            return httpx.Response(404, content=b"not found")
        return httpx.Response(200, content=(FIXTURES / fixture).read_bytes())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = source_candidates(client, release=RELEASE, mapping_path=test_mapping_path)
    assert report.release == RELEASE


def test_repo_mapping_file_is_valid():
    mapping, skips = load_mapping(MODEL_MAPPING_PATH)
    assert mapping["qwen3.6-27b"].hf_repo == "Qwen/Qwen3.6-27B-FP8"
    assert all(entry.hf_repo and entry.family for entry in mapping.values())
    assert "kimi-k2.6-thinking" in skips


def test_malformed_mapping_rejected(tmp_path):
    path = tmp_path / "bad.yaml"

    path.write_text("qwen3.6-27b:\n  hf_repo: x\n", encoding="utf-8")
    with pytest.raises(MappingError, match="missing fields"):
        load_mapping(path)

    path.write_text(
        "qwen3.6-27b:\n  hf_repo: x\n  family: y\n  thinking: hybrid\n  extra: z\n",
        encoding="utf-8",
    )
    with pytest.raises(MappingError, match="unknown fields"):
        load_mapping(path)

    path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(MappingError, match="non-empty mapping"):
        load_mapping(path)

    path.write_text(
        "x:\n  skip_reason: gone\n  hf_repo: also-present\n", encoding="utf-8"
    )
    with pytest.raises(MappingError, match="only a skip_reason"):
        load_mapping(path)

    path.write_text(
        "x:\n  hf_repo: [not, a, string]\n  family: y\n  thinking: hybrid\n",
        encoding="utf-8",
    )
    with pytest.raises(MappingError, match="is invalid"):
        load_mapping(path)


def _clean_report():
    return SourcingReport(
        release=RELEASE,
        candidates=[],
        unmapped=[],
        skipped={"kimi-k2.6-thinking": "No public weight repository identified."},
        snapshots=[],
    )


def test_freshness_fails_on_unmapped_models(monkeypatch, mock_client):
    report = SourcingReport(
        release=RELEASE,
        candidates=[],
        unmapped=["some-new-model"],
        skipped={},
        snapshots=[],
    )
    monkeypatch.setattr(freshness, "source_candidates", lambda client: report)
    monkeypatch.setattr(freshness, "check_thinking_classes", lambda client: [])
    assert freshness.run(mock_client) == 1


def test_freshness_passes_with_known_skips(monkeypatch, mock_client):
    monkeypatch.setattr(
        freshness, "source_candidates", lambda client: _clean_report()
    )
    monkeypatch.setattr(freshness, "check_thinking_classes", lambda client: [])
    assert freshness.run(mock_client) == 0


def test_freshness_fails_on_thinking_mismatch(monkeypatch, mock_client):
    monkeypatch.setattr(
        freshness, "source_candidates", lambda client: _clean_report()
    )
    monkeypatch.setattr(
        freshness,
        "check_thinking_classes",
        lambda client: ["some-model: declared 'hybrid' but no toggle"],
    )
    assert freshness.run(mock_client) == 1
