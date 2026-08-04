"""The grading request derivation: template contract, purity, anonymity."""

import json

import pytest

from governance_service.models import RuntimeProfile
from governance_service.scoring import canonical_sha256
from governance_service.services import edge_cases
from governance_service.services.grading import (
    GRADE_CRITERIA,
    GRADING_MAX_TOKENS,
    GradeOutputError,
    GradingPromptError,
    build_grading_request,
    exam_request_messages,
    load_grading_prompt,
    parse_grade_output,
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


def _valid_grade_payload() -> dict:
    return {
        "criteria": {name: f"Concrete finding for {name}." for name in GRADE_CRITERIA},
        "grade": 85,
        "justification": "One localized defect places the answer in the 80-90 band.",
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


def test_criteria_names_are_bound_to_the_prompt():
    """A prompt revision renaming a finding field must fail loudly here,
    not at runtime against every judge output."""
    system, _ = load_grading_prompt()
    for name in GRADE_CRITERIA:
        assert f'"{name}"' in system


# -- clarity-revision pins ---------------------------------------------------

CLARITY_RULES = (
    "referred to below as the scorer",
    "including rule violations visible in the numbers",
    "missing an input validator",
    "including domain and domain_verified",
    "When the shown instructions state such a ceiling",
    "the evidence fields the shown instructions assign to that dimension",
    "derive the selected-UNL view yourself",
    "volunteers a report the instructions did not request",
    "evidence citation, never subversion",
    "Still write all four findings normally",
    "states the total count of distinct material defects",
    "counting each underlying defect once",
    "the lowest applicable band wins",
    "100 with no blemish at all",
    "the band condition the answer met",
    "never below the band's bottom",
)


def test_clarity_rules_are_bound_to_the_prompt():
    """Each phrase pins one rule the ambiguity revision added; a prompt
    edit that drops a rule must fail loudly here."""
    system, _ = load_grading_prompt()
    for phrase in CLARITY_RULES:
        assert phrase in system, phrase


def _worked_example() -> dict:
    system, _ = load_grading_prompt()
    start = system.index("{", system.index("Example format"))
    payload, _ = json.JSONDecoder().raw_decode(system[start:])
    return payload


def test_worked_example_obeys_the_grade_output_contract():
    """The example is the judge's strongest behavioral template: it must
    satisfy the enforced output contract, grade at the top of the 80-90
    band its single localized defect selects, and model full-set
    verification rather than the spot-checking the rubric forbids."""
    parsed = parse_grade_output(json.dumps(_worked_example()))
    assert parsed.grade == 90
    assert "full-set" in parsed.criteria["evidence_fidelity"].lower()
    findings = json.dumps(parsed.criteria).lower()
    assert "spot-check" not in findings
    assert "spot check" not in findings


# -- grade-output contract ---------------------------------------------------


def test_valid_grade_output_parses():
    parsed = parse_grade_output(json.dumps(_valid_grade_payload()))
    assert parsed.grade == 85
    assert set(parsed.criteria) == set(GRADE_CRITERIA)
    assert parsed.justification.startswith("One localized defect")


def test_grade_zero_and_hundred_are_valid():
    for grade in (0, 100):
        payload = _valid_grade_payload()
        payload["grade"] = grade
        assert parse_grade_output(json.dumps(payload)).grade == grade


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.__setitem__("grade", 87),
        lambda p: p.__setitem__("grade", -5),
        lambda p: p.__setitem__("grade", 105),
        lambda p: p.__setitem__("grade", True),
        lambda p: p.__setitem__("grade", "85"),
        lambda p: p.__setitem__("grade", 85.0),
        lambda p: p.__setitem__("justification", "  "),
        lambda p: p.pop("justification"),
        lambda p: p.__setitem__("extra", "key"),
        lambda p: p.pop("criteria"),
        lambda p: p["criteria"].pop("evidence_fidelity"),
        lambda p: p["criteria"].__setitem__("bonus_criterion", "x"),
        lambda p: p["criteria"].__setitem__("instruction_adherence", ""),
        lambda p: p["criteria"].__setitem__("network_report_quality", 42),
    ],
)
def test_contract_violations_are_rejected(mutate):
    payload = _valid_grade_payload()
    mutate(payload)
    with pytest.raises(GradeOutputError):
        parse_grade_output(json.dumps(payload))


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '"a string"',
        '```json\n{"criteria": {}, "grade": 85, "justification": "x"}\n```',
        json.dumps({"criteria": {}, "grade": 85, "justification": "x"}) + " trailing",
    ],
)
def test_non_contract_content_is_rejected(content):
    with pytest.raises(GradeOutputError):
        parse_grade_output(content)
