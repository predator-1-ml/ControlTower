"""Claims Operations workflow.

    retrieve_claims:
    retrieve ─▶ not_found                                        (no such claim)
             ─▶ validate ─▶ assess ─▶ summarise                  (nothing missing)
                         ─▶ request_information ─▶ read_reply ─┬─▶ request_information
                                                               └─▶ record_information
                                                                        ─▶ assess ─▶ summarise
    register_claim:
    prepare ─▶ not_registered                    (no customer, or the reference is taken)
            ─▶ create ─▶ assess ─▶ summarise     (type, amount and date all stated)
            ─▶ ask_details ─▶ read_details ─┬─▶ create ─▶ assess ─▶ summarise
                                            └─▶ not_registered   (type still unknown)

Two actions, one graph. Registering a claim is the other half of claims work —
first notice of loss — and it shares the assessment and the summary with lookup,
so a claim registered here is reported exactly as it would be if looked up a
minute later. The pause is the same shape as the lookup pause (ask, read, write
in three nodes, for the reasons given at `request_information`), but it is a
separate pause: it asks for the claim's *facts*, before there is a row to attach
documents to. Rejected: one generic pause node parameterised by what it asks —
the two replies are read into different shapes (named references vs. typed
fields), and folding them would make one node explain both.

This is the tool/API-driven workflow of the three: database work and branching,
with the model used at exactly the points where the ambiguity is — reading a
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

import re
from datetime import date
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import BaseModel, Field, ValidationError

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

#: Both thresholds are quoted from the seeded policy text (`backend/seed/seed.sql`),
#: not chosen here. A test asserts these exact numbers appear in that text —
#: which is what makes `docs/assumptions.md`'s "code and policy agree" checkable
#: rather than merely claimed.
ESCALATION_LIMIT = 10000        # operations-runbook.md, Escalation
SECOND_REVIEW_LIMIT = 5000      # claims-handling-policy.md, Motor claims
CLOSED_STATUSES = ("settled", "rejected")

#: What a newly registered claim is still waiting for, by type — also quoted
#: from the seeded policy ("Registering a claim"), and checked against it by the
#: same test as the thresholds. A claim with an entry here starts
#: `awaiting_information`, which is the status the lookup pause chases, so
#: "summarise CLM-9001" the next day asks for exactly this document.
REQUIRED_DOCUMENTS: dict[str, list[str]] = {
    "motor": ["incident_report"],
    "property": ["incident_report"],
    "travel": [],
}

SUMMARY_PROMPT = """You are summarising claim work for an insurance operations handler.

{facts}

Write two or three sentences: what the claim is, where it stands, and what happens
next. Rules:
- Use only the facts above. Copy references, amounts, dates and citations (which
  look like [document, section]) exactly as given. Do not put square brackets
  around anything else.
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


class ClaimDetails(BaseModel):
    """The facts a claim is registered with. Read from the planner's args AND
    from the operator's reply, through the same validation, so a value is typed
    once whichever way it arrived.

    A fixed schema, unlike `Reply`: these three fields are the same for every
    claim, and typing them is the point — `Literal` is what stops "car" being
    stored as a claim type, and `date` is what turns "20 Sep 2026" into a column
    value. Only `claim_type` is mandatory to register (NOT NULL, and the handling
    rules key on it); an amount or a date unknown at first notice is normal.

    Never enters state as a model: `model_dump(mode="json")` makes the date a
    string, so nothing here needs `ALLOWED_MSGPACK_MODULES` either.
    """

    claim_type: Literal["motor", "property", "travel"] | None = Field(
        None, description="The kind of claim, if the handler stated it."
    )
    amount: float | None = Field(None, ge=0, description="Amount claimed, as a number.")
    incident_date: date | None = Field(None, description="When the incident happened.")


READ_DETAILS_PROMPT = """You read an insurance handler's reply and extract the claim details in it.

They were asked for: {fields}

Fill in ONLY what the reply states. Claim type is one of motor, property or
travel. Write amounts as plain numbers and dates as YYYY-MM-DD. Leave anything
the reply does not give empty. Never guess."""


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


