"""The request-adaptation rule: identity, exclusivity, and purity."""

import copy
import json

import pytest
from pydantic import ValidationError

from governance_service.models import RuntimeProfile
from governance_service.scoring import canonical_sha256
from governance_service.services import edge_cases
from governance_service.services.request_adaptation import (
    ADAPTED_FIELDS,
    RequestAdaptationError,
    adapt_request,
    extract_profile,
)

CHALLENGER_PROFILE = RuntimeProfile(
    hf_repo="google/gemma-4-31B-it",
    extra_body={"chat_template_kwargs": {"thinking_mode": "off"}},
)


def _template() -> dict:
    return json.loads(edge_cases.TEMPLATE_PATH.read_text(encoding="utf-8"))


def _corpus_requests() -> dict[str, dict]:
    return {"template": _template(), **edge_cases.build_all()}


def test_identity_on_the_production_template():
    """Adapting to the embedded profile reproduces the original byte-for-byte."""
    request = _template()
    adapted = adapt_request(request, extract_profile(request))
    assert canonical_sha256(adapted) == canonical_sha256(request)
    assert adapted == request


def test_identity_across_every_corpus_item():
    for name, request in _corpus_requests().items():
        adapted = adapt_request(request, extract_profile(request))
        assert canonical_sha256(adapted) == canonical_sha256(request), name


def test_exclusivity_changes_only_the_declared_fields():
    """Adapted bytes equal the original with exactly the two fields replaced.

    ``expected`` is deep-copied before adaptation runs, so an implementation
    that mutated any nested non-declared field in place could not escape the
    comparison by mutating the reference copy along with the input.
    """
    for name, request in _corpus_requests().items():
        expected = copy.deepcopy(request)
        expected["model"] = CHALLENGER_PROFILE.hf_repo
        expected["extra_body"] = CHALLENGER_PROFILE.extra_body

        adapted = adapt_request(request, CHALLENGER_PROFILE)
        assert canonical_sha256(adapted) == canonical_sha256(expected), name

        assert adapted["model"] == CHALLENGER_PROFILE.hf_repo, name
        assert adapted["extra_body"] == CHALLENGER_PROFILE.extra_body, name
        for field in expected:
            if field not in ADAPTED_FIELDS:
                assert adapted[field] == expected[field], (name, field)
        assert canonical_sha256(adapted) != canonical_sha256(expected | {
            "model": request["model"], "extra_body": request["extra_body"]
        }), name


def test_adaptation_is_deterministic():
    request = _template()
    first = adapt_request(request, CHALLENGER_PROFILE)
    second = adapt_request(request, CHALLENGER_PROFILE)
    assert canonical_sha256(first) == canonical_sha256(second)


def test_adaptation_never_mutates_or_aliases_the_input():
    request = _template()
    before = canonical_sha256(request)
    adapted = adapt_request(request, CHALLENGER_PROFILE)
    adapted["messages"][0]["content"] = "tampered"
    adapted["extra_body"]["chat_template_kwargs"]["thinking_mode"] = "on"
    assert canonical_sha256(request) == before
    assert CHALLENGER_PROFILE.extra_body == {
        "chat_template_kwargs": {"thinking_mode": "off"}
    }


def test_extract_profile_reads_the_embedded_identity():
    profile = extract_profile(_template())
    assert profile.hf_repo == "Qwen/Qwen3.6-27B-FP8"
    assert profile.extra_body == {"chat_template_kwargs": {"enable_thinking": False}}


def test_extracted_profile_does_not_alias_the_request():
    request = _template()
    before = canonical_sha256(request)
    profile = extract_profile(request)
    profile.extra_body["chat_template_kwargs"]["enable_thinking"] = True
    assert canonical_sha256(request) == before


def test_non_production_requests_are_rejected():
    with pytest.raises(RequestAdaptationError, match="'model'"):
        adapt_request({"extra_body": {}}, CHALLENGER_PROFILE)
    with pytest.raises(RequestAdaptationError, match="'extra_body'"):
        extract_profile({"model": "x"})


def test_profile_rejects_an_empty_model_identifier():
    with pytest.raises(ValidationError):
        RuntimeProfile(hf_repo="", extra_body={})
