"""Application settings loaded from environment variables."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_PATH = REPO_ROOT / "migrations"
MODEL_MAPPING_PATH = Path(__file__).resolve().parent / "model_mapping.yaml"


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

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()
