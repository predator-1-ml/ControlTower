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
    aws_region: str = "ap-southeast-1"

    # ChatBedrockConverse targets the bedrock-runtime Converse API, which needs
    # an *inference profile* id, not a bare model id — the bare id is rejected
    # for on-demand throughput.
    #
    # The prefix is regional and not guessable. Verified against this account in
    # ap-southeast-1: `apac.` profiles exist only for older models (Claude 3.x,
    # Sonnet 4); every current model is `global.`-prefixed. `us.` does not
    # resolve here at all.
    #
    # Check before changing region:
    #   aws bedrock list-inference-profiles --region <region> \
    #     --query "inferenceProfileSummaries[].inferenceProfileId"
    #
    # Sonnet 4.6 rather than Opus 4.8 because Opus 4.8 and Sonnet 5 are both
    # "not available for this account" on Bedrock here — a fresh account does not
    # get the newest models without contacting AWS Sales. Probed directly; see
    # CLAUDE.md. This is an availability constraint, not a cost decision.
    bedrock_model_id: str = "global.anthropic.claude-sonnet-4-6"

    # Local dev goes through the Anthropic API, which has no such restriction,
    # so it keeps the stronger model. This divergence is exactly what the
    # provider abstraction is for.
    anthropic_model_id: str = "claude-opus-4-8"

    # RAG embeddings. Amazon Titan is not offered in ap-southeast-1; Cohere is.
    # 1024 dimensions, matching the vector(1024) column in migration 0001.
    embedding_model_id: str = "cohere.embed-english-v3"

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
