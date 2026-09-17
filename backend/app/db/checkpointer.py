"""Postgres connection pool and the LangGraph checkpointer.

The checkpointer is what makes the whole system's central claim true: ECS tasks
are disposable, and a workflow paused mid-run can be resumed by a *different*
container. Everything here exists to make that safe.

Three connection kwargs below are mandatory and none of them are guessable.
Know why each one is there — it is the most likely follow-up question about this
file.
"""

from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.core.config import Settings, get_settings

#: Required by `AsyncPostgresSaver`. `from_conn_string` sets exactly these three,
#: which is the authoritative signal that a hand-built pool must match them.
CONNECTION_KWARGS: dict[str, object] = {
    # Checkpointer migrations 6-8 are CREATE INDEX CONCURRENTLY, which Postgres
    # forbids inside a transaction block. Without autocommit, .setup() either
    # fails outright or silently does not commit.
    "autocommit": True,
    # The saver indexes rows by name (row["checkpoint"]). psycopg's default
    # tuple_row gives "TypeError: tuple indices must be integers".
    "row_factory": dict_row,
    # Disables psycopg3 server-side prepared statements. Mandatory behind
    # PgBouncer or RDS Proxy: once connections are re-multiplexed you get
    # 'prepared statement "_pg3_0" does not exist'. Cheap insurance even when
    # connecting straight to RDS, since adding a proxy later would otherwise
    # break production in a way that looks nothing like its cause.
    "prepare_threshold": 0,
}


def build_pool(settings: Settings | None = None) -> AsyncConnectionPool:
    """Construct the pool *closed*.

    psycopg_pool >= 3.2 deprecates opening in ``__init__`` (it emits a
    RuntimeWarning, and the default is slated to flip). Always pass
    ``open=False`` and await ``pool.open()`` explicitly — see ``open_pool``.
    """
    settings = settings or get_settings()
    return AsyncConnectionPool(
        conninfo=settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        max_idle=300,
        timeout=30,
        kwargs=CONNECTION_KWARGS,
        open=False,
    )


async def open_pool(settings: Settings | None = None) -> AsyncConnectionPool:
    pool = build_pool(settings)
    await pool.open(wait=True, timeout=10)
    return pool


def build_checkpointer(pool: AsyncConnectionPool):
    """Build the saver from an open pool.

    MUST be called from inside a running event loop. ``AsyncPostgresSaver.__init__``
    calls ``asyncio.get_running_loop()`` and stores it, so constructing this at
    module import time raises ``RuntimeError: no running event loop``. That is
    why the graph is compiled in the FastAPI lifespan rather than at import.

    Passing a *pool* (not a connection) is first-class — the saver checks out a
    connection per operation. Do not use ``AsyncPostgresSaver.from_conn_string``
    in a long-lived server: it opens a single dedicated connection and is a
    context manager, so every request would contend on one connection.

    Note we deliberately do NOT call ``.setup()`` here. See ``app/scripts/migrate.py``.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    return AsyncPostgresSaver(conn=pool)
