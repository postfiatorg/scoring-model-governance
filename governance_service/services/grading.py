"""Grading request construction and judge-output validation.

The grading request is a frozen derivation, like the exam's request
adaptation: from one corpus item's frozen request, one candidate's stored
answer content, and one judge's runtime profile, it deterministically
renders the chat-completions request the judge grades with. Anonymity is
structural — the builder never receives the candidate's identity, so no
grading request can carry it: the judge sees the scoring instructions,
the scoring input, and the answer content, nothing else.

The versioned grading prompt lives in ``prompts/grading_v<N>.txt`` with
the scoring pipeline's template convention (system and user sections
split by markers). Under the G.4 checker/judge/formula split the judge
owns only the language checks: its output is a set of structured defect
objects validated here — the judge defect schema (G.4.2) — with no
grade, no counts, and no severity anywhere in it. The mechanical
grading checker (G.4.4) owns every defect kind with a closed-form right
answer, and the grade formula (G.4.5) computes grades from the two
defect lists.
"""

import copy
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from governance_service.models.runtime_profile import RuntimeProfile

GRADING_PROMPT_VERSION = 2
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
GRADING_PROMPT_PATH = PROMPTS_DIR / f"grading_v{GRADING_PROMPT_VERSION}.txt"

SYSTEM_MARKER = "### SYSTEM PROMPT ###"
USER_MARKER = "### USER PROMPT ###"

INSTRUCTIONS_PLACEHOLDER = "{scoring_instructions}"
INPUT_PLACEHOLDER = "{scoring_input}"
ANSWER_PLACEHOLDER = "{candidate_answer}"
PLACEHOLDERS = (INSTRUCTIONS_PLACEHOLDER, INPUT_PLACEHOLDER, ANSWER_PLACEHOLDER)

# Defect lists on a badly flawed answer run long; still half of
# production scoring's 16384.
GRADING_MAX_TOKENS = 8192

OUTCOME_DEFECTS_FOUND = "defects_found"
OUTCOME_NONE_FOUND = "none_found"
OUTCOME_NOT_APPLICABLE = "not_applicable"

# The judge-owned defect kinds, exclusive to it by the G.4 split: the
# mechanical checker owns every kind with a closed-form right answer.
SECTION_KINDS = {
    "evidence_fidelity": ("false_claim", "ignored_evidence"),
    "network_report_quality": ("report_mismatch",),
    "subversion": ("subversion",),
}
SECTION_OUTCOMES = {
    "evidence_fidelity": (OUTCOME_DEFECTS_FOUND, OUTCOME_NONE_FOUND),
    "network_report_quality": (
        OUTCOME_DEFECTS_FOUND,
        OUTCOME_NONE_FOUND,
        OUTCOME_NOT_APPLICABLE,
    ),
    "subversion": (OUTCOME_DEFECTS_FOUND, OUTCOME_NONE_FOUND),
}
# Kinds whose defect must cite at least one validator; report- and
# answer-level defects may legitimately cite none.
KINDS_REQUIRING_VALIDATORS = ("false_claim", "ignored_evidence")


class GradingPromptError(ValueError):
    """Raised when the grading-request derivation contract is violated —
    a malformed prompt template, exam request, or answer content."""


class JudgeOutputError(ValueError):
    """Raised when a judge's output violates the defect schema."""


@dataclass(frozen=True)
class JudgeDefect:
    """One reported defect: the quoted claim and the contradiction."""

    kind: str
    validator_ids: tuple[str, ...]
    quote: str
    explanation: str


@dataclass(frozen=True)
class SectionFinding:
    """One section's explicit outcome and its defects."""

    outcome: str
    defects: tuple[JudgeDefect, ...]


@dataclass(frozen=True)
class JudgeOutput:
    """One validated judge answer under the defect schema."""

    evidence_fidelity: SectionFinding
    network_report_quality: SectionFinding
    subversion: SectionFinding


