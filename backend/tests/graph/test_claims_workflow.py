"""Claims workflow: every branch, with no database and no API key.

The nodes take their pool and model from `Runtime[Deps]`, so a test supplies a
stub for each. That is the payoff of injecting dependencies rather than importing
a module-level client: every branch is reachable in milliseconds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.db import repository
from app.graph.deps import Deps
from app.graph.state import PlanTask, TaskStatus, new_state
from app.graph.workflows.claims.graph import build_claims_graph

# --------------------------------------------------------------------- stubs


class StubPool:
    """Stands in for the psycopg pool.

    Deliberately has no methods. Every repository call is patched except
    `record_audit_event`, which therefore genuinely fails against this stub,
    proving audit failures cannot break a workflow.
    """


@dataclass
class StubResponse:
    content: str


class StubModel:
    """Minimal chat-model stand-in: the nodes only ever call `ainvoke`."""

    def __init__(self, reply: str = "Claim summary.") -> None:
        self.reply = reply
        self.calls: list[str] = []

    async def ainvoke(self, prompt: str, *_: Any, **__: Any) -> StubResponse:
        self.calls.append(prompt)
        return StubResponse(self.reply)


CLAIM_COMPLETE = {
    "claim_ref": "CLM-5001",
    "customer_name": "Priya Raman",
    "claim_type": "motor",
    "status": "open",
    "amount": 4820.0,
    "incident_date": "2026-08-21",
    "missing_fields": [],
}

CLAIM_INCOMPLETE = {
    **CLAIM_COMPLETE,
    "claim_ref": "CLM-5003",
    "missing_fields": ["incident_report", "police_reference"],
}


@pytest.fixture
def graph():
    return build_claims_graph()


@pytest.fixture
def db_returns(monkeypatch):
    """Control what the retrieve node finds, without a database.

    Patching the repository is what these tests actually need. Pre-seeding
    `workflow_states` does not work: `retrieve` always runs and overwrites its
    own slice, so the fixture data is silently discarded and every branch falls
    through to not_found. That mistake cost a full test run, which is why this is
    a fixture rather than a comment.
    """

    def _set(claims: list[dict]) -> None:
        async def fake_active(_pool, _customer_id):
            return claims

        async def fake_get(_pool, _claim_ref):
            return claims[0] if claims else None

        monkeypatch.setattr(repository, "get_active_claims", fake_active)
        monkeypatch.setattr(repository, "get_claim", fake_get)

    return _set


def make_state(*, task_args: dict | None = None):
    """State as the supervisor hands it over: one task already RUNNING."""
    state = new_state(session_id="s1", user_id="u1", trace_id="t1")
    state["customer_id"] = "cust-uuid"
    state["plan"] = [
        PlanTask(
            id="1",
            workflow="claims",
            action="retrieve_claims",
            args=task_args or {},
            status=TaskStatus.RUNNING,
        )
    ]
    return state


def deps(model: StubModel | None = None) -> Deps:
    return Deps(pool=StubPool(), model=model or StubModel())


# --------------------------------------------------------------------- tests


async def test_complete_claim_is_summarised(graph, db_returns):
    db_returns([CLAIM_COMPLETE])
    model = StubModel("Motor claim CLM-5001 is open pending assessment.")
    result = await graph.ainvoke(make_state(), context=deps(model))

    claims_state = result["workflow_states"]["claims"]
    assert claims_state["outcome"] == "summarised"
    assert claims_state["summary"] == "Motor claim CLM-5001 is open pending assessment."
    assert result["plan"][0].status is TaskStatus.DONE

    # The prompt must carry the actual claim data, not just the reference,
    # otherwise the model is inventing the summary rather than writing one.
    assert "CLM-5001" in model.calls[0]
    assert "Priya Raman" in model.calls[0]


async def test_incomplete_claim_pauses_instead_of_summarising(graph, db_returns):
    db_returns([CLAIM_INCOMPLETE])
    model = StubModel()
    result = await graph.ainvoke(make_state(), context=deps(model))

    interrupts = result["__interrupt__"]
    assert interrupts, "expected a pause for missing information"
    payload = interrupts[0].value
    assert payload["kind"] == "need_info"
    assert payload["fields"] == ["incident_report", "police_reference"]

    # Deciding to pause is a business rule, so the model must not be consulted at
    # all. A fluent summary of an incomplete claim is exactly the confident-but-
    # wrong output this split exists to prevent.
    assert model.calls == []


async def test_no_claims_is_a_successful_outcome_not_a_failure(graph, db_returns):
    """'No active claims' correctly answers the assignment's worked example."""
    db_returns([])
    result = await graph.ainvoke(make_state(), context=deps())

    assert result["workflow_states"]["claims"]["outcome"] == "no_claims"
    assert result["plan"][0].status is TaskStatus.DONE
    assert result["errors"] == []


async def test_resume_after_supplying_information(db_returns):
    """A paused claim completes once a human answers, across a checkpoint.

    Compiled with its own saver because there is no parent graph here to inherit
    one from.
    """
    db_returns([CLAIM_INCOMPLETE])
    checkpointed = build_claims_graph(checkpointer=InMemorySaver())

    config = {"configurable": {"thread_id": "claims-resume"}}
    await checkpointed.ainvoke(make_state(), config, context=deps())

    result = await checkpointed.ainvoke(
        Command(resume={"incident_report": "IR-77", "police_reference": "PR-12"}),
        config,
        context=deps(),
    )

    claims_state = result["workflow_states"]["claims"]
    assert claims_state["outcome"] == "information_requested"
    assert claims_state["supplied"]["incident_report"] == "IR-77"
    assert result["plan"][0].status is TaskStatus.DONE


async def test_task_update_is_a_delta_not_the_whole_plan(graph, db_returns):
    """Guards the merge_tasks contract where it is easiest to break.

    A node returning the full plan reverts concurrent updates. `task_delta`
    exists to make that impossible; this asserts the workflow actually uses it.
    """
    db_returns([CLAIM_COMPLETE])
    state = make_state()
    state["plan"].append(
        PlanTask(id="2", workflow="onboarding", action="verify", status=TaskStatus.RUNNING)
    )

    result = await graph.ainvoke(state, context=deps())

    # Task 2 belongs to another workflow and must be untouched by this one.
    by_id = {t.id: t for t in result["plan"]}
    assert by_id["1"].status is TaskStatus.DONE
    assert by_id["2"].status is TaskStatus.RUNNING


async def test_audit_failure_does_not_break_the_workflow(graph, db_returns):
    """StubPool has no connection method, so every audit write raises internally.

    The workflow must still complete: losing an audit row is not a reason to fail
    the operation that produced it.
    """
    db_returns([CLAIM_COMPLETE])
    result = await graph.ainvoke(make_state(), context=deps())
    assert result["workflow_states"]["claims"]["outcome"] == "summarised"
