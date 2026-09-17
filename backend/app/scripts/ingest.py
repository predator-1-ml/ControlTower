"""Embed knowledge chunks that do not yet have a vector.

    python -m app.scripts.ingest

Idempotent: only rows with `embedding IS NULL` are processed, so it is safe to
re-run and safe to run after adding documents. Needs live AWS credentials —
`aws login` sessions expire, so re-authenticate if this fails on auth.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from app.core.config import get_settings
from app.core.eventloop import use_compatible_event_loop
from app.db import repository
from app.db.checkpointer import open_pool

log = logging.getLogger("ingest")


async def ingest() -> None:
    settings = get_settings()
    pool = await open_pool(settings)
    try:
        chunks = await repository.unembedded_chunks(pool)
        if not chunks:
            log.info("nothing to embed")
            return

        from langchain_aws import BedrockEmbeddings

        embedder = BedrockEmbeddings(
            model_id=settings.embedding_model_id, region_name=settings.aws_region
        )

        log.info("embedding %d chunks with %s", len(chunks), settings.embedding_model_id)
        vectors = await embedder.aembed_documents([c["content"] for c in chunks])

        for chunk, vector in zip(chunks, vectors, strict=True):
            await repository.store_embedding(pool, chunk["id"], vector)
            log.info("  %s / %s", chunk["source"], chunk.get("section"))

        log.info("embedded %d chunks", len(chunks))
    finally:
        await pool.close()


def main() -> int:
    use_compatible_event_loop()
    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(ingest())
    except Exception:
        log.exception("ingestion failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
