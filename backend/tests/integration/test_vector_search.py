"""The vector half of the schema, against a real pgvector.

CI stands up `pgvector/pgvector:pg17` and the workflow comment claims the
integration tests assert vector search and the HNSW index. Until this file they
did not — the only coverage was a monkeypatched `search_knowledge` in
tests/graph/, which asserts what Python does with the rows and nothing about
whether Postgres returns the right ones.

**Vectors here are handmade, not embedded.** Cosine ordering, the NULL filter and
the model filter are properties of the SQL, not of any model, so a real embedder
would add a model download to CI to test something it does not affect. What
*is* model-dependent — whether the right chunk ranks first for a real question —
belongs in a retrieval eval against a real corpus, not here.

Rows are inserted under a source prefix this file owns and deleted afterwards, so
these never disturb the seed fixtures the other integration tests assert against.
"""

from __future__ import annotations

import os

import pytest

from app.db.checkpointer import open_pool
from app.db.repository import search_knowledge, store_embedding, unembedded_chunks

pytestmark = pytest.mark.skipif(
    os.environ.get("CONTROL_TOWER_DB_TESTS") != "1",
    reason="needs Postgres; set CONTROL_TOWER_DB_TESTS=1",
)

SOURCE = "__test_vector_search__"
MODEL = "test-model-a"
OTHER_MODEL = "test-model-b"


def unit(index: int) -> list[float]:
    """A one-hot 1024-dim vector. Orthogonal to every other index, so cosine
    similarity against `unit(i)` is 1.0 for itself and 0.0 for the rest — the
    ordering is then unambiguous rather than a judgement call about floats."""
    vector = [0.0] * 1024
    vector[index] = 1.0
    return vector


@pytest.fixture
async def pool():
    p = await open_pool()
    async with p.connection() as conn:
        await conn.execute("DELETE FROM knowledge_chunks WHERE source = %s", (SOURCE,))
    yield p
    async with p.connection() as conn:
        await conn.execute("DELETE FROM knowledge_chunks WHERE source = %s", (SOURCE,))
    await p.close()


async def insert(pool, section: str) -> str:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO knowledge_chunks (source, section, content) "
            "VALUES (%s, %s, %s) RETURNING id::text",
            (SOURCE, section, f"content for {section}"),
        )
        # The pool sets a dict row factory, so this is {"id": ...} and not a
        # tuple. `(chunk_id,) = await cur.fetchone()` unpacks the dict's KEYS and
        # binds the string "id" — no error, just every assertion failing later
        # against a value that looks almost plausible.
        row = await cur.fetchone()
    return row["id"]


async def test_results_come_back_in_cosine_order(pool):
    """Nearest first. If the index opclass and the query operator ever disagree,
    Postgres silently falls back to a sequential scan — which still returns the
    right order, so this test passes either way. That is deliberate: it pins
    correctness, and the HNSW index is a latency concern, not a results concern."""
    near = await insert(pool, "near")
    far = await insert(pool, "far")
    await store_embedding(pool, near, unit(0), MODEL)
    await store_embedding(pool, far, unit(1), MODEL)

    results = await search_knowledge(pool, unit(0), MODEL, limit=10)
    sections = [r["section"] for r in results]

    assert sections.index("near") < sections.index("far")
    assert results[0]["similarity"] == pytest.approx(1.0, abs=1e-6)


async def test_unembedded_rows_are_never_returned(pool):
    """A row with no vector must not surface. Without the `IS NOT NULL` guard it
    sorts as maximally distant, pads the result set, and gets cited as a source."""
    embedded = await insert(pool, "embedded")
    await insert(pool, "not-embedded")
    await store_embedding(pool, embedded, unit(0), MODEL)

    results = await search_knowledge(pool, unit(0), MODEL, limit=10)

    assert [r["section"] for r in results] == ["embedded"]


async def test_another_models_vectors_are_never_returned(pool):
    """The failure migration 0002 exists to prevent.

    Both models are 1024-dimensional, so the foreign vector inserts cleanly and
    is a perfect cosine match. Nothing raises. Without the model filter this
    query returns it as the best possible hit.
    """
    ours = await insert(pool, "ours")
    theirs = await insert(pool, "theirs")
    await store_embedding(pool, ours, unit(5), MODEL)
    await store_embedding(pool, theirs, unit(0), OTHER_MODEL)

    results = await search_knowledge(pool, unit(0), MODEL, limit=10)

    assert [r["section"] for r in results] == ["ours"]


async def test_switching_model_marks_the_corpus_for_re_embedding(pool):
    """`make ingest` after a provider switch must re-embed, not skip.

    The old query was `WHERE embedding IS NULL`, so a row already embedded by the
    previous model was considered done — leaving the corpus in one vector space
    while queries arrived in another, permanently and with no error.
    """
    chunk = await insert(pool, "stale")
    await store_embedding(pool, chunk, unit(0), MODEL)

    same_model = await unembedded_chunks(pool, MODEL)
    assert chunk not in {c["id"] for c in same_model}

    after_switch = await unembedded_chunks(pool, OTHER_MODEL)
    assert chunk in {c["id"] for c in after_switch}


async def test_never_embedded_rows_are_selected_for_ingest(pool):
    """`IS DISTINCT FROM` and not `!=`: `NULL != 'model'` is NULL, not true, so a
    plain `!=` would skip every un-embedded row — the exact set ingest needs."""
    chunk = await insert(pool, "fresh")

    pending = await unembedded_chunks(pool, MODEL)

    assert chunk in {c["id"] for c in pending}
