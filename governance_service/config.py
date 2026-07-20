"""Application settings loaded from environment variables."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_PATH = REPO_ROOT / "migrations"
MODEL_MAPPING_PATH = Path(__file__).resolve().parent / "model_mapping.yaml"
MODEL_BLOCKLIST_PATH = Path(__file__).resolve().parent / "model_blocklist.yaml"


class Settings(BaseSettings):

    # -------------------------------------------------------------------------
    # Database
    # -------------------------------------------------------------------------
    database_url: str = Field(
        default="postgresql://postgres:dev_password@localhost:5433/scoring_model_governance",
        description="PostgreSQL connection string",
    )

    # -------------------------------------------------------------------------
    # HTTP clients
    # -------------------------------------------------------------------------
    http_max_retries: int = Field(
        default=3, description="Attempts per upstream HTTP request"
    )
    http_retry_base_delay: int = Field(
        default=2, description="Base seconds for exponential retry backoff"
    )
    http_timeout_seconds: int = Field(
        default=30, description="Timeout per upstream HTTP request"
    )
    github_token: str | None = Field(
        default=None,
        description="Optional GitHub token for release discovery rate limits",
    )

    # -------------------------------------------------------------------------
    # LiveBench
    # -------------------------------------------------------------------------
    livebench_base_url: str = Field(
        default="https://livebench.ai",
        description="Base URL serving the versioned leaderboard data files",
    )
    livebench_registry_url: str = Field(
        default="https://raw.githubusercontent.com/livebench/livebench.github.io/main/src/Table/modelLinks.js",
        description="Raw URL of the site model registry (open-weight flags)",
    )
    livebench_releases_api_url: str = Field(
        default="https://api.github.com/repos/livebench/livebench.github.io/contents/public",
        description="GitHub contents API URL used to discover releases",
    )

    # -------------------------------------------------------------------------
    # HuggingFace
    # -------------------------------------------------------------------------
    hf_api_base_url: str = Field(
        default="https://huggingface.co",
        description="HuggingFace base URL for the models API and file resolution",
    )
    hf_token: str | None = Field(
        default=None, description="Optional HuggingFace API token"
    )

    # -------------------------------------------------------------------------
    # Application
    # -------------------------------------------------------------------------
    environment: str = Field(
        default="local",
        description="Deployment environment name recorded in published "
        "refresh records and their repository paths",
    )

    # -------------------------------------------------------------------------
    # Admin
    # -------------------------------------------------------------------------
    admin_api_key: str = Field(
        default="",
        description="API key for admin endpoints (refresh trigger). "
        "Endpoint disabled if empty.",
    )
    default_page_limit: int = Field(
        default=20,
        description="Default number of items per page for paginated API responses",
    )

    # -------------------------------------------------------------------------
    # Incumbent
    # -------------------------------------------------------------------------
    incumbent_hf_repo: str = Field(
        default="Qwen/Qwen3.6-27B-FP8",
        description="HuggingFace repository of the incumbent scoring model, "
        "a pool member by right",
    )
    incumbent_revision: str | None = Field(
        default=None,
        description="Pinned revision of the incumbent's serving artifact; "
        "the repository's current revision is used when unset",
    )

    # -------------------------------------------------------------------------
    # GPU fit
    # -------------------------------------------------------------------------
    gpu_mem_fraction: float = Field(
        default=0.75,
        description="Usable VRAM fraction, mirroring the production SGLang "
        "--mem-fraction-static profile",
    )
    fit_context_tokens: int = Field(
        default=32768,
        description="Context budget the KV-cache estimate reserves for one round",
    )
    kv_cache_dtype_bytes: int = Field(
        default=2, description="Bytes per KV-cache element (bf16)"
    )

    # -------------------------------------------------------------------------
    # IPFS
    # -------------------------------------------------------------------------
    ipfs_api_url: str = Field(
        default="",
        description="IPFS node HTTP API URL for pinning refresh snapshot "
        "files. Pinning is skipped when empty.",
    )
    ipfs_api_username: str = Field(
        default="", description="Optional IPFS API basic-auth username"
    )
    ipfs_api_password: str = Field(
        default="", description="Optional IPFS API basic-auth password"
    )
    pinata_api_key: str = Field(
        default="", description="Pinata API key for secondary replication"
    )
    pinata_api_secret: str = Field(
        default="", description="Pinata API secret for secondary replication"
    )

    # -------------------------------------------------------------------------
    # Published refresh records
    # -------------------------------------------------------------------------
    records_github_token: str = Field(
        default="",
        description="Fine-grained PAT with contents:write on the records "
        "repository. Record publication is skipped when empty.",
    )
    records_github_repo: str = Field(
        default="postfiatorg/scoring-model-governance",
        description="Repository that hosts the published refresh records",
    )
    records_github_branch: str = Field(
        default="main", description="Branch the record commits target"
    )
    records_base_path: str = Field(
        default="records/pool-refreshes",
        description="Repository directory the record files are committed under",
    )
    records_commit_author_name: str = Field(
        default="PostFiat Governance Service",
        description="Author and committer name on record commits",
    )
    records_commit_author_email: str = Field(
        default="governance@postfiat.org",
        description="Author and committer email on record commits",
    )

    @property
    def ipfs_enabled(self) -> bool:
        return bool(self.ipfs_api_url)

    @property
    def pinata_enabled(self) -> bool:
        return bool(self.pinata_api_key and self.pinata_api_secret)

    @property
    def records_enabled(self) -> bool:
        return bool(self.records_github_token)

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()