async def _named_customer(
    state: ControlTowerState, args: dict[str, Any], pool: Any
) -> tuple[dict[str, Any] | None, str | None]:
    """The customer a task is about, and why there is none if there is none.

    Three sources, in order. A reference the handler typed (`customer_ref`,
    CUST-1001) wins. A name they typed (`customer_name`) is looked up and must
    match exactly one customer — "Ben" matching two is a question for the
    handler, not a coin toss. The customer in focus is the fallback: what an
    earlier task published, and it must not win over a customer named
    explicitly. Both `retrieve` and `prepare` resolve a customer this way, so the
    rule is written once.

    The second value is the problem to report when nothing resolved — a
    reference or name that matched nothing is a different answer from "this
    customer has no claims", and used to be reported as the latter.

    The fallback carries no name — the reference and id are what the workflows
    and the planner use, and the claim row supplies the name once one exists.
    """
    ref = args.get("customer_ref")
    if ref:
        customer = await repository.get_customer(pool, ref)
        return customer, None if customer else f"no customer {ref} was found."
    name = str(args.get("customer_name") or "").strip()
    if name:
        matches = await repository.find_customers(pool, name)
        if len(matches) == 1:
            return matches[0], None
        if not matches:
            return None, f"no customer called {name!r} was found."
        refs = ", ".join(f"{m['external_ref']} {m['full_name']}" for m in matches)
        return None, f"more than one customer matches {name!r}: {refs}. Use the reference."
    if state.get("customer_id"):
        return {"id": state["customer_id"], "external_ref": state.get("customer_ref")}, None
    return None, "no customer was identified. Name the customer (CUST-…) the claim is for."


MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def _stated(name: str, value: Any, text: str) -> bool:
    """Did the handler actually type this value into `text`?

    The planner's args and the extracted reply are both model output, and a
    model fills gaps from whatever it was shown. Observed live, all three: "log a
    travel claim for them" planned with `claim_ref: CLM-5001` (the prompt's own
    example); a new claim typed `property` from the transcript's previous claim;
    a new claim dated 1 Sep 2026, the previous claim's incident date. The rule
    is the reply guard's: a value the handler did not write is not theirs, and
    is dropped — a dropped reference is minted, anything else is asked for.

    A reference or a type is matched as written. A number is matched by its
    digits, separators removed, so "100,000" and 100000 agree. A date is
    normalised by the model (that is what it is asked for), so the ISO form
    cannot be matched; instead its day must appear as a number in the text, and
    so must its month or its year — "12 Sep 2026", "2026-09-12", "12/09/2026"
    and "12th September" all pass, "yesterday" does not and is asked for.
    """
    if value in (None, ""):
        return False
    haystack = _normalise(text)
    if name == "amount":
        digits = re.sub(r"[^\d.]", "", haystack)
        try:
            return str(int(float(value))) in digits
        except (TypeError, ValueError):
            return False
    if name == "incident_date":
        try:
            d = date.fromisoformat(str(value))
        except ValueError:
            return False
        day = re.search(rf"(?<!\d)0?{d.day}(?!\d)", haystack) is not None
        month_or_year = str(d.year) in haystack or (
            MONTHS[d.month - 1] in haystack or re.search(rf"(?<!\d)0?{d.month}(?!\d)", haystack)
        )
        return day and bool(month_or_year)
    return _normalise(str(value)) in haystack


def _request(state: ControlTowerState) -> str:
    """The operator's latest request, for `_stated`."""
    for message in reversed(state.get("messages", [])):
        if isinstance(message, HumanMessage):
            return str(message.content)
    return ""


async def retrieve(state: ControlTowerState, runtime: Runtime[Deps]) -> dict[str, Any]:
    """Load the claim named by the task, or the customer's active claims."""
    task = running_task(state["plan"], WORKFLOW)
    if task is None:
        return {}

    pool = runtime.context.pool
    claim_ref = task.args.get("claim_ref")
    customer, problem = None, None

    if claim_ref:
        claim = await repository.get_claim(pool, claim_ref)
        claims = [claim] if claim else []
        problem = None if claim else f"no claim {claim_ref} was found."
    else:
        customer, problem = await _named_customer(state, task.args, pool)
        claims = await repository.get_active_claims(pool, customer["id"]) if customer else []

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
        "workflow_states": {
            **state.get("workflow_states", {}),
            WORKFLOW: {"claims": claims, "problem": problem},
        },
        "tool_results": [
            {
                "task_id": task.id,
                "tool": "get_claims",
                "ok": True,
                "payload": {"count": len(claims)},
                "latency_ms": 0,
            }
        ],
        **_customer_in_focus(claims, customer),
    }


