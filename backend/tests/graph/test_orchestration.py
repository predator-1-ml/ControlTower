"""Planner + supervisor + workflows, wired together.

No database, no API key: the pool is stubbed and the planner's model returns a
fixed Plan. What is under test is the orchestration — dependency ordering,
workflow switching, failure propagation, and the recursion ceiling — not the
model's ability to produce a good plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.db import repository
from app.graph.build import RECURSION_LIMIT, build_graph
from app.graph.deps import Deps
from app.graph.state import Plan, PlanTask, TaskStatus, new_state

# --------------------------------------------------------------------- stubs


class StubPool:
    """No methods: audit writes genuinely fail and must be swallowed."""


@dataclass
class StubResponse:
    content: str


class StubModel:
    """Serves the planner a fixed Plan and everything else a fixed string.

    `with_structured_output` returns self, so one object covers both the planner
    (structured) and compose/summarise (plain text) call paths.
    """

    def __init__(self, plan: Plan | None = None, reply: str = "Done.") -> None:
        self.plan = plan
        self.reply = reply
        self._structured = False
        self.plan_calls = 0

    def with_structured_output(self, _schema, **_kw):
        clone = StubModel(self.plan, self.reply)
        clone._structured = True
        clone.plan_calls = 0
        self._child = clone
        return clone

    async def ainvoke(self, _messages, *_a: Any, **_kw: Any):
        if self._structured:
            self.plan_calls += 1
            return {"parsed": self.plan, "raw": None, "parsing_error": None}
        return StubResponse(self.reply)


def deps(model: StubModel) -> Deps:
    return Deps(pool=StubPool(), model=model)


def state_with(message: str):
    s = new_state(session_id="s1", user_id="u1", trace_id="t1")
    s["messages"] = [HumanMessage(content=message)]
    return s


def cfg(thread: str) -> dict:
    return {"configurable": {"thread_id": thread}, "recursion_limit": RECURSION_LIMIT}


@pytest.fixture(autouse=True)
def stub_db(monkeypatch):
    """Database responses shared by every test here."""
    customers = {
        "CUST-1002": {
            "id": "id-1002",
            "external_ref": "CUST-1002",
            "full_name": "Daniel Okafor",
            "email": "d@example.com",
            "date_of_birth": "1995-11-03",
            "kyc_status": "verified",
        },
        "CUST-1003": {
            "id": "id-1003",
            "external_ref": "CUST-1003",
            "full_name": "Mei Lin",
            "email": "m@example.com",
            "date_of_birth": "1979-02-27",
            "kyc_status": "failed",
        },
    }

    async def get_customer(_pool, ref):
        return customers.get(ref)

    async def get_active_claims(_pool, _customer_id):
        return []

    async def get_claim(_pool, ref):
        return {
            "claim_ref": ref,
            "customer_name": "Daniel Okafor",
            "claim_type": "motor",
            "status": "open",
            "amount": 100.0,
            "incident_date": "2026-01-01",
            "missing_fields": [],
        }

    async def create_application(_pool, customer_id, product, status):
        return {"id": "app-1", "customer_id": customer_id, "product": product, "status": status}

    monkeypatch.setattr(repository, "get_customer", get_customer)
    monkeypatch.setattr(repository, "get_active_claims", get_active_claims)
    monkeypatch.setattr(repository, "get_claim", get_claim)
    monkeypatch.setattr(repository, "create_application", create_application)


# --------------------------------------------------------------------- tests


async def test_worked_example_plans_before_executing():
    """The assignment's own example, end to end.

    'Onboard this customer and check whether they already have an active claim'
    must produce a dependency edge and execute in that order.
    """
    plan = Plan(
        goal="Onboard CUST-1002 and check for active claims",
        tasks=[
            PlanTask(id="1", workflow="onboarding", action="onboard_customer",
                     args={"customer_ref": "CUST-1002"}),
            PlanTask(id="2", workflow="claims", action="retrieve_claims",
                     args={"customer_ref": "CUST-1002"}, depends_on=["1"]),
        ],
    )
    graph = build_graph(checkpointer=InMemorySaver())
    result = await graph.ainvoke(
        state_with("Onboard CUST-1002 and check if they have an active claim"),
        cfg("worked-example"),
        context=deps(StubModel(plan)),
    )

    by_id = {t.id: t for t in result["plan"]}
    assert by_id["t1"].status is TaskStatus.DONE
    assert by_id["t2"].status is TaskStatus.DONE
    # The dependency survived renumbering — this is the bit that silently breaks.
    assert by_id["t2"].depends_on == ["t1"]
    assert result["final_response"]


async def test_dependency_blocks_until_prerequisite_completes():
    """A task whose dependency never completes must be SKIPPED, not FAILED.

    It did not fail; it never ran. The distinction is what lets the final
    response explain itself.
    """
    plan = Plan(
        goal="unreachable work",
        tasks=[
            PlanTask(id="1", workflow="knowledge", action="answer_question"),
            PlanTask(id="2", workflow="claims", action="retrieve_claims", depends_on=["1"]),
        ],
    )
    # include_knowledge=False makes the knowledge task fail deterministically,
    # which is what this test needs: a failure whose dependents must be skipped.
    graph = build_graph(checkpointer=InMemorySaver(), include_knowledge=False)
    result = await graph.ainvoke(
        state_with("look something up then check claims"),
        cfg("skip-chain"),
        context=deps(StubModel(plan)),
    )

    by_id = {t.id: t for t in result["plan"]}
    assert by_id["t1"].status is TaskStatus.FAILED      # knowledge not implemented
    assert by_id["t2"].status is TaskStatus.SKIPPED     # never attempted
    assert "not implemented" in by_id["t1"].error


async def test_second_turn_extends_the_plan_and_keeps_earlier_tasks():
    """Switching workflow mid-session must not discard prior work.

    This is requirement 11, and it falls out of the planner appending rather than
    replacing — there is no workflow-switch branch anywhere in the code.
    """
    graph = build_graph(checkpointer=InMemorySaver())
    config = cfg("two-turns")

    first = Plan(goal="onboard", tasks=[
        PlanTask(id="1", workflow="onboarding", action="onboard_customer",
                 args={"customer_ref": "CUST-1002"})
    ])
    await graph.ainvoke(state_with("onboard CUST-1002"), config, context=deps(StubModel(first)))

    second = Plan(goal="claims", tasks=[
        PlanTask(id="1", workflow="claims", action="summarise_claim",
                 args={"claim_ref": "CLM-5001"})
    ])
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="now summarise claim CLM-5001")]},
        config,
        context=deps(StubModel(second)),
    )

    ids = [t.id for t in result["plan"]]
    assert ids == ["t1", "t2"], "turn two must append, not overwrite turn one"
    assert all(t.status is TaskStatus.DONE for t in result["plan"])


async def test_failed_identity_routes_to_manual_review():
    plan = Plan(goal="onboard", tasks=[
        PlanTask(id="1", workflow="onboarding", action="onboard_customer",
                 args={"customer_ref": "CUST-1003"})
    ])
    graph = build_graph(checkpointer=InMemorySaver())
    result = await graph.ainvoke(
        state_with("onboard CUST-1003"), cfg("manual-review"), context=deps(StubModel(plan))
    )

    onboarding = result["workflow_states"]["onboarding"]
    assert onboarding["outcome"] == "manual_review"
    assert "identity verification failed" in onboarding["review_reason"]


async def test_unknown_customer_is_handled_not_crashed():
    plan = Plan(goal="onboard", tasks=[
        PlanTask(id="1", workflow="onboarding", action="onboard_customer",
                 args={"customer_ref": "CUST-9999"})
    ])
    graph = build_graph(checkpointer=InMemorySaver())
    result = await graph.ainvoke(
        state_with("onboard CUST-9999"), cfg("unknown"), context=deps(StubModel(plan))
    )

    assert result["workflow_states"]["onboarding"]["outcome"] == "customer_not_found"
    assert result["plan"][0].status is TaskStatus.DONE


async def test_unparseable_plan_is_recorded_not_raised():
    """The planner runs every turn; one bad parse must not kill the session."""
    graph = build_graph(checkpointer=InMemorySaver())
    result = await graph.ainvoke(
        state_with("gibberish"), cfg("bad-plan"), context=deps(StubModel(plan=None))
    )

    assert result["errors"][0]["kind"] == "plan_parse_failed"
    assert result["final_response"]  # still answers the user


async def test_large_plan_does_not_hit_the_recursion_ceiling():
    """12 tasks exceeds LangGraph's default recursion_limit of 25.

    2 super-steps per task means the default dies at ~11. This is the exact
    silent cliff a 3-task demo never reaches.
    """
    plan = Plan(
        goal="many claims",
        tasks=[
            PlanTask(id=str(i), workflow="claims", action="summarise_claim",
                     args={"claim_ref": f"CLM-{5000 + i}"})
            for i in range(1, 13)
        ],
    )
    graph = build_graph(checkpointer=InMemorySaver())
    result = await graph.ainvoke(
        state_with("summarise all the claims"), cfg("big-plan"), context=deps(StubModel(plan))
    )

    assert len(result["plan"]) == 12
    assert all(t.status is TaskStatus.DONE for t in result["plan"])
