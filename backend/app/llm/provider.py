"""LLM provider abstraction — see ADR-004.

Local development runs against the Anthropic API directly (natively async, no
AWS model-access gating, fast iteration). Production runs on Bedrock for the
AWS-native story. Graph code never imports either concrete class; it calls
`get_chat_model()` and stays provider-agnostic.

The uncomfortable detail worth knowing before you defend this design:

    `ChatBedrockConverse` contains no `async def` at all.

`ainvoke` / `astream` work only through `BaseChatModel`'s default
`run_in_executor` bridge around blocking boto3. A worker thread is therefore
held for the *entire* model call, including the full duration of a streamed
response. The default executor is `min(32, cpu_count + 4)`, so a FastAPI service
silently ceilings at ~32 concurrent model calls and everything beyond that
queues with no error and no log line.

Two ways out. We take the first because it keeps the standard Bedrock surface
(Guardrails, Knowledge Bases, invocation logging) and the fix is two settings:

  1. Raise the default executor AND botocore's `max_pool_connections` together.
     Raising one without the other just relocates the bottleneck.
  2. Use `langchain-aws`'s Anthropic-SDK-backed Bedrock client, which is
     genuinely async — at the cost of fewer regions and no Guardrails.

`configure_event_loop_executor()` below implements (1); it is called from the
FastAPI lifespan.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import Any

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel

from app.core.config import Settings, get_settings

log = logging.getLogger(__name__)


def message_text(response: Any) -> str:
    """Extract plain text from a model response, whatever shape it arrives in.

    `.content` is NOT reliably a string. Anthropic returns one; Gemini returns a
    list of content blocks:

        [{"type": "text", "text": "...", "index": 0,
          "extras": {"signature": "<multi-kilobyte blob>"}}]

    Using `.content` directly therefore puts a list — signature blob and all —
    into graph state, where it is checkpointed to Postgres and serialised into
    every SSE frame. It does not raise; it just quietly ships kilobytes of
    provider internals to the browser and stores them forever.

    Centralised here so provider differences stay inside the provider module,
    which is the point of having one.
    """
    content = getattr(response, "content", response)

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)

    return str(content)


def configure_event_loop_executor(settings: Settings | None = None) -> None:
    """Widen the default thread pool that `run_in_executor` uses.

    Only meaningful for the Bedrock path. Harmless otherwise, so it is called
    unconditionally from the lifespan rather than hidden behind a branch.
    """
    settings = settings or get_settings()
    asyncio.get_running_loop().set_default_executor(
        concurrent.futures.ThreadPoolExecutor(
            max_workers=settings.executor_max_workers,
            thread_name_prefix="llm",
        )
    )


def _model_id(settings: Settings) -> str:
    return {
        "bedrock": settings.bedrock_model_id,
        "gemini": settings.gemini_model_id,
        "anthropic": settings.anthropic_model_id,
    }[settings.llm_provider]


def get_chat_model(
    *,
    settings: Settings | None = None,
    **kwargs: Any,
) -> BaseChatModel:
    """Return the chat model for the configured provider.

    Imports are deliberately function-local: a local dev run should not need
    `langchain_aws` importable, and an ECS task should not need an Anthropic key
    present just to import the module.
    """
    settings = settings or get_settings()
    model_id = _model_id(settings)

    if settings.llm_provider == "bedrock":
        from botocore.config import Config
        from langchain_aws import ChatBedrockConverse

        return ChatBedrockConverse(
            model=model_id,
            region_name=settings.aws_region,
            # Must match executor_max_workers or the two throttle each other.
            config=Config(max_pool_connections=settings.boto_max_pool_connections),
            **kwargs,
        )

    if settings.llm_provider == "gemini":
        # Local development only. Deliberately never used by the deployed stack:
        # calling Google for inference from a system whose whole networking and
        # IAM story is AWS would be an odd thing to have to explain.
        from langchain_google_genai import ChatGoogleGenerativeAI

        if not settings.google_api_key:
            raise RuntimeError(
                "LLM_PROVIDER=gemini but GOOGLE_API_KEY is unset. "
                "Put it in backend/.env (gitignored)."
            )

        return ChatGoogleGenerativeAI(
            model=model_id,
            google_api_key=settings.google_api_key,
            **kwargs,
        )

    from langchain_anthropic import ChatAnthropic

    if not settings.anthropic_api_key:
        raise RuntimeError(
            "LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is unset. "
            "Set it in .env, or switch LLM_PROVIDER=bedrock or gemini."
        )

    return ChatAnthropic(
        model=model_id,
        api_key=settings.anthropic_api_key,
        **kwargs,
    )


def get_embeddings(settings: Settings | None = None) -> Embeddings | None:
    """Always Cohere on Bedrock — deliberately NOT tied to `llm_provider`.

    Chat provider and embedding provider are independent choices here, and on this
    account they have to be: Bedrock's Anthropic models are gated behind a use-case
    form and return `ResourceNotFoundException`, while `cohere.embed-english-v3`
    invokes fine. Coupling the two would mean either no local retrieval at all, or
    a second local model — and a second model means a second vector space in the
    same `vector(1024)` column, which is the failure migration 0002 exists to stop.

    So: chat follows `LLM_PROVIDER`; embeddings are Cohere everywhere. Local
    retrieval then exercises the exact vectors production uses.

    Returns None rather than raising, because a missing embedder must cost the
    knowledge workflow and nothing else. Onboarding and claims have no use for
    vectors and must not be taken down by absent AWS credentials; `retrieve` then
    finds nothing and the graph routes to `no_results`.
    """
    settings = settings or get_settings()

    try:
        from langchain_aws import BedrockEmbeddings

        return BedrockEmbeddings(
            model_id=settings.embedding_model_id, region_name=settings.aws_region
        )
    except Exception as exc:  # noqa: BLE001 - see docstring
        log.warning("Bedrock embedder unavailable: %s", exc)
        return None
