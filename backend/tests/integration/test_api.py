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
    """Serves the planner a fixed Plan and everything else fixed text."""

    def __init__(self, plan: Plan) -> None:
        self.plan = plan
        self._structured = False

    def with_structured_output(self, _schema, **_kw):
        clone = StubModel(self.plan)
        clone._structured = True
        return clone

    async def ainvoke(self, _messages, *_a: Any, **_kw: Any):
        if self._structured:
            return {"parsed": self.plan, "raw": None, "parsing_error": None}
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


async def test_interrupt_surfaces_and_message_resumes_it(app_client):
    """A message sent while paused is the answer, not a new request.

    Planning instead would re-plan over the paused workflow and discard what the
    user typed. This is the ordering the endpoint exists to get right.
    """
    client, app = app_client
    app.state.deps.model = StubModel(PLAN_INCOMPLETE_CLAIM)
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
    assert after["workflow_states"]["claims"]["outcome"] == "information_requested"


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
