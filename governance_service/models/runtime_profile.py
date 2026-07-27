"""The candidate runtime profile consumed by the request-adaptation rule.

This is the rule's minimal frozen input contract: exactly the fields the
adaptation derives per-candidate request bytes from. The exam-runtime work
(G.3.3) extends this schema with deployment fields (pinned revision, GPU,
serving image and arguments) without changing the adaptation rule.
"""

from typing import Any

from pydantic import BaseModel, Field


class RuntimeProfile(BaseModel):
    """The profile-derived identity a request carries for one candidate.

    ``hf_repo`` fills the request's ``model`` field — production serves
    models under their HuggingFace repository id — and ``extra_body`` is
    the request's chat-template settings block verbatim, carrying the
    candidate's thinking-off mechanism (e.g. the incumbent's
    ``chat_template_kwargs.enable_thinking: false``).
    """

    hf_repo: str = Field(min_length=1)
    extra_body: dict[str, Any]
