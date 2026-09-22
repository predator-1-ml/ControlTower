"""Claims Operations workflow.

    retrieve ─▶ not_found                                        (no such claim)
             ─▶ validate ─▶ assess ─▶ summarise                  (nothing missing)
                         ─▶ request_information ─▶ read_reply ─┬─▶ request_information
                                                               └─▶ record_information
                                                                        ─▶ assess ─▶ summarise

This is the tool/API-driven workflow of the three: database work and branching,
with the model used at exactly the two points where the ambiguity is — reading a
human's free text, and writing the summary. Everything between them is code:

> **The LLM does language. Code makes decisions.**

Whether a claim is incomplete, whether a supplied value is real, whether the
claim is escalated, what the status becomes — all code, all testable without a
model. `next_actions` below is the sharpest example: the thresholds come from the
seeded policy documents, so the RAG answer and the deterministic workflow cannot
disagree about when a claim needs a second review.

The pause is the other half. It used to accept anything: the reply was echoed
into state, the claim row never changed, and no audit row was written. Now the
model extracts what was supplied, **code verifies it against the operator's own
words**, one node writes it, and a claim that is still incomplete says so.
"""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from app.db import repository
from app.graph.compose import claims_facts
from app.graph.deps import Deps
from app.graph.state import (
    ControlTowerState,
    TaskStatus,
    running_task,
    task_delta,
)
from app.llm.provider import message_text

WORKFLOW = "claims"

#: Both thresholds are quoted from the seeded policy text (`data/seed/seed.sql`),
#: not chosen here. A test asserts these exact numbers appear in that text —
#: which is what makes `docs/assumptions.md`'s "code and policy agree" checkable
#: rather than merely claimed.
ESCALATION_LIMIT = 10000        # operations-runbook.md, Escalation
SECOND_REVIEW_LIMIT = 5000      # claims-handling-policy.md, Motor claims
CLOSED_STATUSES = ("settled", "rejected")

SUMMARY_PROMPT = """You are summarising claim work for an insurance operations handler.

{facts}

Write two or three sentences: what the claim is, where it stands, and what happens
next. Rules:
- Use only the facts above. Copy references, amounts, dates and anything in
  [square brackets] exactly as given.
- The next step has already been decided. Phrase it; never choose one.
- Plain text. No headings, no preamble."""

READ_REPLY_PROMPT = """You read an insurance handler's reply and extract what they supplied.

They were asked for these details, by name: {fields}

List one entry per detail they actually gave, using those names exactly. Copy the
value they wrote, verbatim. If they supplied none — they asked a question, or said
they are still chasing it — return an empty list. Never guess a value."""


class Supplied(BaseModel):
    """One requested detail and the value the operator gave for it."""

    name: str = Field(description="The requested field name, exactly as listed.")
    value: str = Field(description="The value the operator wrote, verbatim.")


class Reply(BaseModel):
    """Structured output for `read_reply`.

    A list of pairs rather than a free-form object: the field names vary per
    claim, and a fixed schema is what tool-calling models fill reliably.

    It never enters graph state — `read_reply` converts it to a plain
    `dict[str, str]` — so there is nothing to add to `ALLOWED_MSGPACK_MODULES`.
    """

    supplied: list[Supplied] = Field(default_factory=list)


def next_actions(claim: dict[str, Any]) -> list[str]:
    """What a handler should do with this claim, from the seeded policy rules.

    Pure, and deliberately: this is the one place a business decision is made in
    this workflow, and it is a function of a dict — no graph, no database, no
    model. `summarise` phrases what this returns; `compose` reports it. Neither
    may choose one.

    Two rules can apply at once (a motor claim of 12,000 is both over the
    escalation limit and over the second-review limit), hence a list.

    The second-review rule is restricted to motor claims. The policy sentence
    "Claims of 5000 or more require a second review" sits under *Motor claims*
    and directly follows "Motor claims under 5000 are settled by a single
    assessor", so reading it as a rule for every claim type would over-escalate
    every large travel and property claim in the book.

    Each string ends with the citation the UI renders as a source chip, in the
    same `[source, section]` form the knowledge workflow emits — so a handler can
    check the rule against the document it came from.
    """
    if claim.get("status") in CLOSED_STATUSES:
        return ["None: the claim is closed."]

    actions: list[str] = []
    missing = claim.get("missing_fields") or []
    if missing:
        actions.append(
            "Still needed: "
            + ", ".join(f.replace("_", " ") for f in missing)
            + ". The claim stays Awaiting information and closes automatically after "
            "30 days [claims-handling-policy.md, Missing information]"
        )

    amount = float(claim.get("amount") or 0)
    if amount > ESCALATION_LIMIT:
        actions.append(
            f"Escalate to the duty manager: the claim exceeds {ESCALATION_LIMIT:,} "
            "[operations-runbook.md, Escalation]"
        )
    if claim.get("claim_type") == "motor" and amount >= SECOND_REVIEW_LIMIT:
        actions.append(
            "Book a second review before settlement [claims-handling-policy.md, Motor claims]"
        )

    return actions or ["Proceed with standard assessment by a single assessor."]


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
            # The outstanding fields of the ONE claim the pause will ask about —
            # not a union across every incomplete claim. A value supplied against
            # a union has no owning claim to be recorded on. See
            # request_information.
            missing_fields=list(incomplete[0]["missing_fields"]) if incomplete else [],
        )
    }


