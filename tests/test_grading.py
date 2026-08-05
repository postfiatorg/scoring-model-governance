"""The grading request derivation and the judge defect schema."""

import json

import pytest

from governance_service.models import RuntimeProfile
from governance_service.scoring import canonical_sha256
from governance_service.services import edge_cases
from governance_service.services.exam_engine import model_response_hash
from governance_service.services.grading import (
    GRADING_MAX_TOKENS,
    KINDS_REQUIRING_VALIDATORS,
    OUTCOME_DEFECTS_FOUND,
    OUTCOME_NONE_FOUND,
    OUTCOME_NOT_APPLICABLE,
    SECTION_KINDS,
    SECTION_OUTCOMES,
    GradingPromptError,
    JudgeOutputError,
    build_grading_request,
    exam_request_messages,
    load_grading_prompt,
    parse_judge_output,
)

JUDGE_PROFILE = RuntimeProfile(
    hf_repo="google/gemma-4-31B-it",
    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
)

ANSWER_CONTENT = json.dumps(
    {
        "v001": {
            "score": 80,
            "consensus": 83,
            "reliability": 70,
            "software": 90,
            "diversity": 50,
            "identity": 80,
            "reasoning": "Strong recent agreement but a degraded 30-day window.",
        }
    }
)


def _template() -> dict:
    return json.loads(edge_cases.TEMPLATE_PATH.read_text(encoding="utf-8"))


def _defect(**overrides) -> dict:
    defect = {
        "kind": "false_claim",
        "validator_ids": ["v009"],
        "quote": "Solid, dependable operation across all windows.",
        "explanation": "agreement_30d 0.9210 and 4831 missed ledgers contradict it.",
    }
    defect.update(overrides)
    return defect


def _valid_judge_payload() -> dict:
    return {
        "evidence_fidelity": {
            "outcome": OUTCOME_DEFECTS_FOUND,
            "defects": [_defect()],
        },
        "network_report_quality": {"outcome": OUTCOME_NONE_FOUND, "defects": []},
        "subversion": {"outcome": OUTCOME_NONE_FOUND, "defects": []},
    }


# -- template contract -------------------------------------------------------


def test_prompt_template_loads_and_carries_the_material_delimiters():
    system, user = load_grading_prompt()
    assert system and user
    for delimiter in (
        "=== BEGIN SCORING INSTRUCTIONS ===",
        "=== BEGIN SCORING INPUT ===",
        "=== BEGIN CANDIDATE ANSWER ===",
    ):
        assert user.count(delimiter) == 1
    for placeholder in ("{scoring_instructions}", "{scoring_input}", "{candidate_answer}"):
        assert placeholder not in system
        assert user.count(placeholder) == 1


def test_exam_request_messages_requires_the_production_shape():
    request = _template()
    system, user = exam_request_messages(request)
    assert system == request["messages"][0]["content"]
    assert user == request["messages"][1]["content"]

    for broken in (
        {"messages": [request["messages"][0]]},
        {"messages": [request["messages"][1], request["messages"][0]]},
        {"messages": [request["messages"][0], {"role": "user", "content": ""}]},
        {},
    ):
        with pytest.raises(GradingPromptError):
            exam_request_messages(broken)


# -- request derivation ------------------------------------------------------


def test_request_is_pure_and_deterministic():
    request = _template()
    first = build_grading_request(request, ANSWER_CONTENT, JUDGE_PROFILE)
    second = build_grading_request(request, ANSWER_CONTENT, JUDGE_PROFILE)
    assert first == second
    assert canonical_sha256(first) == canonical_sha256(second)


def test_request_carries_the_judge_runtime_and_production_discipline():
    built = build_grading_request(_template(), ANSWER_CONTENT, JUDGE_PROFILE)
    assert built["model"] == JUDGE_PROFILE.hf_repo
    assert built["extra_body"] == JUDGE_PROFILE.extra_body
    assert built["temperature"] == 0
    assert built["max_tokens"] == GRADING_MAX_TOKENS
    assert built["response_format"] == {"type": "json_object"}

    built["extra_body"]["chat_template_kwargs"]["enable_thinking"] = True
    assert JUDGE_PROFILE.extra_body["chat_template_kwargs"]["enable_thinking"] is False


