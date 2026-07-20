"""Thinking-mode eligibility: pool rules and chat-template validation."""

import httpx
import pytest

from governance_service.clients.huggingface import (
    HuggingFaceError,
    fetch_chat_template,
)
from governance_service.config import MODEL_MAPPING_PATH, settings
from governance_service.freshness import validate_thinking_class
from governance_service.models import MappingEntry, ThinkingMode
from governance_service.services.candidate_sourcing import (
    MappingError,
    load_mapping,
)
from governance_service.services.pool_refresh import (
    RULE_THINKING_NOT_DISABLEABLE,
    RULE_THINKING_UNVERIFIED,
    evaluate_release,
)
from tests.test_pool_rules import INCUMBENT_REPO, descriptor, outcomes

HYBRID_TEMPLATE = "{% if enable_thinking %}<think>{% endif %}{{ content }}"
PLAIN_TEMPLATE = "{{ content }}"


def entry(thinking: str) -> MappingEntry:
    return MappingEntry(hf_repo="org/model", family="fam", thinking=thinking)


class TestPoolRule:
    def test_thinking_always_is_excluded(self):
        evaluations = evaluate_release(
            [descriptor("locked", "a", 80.0, thinking="always")], [], INCUMBENT_REPO
        )
        assert outcomes(evaluations)["locked"] == (
            False,
            RULE_THINKING_NOT_DISABLEABLE,
        )

    def test_thinking_unknown_fails_closed(self):
        evaluations = evaluate_release(
            [descriptor("mystery", "a", 80.0, thinking="unknown")], [], INCUMBENT_REPO
        )
        assert outcomes(evaluations)["mystery"] == (False, RULE_THINKING_UNVERIFIED)

    def test_none_and_hybrid_are_eligible(self):
        evaluations = evaluate_release(
            [
                descriptor("plain", "a", 80.0, thinking="none"),
                descriptor("toggle", "b", 79.0, thinking="hybrid"),
            ],
            [],
            INCUMBENT_REPO,
        )
        assert all(evaluation.in_pool for evaluation in evaluations)

    def test_excluded_model_passes_family_slot(self):
        evaluations = evaluate_release(
            [
                descriptor("locked-best", "a", 80.0, thinking="always"),
                descriptor("open-sibling", "a", 75.0, thinking="hybrid"),
            ],
            [],
            INCUMBENT_REPO,
        )
        results = outcomes(evaluations)
        assert results["locked-best"] == (False, RULE_THINKING_NOT_DISABLEABLE)
        assert results["open-sibling"] == (True, None)


class TestMappingValidation:
    def test_repo_mapping_declares_valid_classes_everywhere(self):
        mapping, _ = load_mapping(MODEL_MAPPING_PATH)
        incumbent = mapping["qwen3.6-27b"]
        assert incumbent.thinking is ThinkingMode.HYBRID

    def test_entry_without_thinking_is_rejected(self, tmp_path):
        path = tmp_path / "mapping.yaml"
        path.write_text("m:\n  hf_repo: org/m\n  family: f\n", encoding="utf-8")
        with pytest.raises(MappingError, match="missing fields.*thinking"):
            load_mapping(path)

    def test_invalid_thinking_value_is_rejected(self, tmp_path):
        path = tmp_path / "mapping.yaml"
        path.write_text(
            "m:\n  hf_repo: org/m\n  family: f\n  thinking: sometimes\n",
            encoding="utf-8",
        )
        with pytest.raises(MappingError, match="is invalid"):
            load_mapping(path)


class TestTemplateValidation:
    def test_hybrid_with_toggle_is_consistent(self):
        assert validate_thinking_class(entry("hybrid"), HYBRID_TEMPLATE) is None

    def test_hybrid_without_toggle_fails(self):
        verdict = validate_thinking_class(entry("hybrid"), PLAIN_TEMPLATE)
        assert "no thinking toggle" in verdict

    def test_non_hybrid_with_toggle_fails(self):
        verdict = validate_thinking_class(entry("none"), HYBRID_TEMPLATE)
        assert "reclassify as 'hybrid'" in verdict

    def test_always_without_toggle_is_consistent(self):
        assert validate_thinking_class(entry("always"), PLAIN_TEMPLATE) is None

    def test_unknown_requires_absent_template(self):
        assert validate_thinking_class(entry("unknown"), None) is None
        verdict = validate_thinking_class(entry("unknown"), PLAIN_TEMPLATE)
        assert "reclassify instead of 'unknown'" in verdict

    def test_declared_class_with_missing_template_fails(self):
        verdict = validate_thinking_class(entry("hybrid"), None)
        assert "only 'unknown' is consistent" in verdict


class TestTemplateFetch:
    def _client(self, handler):
        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_reads_tokenizer_config_field(self):
        def handler(request):
            if request.url.path.endswith("tokenizer_config.json"):
                return httpx.Response(200, json={"chat_template": HYBRID_TEMPLATE})
            return httpx.Response(404)

        assert (
            fetch_chat_template(self._client(handler), "org/model")
            == HYBRID_TEMPLATE
        )

    def test_falls_back_to_jinja_file(self):
        def handler(request):
            if request.url.path.endswith("tokenizer_config.json"):
                return httpx.Response(200, json={"eos_token": "x"})
            if request.url.path.endswith("chat_template.jinja"):
                return httpx.Response(200, text=PLAIN_TEMPLATE)
            return httpx.Response(404)

        assert (
            fetch_chat_template(self._client(handler), "org/model")
            == PLAIN_TEMPLATE
        )

    def test_returns_none_when_no_template_ships(self):
        def handler(request):
            return httpx.Response(404)

        assert fetch_chat_template(self._client(handler), "org/model") is None

    def test_transport_failure_raises_instead_of_none(self, monkeypatch):
        """An HF outage must never read as an absent template."""
        monkeypatch.setattr(settings, "http_max_retries", 1)

        def handler(request):
            return httpx.Response(500, text="outage")

        with pytest.raises(HuggingFaceError):
            fetch_chat_template(self._client(handler), "org/model")