def _customer_in_focus(
    claims: list[dict[str, Any]], customer: dict[str, Any] | None
) -> dict[str, Any]:
    """The shared-state update that names who this claims task was about.

    Onboarding has always published `customer_id`; this workflow did not, so a
    session that opened with "summarise CLM-5003" had no customer in focus and
    "register a claim for them" could not be resolved. The claim's owner is
    taken from the row (every claim query joins the customer); a customer named
    with no active claims is still the customer in focus.

    Only known values are written. A channel with no reducer is overwritten by
    whatever a node returns, so `{"customer_id": None}` here would erase the
    customer an earlier onboarding task published.
    """
    if claims and claims[0].get("customer_id"):
        owner = claims[0]
        return {"customer_id": owner["customer_id"], "customer_ref": owner.get("customer_ref")}
    if customer:
        return {"customer_id": customer["id"], "customer_ref": customer["external_ref"]}
    return {}


def route_after_retrieve(state: ControlTowerState) -> Literal["validate", "not_found"]:
    return "validate" if _claims_state(state).get("claims") else "not_found"


async def not_found(state: ControlTowerState) -> dict[str, Any]:
    """Terminal, and deliberately not an error.

    'This customer has no active claims' is a correct, useful answer to the
    assignment's worked example. Modelling it as a failure would make the plan
    look broken whenever the true answer is 'nothing here'.

    Two outcomes, though. A customer (or claim) that does not exist is not a
    customer with no claims, and "does Hiro Tanka have open claims?" used to get
    "no active claims were found" — true of nobody. When `retrieve` recorded a
    problem, it is written as the slice's `summary` so compose passes it through
    verbatim, and the outcome says what happened.
    """
    task = running_task(state["plan"], WORKFLOW)
    if task is None:
        return {}
    problem = _claims_state(state).get("problem")
    if problem:
        return {
            "plan": task_delta(task, status=TaskStatus.DONE, result_ref="not_found"),
            "workflow_states": _merge_claims_state(
                state, outcome="not_found", summary=f"Nothing to report: {problem}"
            ),
        }
    return {
        "plan": task_delta(task, status=TaskStatus.DONE, result_ref="no_claims"),
        "workflow_states": _merge_claims_state(state, outcome="no_claims"),
    }


# ------------------------------------------------------------ register_claim


def _details(values: dict[str, Any]) -> ClaimDetails:
    """Read supplied values into `ClaimDetails`, keeping each one that validates.

    Field by field, not the whole model at once: the planner writes "100,000" for
    an amount often enough, and one bad value must not throw away a good type and
    date alongside it. A value that does not validate is simply not supplied,
    which the caller then asks for — the honest reading of a number it cannot
    parse.
    """
    kept: dict[str, Any] = {}
    for name in ClaimDetails.model_fields:
        value = values.get(name)
        if value in (None, ""):
            continue
        if name == "amount":
            value = re.sub(r"[^\d.]", "", str(value))  # "100,000" / "$1,200.50"
        if name == "claim_type":
            value = str(value).strip().lower()
        try:
            kept[name] = getattr(ClaimDetails.model_validate({name: value}), name)
        except ValidationError:
            pass
    return ClaimDetails(**kept)


def _needed(draft: dict[str, Any]) -> list[str]:
    return [name for name in ClaimDetails.model_fields if draft.get(name) is None]


async def prepare(state: ControlTowerState, runtime: Runtime[Deps]) -> dict[str, Any]:
    """Resolve who the claim is for and what is already known about it.

    No write, no model: this node decides whether registration can proceed,
    and every reason it cannot is a fact a handler can check — no customer
    identified, or a reference that is already someone's claim. `reason` set
    here routes to `not_registered`; otherwise `needed` says whether to ask first.
    """
    task = running_task(state["plan"], WORKFLOW)
    if task is None:
        return {}

    pool = runtime.context.pool
    customer, reason = await _named_customer(state, task.args, pool)
    request = _request(state)
    # Validate first, then keep only what the handler typed (`_stated`): the
    # guard compares the typed value, so "100,000" is read as a number before
    # its digits are looked for.
    draft = {
        name: value if _stated(name, value, request) else None
        for name, value in _details(task.args).model_dump(mode="json").items()
    }
    ref = task.args.get("claim_ref")
    claim_ref = str(ref).strip().upper() if _stated("claim_ref", ref, request) else None

    existing = await repository.get_claim(pool, claim_ref) if customer and claim_ref else None
    if existing:
        reason = (
            f"{claim_ref} already exists, for {existing.get('customer_name')} "
            f"({existing.get('customer_ref')}). A new claim needs a new reference, "
            "or none — one will be assigned."
        )

    return {
        # REPLACE the slice, for the same reason `retrieve` does.
        "workflow_states": {
            **state.get("workflow_states", {}),
            WORKFLOW: {
                "claims": [],
                "customer": customer,
                "claim_ref": claim_ref,
                "draft": draft,
                "needed": _needed(draft),
                "reason": reason,
            },
        },
        **_customer_in_focus([], customer),
    }


