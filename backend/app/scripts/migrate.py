"""One-off migration entrypoint. Run as a standalone ECS task BEFORE the service update.

Why this is a separate process rather than something the app does at startup:

`AsyncPostgresSaver.setup()` has a concurrency hazard that is not documented. It
holds no advisory lock, does no SELECT ... FOR UPDATE, and cannot be wrapped in a
transaction (its migrations use CREATE INDEX CONCURRENTLY, which Postgres forbids
inside one). It reads MAX(v) from `checkpoint_migrations` and applies the tail.

So if N ECS tasks boot simultaneously against a stale schema:

  * All read version = k, all apply k+1..n, all INSERT the same version rows.
    Losers hit a UniqueViolation on the primary key and **crash at boot**.
  * Two concurrent CREATE INDEX CONCURRENTLY on the same table can abort, leaving
    an INVALID index behind. Symptom: queries quietly stop using it.
    Detect with:  SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;

The window is only open on the first boot after a checkpointer upgrade that adds
a migration — which is exactly the rolling-deploy moment. Running setup() in the
app lifespan works fine right up until the deploy where it doesn't.

Fix: one process, one advisory lock, before any app task starts.

Usage:
    python -m app.scripts.migrate
"""

from __future__ import annotations

import asyncio
import logging
import sys

from app.core.config import get_settings
from app.core.eventloop import use_compatible_event_loop
from app.db.checkpointer import open_pool

log = logging.getLogger("migrate")

#: Arbitrary but stable. Any process taking this lock serializes against the others.
LOCK_KEY = "langgraph_checkpoint_setup"


async def run_migrations() -> None:
    settings = get_settings()
    pool = await open_pool(settings)

    try:
        async with pool.connection() as conn:
            log.info("acquiring advisory lock %s", LOCK_KEY)
            # Blocks (rather than failing) if another migration task holds it,
            # so a retried ECS task waits instead of racing.
            await conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (LOCK_KEY,))
            try:
                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

                log.info("running checkpointer setup")
                await AsyncPostgresSaver(conn=conn).setup()

                # Domain schema (pgvector extension + tables) is Alembic's job and
                # runs under the same lock, so the two can never interleave.
                # TODO(day-1): await _run_alembic_upgrade()
                log.info("checkpointer setup complete")
            finally:
                await conn.execute(
                    "SELECT pg_advisory_unlock(hashtext(%s))", (LOCK_KEY,)
                )
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
