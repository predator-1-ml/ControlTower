"""Repository queries against a real Postgres.

Skipped unless CONTROL_TOWER_DB_TESTS=1, so `make test` stays runnable with no
database. Run these with `make test-db` after `make up && make migrate && make seed`.

These assert against the seed fixtures, so they are also a check that the seed
still exercises every branch the workflows depend on.
"""

from __future__ import annotations

import os

import pytest

from app.db.checkpointer import open_pool
from app.db.repository import (
    create_application,
    get_active_claims,
    get_claim,
    get_customer,
    record_audit_event,
    record_claim_information,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("CONTROL_TOWER_DB_TESTS") != "1",
    reason="needs Postgres; set CONTROL_TOWER_DB_TESTS=1",
)


@pytest.fixture
async def pool():
    p = await open_pool()
    yield p
    await p.close()


async def test_get_customer(pool):
    customer = await get_customer(pool, "CUST-1001")
    assert customer is not None
    assert customer["full_name"] == "Priya Raman"
    assert customer["kyc_status"] == "verified"


async def test_unknown_customer_returns_none(pool):
    assert await get_customer(pool, "CUST-9999") is None


async def test_active_claims_excludes_settled(pool):
    """The worked example: does CUST-1001 have an active claim?

    They have two claims, one settled. Only the open one may come back — if the
    status filter regresses, the demo answers the assignment's own question wrong.
    """
    customer = await get_customer(pool, "CUST-1001")
    claims = await get_active_claims(pool, customer["id"])

    assert [c["claim_ref"] for c in claims] == ["CLM-5001"]
    assert claims[0]["status"] == "open"


async def test_customer_with_no_claims(pool):
    customer = await get_customer(pool, "CUST-1002")
    assert await get_active_claims(pool, customer["id"]) == []


async def test_claim_carries_missing_fields(pool):
    """Drives the claims workflow's request_information branch."""
    claim = await get_claim(pool, "CLM-5003")
    assert claim["status"] == "awaiting_information"
    assert claim["missing_fields"] == ["incident_report", "police_reference"]
    assert claim["customer_name"] == "Tom Baker"


async def test_rows_are_json_serialisable(pool):
    """Everything a tool returns ends up in checkpointed state, so it must survive JSON.

    `date` and `Decimal` round-trip through psycopg but not through JSON, and the
    failure would surface as a checkpoint write error deep in a graph run rather
    than here.
    """
    import json

    claim = await get_claim(pool, "CLM-5001")
    json.dumps(claim)  # raises if _serialise missed a type

    assert isinstance(claim["amount"], float)
    assert isinstance(claim["incident_date"], str)


async def test_recording_information_clears_the_fields_and_reopens_the_claim(pool):
    """One statement does the removal, the status move and the read-back.

    Then it runs AGAIN. A process death between this UPDATE and the checkpoint
    re-runs the node on resume, so a second run must leave the same row — the
    guarantee the node's docstring claims and this is the only place that can
    actually prove it.
    """
    try:
        first = await record_claim_information(
            pool, "CLM-5003", ["incident_report", "police_reference"]
        )
        assert first == {"status": "under_review", "missing_fields": []}

        second = await record_claim_information(
            pool, "CLM-5003", ["incident_report", "police_reference"]
        )
        assert second == first, "the write must be idempotent"
    finally:
        # The seed is load-bearing for other tests in this file and for the demo.
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE claims SET status = 'awaiting_information', "
                "missing_fields = '[\"incident_report\", \"police_reference\"]'::jsonb "
                "WHERE claim_ref = 'CLM-5003'"
            )


async def test_a_partial_supply_removes_only_that_field_and_holds_the_status(pool):
    """Still incomplete means still `awaiting_information` — no early re-opening."""
    try:
        row = await record_claim_information(pool, "CLM-5003", ["police_reference"])
        assert row == {"status": "awaiting_information", "missing_fields": ["incident_report"]}
    finally:
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE claims SET status = 'awaiting_information', "
                "missing_fields = '[\"incident_report\", \"police_reference\"]'::jsonb "
                "WHERE claim_ref = 'CLM-5003'"
            )


async def test_create_application(pool):
    customer = await get_customer(pool, "CUST-1002")
    app_row = await create_application(pool, customer["id"], "motor_policy", "submitted")

    assert app_row["status"] == "submitted"
    assert app_row["customer_id"] == customer["id"]


async def test_audit_event_never_raises(pool):
    """Audit failures must not break the workflow that produced them."""
    await record_audit_event(
        pool,
        trace_id="t-1",
        session_id="s-1",
        node="test_node",
        status="ok",
        detail={"nested": {"value": 1}},
    )
    # A bad status value violates nothing here, but an oversized detail or a
    # dropped connection would — and must still be swallowed.
    await record_audit_event(
        pool, trace_id="t-2", session_id="s-2", node="n", status="ok", latency_ms=None
    )