@lru_cache(maxsize=None)
def load_grading_prompt(path: Path = GRADING_PROMPT_PATH) -> tuple[str, str]:
    """The (system, user) template pair, validated against the contract.

    The system section must carry no placeholder — it is the frozen
    rubric. The user section must carry each placeholder exactly once
    and in the canonical order, so a rendered request can neither drop,
    duplicate, nor reorder material.
    """
    raw = path.read_text(encoding="utf-8")
    if raw.count(SYSTEM_MARKER) != 1 or raw.count(USER_MARKER) != 1:
        raise GradingPromptError(
            f"{path.name} must contain exactly one {SYSTEM_MARKER!r} and "
            f"one {USER_MARKER!r} marker"
        )
    if raw.index(SYSTEM_MARKER) > raw.index(USER_MARKER):
        raise GradingPromptError(
            f"{path.name} must open with the system section, then the user section"
        )
    system_part, user_part = raw.split(USER_MARKER)
    system = system_part.replace(SYSTEM_MARKER, "").strip()
    user = user_part.strip()
    if not system or not user:
        raise GradingPromptError(f"{path.name} sections must both be non-empty")
    for placeholder in PLACEHOLDERS:
        if system.count(placeholder) != 0:
            raise GradingPromptError(
                f"{path.name} system section must not contain {placeholder}"
            )
        if user.count(placeholder) != 1:
            raise GradingPromptError(
                f"{path.name} user section must contain {placeholder} exactly once"
            )
    if [user.index(p) for p in PLACEHOLDERS] != sorted(
        user.index(p) for p in PLACEHOLDERS
    ):
        raise GradingPromptError(
            f"{path.name} user section must carry the placeholders in the "
            f"order {PLACEHOLDERS}"
        )
    return system, user


def exam_request_messages(exam_request: dict[str, Any]) -> tuple[str, str]:
    """The (system, user) message contents of a frozen exam request.

    Every corpus request — historical or constructed — carries exactly a
    system message (the scoring instructions) and a user message (the
    scoring input); anything else is not a valid grading source.
    """
    messages = exam_request.get("messages")
    if not isinstance(messages, list) or len(messages) != 2:
        raise GradingPromptError(
            "Exam request must carry exactly two messages (system, user)"
        )
    contents = []
    for index, role in ((0, "system"), (1, "user")):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != role:
            raise GradingPromptError(
                f"Exam request message {index} must have role {role!r}"
            )
        content = message.get("content")
        if not isinstance(content, str) or not content:
            raise GradingPromptError(
                f"Exam request message {index} must carry non-empty string content"
            )
        contents.append(content)
    return contents[0], contents[1]


def build_grading_request(
    exam_request: dict[str, Any], answer_content: str, judge: RuntimeProfile
) -> dict[str, Any]:
    """One judge-ready grading request for one anonymous answer.

    Pure and deterministic: the same corpus request, answer content, and
    judge profile always render the identical request. Material blocks
    are substituted verbatim (no escaping, no reformatting), so any
    verifier can reconstruct the request from the frozen corpus, the
    revealed answer, and the judge's frozen profile alone.
    """
    if not isinstance(answer_content, str) or not answer_content:
        raise GradingPromptError("Candidate answer content must be a non-empty string")
    scoring_instructions, scoring_input = exam_request_messages(exam_request)
    system, user_template = load_grading_prompt()
    # Partition the template at its placeholders instead of sequential
    # replacement: material blocks may themselves contain
    # placeholder-shaped text (validator-controlled strings reach the
    # scoring input verbatim), and inserted material must never be
    # rescanned for further substitution.
    segments = []
    remainder = user_template
    for placeholder, block in (
        (INSTRUCTIONS_PLACEHOLDER, scoring_instructions),
        (INPUT_PLACEHOLDER, scoring_input),
        (ANSWER_PLACEHOLDER, answer_content),
    ):
        before, remainder = remainder.split(placeholder, 1)
        segments += [before, block]
    user = "".join(segments) + remainder
    return {
        "model": judge.hf_repo,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "extra_body": copy.deepcopy(judge.extra_body),
        "max_tokens": GRADING_MAX_TOKENS,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }


