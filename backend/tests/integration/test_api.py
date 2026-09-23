"""API tests against the real app, with a real checkpointer and a stubbed model.

Runs the actual FastAPI app (lifespan included) so route wiring, SSE framing, and
checkpointer integration are exercised together. Only the LLM is faked — the
database is real, because the interesting failures here are in persistence and
reconnect, not in prompt quality.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from app.graph.state import Plan, PlanTask

pytestmark = pytest.mark.skipif(
    os.environ.get("CONTROL_TOWER_DB_TESTS") != "1",
    reason="needs Postgres; set CONTROL_TOWER_DB_TESTS=1",
)


@dataclass
class StubResponse:
    content: str


class StubModel:
    """Serves the planner a fixed Plan and everything else fixed text.

    Structured calls dispatch on the SCHEMA: the planner asks for `Plan` and the
    claims workflow's `read_reply` asks for `Reply`. Answering both with a `Plan`
    gives `read_reply` an object with no `.supplied`.
    """

    def __init__(self, plan: Plan, fields: list[tuple[str, str]] | None = None) -> None:
        self.plan = plan
        self.fields = fields or []
        self._schema: Any = None

    def with_structured_output(self, schema, **_kw):
        clone = StubModel(self.plan, self.fields)
        clone._schema = schema
        return clone

    async def ainvoke(self, _messages, *_a: Any, **_kw: Any):
        if self._schema is Plan:
            return {"parsed": self.plan, "raw": None, "parsing_error": None}
        if self._schema is not None:
            parsed = self._schema(supplied=[{"name": n, "value": v} for n, v in self.fields])
            return {"parsed": parsed, "raw": None, "parsing_error": None}
        return StubResponse("All tasks completed.")


# CUST-1003 has kyc_status 'failed' in the seed, so onboarding routes to
# manual_review and COMPLETES without pausing. CUST-1002 is 'unverified' and
# correctly pauses for documents — used by the interrupt test below.
PLAN_ONBOARD = Plan(
    goal="Onboard CUST-1003",
    tasks=[
        PlanTask(id="1", workflow="onboarding", action="onboard_customer",
                 args={"customer_ref": "CUST-1003"})
    ],
)

PLAN_ONBOARD_UNVERIFIED = Plan(
    goal="Onboard CUST-1002",
    tasks=[
        PlanTask(id="1", workflow="onboarding", action="onboard_customer",
                 args={"customer_ref": "CUST-1002"})
    ],
)

PLAN_INCOMPLETE_CLAIM = Plan(
    goal="Summarise CLM-5003",
    tasks=[
        PlanTask(id="1", workflow="claims", action="summarise_claim",
                 args={"claim_ref": "CLM-5003"})
    ],
)


async def _collect_sse(client: httpx.AsyncClient, body: dict) -> list[tuple[str, dict]]:
    """Read an SSE response into (event, payload) pairs."""
    events: list[tuple[str, dict]] = []
    async with client.stream("POST", "/chat", json=body) as response:
        assert response.status_code == 200, await response.aread()
        event = None
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event:
                raw = line.split(":", 1)[1].strip()
                if raw:
                    events.append((event, json.loads(raw)))
    return events


@pytest.fixture
async def app_client(monkeypatch):
    """Real app, real database, stubbed model.

    The lifespan builds the chat model eagerly and raises without an API key —
    correct production behaviour (fail fast on missing config) but it means
    startup needs *a* key even though every test replaces the model immediately
    after. A placeholder satisfies construction and is never used to call
    anything; `get_settings` is lru_cached, so the cache must be cleared for the
    env var to take effect.
    """
    from asgi_lifespan import LifespanManager  # type: ignore

    from app.core.config import get_settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder-never-called")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    get_settings.cache_clear()

    from app.main import app

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, app


async def test_health_does_not_touch_the_database(app_client):
    client, _ = app_client
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_chat_streams_plan_then_final(app_client):
    """The plan must reach the client before the answer does.

    That ordering is the observable form of "planning before execution" — if the
    plan arrived with the final response, the UI could not show it first.
    """
    client, app = app_client
    app.state.deps.model = StubModel(PLAN_ONBOARD)

    events = await _collect_sse(
        client, {"session_id": f"api-{uuid.uuid4()}", "message": "onboard CUST-1003"}
    )
    names = [name for name, _ in events]

    assert "plan" in names
    assert "final" in names
    assert names.index("plan") < names.index("final")
    assert names[-1] == "done"

    plan_payload = next(p for n, p in events if n == "plan")
    assert plan_payload["tasks"][0]["workflow"] == "onboarding"
    # trace_id rides every frame so a reported failure joins to its audit rows.
    assert all("trace_id" in payload for _, payload in events)


@pytest.fixture
async def restored_claim():
    """Put CLM-5003 back the way the seed left it, whatever the test did to it.

    The app now MUTATES the seed: satisfying the pause clears `missing_fields`
    and moves the claim to `under_review`. Without this,
    `test_repository.py::test_claim_carries_missing_fields` passes or fails
    depending on which file pytest collected first — the worst kind of red.
    """
    from app.db.checkpointer import open_pool

    yield
    pool = await open_pool()
    try:
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE claims SET status = 'awaiting_information', "
                "missing_fields = '[\"incident_report\", \"police_reference\"]'::jsonb "
                "WHERE claim_ref = 'CLM-5003'"
            )
    finally:
        await pool.close()


async def test_interrupt_surfaces_and_the_reply_changes_the_claim(app_client, restored_claim):
    """A message sent while paused is the answer, not a new request — and it lands.

    Planning instead would re-plan over the paused workflow and discard what the
    user typed. That ordering is what the endpoint exists to get right; the rest
    of this test is what the pause exists to get right. It used to store the
    reply verbatim and end: the claim row never changed and no audit row was
    written, so "supply anything and it completes" was literally true.
    """
    client, app = app_client
    app.state.deps.model = StubModel(
        PLAN_INCOMPLETE_CLAIM,
        fields=[("incident_report", "IR-77"), ("police_reference", "PR-12")],
    )
    session = f"api-{uuid.uuid4()}"

    first = await _collect_sse(client, {"session_id": session, "message": "summarise CLM-5003"})
    assert any(name == "interrupt" for name, _ in first)

    state = (await client.get(f"/sessions/{session}")).json()
    assert state["status"] == "awaiting_input"
    assert state["pending_question"]["fields"] == ["incident_report", "police_reference"]

    second = await _collect_sse(client, {"session_id": session, "message": "IR-77 and PR-12"})
    assert any(name == "final" for name, _ in second)

    after = (await client.get(f"/sessions/{session}")).json()
    assert after["status"] == "idle"
    assert after["workflow_states"]["claims"]["outcome"] == "summarised"

    from app.db.checkpointer import open_pool

    pool = await open_pool()
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT status, missing_fields FROM claims WHERE claim_ref = 'CLM-5003'"
            )
            row = await cur.fetchone()
            cur = await conn.execute(
                "SELECT detail FROM audit_events WHERE session_id = %s AND node = %s",
                (session, "record_information"),
            )
            audit = await cur.fetchall()
    finally:
        await pool.close()

    assert row["status"] == "under_review"
    assert row["missing_fields"] == []
    # The only record that IR-77 was ever supplied: there is no documents table.
    assert len(audit) == 1
    assert audit[0]["detail"]["received"] == {
        "incident_report": "IR-77",
        "police_reference": "PR-12",
    }


async def test_a_reply_that_supplies_nothing_leaves_the_claim_alone(app_client, restored_claim):
    """The pause ends honestly rather than looping or pretending it completed."""
    client, app = app_client
    app.state.deps.model = StubModel(PLAN_INCOMPLETE_CLAIM, fields=[])
    session = f"api-{uuid.uuid4()}"

    await _collect_sse(client, {"session_id": session, "message": "summarise CLM-5003"})
    second = await _collect_sse(
        client, {"session_id": session, "message": "what does this claim need again?"}
    )

    assert not any(name == "interrupt" for name, _ in second), "must not re-ask"
    after = (await client.get(f"/sessions/{session}")).json()
    assert after["status"] == "idle"
    assert after["workflow_states"]["claims"]["outcome"] == "information_incomplete"


async def test_session_survives_reconnect(app_client):
    """GET /sessions is how a dropped client redraws: SSE has no replay."""
    client, app = app_client
    app.state.deps.model = StubModel(PLAN_ONBOARD)
    session = f"api-{uuid.uuid4()}"

    await _collect_sse(client, {"session_id": session, "message": "onboard CUST-1003"})

    state = (await client.get(f"/sessions/{session}")).json()
    assert state["plan"][0]["status"] == "done"
    assert state["final_response"]

    # The transcript comes back too, both sides, in order. A refreshed browser
    # has lost every token it was streamed; without this it redraws a plan with
    # no conversation above it.
    assert [m["role"] for m in state["messages"]] == ["user", "assistant"]
    assert state["messages"][0]["text"] == "onboard CUST-1003"
    assert state["messages"][1]["text"] == state["final_response"]


async def test_second_turn_keeps_the_first_turns_context(app_client):
    """Regression: graph INPUT is a write, and it used to erase the session.

    `/chat` seeded every channel on every turn. `customer_id` and
    `workflow_states` have no reducer, so turn two's seed overwrote them with
    None and {} — the claims task then had no customer to look up, and the
    onboarding outcome was gone. The graph-level two-turn test never caught it
    because it sends only `messages`; the bug lived in this endpoint's payload.
    """
    client, app = app_client
    session = f"api-{uuid.uuid4()}"

    app.state.deps.model = StubModel(PLAN_ONBOARD)
    await _collect_sse(client, {"session_id": session, "message": "onboard CUST-1003"})

    # No customer in args: the claims task must find it in shared state.
    app.state.deps.model = StubModel(
        Plan(goal="Check claims", tasks=[
            PlanTask(id="1", workflow="claims", action="retrieve_claims")
        ])
    )
    await _collect_sse(client, {"session_id": session, "message": "any open claims?"})

    state = (await client.get(f"/sessions/{session}")).json()
    assert [t["id"] for t in state["plan"]] == ["t1", "t2"]
    assert state["workflow_states"]["onboarding"]["outcome"] == "manual_review"
    assert "claims" in state["workflow_states"]


async def test_onboarding_interrupt_reaches_the_client(app_client):
    """Regression: an interrupt raised inside a SUBGRAPH must reach the client.

    This failed originally. Interrupts arrive on the `updates` channel keyed
    `__interrupt__`, not on `values`, and the value is a tuple rather than a
    state dict — so an `isinstance(update, dict)` guard swallowed them. The
    symptom was the worst kind: the stream simply ended, with the graph paused
    and the user waiting for a question that was never sent.
    """
    client, app = app_client
    app.state.deps.model = StubModel(PLAN_ONBOARD_UNVERIFIED)
    session = f"api-{uuid.uuid4()}"

    events = await _collect_sse(
        client, {"session_id": session, "message": "onboard CUST-1002"}
    )

    interrupts = [payload for name, payload in events if name == "interrupt"]
    assert interrupts, f"interrupt never reached the client; got {[n for n, _ in events]}"
    # Exactly once. `subgraphs=True` surfaces the same interrupt from the
    # subgraph's namespace and again from the parent's; undeduplicated, the
    # client is asked the same question twice. Observed against real Bedrock.
    assert len(interrupts) == 1
    assert interrupts[0]["kind"] == "need_documents"
    assert "photo_id" in interrupts[0]["fields"]

    state = (await client.get(f"/sessions/{session}")).json()
    assert state["status"] == "awaiting_input"


async def test_unknown_session_is_404(app_client):
    client, _ = app_client
    response = await client.get(f"/sessions/does-not-exist-{uuid.uuid4()}")
    assert response.status_code == 404


PLAN_REGISTER = Plan(
    goal="Register a travel claim for CUST-1005",
    tasks=[
        PlanTask(id="1", workflow="claims", action="register_claim",
                 args={"customer_ref": "CUST-1005", "claim_type": "travel",
                       "amount": "450", "incident_date": "2026-09-12"})
    ],
)


async def test_registering_a_claim_writes_the_row_and_names_the_customer(app_client):
    """The write path end to end: a minted reference, a row a later lookup finds,
    and the customer published to the session so "their claims" resolves next turn.

    Cleaned up afterwards: CUST-1005 is the seed's "no claims, clean onboarding"
    customer, and leaving a claim on them would turn that branch into manual review.
    """
    client, app = app_client
    app.state.deps.model = StubModel(PLAN_REGISTER)
    session = f"api-{uuid.uuid4()}"

    events = await _collect_sse(client, {
        "session_id": session,
        "message": "register a travel claim for CUST-1005, 450, incident 2026-09-12",
    })
    assert not any(name == "interrupt" for name, _ in events), "everything was stated"
    assert "registered the claim" in [p.get("label") for n, p in events if n == "step"]

    state = (await client.get(f"/sessions/{session}")).json()
    claim = state["workflow_states"]["claims"]["claims"][0]
    from app.db.checkpointer import open_pool

    pool = await open_pool()
    try:
        try:
            assert state["workflow_states"]["claims"]["outcome"] == "registered"
            assert claim["claim_ref"].startswith("CLM-9")
            assert claim["status"] == "open" and claim["customer_ref"] == "CUST-1005"

            async with pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT claim_type, amount FROM claims WHERE claim_ref = %s",
                    (claim["claim_ref"],),
                )
                row = await cur.fetchone()
            assert row["claim_type"] == "travel" and float(row["amount"]) == 450.0

            snapshot = await app.state.graph.aget_state(
                {"configurable": {"thread_id": session}}
            )
            assert snapshot.values["customer_ref"] == "CUST-1005"
        finally:
            async with pool.connection() as conn:
                await conn.execute(
                    "DELETE FROM claims WHERE claim_ref = %s", (claim["claim_ref"],)
                )
    finally:
        await pool.close()
