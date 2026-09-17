"""FastAPI application entrypoint.

Everything expensive and loop-bound is built in the lifespan, not at import:

  * `AsyncPostgresSaver.__init__` captures the running event loop, so building it
    at module scope raises `RuntimeError: no running event loop`.
  * The graph must be compiled *with* that checkpointer, so it is loop-bound too.
  * The executor widening in ADR-004 must happen on the loop that will serve
    requests.

Consequence worth stating plainly: there is no importable module-level `graph`.
Handlers read `request.app.state.graph`.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import get_settings
from app.core.eventloop import use_compatible_event_loop
from app.db.checkpointer import build_checkpointer, open_pool
from app.graph.smoke import build_smoke_graph
from app.llm.provider import configure_event_loop_executor

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
    # Day 1: the smoke graph. Replaced by the planner/supervisor graph on Day 4.
    app.state.graph = build_smoke_graph(checkpointer)

    log.info("startup complete (provider=%s)", settings.llm_provider)
    try:
        yield
    finally:
        await pool.close()
        log.info("shutdown complete")


app = FastAPI(title="AI Operations Control Tower", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness. Deliberately does NOT touch the database.

    Both the ALB target group and the ECS container health check hit this. If it
    depended on Postgres, a brief RDS blip would make ECS kill every task at
    once — turning a recoverable dependency wobble into an outage.
    """
    return {"status": "ok"}