def route_after_prepare(
    state: ControlTowerState,
) -> Literal["not_registered", "ask_details", "create"]:
    ws = _claims_state(state)
    if ws.get("reason"):
        return "not_registered"
    return "ask_details" if ws.get("needed") else "create"


async def ask_details(state: ControlTowerState) -> dict[str, Any]:
    """Pause for the claim's facts. `interrupt()` first — see `request_information`."""
    ws = _claims_state(state)
    needed = ws.get("needed", [])
    customer = ws.get("customer") or {}
    which = ws.get("claim_ref") or "the new claim"
    reply = interrupt(
        {
            "kind": "need_info",
            "workflow": WORKFLOW,
            "question": (
                f"To register {which} for {customer.get('external_ref')} I need the "
                f"{' and '.join(f.replace('_', ' ') for f in needed)}. "
                "Please supply what you have; the claim type is required."
            ),
            "fields": needed,
        }
    )
    return {"workflow_states": _merge_claims_state(state, reply=reply)}


async def read_details(state: ControlTowerState, runtime: Runtime[Deps]) -> dict[str, Any]:
    """Read the reply into typed fields — the model call of the register pause.

    Same failure handling as `read_reply`: every failure is caught and reads as
    "nothing supplied", because an exception here would leave the task RUNNING
    with the interrupt consumed. Same anti-invention guard as the planner's args
    (`_stated`): a value the reply does not contain is not the handler's.
    """
    ws = _claims_state(state)
    reply = str(ws.get("reply") or "")
    needed = list(ws.get("needed") or [])
    draft = dict(ws.get("draft") or {})

    supplied: dict[str, Any] = {}
    failure: str | None = None
    try:
        if reply.strip():
            extractor = runtime.context.model.with_structured_output(
                ClaimDetails, include_raw=True
            )
            result = await extractor.ainvoke(
                [
                    SystemMessage(
                        content=READ_DETAILS_PROMPT.format(
                            fields=", ".join(f.replace("_", " ") for f in needed)
                        )
                    ),
                    HumanMessage(content=reply),
                ]
            )
            parsed: ClaimDetails | None = result.get("parsed")
            if parsed is None:
                failure = str(result.get("parsing_error"))[:500]
            else:
                supplied = {
                    k: v
                    for k, v in parsed.model_dump(mode="json", exclude_none=True).items()
                    if _stated(k, v, reply)
                }
    except Exception as exc:  # noqa: BLE001 - see docstring
        failure = f"{type(exc).__name__}: {exc}"[:500]

    draft.update({k: v for k, v in supplied.items() if k in needed})
    update: dict[str, Any] = {
        "workflow_states": _merge_claims_state(
            state,
            draft=draft,
            needed=_needed(draft),
            reply=None,  # consumed
            reason=None if draft.get("claim_type") else "the claim type was not supplied.",
        )
    }
    if failure:
        task = running_task(state["plan"], WORKFLOW)
        update["errors"] = [
            {
                "task_id": task.id if task else None,
                "node": "read_details",
                "kind": "reply_unreadable",
                "message": failure,
                "retryable": False,
            }
        ]
    return update


def route_after_details(state: ControlTowerState) -> Literal["create", "not_registered"]:
    """One ask, then decide. The type is the only thing worth refusing over;
    an amount or date still unknown is recorded as unknown, which is what
    first notice of loss usually looks like."""
    return "not_registered" if _claims_state(state).get("reason") else "create"


