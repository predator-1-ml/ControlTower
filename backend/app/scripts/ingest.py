"""Embed knowledge chunks the configured model has not embedded yet.

    python -m app.scripts.ingest

Idempotent, and idempotent *per model*: it selects rows with no vector **or** a
vector from a different model, so re-running is safe, adding documents is safe,
and switching LLM_PROVIDER re-embeds the corpus into the new vector space instead
of leaving a silent mix of two. See migration 0002 for why that matters.

Needs live AWS credentials: embeddings are Bedrock-only, in local development as
well as production, so there is one vector space rather than two. `aws login`
sessions expire, so re-authenticate if this fails on auth.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from app.core.config import get_settings
from app.core.eventloop import use_compatible_event_loop
from app.db import repository
from app.db.checkpointer import open_pool
from app.llm.provider import get_embeddings

log = logging.getLogger("ingest")


async def ingest() -> None:
    settings = get_settings()
    model = settings.embedding_model_id

    embedder = get_embeddings(settings)
    if embedder is None:
        # Fail loudly here, unlike the request path. A backend that cannot embed
        # should still serve onboarding and claims; an ingest run that cannot
        # embed has nothing to do and must not exit 0 as though it had.
        raise RuntimeError(
            f"no embedding model available for LLM_PROVIDER={settings.llm_provider}"
        )

    pool = await open_pool(settings)
    try:
        chunks = await repository.unembedded_chunks(pool, model)
        if not chunks:
            log.info("nothing to embed (corpus is current for %s)", model)
            return

        log.info("embedding %d chunks with %s", len(chunks), model)
        vectors = await embedder.aembed_documents([c["content"] for c in chunks])

        for chunk, vector in zip(chunks, vectors, strict=True):
            await repository.store_embedding(pool, chunk["id"], vector, model)
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