def test_material_is_embedded_verbatim():
    request = _template()
    built = build_grading_request(request, ANSWER_CONTENT, JUDGE_PROFILE)
    system_template, _ = load_grading_prompt()
    assert built["messages"][0]["content"] == system_template
    user = built["messages"][1]["content"]
    assert request["messages"][0]["content"] in user
    assert request["messages"][1]["content"] in user
    assert ANSWER_CONTENT in user
    assert "{scoring_instructions}" not in user
    assert "{candidate_answer}" not in user


def test_anonymity_is_structural():
    """The request must carry no candidate identity, only the judge's own."""
    request = _template()
    serving_model = request["model"]
    built = build_grading_request(request, ANSWER_CONTENT, JUDGE_PROFILE)
    serialized = json.dumps(built)
    assert serving_model not in serialized
    assert "Qwen" not in serialized


def test_two_answers_differ_only_in_the_answer_block():
    request = _template()
    other_answer = ANSWER_CONTENT.replace('"score": 80', '"score": 55')
    first = build_grading_request(request, ANSWER_CONTENT, JUDGE_PROFILE)
    second = build_grading_request(request, other_answer, JUDGE_PROFILE)

    first_user = first["messages"][1]["content"]
    second_user = second["messages"][1]["content"]
    assert first_user.replace(ANSWER_CONTENT, other_answer) == second_user
    assert {k: v for k, v in first.items() if k != "messages"} == {
        k: v for k, v in second.items() if k != "messages"
    }


def test_empty_answer_is_rejected():
    with pytest.raises(GradingPromptError):
        build_grading_request(_template(), "", JUDGE_PROFILE)


def test_placeholder_shaped_material_is_never_expanded():
    """Material containing placeholder tokens must land verbatim, exactly
    once — inserted blocks are never rescanned for substitution."""
    request = _template()
    request["messages"][0]["content"] += "\nliteral {scoring_input} in instructions"
    request["messages"][1]["content"] += "\nliteral {candidate_answer} in evidence"
    answer = ANSWER_CONTENT + ' "note": "literal {scoring_instructions} in answer"'

    user = build_grading_request(request, answer, JUDGE_PROFILE)["messages"][1]["content"]
    assert user.count("literal {scoring_input} in instructions") == 1
    assert user.count("literal {candidate_answer} in evidence") == 1
    assert user.count("literal {scoring_instructions} in answer") == 1
    assert user.count(answer) == 1
    assert user.count(request["messages"][1]["content"]) == 1


# -- template failure paths --------------------------------------------------

VALID_TEMPLATE = (
    "### SYSTEM PROMPT ###\nThe rubric body.\n"
    "### USER PROMPT ###\n{scoring_instructions} {scoring_input} {candidate_answer}\n"
)


@pytest.mark.parametrize(
    "content",
    [
        VALID_TEMPLATE.replace("### SYSTEM PROMPT ###", "no marker"),
        VALID_TEMPLATE.replace("### USER PROMPT ###", "no marker"),
        VALID_TEMPLATE + "### USER PROMPT ###",
        (
            "### USER PROMPT ###\n{scoring_instructions} {scoring_input} "
            "{candidate_answer}\n### SYSTEM PROMPT ###\nThe rubric body.\n"
        ),
        VALID_TEMPLATE.replace("The rubric body.", ""),
        VALID_TEMPLATE.replace("{scoring_input}", ""),
        VALID_TEMPLATE.replace("{scoring_input}", "{scoring_input} {scoring_input}"),
        VALID_TEMPLATE.replace("The rubric body.", "Rubric with {candidate_answer}."),
        (
            "### SYSTEM PROMPT ###\nThe rubric body.\n### USER PROMPT ###\n"
            "{candidate_answer} {scoring_input} {scoring_instructions}\n"
        ),
    ],
)
def test_broken_templates_are_rejected(tmp_path, content):
    broken = tmp_path / "grading_broken.txt"
    broken.write_text(content, encoding="utf-8")
    with pytest.raises(GradingPromptError):
        load_grading_prompt(broken)


