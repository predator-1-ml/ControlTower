"""Claims Operations workflow.

    retrieve -> not_found                        (no such claim)
             -> validate -> request_information  (gaps -> pause for a human)
                         -> summarise            (complete -> LLM summary)

This is the tool/API-driven workflow of the three: most of the behaviour is
deterministic database work and branching, with the LLM used only at the end,
where the ambiguity actually is. Deciding *whether* a claim is incomplete is a
business rule and stays in code; *describing* the claim is language work and goes
to the model. Keeping that line sharp is the difference between an orchestrator
and a chatbot with database access.
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from app.db import repository
from app.graph.deps import Deps
from app.graph.state import (
    ControlTowerState,
    TaskStatus,
    running_task,
    task_delta,
)
from app.llm.provider import message_text

WORKFLOW = "claims"

SUMMARY_PROMPT = """You are summarising an insurance claim for an operations handler.

Claim reference: {claim_ref}
Customer: {customer_name}
Type: {claim_type}
Status: {status}
Amount: {amount}
Incident date: {incident_date}

Write two or three sentences covering what happened, where the claim stands, and
what the handler should do next. State only what the data above supports."""


def _claims_state(state: ControlTowerState) -> dict[str, Any]:
    """This workflow's slice of `workflow_states`.

    Keyed per workflow so a user can leave claims mid-flow, do something else,
    and come back to it intact — the 'move between workflows during a session'
    requirement.
    """
    return state.get("workflow_states", {}).get(WORKFLOW, {})


def _merge_claims_state(state: ControlTowerState, **changes: Any) -> dict[str, Any]:
    """`workflow_states` has no reducer, so merge the whole dict explicitly."""
    all_states = dict(state.get("workflow_states", {}))
    all_states[WORKFLOW] = {**_claims_state(state), **changes}
    return all_states


async def retrieve(state: ControlTowerState, runtime: Runtime[Deps]) -> dict[str, Any]:
    """Load the claim named by the task, or the customer's active claims."""
    task = running_task(state["plan"], WORKFLOW)
    if task is None:
        return {}

    pool = runtime.context.pool
    claim_ref = task.args.get("claim_ref")

    if claim_ref:
        claim = await repository.get_claim(pool, claim_ref)
        claims = [claim] if claim else []
    else:
        # The planner is told to put `customer_ref` (CUST-1001) in args, so a
        # standalone "does CUST-1001 have a claim?" names the customer there.
        # Shared `customer_id` is the fallback: it is what an earlier onboarding
        # task published, and it must not win over a customer named explicitly.
        customer_ref = task.args.get("customer_ref")
        customer = await repository.get_customer(pool, customer_ref) if customer_ref else None
        customer_id = customer["id"] if customer else state.get("customer_id")
        claims = await repository.get_active_claims(pool, customer_id) if customer_id else []

    await repository.record_audit_event(
        pool,
        trace_id=state["trace_id"],
        session_id=state["session_id"],
        workflow=WORKFLOW,
        node="retrieve",
        status="ok",
        detail={"found": len(claims)},
    )

    return {
        # REPLACE this workflow's slice, do not merge into it. `workflow_states`
        # outlives the turn, so a second claims task in one session would
        # otherwise inherit the first one's `summary` and `outcome`, and compose
        # would report both. Other workflows' slices are carried over untouched.
        "workflow_states": {**state.get("workflow_states", {}), WORKFLOW: {"claims": claims}},
        "tool_results": [
            {
                "task_id": task.id,
                "tool": "get_claims",
                "ok": True,
                "payload": {"count": len(claims)},
                "latency_ms": 0,
            }
        ],
    }


def route_after_retrieve(state: ControlTowerState) -> Literal["validate", "not_found"]:
    return "validate" if _claims_state(state).get("claims") else "not_found"


async def not_found(state: ControlTowerState) -> dict[str, Any]:
    """Terminal, and deliberately not an error.

    'This customer has no active claims' is a correct, useful answer to the
    assignment's worked example. Modelling it as a failure would make the plan
    look broken whenever the true answer is 'nothing here'.
    """
    task = running_task(state["plan"], WORKFLOW)
    if task is None:
        return {}
    return {
        "plan": task_delta(task, status=TaskStatus.DONE, result_ref="no_claims"),
        "workflow_states": _merge_claims_state(state, outcome="no_claims"),
    }


