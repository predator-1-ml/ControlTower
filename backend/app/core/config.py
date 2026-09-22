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

    # On ECS there is no DATABASE_URL. RDS owns the master password and rotates
    # it every 7 days, so Terraform passes where the database is and WHICH secret
    # holds the credentials, never the credentials themselves. When
    # `db_secret_arn` is set it wins over `database_url` — see
    # `app/db/checkpointer.py::conninfo`.
    db_host: str | None = None
    db_port: int = 5432
    db_name: str | None = None
    db_secret_arn: str | None = None

    # --- LLM provider (ADR-004) -------------------------------------------
    #
    # bedrock   production, and the only one the deployed stack uses
    # anthropic local dev, strongest model
    # gemini    local dev, free tier — for working without AWS credentials
    #
    # All three exist behind one interface precisely so this choice is a config
    # value rather than a code change. Production is Bedrock regardless.
    llm_provider: Literal["anthropic", "bedrock", "gemini"] = "anthropic"
    anthropic_api_key: str | None = None
    google_api_key: str | None = None
    gemini_model_id: str = "gemini-flash-latest"
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
    # Nova Pro, not Claude, and that is an availability constraint rather than a
    # preference. Probed 2026-09-18: EVERY Anthropic model on Bedrock in this
    # account returns
    #
    #   ResourceNotFoundException: Model use case details have not been
    #   submitted for this account.
    #
    # — an account-level questionnaire in the Bedrock console, not per-model
    # access and not a quota. Nova needs no such form and invokes today, and the
    # full graph (planning, dependencies, interrupt, RAG) is verified on it.
    #
    # Note the `apac.` prefix: Nova's bare model id is rejected for on-demand
    # throughput, and `global.` resolves only for `nova-2-lite` here.
    #
    # Once the form clears, this line becomes
    # "global.anthropic.claude-sonnet-4-6" and nothing else changes. That the
    # swap is one config value is the entire point of the provider abstraction.
    bedrock_model_id: str = "apac.amazon.nova-pro-v1:0"

    # Local dev goes through the Anthropic API, which has no such restriction,
    # so it keeps the stronger model. This divergence is exactly what the
    # provider abstraction is for.
    anthropic_model_id: str = "claude-opus-4-8"

    # RAG embeddings. Amazon Titan is not offered in ap-southeast-1; Cohere is.
    # 1024 dimensions, matching the vector(1024) column in migration 0001.
    embedding_model_id: str = "cohere.embed-english-v3"


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


@lru_cache
def get_settings() -> Settings:
    return Settings()
