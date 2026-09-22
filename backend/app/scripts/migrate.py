"""Migration entrypoint. Run as a standalone ECS task BEFORE the service update.

Applies, in one advisory-locked process:
  1. numbered .sql files in backend/migrations/  (domain schema)
  2. AsyncPostgresSaver.setup()                  (LangGraph checkpointer tables)

Why a separate process rather than something the app does at startup:

`setup()` holds no advisory lock, does no SELECT ... FOR UPDATE, and cannot be
wrapped in a transaction (its own migrations use CREATE INDEX CONCURRENTLY,
which Postgres forbids inside one). It reads MAX(v) from `checkpoint_migrations`
and applies the tail. So if N ECS tasks boot at once against a stale schema:

  * All read version k, all apply k+1..n, all INSERT the same rows. Losers hit a
    UniqueViolation and **crash at boot**.
  * Two concurrent CREATE INDEX CONCURRENTLY on one table can abort, leaving an
    INVALID index. Queries then quietly stop using it. Detect with:
        SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;

That window opens on the first boot after a checkpointer upgrade — exactly the
rolling-deploy moment. Running setup() in the app lifespan works right up until
the deploy where it doesn't.

Usage:
    python -m app.scripts.migrate
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from app.core.config import get_settings
from app.core.eventloop import use_compatible_event_loop
from app.db.checkpointer import open_pool

log = logging.getLogger("migrate")

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

#: Arbitrary but stable. Any process taking this lock serializes against the rest.
LOCK_KEY = "control_tower_migrations"

_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


async def _apply_sql_migrations(conn) -> None:
    """Apply any .sql file not yet recorded, in filename order."""
    await conn.execute(_TRACKING_TABLE)

    rows = await (await conn.execute("SELECT version FROM schema_migrations")).fetchall()
    applied = {r["version"] for r in rows}

    paths = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not paths:
        # An empty glob is a packaging bug (the image was built without
        # migrations/), never a valid state. Exiting 0 here would report a
        # successful migration against a database with no tables.
        raise FileNotFoundError(f"no .sql migrations found in {MIGRATIONS_DIR}")

    for path in paths:
        version = path.stem
        if version in applied:
            continue
        log.info("applying %s", version)
        # The pool runs autocommit (the checkpointer requires it), so wrap each
        # migration explicitly: a file either lands whole or not at all.
        async with conn.transaction():
            # prepare=False forces the simple query protocol. psycopg defaults to
            # the extended protocol, which accepts exactly one statement per
            # execute — a multi-statement file fails with the misleading
            # "cannot insert multiple commands into a prepared statement".
            await conn.execute(path.read_text(encoding="utf-8"), prepare=False)
            await conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s)", (version,)
            )

    if not applied:
        log.info("domain schema created")


async def run_migrations() -> None:
    pool = await open_pool(get_settings())
    try:
        async with pool.connection() as conn:
            log.info("acquiring advisory lock %s", LOCK_KEY)
            # Blocks rather than failing, so a retried ECS task waits its turn
            # instead of racing.
            await conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (LOCK_KEY,))
            try:
                await _apply_sql_migrations(conn)

                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

                await AsyncPostgresSaver(conn=conn).setup()
                log.info("checkpointer setup complete")
            finally:
                await conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (LOCK_KEY,))
                log.info("released advisory lock")
    finally:
        await pool.close()


def main() -> int:
    use_compatible_event_loop()  # local Windows dev; no-op on Linux
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(run_migrations())
    except Exception:
        log.exception("migration failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