async def create(state: ControlTowerState, runtime: Runtime[Deps]) -> dict[str, Any]:
    """The write, in its own node with no interrupt anywhere near it.

    The required documents come from `REQUIRED_DOCUMENTS`, so the row starts in
    the status the lookup pause reads and `assess` reports what is still needed
    — the claim is not paused a second time in the same turn to collect them.
    A handler at first notice rarely has the incident report in hand; recording
    that it is outstanding is the work, chasing it is a later turn.
    """
    ws = _claims_state(state)
    draft = ws.get("draft") or {}
    customer = ws.get("customer") or {}
    pool = runtime.context.pool

    row = await repository.create_claim(
        pool,
        customer_id=customer["id"],
        claim_ref=ws.get("claim_ref"),
        claim_type=draft["claim_type"],
        amount=draft.get("amount"),
        incident_date=draft.get("incident_date"),
        missing_fields=REQUIRED_DOCUMENTS[draft["claim_type"]],
    )
    if row is None and ws.get("claim_ref"):
        # `prepare` saw no such claim, so the reference was taken by THIS run:
        # a process death between the INSERT and the checkpoint replayed the
        # node. Read the claim it made rather than fail on a duplicate. With a
        # minted reference the replay mints a second claim; the audit rows share
        # a trace id, which is how the duplicate is found.
        row = await repository.get_claim(pool, ws["claim_ref"])

    await repository.record_audit_event(
        pool,
        trace_id=state["trace_id"],
        session_id=state["session_id"],
        workflow=WORKFLOW,
        node="create",
        status="ok",
        detail={"claim_ref": (row or {}).get("claim_ref"), "supplied": draft},
    )

    claims = [row] if row else []
    return {
        "workflow_states": _merge_claims_state(
            state,
            claims=claims,
            registered=True,
            incomplete=[c for c in claims if c.get("missing_fields")],
            missing_fields=list((row or {}).get("missing_fields") or []),
        )
    }


async def not_registered(state: ControlTowerState) -> dict[str, Any]:
    """Terminal, and an outcome rather than a failure: the workflow decided
    correctly not to write.

    The reason is written as the slice's `summary`, so compose passes it through
    verbatim and makes no model call — the rule for any text a workflow wrote
    itself. Narrated instead, Nova Pro appended a "Next step:" the facts never
    contained (observed live: "Assign a new reference or none for the new claim").
    """
    task = running_task(state["plan"], WORKFLOW)
    if task is None:
        return {}
    ws = _claims_state(state)
    return {
        "plan": task_delta(task, status=TaskStatus.DONE, result_ref="not_registered"),
        "workflow_states": _merge_claims_state(
            state,
            outcome="not_registered",
            summary=f"The claim was not registered: {ws.get('reason')}",
        ),
    }


# ------------------------------------------------------------ retrieve_claims


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


class _Skipped(Exception):
    """Control flow only: leaves `read_reply`'s try with nothing extracted."""


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
    # An empty reply is the operator's explicit "I do not have this yet"
    # (api/chat.py). The model is not asked to read nothing: the Phase 0 probe
    # showed it invents references when the reply contains none.
    skipped = not reply.strip()
    try:
        if skipped:
            raise _Skipped
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
    except _Skipped:
        pass
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

    # A claim that is still short of information says so, rather than reporting
    # a completed pause that changed nothing. A claim registered this turn is
    # "registered" even when a document is outstanding: the outstanding document
    # is in the next step, and the thing that happened was the registration.
    if ws.get("registered"):
        outcome = "registered"
    elif any(c.get("missing_fields") for c in ws.get("claims") or []):
        outcome = "information_incomplete"
    else:
        outcome = "summarised"
    return {
        "plan": task_delta(task, status=TaskStatus.DONE, result_ref=outcome),
        "workflow_states": _merge_claims_state(state, outcome=outcome, summary=summary),
    }


def route_action(state: ControlTowerState) -> Literal["retrieve", "prepare"]:
    """Which half of the workflow the running task asks for.

    Only `register_claim` goes to the register path. Anything else — including
    an action name the planner improvised, which happens — is a lookup, because
    a lookup is the safe default: it writes nothing.
    """
    task = running_task(state["plan"], WORKFLOW)
    return "prepare" if task and task.action == "register_claim" else "retrieve"


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
        .add_node("prepare", prepare)
        .add_node("ask_details", ask_details)
        .add_node("read_details", read_details)
        .add_node("create", create)
        .add_node("not_registered", not_registered)
        .add_conditional_edges(START, route_action, ["retrieve", "prepare"])
        .add_conditional_edges("retrieve", route_after_retrieve, ["validate", "not_found"])
        .add_conditional_edges(
            "prepare", route_after_prepare, ["not_registered", "ask_details", "create"]
        )
        .add_edge("ask_details", "read_details")
        .add_conditional_edges("read_details", route_after_details, ["create", "not_registered"])
        .add_edge("create", "assess")
        .add_edge("not_registered", END)
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
