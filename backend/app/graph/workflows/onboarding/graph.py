"""Customer Onboarding workflow.

    load -> validate -> request_information (gaps -> pause)
                     -> verify_identity
                            -> failed      -> manual_review
                            -> unverified  -> request_documents (pause) -> check_eligibility
                            -> verified    -> check_eligibility
                                                  -> ineligible -> reject
                                                  -> review     -> manual_review
                                                  -> eligible   -> create_application

The deterministic counterpart to the claims workflow: no LLM at all. Every
decision here is a business rule with a legal or financial consequence, and those
belong in code where they can be read, tested, and pointed at during an audit.

The seed fixtures were chosen to reach every branch:
  CUST-1002  unverified          -> pauses for documents, then approves
  CUST-1003  kyc_status failed   -> manual review
  CUST-1001  verified + active claim -> manual review (policy rule)
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from app.db import repository
from app.graph.deps import Deps
from app.graph.state import ControlTowerState, TaskStatus, running_task, task_delta

WORKFLOW = "onboarding"
REQUIRED_FIELDS = ("full_name", "email", "date_of_birth")
MIN_AGE = 18


def _ws(state: ControlTowerState) -> dict[str, Any]:
    return state.get("workflow_states", {}).get(WORKFLOW, {})


def _merge(state: ControlTowerState, **changes: Any) -> dict[str, Any]:
    all_states = dict(state.get("workflow_states", {}))
    all_states[WORKFLOW] = {**_ws(state), **changes}
    return all_states


def _finish(state: ControlTowerState, outcome: str, **extra: Any) -> dict[str, Any]:
    """Mark this workflow's task done and record the outcome.

    Every terminal node ends the same way, so it is written once. 'Rejected' and
    'manual review' are outcomes, not failures — the workflow did its job.
    """
    task = running_task(state["plan"], WORKFLOW)
    if task is None:
        return {}
    return {
        "plan": task_delta(task, status=TaskStatus.DONE, result_ref=outcome),
        "workflow_states": _merge(state, outcome=outcome, **extra),
    }


async def load_customer(state: ControlTowerState, runtime: Runtime[Deps]) -> dict[str, Any]:
    task = running_task(state["plan"], WORKFLOW)
    if task is None:
        return {}

    ref = task.args.get("customer_ref")
    customer = await repository.get_customer(runtime.context.pool, ref) if ref else None

    await repository.record_audit_event(
        runtime.context.pool,
        trace_id=state["trace_id"],
        session_id=state["session_id"],
        workflow=WORKFLOW,
        node="load_customer",
        status="ok" if customer else "not_found",
        detail={"customer_ref": ref},
    )

    return {
        # REPLACE this workflow's slice, do not merge into it. `workflow_states`
        # outlives the turn, so onboarding a second customer in one session would
        # otherwise inherit the first one's `application` or `review_reason`, and
        # compose would report both. Other workflows' slices are left untouched.
        "workflow_states": {
            **state.get("workflow_states", {}),
            WORKFLOW: {"customer": customer, "customer_ref": ref},
        },
        # Publish to shared state so a later claims task can depend on it without
        # re-reading the database. This is how "onboard X and check their claims"
        # passes context between two different workflows.
        "customer_id": customer["id"] if customer else state.get("customer_id"),
    }


def route_after_load(state: ControlTowerState) -> Literal["validate", "not_found"]:
    return "validate" if _ws(state).get("customer") else "not_found"


async def not_found(state: ControlTowerState) -> dict[str, Any]:
    return _finish(state, "customer_not_found")


async def validate(state: ControlTowerState) -> dict[str, Any]:
    customer = _ws(state).get("customer") or {}
    missing = [f for f in REQUIRED_FIELDS if not customer.get(f)]
    return {"workflow_states": _merge(state, missing_fields=missing)}


def route_after_validate(
    state: ControlTowerState,
) -> Literal["request_information", "verify_identity"]:
    return "request_information" if _ws(state).get("missing_fields") else "verify_identity"


async def request_information(state: ControlTowerState) -> dict[str, Any]:
    """Pause for missing profile fields.

    interrupt() first: resume re-runs the node from the top, so anything above it
    would run twice. No writes in here for the same reason.
    """
    missing = _ws(state).get("missing_fields", [])
    supplied = interrupt(
        {
            "kind": "need_info",
            "workflow": WORKFLOW,
            "question": f"Missing customer details: {', '.join(missing)}.",
            "fields": missing,
        }
    )
    # `/chat` resumes with the operator's free text, never a dict, so it is
    # recorded as given rather than unpacked into the customer record (`**text`
    # raises TypeError and, because the thread stays parked on this interrupt,
    # every later message would resume into the same crash). Turning "born
    # 1990-01-01" into fields is language work this workflow deliberately has none of.
    return {"workflow_states": _merge(state, supplied=supplied, missing_fields=[])}


async def verify_identity(state: ControlTowerState) -> dict[str, Any]:
    """Read persisted KYC state rather than re-deriving it.

    Identity verification is a regulated decision made elsewhere; this workflow
    consumes its result. Re-deciding it here would be inventing an authority the
    system does not have.
    """
    customer = _ws(state).get("customer") or {}
    return {"workflow_states": _merge(state, kyc=customer.get("kyc_status"))}


def route_after_verify(
    state: ControlTowerState,
) -> Literal["manual_review", "request_documents", "check_eligibility"]:
    kyc = _ws(state).get("kyc")
    if kyc == "failed":
        return "manual_review"
    if kyc == "verified":
        return "check_eligibility"
    return "request_documents"  # unverified / pending


async def request_documents(state: ControlTowerState) -> dict[str, Any]:
    documents = interrupt(
        {
            "kind": "need_documents",
            "workflow": WORKFLOW,
            "question": (
                "Identity is not yet verified. Supply a government photo ID and "
                "proof of address issued within the last three months."
            ),
            "fields": ["photo_id", "proof_of_address"],
        }
    )
    return {"workflow_states": _merge(state, documents=documents, kyc="verified")}


async def check_eligibility(state: ControlTowerState, runtime: Runtime[Deps]) -> dict[str, Any]:
    """Age and existing-claims rules, both from the seeded policy."""
    customer = _ws(state).get("customer") or {}
    reasons: list[str] = []

    dob = customer.get("date_of_birth")
    if dob:
        born = date.fromisoformat(dob)
        today = date.today()
        age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
        if age < MIN_AGE:
            reasons.append(f"applicant is {age}, minimum is {MIN_AGE}")

    # Policy: an unsettled claim is not automatically disqualifying, but it does
    # require a human. Same ACTIVE_CLAIM_STATUSES definition the claims workflow
    # uses, so the two can never drift apart.
    active = (
        await repository.get_active_claims(runtime.context.pool, customer["id"])
        if customer.get("id")
        else []
    )

    return {
        "workflow_states": _merge(
            state,
            ineligible_reasons=reasons,
            active_claims=[c["claim_ref"] for c in active],
        )
    }


def route_after_eligibility(
    state: ControlTowerState,
) -> Literal["reject", "manual_review", "create_application"]:
    ws = _ws(state)
    if ws.get("ineligible_reasons"):
        return "reject"
    if ws.get("active_claims"):
        return "manual_review"
    return "create_application"


async def create_application(state: ControlTowerState, runtime: Runtime[Deps]) -> dict[str, Any]:
    """The only write in the system.

    Deliberately in its own node with no interrupt() anywhere near it: resume
    re-runs a node from the top, so an interrupt sharing this node would create
    two applications for one customer.
    """
    customer = _ws(state).get("customer") or {}
    task = running_task(state["plan"], WORKFLOW)
    if task is None or not customer.get("id"):
        return {}

    application = await repository.create_application(
        runtime.context.pool,
        customer_id=customer["id"],
        product=task.args.get("product", "motor_policy"),
        status="submitted",
    )
    await repository.record_audit_event(
        runtime.context.pool,
        trace_id=state["trace_id"],
        session_id=state["session_id"],
        workflow=WORKFLOW,
        node="create_application",
        status="ok",
        detail={"application_id": application["id"]},
    )
    return _finish(state, "application_created", application=application)


async def manual_review(state: ControlTowerState) -> dict[str, Any]:
    ws = _ws(state)
    reason = (
        "identity verification failed"
        if ws.get("kyc") == "failed"
        else f"existing active claim(s): {', '.join(ws.get('active_claims', []))}"
    )
    return _finish(state, "manual_review", review_reason=reason)


async def reject(state: ControlTowerState) -> dict[str, Any]:
    return _finish(state, "rejected", reasons=_ws(state).get("ineligible_reasons", []))


def build_onboarding_graph(checkpointer=None):
    return (
        StateGraph(ControlTowerState, context_schema=Deps)
        .add_node("load_customer", load_customer)
        .add_node("not_found", not_found)
        .add_node("validate", validate)
        .add_node("request_information", request_information)
        .add_node("verify_identity", verify_identity)
        .add_node("request_documents", request_documents)
        .add_node("check_eligibility", check_eligibility)
        .add_node("create_application", create_application)
        .add_node("manual_review", manual_review)
        .add_node("reject", reject)
        .add_edge(START, "load_customer")
        .add_conditional_edges("load_customer", route_after_load, ["validate", "not_found"])
        .add_conditional_edges(
            "validate", route_after_validate, ["request_information", "verify_identity"]
        )
        .add_edge("request_information", "verify_identity")
        .add_conditional_edges(
            "verify_identity",
            route_after_verify,
            ["manual_review", "request_documents", "check_eligibility"],
        )
        .add_edge("request_documents", "check_eligibility")
        .add_conditional_edges(
            "check_eligibility",
            route_after_eligibility,
            ["reject", "manual_review", "create_application"],
        )
        .add_edge("not_found", END)
        .add_edge("create_application", END)
        .add_edge("manual_review", END)
        .add_edge("reject", END)
        .compile(checkpointer=checkpointer)
    )
