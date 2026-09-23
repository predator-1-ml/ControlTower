"""Load the demo fixtures. Run locally, in CI, and as a one-off ECS task.

    python -m app.scripts.seed

One mechanism for every environment, for the same reason migrations run as a
task on the backend image rather than at app startup: RDS is private, so there
is no psql from a laptop, and a fixture the image does not carry cannot be
loaded there at all. The seed lives in the build context (backend/seed/) for
that reason.

Deployed:
    aws ecs run-task ... --overrides '{"containerOverrides":[{"name":"backend",
        "command":["python","-m","app.scripts.seed"]}]}'

The file truncates before inserting, so re-running is safe, and it truncates
knowledge_chunks too, so every run must be followed by `app.scripts.ingest`
or retrieval finds nothing and the knowledge workflow reads as broken.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from app.core.config import get_settings
from app.core.eventloop import use_compatible_event_loop
from app.db.checkpointer import open_pool

log = logging.getLogger("seed")

SEED_FILE = Path(__file__).resolve().parents[2] / "seed" / "seed.sql"


async def run_seed() -> None:
    # A missing file is a packaging bug (image built without seed/), never a
    # valid state: exit non-zero rather than report a seed that loaded nothing.
    sql = SEED_FILE.read_text(encoding="utf-8")

    pool = await open_pool(get_settings())
    try:
        async with pool.connection() as conn:
            # The pool is autocommit; one transaction so the TRUNCATEs and the
            # INSERTs land together or not at all. prepare=False: the file is
            # many statements and the extended protocol accepts exactly one.
            async with conn.transaction():
                await conn.execute(sql, prepare=False)
            row = await (await conn.execute("SELECT count(*) AS n FROM customers")).fetchone()
        log.info("seed applied from %s (%s customers)", SEED_FILE.name, row["n"])
    finally:
        await pool.close()


def main() -> int:
    use_compatible_event_loop()  # local Windows dev; no-op on Linux
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(run_seed())
    except Exception:
        log.exception("seed failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
