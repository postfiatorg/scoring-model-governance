"""Deployable runtime profiles for the current candidate pool.

One profile per pool member of devnet pool refresh 1, pinned to the
revisions the refresh recorded and to the production serving discipline:
the digest-pinned SGLang image and deterministic launch arguments the
scoring endpoint runs (dynamic-unl-scoring `infra/deploy_endpoint.py`,
mirrored in every execution manifest), with thinking disabled explicitly
for every candidate.

Thinking-off evidence, per candidate (fail-closed — each value is
established from a public artifact, never guessed):

- ``Qwen/Qwen3.6-27B-FP8`` (incumbent): production serves it with
  ``chat_template_kwargs.enable_thinking: false`` — the value every frozen
  round request carries (``inputs/model_request.json`` extra_body).
- ``Qwen/Qwen3-32B-FP8``: same Qwen hybrid template family as the
  incumbent; its chat template carries the ``enable_thinking`` toggle,
  validated weekly by the mapping-freshness check (hybrid ⇒ toggle
  present).
- ``google/gemma-4-31B-it``: ``chat_template.jinja`` at the pinned
  revision sets ``enable_thinking = enable_thinking | default(false)`` and
  injects the thinking system block only when the toggle is on — the same
  kwarg production already uses, disabled explicitly here.

The reasoning parser is Qwen-specific serving plumbing (it strips think
blocks from Qwen outputs); Gemma serves without one.
"""

from governance_service.models.runtime_profile import RuntimeProfile

PRODUCTION_SGLANG_IMAGE = (
    "lmsysorg/sglang:nightly-dev-cu13-20260430-e60c60ef"
    "@sha256:5d9ec71597ade6b8237d61ae6f01b976cb3d5ad2c1e3cf4e0acaf27a9ff49a65"
)

FLASHINFER_ENVIRONMENT = {"SGLANG_FLASHINFER_WORKSPACE_SIZE": "2147483648"}

THINKING_OFF_EXTRA_BODY = {"chat_template_kwargs": {"enable_thinking": False}}

# The production deterministic serving arguments (deploy_endpoint.py);
# model path, served name, host, port, and tensor parallelism are composed
# by the exam app from the profile's own fields.
_DETERMINISTIC_ARGS = [
    "--mem-fraction-static",
    "0.75",
    "--chunked-prefill-size",
    "4096",
    "--max-running-requests",
    "1",
    "--enable-deterministic-inference",
    "--enable-metrics",
    "--trust-remote-code",
]

_QWEN_REASONING_PARSER_ARGS = ["--reasoning-parser", "qwen3"]


def _profile(
    hf_repo: str, revision: str, gpu: str, *, reasoning_parser: bool
) -> RuntimeProfile:
    launch_args = list(_DETERMINISTIC_ARGS)
    if reasoning_parser:
        launch_args += _QWEN_REASONING_PARSER_ARGS
    return RuntimeProfile(
        hf_repo=hf_repo,
        extra_body=dict(THINKING_OFF_EXTRA_BODY),
        revision=revision,
        gpu=gpu,
        image=PRODUCTION_SGLANG_IMAGE,
        launch_args=launch_args,
        environment=dict(FLASHINFER_ENVIRONMENT),
    )


CURRENT_POOL_PROFILES: dict[str, RuntimeProfile] = {
    "Qwen/Qwen3.6-27B-FP8": _profile(
        "Qwen/Qwen3.6-27B-FP8",
        "e89b16ebf1988b3d6befa7de50abc2d76f26eb09",
        "H100",
        reasoning_parser=True,
    ),
    "google/gemma-4-31B-it": _profile(
        "google/gemma-4-31B-it",
        "842da3794eaa0b77d5f08bae87a17459d91ff475",
        "H200",
        reasoning_parser=False,
    ),
    "Qwen/Qwen3-32B-FP8": _profile(
        "Qwen/Qwen3-32B-FP8",
        "aa55da1ecc13d006e8b8e4f54579b1ea8c3db2df",
        "H100",
        reasoning_parser=True,
    ),
}
