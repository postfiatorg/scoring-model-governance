"""The candidate runtime profile: adaptation-rule contract plus deployment pins.

The minimal fields (``hf_repo``, ``extra_body``) are the request-adaptation
rule's input contract (G.3.2). The deployment fields pin how the candidate
serves on Modal (G.3.3), following the production execution manifest's
runtime conventions (``kind: modal_sglang``, digest-pinned image,
deterministic launch arguments). They are optional at the model level so a
minimal profile still satisfies the adaptation rule; the runtime manager
refuses to deploy a profile that has not pinned all of them.
"""

from typing import Any

from pydantic import BaseModel, Field

from governance_service.scoring import canonical_sha256

HF_REVISION_PATTERN = r"^[0-9a-f]{40}$"
DETERMINISTIC_INFERENCE_FLAG = "--enable-deterministic-inference"

DEPLOYMENT_FIELDS = ("revision", "gpu", "image", "launch_args")


class RuntimeProfile(BaseModel):
    """The frozen identity and serving configuration of one candidate.

    ``hf_repo`` fills the request's ``model`` field — production serves
    models under their HuggingFace repository id, which is also the served
    model name — and ``extra_body`` is the request's chat-template settings
    block verbatim, carrying the candidate's thinking-off mechanism (e.g.
    ``chat_template_kwargs.enable_thinking: false``).

    The deployment pins mirror the execution manifest's runtime block:
    ``revision`` is the full commit of the weight artifact, ``image`` the
    digest-pinned SGLang serving image, ``gpu`` the assigned class from the
    pool record, ``launch_args`` the deterministic SGLang arguments, and
    ``environment`` the serving environment variables.
    """

    hf_repo: str = Field(min_length=1)
    extra_body: dict[str, Any]

    revision: str | None = Field(default=None, pattern=HF_REVISION_PATTERN)
    gpu: str | None = None
    image: str | None = None
    tensor_parallelism: int = Field(default=1, ge=1)
    launch_args: list[str] | None = None
    environment: dict[str, str] = Field(default_factory=dict)

    def missing_deployment_fields(self) -> list[str]:
        """Deployment pins this profile has not set; empty means deployable."""
        return [name for name in DEPLOYMENT_FIELDS if not getattr(self, name)]

    def content_hash(self) -> str:
        """The canonical hash reuse decisions key on: same hash, same runtime."""
        return canonical_sha256(self.model_dump())
