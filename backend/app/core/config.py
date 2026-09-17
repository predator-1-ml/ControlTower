"""Application settings, loaded from the environment."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_env: Literal["local", "dev", "prod"] = "local"
    log_level: str = "INFO"

    database_url: str = (
        "postgresql://control_tower:control_tower@localhost:5432/control_tower"
    )

    # --- LLM provider (ADR-004) -------------------------------------------
    llm_provider: Literal["anthropic", "bedrock"] = "anthropic"
    anthropic_api_key: str | None = None
    aws_region: str = "us-east-1"

    # ChatBedrockConverse targets the bedrock-runtime Converse API, which
    # requires a geo inference-profile prefix (us. / eu. / apac.). The bare id
    # is rejected for on-demand throughput — a genuinely confusing 400.
    bedrock_model_id: str = "us.anthropic.claude-opus-4-8"
    anthropic_model_id: str = "claude-opus-4-8"

    # Optional cheaper tiers for the two highest-frequency nodes. Empty = use
    # the main model. Planner and supervisor are structured-output calls made on
    # every turn, so this is where token spend concentrates.
    planner_model: str | None = None
    supervisor_model: str | None = None

    # --- concurrency (ADR-004) --------------------------------------------
    # ChatBedrockConverse has NO native async: ainvoke/astream bridge blocking
    # boto3 through run_in_executor, holding a worker thread for the entire
    # call — including the full duration of a streamed response. Python's
    # default executor is min(32, cpu_count + 4), so FastAPI concurrency
    # silently ceilings there and excess requests queue invisibly.
    #
    # These two must be raised TOGETHER. Raising the thread pool while boto3's
    # connection pool stays at its default of 10 just moves the bottleneck.
    executor_max_workers: int = 128
    boto_max_pool_connections: int = 128

    # --- database pool -----------------------------------------------------
    db_pool_min_size: int = 5
    db_pool_max_size: int = 20

    @property
    def is_local(self) -> bool:
        return self.app_env == "local"


@lru_cache
def get_settings() -> Settings:
    return Settings()
