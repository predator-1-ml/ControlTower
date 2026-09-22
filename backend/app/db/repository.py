"""Database queries used by the workflow tools.

Plain SQL over the psycopg pool. No ORM: there are a handful of queries, they are
all simple, and an ORM layer would be a second thing to explain for no benefit.

Everything here returns plain dicts. The graph state is checkpointed, so whatever
a tool puts into state has to be JSON-serialisable anyway — returning rich
objects would only mean converting them back.
"""

from __future__ import annotations

import json
from typing import Any

from psycopg_pool import AsyncConnectionPool

#: A claim is "active" if it still needs someone to do something. Defined once
#: here because both the claims workflow and the onboarding eligibility check
#: ask the same question, and they must not drift apart.
ACTIVE_CLAIM_STATUSES = ("open", "awaiting_information", "under_review")


async def get_customer(pool: AsyncConnectionPool, external_ref: str) -> dict[str, Any] | None:
    """Look up a customer by the reference an operator would type (CUST-1001)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT id::text, external_ref, full_name, email, date_of_birth, kyc_status
            FROM customers
            WHERE external_ref = %s
            """,
            (external_ref,),
        )
        row = await cur.fetchone()
    return _serialise(row)


async def get_active_claims(pool: AsyncConnectionPool, customer_id: str) -> list[dict[str, Any]]:
    """Claims still requiring action. The assignment's worked example needs this."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT id::text, claim_ref, claim_type, status, amount,
                   incident_date, missing_fields
            FROM claims
            WHERE customer_id = %s AND status = ANY(%s)
            ORDER BY created_at DESC
            """,
            (customer_id, list(ACTIVE_CLAIM_STATUSES)),
        )
        rows = await cur.fetchall()
    return [_serialise(r) for r in rows]


async def get_claim(pool: AsyncConnectionPool, claim_ref: str) -> dict[str, Any] | None:
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT c.id::text, c.claim_ref, c.claim_type, c.status, c.amount,
                   c.incident_date, c.missing_fields,
                   cu.external_ref AS customer_ref, cu.full_name AS customer_name
            FROM claims c
            JOIN customers cu ON cu.id = c.customer_id
            WHERE c.claim_ref = %s
            """,
            (claim_ref,),
        )
        row = await cur.fetchone()
    return _serialise(row)


async def create_application(
    pool: AsyncConnectionPool, customer_id: str, product: str, status: str
) -> dict[str, Any]:
    """Create an application. The one genuine write in the onboarding workflow.

    Kept explicitly separate from the nodes that decide *whether* to write,
    because `interrupt()` re-runs its node from the top on resume — so any node
    that both pauses and writes would write twice.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            INSERT INTO applications (customer_id, product, status)
            VALUES (%s, %s, %s)
            RETURNING id::text, customer_id::text, product, status
            """,
            (customer_id, product, status),
        )
        row = await cur.fetchone()
    return _serialise(row)


async def record_claim_information(
    pool: AsyncConnectionPool, claim_ref: str, received: list[str]
) -> dict[str, Any] | None:
    """Clear the fields an operator has now supplied, and re-open the claim if none remain.

    **One statement, on purpose.** Read-then-write would need the claim's current
    `missing_fields`, and between the read and the write another handler's update
    is lost; `missing_fields - %s::text[]` removes exactly the supplied names from
    whatever is there at write time, so a concurrent update survives. `RETURNING`
    then gives the caller the post-write row, so there is no refresh query either
    — and no window in which graph state disagrees with the database.

    **Idempotent, and it has to be.** A process death between this UPDATE and the
    checkpoint re-runs the node on resume. Removing names that are already gone
    is a no-op and the status CASE is already false, so the second run leaves the
    same row. The audit row the caller writes afterwards can duplicate — the
    honest at-least-once guarantee, and a duplicate audit row is readable while a
    lost one is not.

    The status move is gated on `awaiting_information` rather than only on the
    array emptying: a claim that is `open` or `under_review` with an outstanding
    field must not be silently transitioned by an information update.

    `missing_fields` is `jsonb`, and `jsonb - text[]` deletes those array
    elements. The same array is passed three times because the extended query
    protocol has no named parameters.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            UPDATE claims
               SET missing_fields = missing_fields - %s::text[],
                   status = CASE
                       WHEN status = 'awaiting_information'
                        AND (missing_fields - %s::text[]) = '[]'::jsonb
                       THEN 'under_review' ELSE status END
             WHERE claim_ref = %s
            RETURNING status, missing_fields
            """,
            (received, received, claim_ref),
        )
        row = await cur.fetchone()
    return _serialise(row)


async def search_knowledge(
    pool: AsyncConnectionPool, embedding: list[float], model: str, limit: int = 4
) -> list[dict[str, Any]]:
    """Nearest knowledge chunks by cosine distance, within one model's vector space.

    `<=>` is cosine distance and must pair with the `vector_cosine_ops` opclass on
    the index. If the two disagree the planner silently falls back to a sequential
    scan: correct answers, terrible latency, and every test still passes.

    `embedding IS NOT NULL` matters because seed rows are inserted before they are
    embedded. Without it, un-embedded rows sort as maximally distant and pad the
    result set with irrelevant text that then gets cited.

    `embedding_model = %s` matters for the same reason one layer up: a chunk
    embedded by a *different* 1024-dimensional model is not maximally distant, it
    is plausibly distant, which is worse. Returning nothing is a visible failure;
    returning four wrong chunks is an invisible one. See migration 0002.
    """
    vector = "[" + ",".join(str(x) for x in embedding) + "]"

    async with pool.connection() as conn:
        # Without iterative_scan, a filtered HNSW query can return fewer rows than
        # requested — no error, just quietly missing results. This is the single
        # most common "pgvector is broken" report. SET LOCAL needs a transaction.
        # Note this query is now doubly filtered, which is exactly the shape that
        # triggers it.
        async with conn.transaction():
            await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
            cur = await conn.execute(
                """
                SELECT source, section, content,
                       1 - (embedding <=> %s::vector) AS similarity
                FROM knowledge_chunks
                WHERE embedding IS NOT NULL
                  AND embedding_model = %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (vector, model, vector, limit),
            )
            rows = await cur.fetchall()
    return [_serialise(r) for r in rows]


