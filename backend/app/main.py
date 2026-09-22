"""FastAPI application entrypoint.

Everything expensive and loop-bound is built in the lifespan, not at import:

  * `AsyncPostgresSaver.__init__` captures the running event loop, so building it
    at module scope raises `RuntimeError: no running event loop`.
  * The graph must be compiled *with* that checkpointer, so it is loop-bound too.
  * The executor widening in ADR-004 must happen on the loop that serves requests.

Consequence worth stating plainly: there is no importable module-level `graph`.
Handlers read `request.app.state.graph`.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.chat import router as chat_router
from app.core.config import get_settings
from app.core.eventloop import use_compatible_event_loop
from app.db.checkpointer import build_checkpointer, open_pool
from app.graph.build import build_graph
from app.graph.deps import Deps
from app.llm.provider import configure_event_loop_executor, get_chat_model, get_embeddings

# Must run at import time, before uvicorn creates its event loop. Only affects
# native Windows dev runs; a no-op in the container and on ECS.
use_compatible_event_loop()

log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    configure_event_loop_executor(settings)

    pool = await open_pool(settings)
    checkpointer = build_checkpointer(pool)

    app.state.settings = settings
    app.state.pool = pool
    app.state.checkpointer = checkpointer
    # Embeddings are on Bedrock for EVERY chat provider (see get_embeddings), so
    # this is None only when AWS credentials are unavailable. Only the knowledge
    # workflow needs it, so the other two stay runnable without AWS.
    embedder = get_embeddings(settings)
    app.state.deps = Deps(
        pool=pool,
        model=get_chat_model(settings=settings),
        embedder=embedder,
        embedding_model=settings.embedding_model_id if embedder else None,
    )
    app.state.graph = build_graph(checkpointer)

    log.info(
        "startup complete (provider=%s, embeddings=%s)",
        settings.llm_provider,
        settings.embedding_model_id if embedder else "disabled",
    )
    try:
        yield
    finally:
        await pool.close()
        log.info("shutdown complete")


app = FastAPI(title="AI Operations Control Tower", lifespan=lifespan)
app.include_router(chat_router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness. Deliberately does NOT touch the database.

    Both the ALB target group and the ECS container health check hit this. If it
    depended on Postgres, a brief RDS blip would make ECS kill every task at
    once — turning a recoverable dependency wobble into an outage.
    """
    return {"status": "ok"}