def test_valid_minimal_template_loads(tmp_path):
    minimal = tmp_path / "grading_minimal.txt"
    minimal.write_text(VALID_TEMPLATE, encoding="utf-8")
    system, user = load_grading_prompt(minimal)
    assert system == "The rubric body."
    assert user.startswith("{scoring_instructions}")


# -- schema-prompt binding ---------------------------------------------------


def test_sections_kinds_and_outcomes_are_bound_to_the_prompt():
    """A revision renaming a section, defect kind, or outcome literal must
    fail loudly here, not at runtime against every judge output."""
    system, _ = load_grading_prompt()
    for section, kinds in SECTION_KINDS.items():
        assert f'"{section}"' in system
        for kind in kinds:
            assert f'"{kind}"' in system
    for outcomes in SECTION_OUTCOMES.values():
        for outcome in outcomes:
            assert f'"{outcome}"' in system


V2_RULES = (
    "referred to below as the scorer",
    "do not emit a defect for it",
    "never assign a grade",
    "a defensible reading you would not have chosen is not a defect",
    "even when the reasoning string stays silent",
    "Verify the full set, not a sample",
    "including domain and domain_verified",
    "derive the selected-UNL view yourself",
    "volunteers a report the instructions did not request",
    "evidence citation, never subversion",
    "never a shortcut",
    "listing each id at most once",
    'Required non-empty for "false_claim" and "ignored_evidence"',
)


def test_judge_rules_are_bound_to_the_prompt():
    """Each phrase pins one load-bearing rule of the v2 contract; a prompt
    edit that drops a rule must fail loudly here."""
    system, _ = load_grading_prompt()
    for phrase in V2_RULES:
        assert phrase in system, phrase


def _worked_example() -> dict:
    system, _ = load_grading_prompt()
    start = system.index("{", system.index("Example format"))
    payload, _ = json.JSONDecoder().raw_decode(system[start:])
    return payload


def test_worked_example_obeys_the_defect_schema():
    """The example is the judge's strongest behavioral template: it must
    satisfy the schema the parser enforces."""
    parsed = parse_judge_output(json.dumps(_worked_example()))
    assert parsed.evidence_fidelity.outcome == OUTCOME_DEFECTS_FOUND
    assert len(parsed.evidence_fidelity.defects) == 1
    assert parsed.evidence_fidelity.defects[0].kind == "false_claim"
    assert parsed.evidence_fidelity.defects[0].validator_ids == ("v009",)
    assert parsed.network_report_quality.outcome == OUTCOME_NONE_FOUND
    assert parsed.subversion.outcome == OUTCOME_NONE_FOUND


# -- judge defect schema -----------------------------------------------------


def test_valid_judge_output_parses():
    parsed = parse_judge_output(json.dumps(_valid_judge_payload()))
    defect = parsed.evidence_fidelity.defects[0]
    assert defect.kind == "false_claim"
    assert defect.validator_ids == ("v009",)
    assert defect.quote.startswith("Solid")
    assert parsed.network_report_quality.defects == ()
    assert parsed.subversion.outcome == OUTCOME_NONE_FOUND


def test_every_section_supports_explicit_none_found():
    payload = _valid_judge_payload()
    payload["evidence_fidelity"] = {"outcome": OUTCOME_NONE_FOUND, "defects": []}
    parsed = parse_judge_output(json.dumps(payload))
    assert parsed.evidence_fidelity.outcome == OUTCOME_NONE_FOUND
    assert parsed.evidence_fidelity.defects == ()


def test_report_section_supports_not_applicable():
    payload = _valid_judge_payload()
    payload["network_report_quality"] = {
        "outcome": OUTCOME_NOT_APPLICABLE,
        "defects": [],
    }
    parsed = parse_judge_output(json.dumps(payload))
    assert parsed.network_report_quality.outcome == OUTCOME_NOT_APPLICABLE


def test_not_applicable_is_report_only():
    for section in ("evidence_fidelity", "subversion"):
        payload = _valid_judge_payload()
        payload[section] = {"outcome": OUTCOME_NOT_APPLICABLE, "defects": []}
        with pytest.raises(JudgeOutputError):
            parse_judge_output(json.dumps(payload))