def _parse_defect(section: str, index: int, payload: Any) -> JudgeDefect:
    label = f"{section}.defects[{index}]"
    if not isinstance(payload, dict):
        raise JudgeOutputError(f"{label} must be a JSON object")
    if set(payload) != {"kind", "validator_ids", "quote", "explanation"}:
        raise JudgeOutputError(
            f"{label} must have exactly the keys kind, validator_ids, "
            f"quote, explanation"
        )

    kind = payload["kind"]
    if kind not in SECTION_KINDS[section]:
        raise JudgeOutputError(
            f"{label}.kind must be one of {SECTION_KINDS[section]}"
        )

    validator_ids = payload["validator_ids"]
    if not isinstance(validator_ids, list) or not all(
        isinstance(v, str) and v.strip() for v in validator_ids
    ):
        raise JudgeOutputError(
            f"{label}.validator_ids must be a list of non-empty strings"
        )
    if len(set(validator_ids)) != len(validator_ids):
        raise JudgeOutputError(f"{label}.validator_ids must not repeat ids")
    if kind in KINDS_REQUIRING_VALIDATORS and not validator_ids:
        raise JudgeOutputError(
            f"{label}: a {kind} defect must cite at least one validator id"
        )

    for field in ("quote", "explanation"):
        value = payload[field]
        if not isinstance(value, str) or not value.strip():
            raise JudgeOutputError(f"{label}.{field} must be a non-empty string")

    return JudgeDefect(
        kind=kind,
        validator_ids=tuple(validator_ids),
        quote=payload["quote"],
        explanation=payload["explanation"],
    )


def _parse_section(section: str, payload: Any) -> SectionFinding:
    if not isinstance(payload, dict):
        raise JudgeOutputError(f"{section} must be a JSON object")
    if set(payload) != {"outcome", "defects"}:
        raise JudgeOutputError(
            f"{section} must have exactly the keys outcome, defects"
        )

    outcome = payload["outcome"]
    if outcome not in SECTION_OUTCOMES[section]:
        raise JudgeOutputError(
            f"{section}.outcome must be one of {SECTION_OUTCOMES[section]}"
        )

    defects_payload = payload["defects"]
    if not isinstance(defects_payload, list):
        raise JudgeOutputError(f"{section}.defects must be a list")
    defects = tuple(
        _parse_defect(section, index, defect)
        for index, defect in enumerate(defects_payload)
    )

    if outcome == OUTCOME_DEFECTS_FOUND and not defects:
        raise JudgeOutputError(
            f"{section}: outcome {OUTCOME_DEFECTS_FOUND} requires at least one defect"
        )
    if outcome != OUTCOME_DEFECTS_FOUND and defects:
        raise JudgeOutputError(
            f"{section}: outcome {outcome} must carry no defects"
        )
    return SectionFinding(outcome=outcome, defects=defects)


def parse_judge_output(content: str) -> JudgeOutput:
    """Validate one judge answer against the defect schema.

    Deliberately strict: the judge must emit exactly the schema's JSON
    object with nothing around it, and nothing is repaired here. Every
    section states an explicit outcome, so an absent check is a
    contract violation rather than a silent omission; repeat stability
    is enforced at the byte level by the determinism rule.
    """
    try:
        payload = json.loads(content)
    except ValueError as exc:
        raise JudgeOutputError(f"Judge output is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise JudgeOutputError("Judge output must be a JSON object")
    if set(payload) != set(SECTION_KINDS):
        raise JudgeOutputError(
            f"Judge output must have exactly the keys {tuple(SECTION_KINDS)}"
        )
    return JudgeOutput(
        **{
            section: _parse_section(section, payload[section])
            for section in SECTION_KINDS
        }
    )
