"""Grading request construction and grade-output validation.

The grading request is a frozen derivation, like the exam's request
adaptation: from one corpus item's frozen request, one candidate's stored
answer content, and one judge's runtime profile, it deterministically
renders the chat-completions request the judge grades with. Anonymity is
structural — the builder never receives the candidate's identity, so no
grading request can carry it: the judge sees the scoring instructions,
the scoring input, and the answer content, nothing else.

The versioned grading prompt lives in ``prompts/grading_v<N>.txt`` with
the scoring pipeline's template convention (system and user sections
split by markers). Grade outputs are validated here to the v1 contract —
four rubric findings, one absolute grade in multiples of 5, a
justification — which G.4.2 formalizes into the frozen grade schema,
parser, and canonical hashing.
"""

import copy
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from governance_service.models.runtime_profile import RuntimeProfile

GRADING_PROMPT_VERSION = 1
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
GRADING_PROMPT_PATH = PROMPTS_DIR / f"grading_v{GRADING_PROMPT_VERSION}.txt"

SYSTEM_MARKER = "### SYSTEM PROMPT ###"
USER_MARKER = "### USER PROMPT ###"

INSTRUCTIONS_PLACEHOLDER = "{scoring_instructions}"
INPUT_PLACEHOLDER = "{scoring_input}"
ANSWER_PLACEHOLDER = "{candidate_answer}"
PLACEHOLDERS = (INSTRUCTIONS_PLACEHOLDER, INPUT_PLACEHOLDER, ANSWER_PLACEHOLDER)

# Findings plus one grade are far smaller than a scoring answer; generous
# headroom without production's 16384.
GRADING_MAX_TOKENS = 4096

GRADE_CRITERIA = (
    "evidence_fidelity",
    "instruction_adherence",
    "cross_validator_consistency",
    "network_report_quality",
)
GRADE_MIN = 0
GRADE_MAX = 100
GRADE_STEP = 5


class GradingPromptError(ValueError):
    """Raised when the grading-request derivation contract is violated —
    a malformed prompt template, exam request, or answer content."""


class GradeOutputError(ValueError):
    """Raised when a judge's output violates the grade-output contract."""


@dataclass(frozen=True)
class GradeOutput:
    """One validated grading answer."""

    criteria: dict[str, str]
    grade: int
    justification: str


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


def parse_grade_output(content: str) -> GradeOutput:
    """Validate one judge answer against the v1 grade-output contract.

    Deliberately strict: the judge must emit exactly the contract's JSON
    object with nothing around it, and nothing is repaired here. Key
    order is deliberately not checked — the prompt requests an order to
    shape the judge's reasoning, while repeat stability is enforced at
    the byte level by the determinism rule.
    """
    try:
        payload = json.loads(content)
    except ValueError as exc:
        raise GradeOutputError(f"Grade output is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise GradeOutputError("Grade output must be a JSON object")
    if set(payload) != {"criteria", "grade", "justification"}:
        raise GradeOutputError(
            "Grade output must have exactly the keys criteria, grade, justification"
        )

    criteria = payload["criteria"]
    if not isinstance(criteria, dict) or set(criteria) != set(GRADE_CRITERIA):
        raise GradeOutputError(
            f"criteria must be an object with exactly the keys {GRADE_CRITERIA}"
        )
    findings: dict[str, str] = {}
    for name in GRADE_CRITERIA:
        finding = criteria[name]
        if not isinstance(finding, str) or not finding.strip():
            raise GradeOutputError(f"criteria.{name} must be a non-empty string")
        findings[name] = finding

    grade = payload["grade"]
    if isinstance(grade, bool) or not isinstance(grade, int):
        raise GradeOutputError("grade must be an integer")
    if not GRADE_MIN <= grade <= GRADE_MAX:
        raise GradeOutputError(f"grade must be between {GRADE_MIN} and {GRADE_MAX}")
    if grade % GRADE_STEP:
        raise GradeOutputError(f"grade must be a multiple of {GRADE_STEP}")

    justification = payload["justification"]
    if not isinstance(justification, str) or not justification.strip():
        raise GradeOutputError("justification must be a non-empty string")

    return GradeOutput(
        criteria=findings, grade=grade, justification=justification
    )