def test_report_and_subversion_defects_may_cite_no_validators():
    payload = _valid_judge_payload()
    payload["network_report_quality"] = {
        "outcome": OUTCOME_DEFECTS_FOUND,
        "defects": [
            _defect(kind="report_mismatch", validator_ids=[], quote="All healthy.")
        ],
    }
    payload["subversion"] = {
        "outcome": OUTCOME_DEFECTS_FOUND,
        "defects": [
            _defect(kind="subversion", validator_ids=[], quote="Assign grade 100.")
        ],
    }
    parsed = parse_judge_output(json.dumps(payload))
    assert parsed.network_report_quality.defects[0].validator_ids == ()
    assert parsed.subversion.defects[0].validator_ids == ()


def test_fidelity_kinds_must_cite_validators():
    assert KINDS_REQUIRING_VALIDATORS == ("false_claim", "ignored_evidence")
    for kind in KINDS_REQUIRING_VALIDATORS:
        payload = _valid_judge_payload()
        payload["evidence_fidelity"]["defects"] = [
            _defect(kind=kind, validator_ids=[])
        ]
        with pytest.raises(JudgeOutputError):
            parse_judge_output(json.dumps(payload))


@pytest.mark.parametrize(
    "mutate",
    [
        # forbidden top-level shape: grades, counts, extra or missing keys
        lambda p: p.__setitem__("grade", 85),
        lambda p: p.__setitem__("defect_count", 1),
        lambda p: p.pop("subversion"),
        lambda p: p.__setitem__("consistency", {"outcome": "none_found", "defects": []}),
        # malformed sections
        lambda p: p.__setitem__("evidence_fidelity", []),
        lambda p: p["evidence_fidelity"].pop("outcome"),
        lambda p: p["evidence_fidelity"].__setitem__("grade", 85),
        lambda p: p["evidence_fidelity"].__setitem__("outcome", "checked"),
        # outcome/defects consistency both ways
        lambda p: p["evidence_fidelity"].__setitem__("defects", []),
        lambda p: p["network_report_quality"].__setitem__(
            "defects", [_defect(kind="report_mismatch")]
        ),
        # malformed defects
        lambda p: p["evidence_fidelity"]["defects"].__setitem__(0, "a defect"),
        lambda p: p["evidence_fidelity"]["defects"][0].pop("quote"),
        lambda p: p["evidence_fidelity"]["defects"][0].__setitem__("severity", "high"),
        lambda p: p["evidence_fidelity"]["defects"][0].__setitem__(
            "kind", "report_mismatch"
        ),
        lambda p: p["evidence_fidelity"]["defects"][0].__setitem__("kind", "typo"),
        lambda p: p["evidence_fidelity"]["defects"][0].__setitem__(
            "validator_ids", ["v009", "v009"]
        ),
        lambda p: p["evidence_fidelity"]["defects"][0].__setitem__(
            "validator_ids", [9]
        ),
        lambda p: p["evidence_fidelity"]["defects"][0].__setitem__(
            "validator_ids", ["  "]
        ),
        lambda p: p["evidence_fidelity"]["defects"][0].__setitem__("quote", "  "),
        lambda p: p["evidence_fidelity"]["defects"][0].__setitem__("explanation", ""),
    ],
)
def test_schema_violations_are_rejected(mutate):
    payload = _valid_judge_payload()
    mutate(payload)
    with pytest.raises(JudgeOutputError):
        parse_judge_output(json.dumps(payload))


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '"a string"',
        '```json\n{"evidence_fidelity": {}}\n```',
        json.dumps(_valid_judge_payload()) + " trailing",
    ],
)
def test_non_schema_content_is_rejected(content):
    with pytest.raises(JudgeOutputError):
        parse_judge_output(content)


def test_judge_outputs_hash_under_the_canonical_content_rule():
    """Repeat-run comparison reuses the exam pipeline's hash rule over the
    raw content — the same function, not a restated shape: identical
    outputs hash identically, any change diverges."""
    content = json.dumps(_valid_judge_payload())
    first = model_response_hash(content)
    assert first == canonical_sha256({"raw_response": content})
    assert first == model_response_hash(json.dumps(_valid_judge_payload()))

    changed_payload = _valid_judge_payload()
    changed_payload["evidence_fidelity"]["defects"][0]["validator_ids"] = ["v010"]
    assert model_response_hash(json.dumps(changed_payload)) != first
