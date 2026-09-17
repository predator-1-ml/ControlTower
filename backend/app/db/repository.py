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


async def search_knowledge(
    pool: AsyncConnectionPool, embedding: list[float], limit: int = 4
) -> list[dict[str, Any]]:
    """Nearest knowledge chunks by cosine distance.

    `<=>` is cosine distance and must pair with the `vector_cosine_ops` opclass on
    the index. If the two disagree the planner silently falls back to a sequential
    scan: correct answers, terrible latency, and every test still passes.

    `embedding IS NOT NULL` matters because seed rows are inserted before they are
    embedded. Without it, un-embedded rows sort as maximally distant and pad the
    result set with irrelevant text that then gets cited.
    """
    vector = "[" + ",".join(str(x) for x in embedding) + "]"

    async with pool.connection() as conn:
        # Without iterative_scan, a filtered HNSW query can return fewer rows than
        # requested — no error, just quietly missing results. This is the single
        # most common "pgvector is broken" report. SET LOCAL needs a transaction.
        async with conn.transaction():
            await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
            cur = await conn.execute(
                """
                SELECT source, section, content,
                       1 - (embedding <=> %s::vector) AS similarity
                FROM knowledge_chunks
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (vector, vector, limit),
            )
            rows = await cur.fetchall()
    return [_serialise(r) for r in rows]


async def store_embedding(
    pool: AsyncConnectionPool, chunk_id: str, embedding: list[float]
) -> None:
    vector = "[" + ",".join(str(x) for x in embedding) + "]"
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE knowledge_chunks SET embedding = %s::vector WHERE id = %s",
            (vector, chunk_id),
        )


async def unembedded_chunks(pool: AsyncConnectionPool) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id::text, source, section, content FROM knowledge_chunks "
            "WHERE embedding IS NULL"
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
