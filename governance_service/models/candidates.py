"""Data models for the candidate-sourcing layer."""

from enum import Enum

from pydantic import BaseModel


class Precision(str, Enum):
    """Precision variant of a deployable model artifact.

    The methodology's variant rule admits only FP8 and full-precision
    artifacts into a pool; other detected precisions are carried honestly
    on the descriptor and excluded by the pool rules.
    """

    FP8 = "fp8"
    BF16 = "bf16"
    FP16 = "fp16"
    INT4 = "int4"


class ThinkingMode(str, Enum):
    """Curated thinking-mode class of a mapped model.

    Production serves the scoring model with thinking disabled, so only
    models that can serve without thinking are pool-eligible: NONE and
    HYBRID qualify; ALWAYS cannot disable thinking, and UNKNOWN records
    that the class could not be established with evidence — both are
    excluded by the pool rules (fail closed, never guessed).
    """

    NONE = "none"
    HYBRID = "hybrid"
    ALWAYS = "always"
    UNKNOWN = "unknown"


class RegistryEntry(BaseModel):
    """One model entry from the LiveBench site registry (modelLinks.js)."""

    openweight: bool
    organization: str
    display_name: str
    url: str


class LeaderboardStanding(BaseModel):
    """A model's computed standing on one LiveBench release."""

    model_key: str
    global_average: float
    category_averages: dict[str, float]
    organization: str
    display_name: str
    openweight: bool


class ModelGeometry(BaseModel):
    """Attention geometry needed for the KV-cache estimate."""

    num_hidden_layers: int
    num_key_value_heads: int
    head_dim: int


class ModelArtifact(BaseModel):
    """A pinned, deployable HuggingFace artifact."""

    repo_id: str
    revision: str
    precision: Precision
    weight_bytes: int
    geometry: ModelGeometry
    license: str | None
    gated: bool


class GpuSpec(BaseModel):
    """One supported GPU class for the fit calculation."""

    name: str
    vram_gib: int
    supports_fp8: bool


class MappingEntry(BaseModel):
    """One curated mapping from a LiveBench model key to its artifact."""

    hf_repo: str
    family: str
    thinking: ThinkingMode
    note: str | None = None


class SnapshotFile(BaseModel):
    """One raw upstream file fetched during sourcing.

    The raw bytes ride along so a refresh can pin its exact inputs to
    IPFS; published records and database rows carry only the hash
    metadata (dump with ``exclude={"content"}``).
    """

    name: str
    sha256: str
    size_bytes: int
    content: bytes


class CandidateDescriptor(BaseModel):
    """A fully resolved pool candidate produced by one sourcing pass."""

    livebench_key: str
    display_name: str
    organization: str
    family: str
    thinking: ThinkingMode
    global_average: float
    category_averages: dict[str, float]
    hf_repo: str
    revision: str
    precision: Precision
    weight_bytes: int
    license: str | None
    gated: bool
    assigned_gpu: str | None
    release: str


class SourcingReport(BaseModel):
    """The complete result of one candidate-sourcing pass."""

    release: str
    candidates: list[CandidateDescriptor]
    unmapped: list[str]
    skipped: dict[str, str]
    snapshots: list[SnapshotFile]