async def validate(state: ControlTowerState) -> dict[str, Any]:
    """Business rule, not an LLM judgement: which required fields are absent.

    `missing_fields` is persisted on the claim, so validation is a read rather
    than a re-derivation. That keeps the decision auditable — a handler can see
    why the workflow paused.
    """
    claims = _claims_state(state).get("claims", [])
    incomplete = [c for c in claims if c.get("missing_fields")]
    return {
        "workflow_states": _merge_claims_state(
            state,
            incomplete=incomplete,
            missing_fields=sorted({f for c in incomplete for f in c["missing_fields"]}),
        )
    }


def route_after_validate(state: ControlTowerState) -> Literal["request_information", "summarise"]:
    return "request_information" if _claims_state(state).get("incomplete") else "summarise"


async def request_information(state: ControlTowerState) -> dict[str, Any]:
    """Pause for a human.

    `interrupt()` is the FIRST statement, and that is load-bearing rather than
    stylistic: on resume LangGraph re-runs this node from the top, not from the
    interrupt line. Anything above it would execute twice. There are deliberately
    no database writes in this node for the same reason.
    """
    missing = _claims_state(state).get("missing_fields", [])
    claim_refs = [c["claim_ref"] for c in _claims_state(state).get("incomplete", [])]

    answer = interrupt(
        {
            "kind": "need_info",
            "workflow": WORKFLOW,
            # Words, not column names: an operator reads this. `fields` below
            # keeps the raw names for anything that needs to match on them.
            "question": (
                f"Claim {', '.join(claim_refs)} is missing "
                f"{' and '.join(f.replace('_', ' ') for f in missing)}. "
                "Please supply the missing details."
            ),
            "fields": missing,
        }
    )

    task = running_task(state["plan"], WORKFLOW)
    if task is None:
        return {}
    return {
        "plan": task_delta(task, status=TaskStatus.DONE, result_ref="information_requested"),
        "workflow_states": _merge_claims_state(
            state, outcome="information_requested", supplied=answer
        ),
        "pending_question": None,
    }


async def summarise(state: ControlTowerState, runtime: Runtime[Deps]) -> dict[str, Any]:
    """The one LLM call in this workflow, and the only genuinely ambiguous step."""
    task = running_task(state["plan"], WORKFLOW)
    if task is None:
        return {}

    claims = _claims_state(state).get("claims", [])
    prompt = "\n\n".join(
        SUMMARY_PROMPT.format(
            claim_ref=c.get("claim_ref"),
            customer_name=c.get("customer_name", "unknown"),
            claim_type=c.get("claim_type"),
            status=c.get("status"),
            amount=c.get("amount"),
            incident_date=c.get("incident_date"),
        )
        for c in claims
    )

    response = await runtime.context.model.ainvoke(prompt)
    summary = message_text(response)

    await repository.record_audit_event(
        runtime.context.pool,
        trace_id=state["trace_id"],
        session_id=state["session_id"],
        workflow=WORKFLOW,
        node="summarise",
        status="ok",
        tool="llm",
    )

    return {
        "plan": task_delta(task, status=TaskStatus.DONE, result_ref="summary"),
        "workflow_states": _merge_claims_state(state, outcome="summarised", summary=summary),
    }


def build_claims_graph(checkpointer=None):
    """Compile the subgraph.

    In the real graph no checkpointer is passed: a compiled subgraph added as a
    node inherits the parent's, which is what lets `interrupt()` here propagate
    to the top-level graph. Passing `checkpointer=False` would silently disable
    interrupts inside this workflow.

    The argument exists so tests can compile it standalone — with no parent to
    inherit from, resume needs a saver of its own.
    """
    return (
        StateGraph(ControlTowerState, context_schema=Deps)
        .add_node("retrieve", retrieve)
        .add_node("validate", validate)
        .add_node("request_information", request_information)
        .add_node("summarise", summarise)
        .add_node("not_found", not_found)
        .add_edge(START, "retrieve")
        .add_conditional_edges("retrieve", route_after_retrieve, ["validate", "not_found"])
        .add_conditional_edges(
            "validate", route_after_validate, ["request_information", "summarise"]
        )
        .add_edge("request_information", END)
        .add_edge("summarise", END)
        .add_edge("not_found", END)
        .compile(checkpointer=checkpointer)
    )