def route_after_validate(state: ControlTowerState) -> Literal["request_information", "assess"]:
    return "request_information" if _claims_state(state).get("incomplete") else "assess"


async def request_information(state: ControlTowerState) -> dict[str, Any]:
    """Pause for a human, about ONE claim.

    `interrupt()` is the FIRST statement, and that is load-bearing rather than
    stylistic: on resume LangGraph re-runs this node from the top, not from the
    interrupt line. Anything above it would execute twice. So this node holds no
    database write and no model call — reading the reply is `read_reply`'s job,
    in its own node, which also means the operator's words are checkpointed
    before that fallible call is made.

    It asks about `incomplete[0]` only. The question is built from the slice's
    *current* `missing_fields`, which `read_reply` narrows, so a second ask lists
    only what is still outstanding.

    Nothing here marks the task done: `summarise` is the single owner of that.

    Failure mode this prevents: with the previous union of every incomplete
    claim's fields, a reply supplying "police reference" had no owning claim to
    be recorded against. No seeded customer has two incomplete claims, so the
    demo would never show it and no other test would catch it — hence a test.
    """
    ws = _claims_state(state)
    missing = ws.get("missing_fields", [])
    claim = (ws.get("incomplete") or [{}])[0]

    reply = interrupt(
        {
            "kind": "need_info",
            "workflow": WORKFLOW,
            # Words, not column names: an operator reads this. `fields` below
            # keeps the raw names for anything that needs to match on them.
            "question": (
                f"Claim {claim.get('claim_ref')} is missing "
                f"{' and '.join(f.replace('_', ' ') for f in missing)}. "
                "Please supply the missing details."
            ),
            "fields": missing,
        }
    )

    return {"workflow_states": _merge_claims_state(state, reply=reply)}


def _normalise(text: str) -> str:
    return " ".join(str(text).split()).casefold()


def _verified(supplied: list[Supplied], missing: list[str], reply: str) -> dict[str, str]:
    """Keep only pairs that were asked for AND actually occur in the reply.

    **This guards against invention, and nothing else.** A model asked to extract
    two references from a reply that contains one will sometimes produce a
    plausible second; requiring the value to appear in the operator's own words
    makes that impossible. It does NOT validate a reference format — "yes" is
    accepted as a police reference if the operator wrote "yes". Saying so here
    matters, because a guard that reads as stronger than it is gets trusted for
    something it never did.

    Names are normalised (`Police Reference` -> `police_reference`) because the
    model echoes them back in whatever case it likes; the value is kept verbatim,
    since it is a reference a human will search for.
    """
    haystack = _normalise(reply)
    verified: dict[str, str] = {}
    for pair in supplied:
        name = pair.name.strip().lower().replace(" ", "_")
        if name in missing and pair.value.strip() and _normalise(pair.value) in haystack:
            verified[name] = pair.value.strip()
    return verified


async def read_reply(state: ControlTowerState, runtime: Runtime[Deps]) -> dict[str, Any]:
    """Read the operator's free text — the first of this workflow's two model calls.

    Its own node, and the reason is not double execution (code after
    `interrupt()` runs once): it is that the reply must be **checkpointed before**
    the fallible call. A process death here resumes with the operator's words
    intact and re-runs only the extraction.

    Every failure is caught and treated as "nothing was extracted". Uncaught, the
    interrupt has already been consumed, the task is still RUNNING, the graph
    unwinds, and the operator's *next* message is planned as a brand-new request
    — so they are asked the same question again from `retrieve`, with their
    answer gone. `include_raw=True` turns a bad parse into a value rather than an
    exception, but it does not cover a transport error: the Phase 0 probe raised
    `LoginRefreshRequired` straight out of `ainvoke`, which is why the try
    wraps the call as well.
    """
    ws = _claims_state(state)
    reply = str(ws.get("reply") or "")
    missing = list(ws.get("missing_fields") or [])

    supplied_now: dict[str, str] = {}
    failure: str | None = None
    try:
        extractor = runtime.context.model.with_structured_output(Reply, include_raw=True)
        result = await extractor.ainvoke(
            [
                SystemMessage(content=READ_REPLY_PROMPT.format(fields=", ".join(missing))),
                HumanMessage(content=reply),
            ]
        )
        parsed: Reply | None = result.get("parsed")
        if parsed is None:
            failure = str(result.get("parsing_error"))[:500]
        else:
            supplied_now = _verified(parsed.supplied, missing, reply)
    except Exception as exc:  # noqa: BLE001 - see docstring
        failure = f"{type(exc).__name__}: {exc}"[:500]

    received = {**(ws.get("received") or {}), **supplied_now}
    update: dict[str, Any] = {
        "workflow_states": _merge_claims_state(
            state,
            received=received,
            missing_fields=[f for f in missing if f not in received],
            # The router needs to know whether THIS reply made progress;
            # `received` accumulates across re-asks and cannot answer that.
            supplied_now=sorted(supplied_now),
            reply_unreadable=failure is not None,
            reply=None,  # consumed; a stale reply must not be re-read next turn
        )
    }
    if failure:
        task = running_task(state["plan"], WORKFLOW)
        update["errors"] = [
            {
                "task_id": task.id if task else None,
                "node": "read_reply",
                "kind": "reply_unreadable",
                "message": failure,
                "retryable": False,
            }
        ]
    return update


