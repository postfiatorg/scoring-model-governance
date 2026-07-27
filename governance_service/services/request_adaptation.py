"""The request-adaptation rule: one exam request, re-addressed per candidate.

Every corpus request embeds the then-serving model's identity — its model
identifier and chat-template settings — so no other candidate can replay
it verbatim. This rule is the methodology's frozen derivation: given one
corpus request and a candidate's runtime profile, it rewrites exactly the
profile-derived fields (``ADAPTED_FIELDS``) and nothing else, as a pure
function — no network, no configuration, no nondeterminism — so any
verifier reconstructs the identical per-candidate requests from the frozen
corpus and profiles alone.
"""

import copy
from typing import Any, Mapping

from governance_service.models.runtime_profile import RuntimeProfile

# The only request fields the adaptation may change. Everything else —
# messages, temperature, max_tokens, response_format, method — is serving
# discipline shared by every candidate and must survive byte-identically.
ADAPTED_FIELDS = ("model", "extra_body")


class RequestAdaptationError(RuntimeError):
    """Raised when a request does not carry the adaptable production shape."""


def _require_production_shape(request: Mapping[str, Any]) -> None:
    for field in ADAPTED_FIELDS:
        if field not in request:
            raise RequestAdaptationError(
                f"Request has no {field!r} field — not an adaptable production request"
            )
    if not isinstance(request["model"], str):
        raise RequestAdaptationError("Request 'model' is not a string")
    if not isinstance(request["extra_body"], Mapping):
        raise RequestAdaptationError("Request 'extra_body' is not a JSON object")


def extract_profile(request: Mapping[str, Any]) -> RuntimeProfile:
    """The runtime profile embedded in a request's own bytes.

    Adapting a request to its extracted profile is the identity operation —
    the property the byte-stability tests anchor on.
    """
    _require_production_shape(request)
    return RuntimeProfile(
        hf_repo=request["model"],
        extra_body=copy.deepcopy(request["extra_body"]),
    )


def adapt_request(
    request: Mapping[str, Any], profile: RuntimeProfile
) -> dict[str, Any]:
    """One corpus request re-addressed to a candidate, nothing else changed.

    The input is never mutated; the adapted request shares no structure
    with it, so downstream use cannot alias corpus items.
    """
    _require_production_shape(request)
    adapted = copy.deepcopy(dict(request))
    adapted["model"] = profile.hf_repo
    adapted["extra_body"] = copy.deepcopy(profile.extra_body)
    return adapted