async def store_embedding(
    pool: AsyncConnectionPool, chunk_id: str, embedding: list[float], model: str
) -> None:
    """Write a vector and stamp it with the model that produced it.

    The two are set in one statement on purpose: a vector whose provenance is
    unknown is unusable, so there must be no window in which one exists without
    the other.
    """
    vector = "[" + ",".join(str(x) for x in embedding) + "]"
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE knowledge_chunks SET embedding = %s::vector, embedding_model = %s "
            "WHERE id = %s",
            (vector, model, chunk_id),
        )


async def unembedded_chunks(pool: AsyncConnectionPool, model: str) -> list[dict[str, Any]]:
    """Chunks that `model` has not embedded — never embedded, or embedded by another.

    `IS DISTINCT FROM` rather than `!=` because `embedding_model` is nullable and
    `NULL != 'x'` evaluates to NULL, not true — a plain `!=` would skip every
    un-embedded row, which is the exact set this function exists to return.
    """
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id::text, source, section, content FROM knowledge_chunks "
            "WHERE embedding IS NULL OR embedding_model IS DISTINCT FROM %s",
            (model,),
        )
        rows = await cur.fetchall()
    return [_serialise(r) for r in rows]


async def record_audit_event(
    pool: AsyncConnectionPool,
    *,
    trace_id: str,
    session_id: str,
    node: str,
    status: str,
    workflow: str | None = None,
    tool: str | None = None,
    latency_ms: int | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """One row per node execution — how a run is explained after the fact.

    Deliberately never raises: losing an audit row must not fail the workflow
    that produced it.
    """
    try:
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO audit_events
                    (trace_id, session_id, workflow, node, tool, status, latency_ms, detail)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (trace_id, session_id, workflow, node, tool, status, latency_ms,
                 json.dumps(detail or {})),
            )
    except Exception:  # noqa: BLE001 - audit must never break the caller
        pass


def _serialise(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Coerce Postgres types the checkpointer cannot serialise.

    `date` and `Decimal` survive a round trip through psycopg but not through
    JSON, and anything a tool returns ends up in checkpointed graph state. Fixing
    it here means a tool author cannot forget.
    """
    if row is None:
        return None
    out: dict[str, Any] = {}
    for key, value in row.items():
        if hasattr(value, "isoformat"):          # date / datetime
            out[key] = value.isoformat()
        elif type(value).__name__ == "Decimal":  # numeric
            out[key] = float(value)
        else:
            out[key] = value
    return out