def route_after_reply(
    state: ControlTowerState,
) -> Literal["request_information", "record_information"]:
    """Re-ask only after progress.

    **No counter in state.** Progress is the loop variant: `missing_fields` is
    finite and strictly shrinks on every re-ask, so the loop terminates on its
    own. A reply that supplied nothing usable ends the pause, and the claim
    truthfully stays `awaiting_information` rather than the operator being asked
    the same question until a counter gives up.
    """
    ws = _claims_state(state)
    if ws.get("missing_fields") and ws.get("supplied_now"):
        return "request_information"
    return "record_information"


async def record_information(state: ControlTowerState, runtime: Runtime[Deps]) -> dict[str, Any]:
    """The only database write in this workflow, in its own node.

    Nothing to record is a real case — an operator can end the pause without
    supplying anything — and it returns early rather than issuing a no-op UPDATE
    and an audit row saying nothing was received, which is noise a reviewer has
    to read past.

    The row the UPDATE returns is merged straight into the slice, so state and
    database cannot disagree and no refresh query is needed.
    """
    ws = _claims_state(state)
    received = ws.get("received") or {}
    claim_ref = (ws.get("incomplete") or [{}])[0].get("claim_ref")
    if not received or not claim_ref:
        return {}

    row = await repository.record_claim_information(
        runtime.context.pool, claim_ref, sorted(received)
    )
    await repository.record_audit_event(
        runtime.context.pool,
        trace_id=state["trace_id"],
        session_id=state["session_id"],
        workflow=WORKFLOW,
        node="record_information",
        status="ok",
        # The values themselves, because there is no documents table: this audit
        # row is the only record that IR-2291 was ever supplied.
        detail={"claim_ref": claim_ref, "received": received},
    )

    claims = [
        {**c, **(row or {})} if c.get("claim_ref") == claim_ref else c
        for c in ws.get("claims") or []
    ]
    return {
        "workflow_states": _merge_claims_state(
            state,
            claims=claims,
            incomplete=[c for c in claims if c.get("missing_fields")],
        )
    }


async def assess(state: ControlTowerState) -> dict[str, Any]:
    """Apply the handling rules. No model, no write — just `next_actions`."""
    claims = _claims_state(state).get("claims", [])
    return {
        "workflow_states": _merge_claims_state(
            state, next_actions={c["claim_ref"]: next_actions(c) for c in claims}
        )
    }


async def summarise(state: ControlTowerState, runtime: Runtime[Deps]) -> dict[str, Any]:
    """Write the claim summary — the assignment names this as a claims capability.

    Its prompt is built from `claims_facts`, the same lines the final answer is
    written from, so the summary and the answer can never describe one claim
    differently.

    **The single owner of `done` and `outcome`, on every completed path.** If two
    nodes could mark the task done, a path that missed one would leave it RUNNING,
    and the supervisor's guard would retry it three times before failing it.
    """
    task = running_task(state["plan"], WORKFLOW)
    if task is None:
        return {}

    ws = _claims_state(state)
    facts = "\n".join(f"- {line}" for line in claims_facts(ws))
    response = await runtime.context.model.ainvoke(SUMMARY_PROMPT.format(facts=facts))
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

    # Three outcomes. A claim that is still short of information says so, rather
    # than reporting a completed pause that changed nothing.
    outcome = (
        "information_incomplete"
        if any(c.get("missing_fields") for c in ws.get("claims") or [])
        else "summarised"
    )
    return {
        "plan": task_delta(task, status=TaskStatus.DONE, result_ref=outcome),
        "workflow_states": _merge_claims_state(state, outcome=outcome, summary=summary),
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
        .add_node("read_reply", read_reply)
        .add_node("record_information", record_information)
        .add_node("assess", assess)
        .add_node("summarise", summarise)
        .add_node("not_found", not_found)
        .add_edge(START, "retrieve")
        .add_conditional_edges("retrieve", route_after_retrieve, ["validate", "not_found"])
        .add_conditional_edges("validate", route_after_validate, ["request_information", "assess"])
        .add_edge("request_information", "read_reply")
        .add_conditional_edges(
            "read_reply", route_after_reply, ["request_information", "record_information"]
        )
        .add_edge("record_information", "assess")
        .add_edge("assess", "summarise")
        .add_edge("summarise", END)
        .add_edge("not_found", END)
        .compile(checkpointer=checkpointer)
    )
