"""Postgres connection pool and the LangGraph checkpointer.

The checkpointer is what makes the whole system's central claim true: ECS tasks
are disposable, and a workflow paused mid-run can be resumed by a *different*
container. Everything here exists to make that safe.

Three connection kwargs below are mandatory and none of them are guessable.
Know why each one is there — it is the most likely follow-up question about this
file.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

from psycopg.conninfo import make_conninfo
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


def conninfo(settings: Settings) -> str | Callable[[], Awaitable[str]]:
    """Where to connect: a fixed URL locally, a per-connection lookup on ECS.

    On ECS the password lives in a Secrets Manager secret that RDS rotates every
    7 days. The pool accepts a *callable* conninfo and calls it each time it opens
    a connection, so a connection opened after a rotation simply reads the new
    password. Nothing restarts and nothing caches.

    Rejected: injecting the secret through the task definition's `secrets` block.
    ECS resolves that once, at task start — it works for a week, then every new
    connection fails authentication until the task is replaced.
    """
    if not settings.db_secret_arn:
        return settings.database_url

    def _fetch() -> str:
        import boto3  # only needed on ECS; keeps AWS out of local and CI imports

        secret = json.loads(
            boto3.client("secretsmanager", region_name=settings.aws_region)
            .get_secret_value(SecretId=settings.db_secret_arn)["SecretString"]
        )
        return make_conninfo(
            host=settings.db_host,
            port=settings.db_port,
            dbname=settings.db_name,
            user=secret["username"],
            password=secret["password"],
            # RDS PostgreSQL 15+ rejects unencrypted connections by default
            # (rds.force_ssl=1); psycopg's default `prefer` would also work, but
            # `require` fails loudly instead of silently downgrading.
            sslmode="require",
        )

    async def _fetch_async() -> str:
        # boto3 is blocking. The pool opens connections on the event loop, so a
        # direct call would stall every in-flight SSE stream for the round trip.
        return await asyncio.to_thread(_fetch)

    return _fetch_async


def build_pool(settings: Settings | None = None) -> AsyncConnectionPool:
    """Construct the pool *closed*.

    psycopg_pool >= 3.2 deprecates opening in ``__init__`` (it emits a
    RuntimeWarning, and the default is slated to flip). Always pass
    ``open=False`` and await ``pool.open()`` explicitly — see ``open_pool``.
    """
    settings = settings or get_settings()
    return AsyncConnectionPool(
        conninfo=conninfo(settings),
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


#: Custom types that appear inside checkpointed graph state.
#:
#: LangGraph serialises state with msgpack and, on load, warns about any type it
#: was not told to expect: "Deserializing unregistered type ... This will be
#: blocked in a future version."
#:
#: The failure mode once it IS blocked is the dangerous part — it does not raise.
#: It logs, then hands back the raw dict. A node expecting a PlanTask gets a
#: plain dict, so `task.status` raises AttributeError somewhere far from the
#: cause, or a `.get()` quietly takes the wrong branch. Registering the types
#: explicitly turns a future silent-wrong into a non-event.
ALLOWED_MSGPACK_MODULES = [
    ("app.graph.state", "PlanTask"),
    ("app.graph.state", "TaskStatus"),
]


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
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    return AsyncPostgresSaver(
        conn=pool,
        serde=JsonPlusSerializer(allowed_msgpack_modules=ALLOWED_MSGPACK_MODULES),
    )
