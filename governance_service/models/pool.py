"""Data models for the pool-refresh layer."""

from pydantic import BaseModel

from governance_service.models.candidates import CandidateDescriptor, Precision


class BlocklistEntry(BaseModel):
    """One standing-blocklist entry: a pinned revision that failed a round."""

    hf_repo: str
    revision: str
    reason: str
    round_reference: str


class CandidateEvaluation(BaseModel):
    """One candidate's rule outcome within a refresh evaluation."""

    descriptor: CandidateDescriptor
    is_incumbent: bool = False
    in_pool: bool
    exclusion_rule: str | None = None


class IncumbentMember(BaseModel):
    """The incumbent's pool entry, present in every refresh by right.

    Leaderboard standing fields are optional because the incumbent is
    exempt from the leaderboard rule; they are filled in when the
    incumbent happens to appear on the release used.
    """

    hf_repo: str
    revision: str
    precision: Precision
    weight_bytes: int
    license: str | None
    gated: bool
    assigned_gpu: str | None
    livebench_key: str | None = None
    display_name: str | None = None
    organization: str | None = None
    family: str | None = None
    global_average: float | None = None
    category_averages: dict[str, float] | None = None


class ReleaseOutcome(BaseModel):
    """The headline result of evaluating one considered release."""

    release: str
    challenger_count: int
    viable: bool
    fallback_reason: str | None = None
    unmapped: list[str]
    skipped: dict[str, str]


class ReleaseEvaluation(BaseModel):
    """The full evaluation of one release: outcome plus rule detail."""

    outcome: ReleaseOutcome
    evaluations: list[CandidateEvaluation]


class RefreshResult(BaseModel):
    """The complete result of one pool refresh.

    Every considered release keeps its full per-candidate rule detail so
    the audit record shows why each release fell back, not just that it
    did — including the no-viable-pool case, where the detail is the
    finding.
    """

    status: str
    release_used: str | None
    incumbent: IncumbentMember
    releases: list[ReleaseEvaluation]
