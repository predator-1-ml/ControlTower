"""Compose the final answer from what the workflows actually produced.

Three rules, in order of how much they cost to learn:

**The model is given facts, never state.** This node used to hand the model
`str(dict)` of each workflow slice with two keys excluded, so the answer it wrote
back was a state dump: UUIDs, `12750.0`, and `[t1] claims.retrieve_claims ->
done`. Removing that from the *prompt* rather than from the *input* is a losing
game — the model can only leak what it was shown. So the slices are turned into
operator-facing lines here, in code, and the model never sees anything else.

**It reports; it never decides.** Every decision was made deterministically
upstream (`next_actions` in the claims workflow, the onboarding routing rules).
Re-opening one here is how a system starts confidently contradicting its own
audit trail. "Next step:" lines arrive already written and are copied.

**Text a workflow wrote is never paraphrased.** Knowledge's `answer` carries
citations built from the retrieved chunks, not from the model; a second model
pass can only damage them, and a damaged citation silently stops being a source
chip in the UI. Claims' `summary` is a capability the assignment names in its own
right. So when a workflow wrote operator-facing text this turn, that text is
passed through verbatim — and when it was the turn's only task, this node makes
**zero** model calls.

Partial success is the normal case, not an edge case: an operations request that
half-succeeds is a Tuesday. The prompt says so explicitly, because a model given
a mixed result will otherwise narrate the successful half and quietly drop the
rest.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.runtime import Runtime

from app.graph.deps import Deps
from app.graph.state import ControlTowerState, PlanTask, TaskStatus
from app.llm.provider import message_text

SYSTEM_PROMPT = """You are writing to an insurance operations handler who asked the request below.

Answer their request first, in the first sentence. Then the two to four facts that
matter to them. If a "Next step" is given, end with a line starting "Next step:".
If none is given, do not write one.

Rules:
- Use only the facts given. Do not infer, soften, or add advice.
- Refer to things the way the handler does: CLM- and CUST- references and names.
- Copy amounts, dates and anything in [square brackets] exactly as given.
- If something could not be completed, say so plainly. Never report only what worked.
- Plain text. Short lines. "- " for a list. No headings, no preamble."""

#: Section headings for the fact block. Workflow names are internal; these are
#: the words a handler uses, and they are the only place a workflow name appears
#: anywhere near the model.
TITLES = {"claims": "Claims", "onboarding": "Onboarding", "knowledge": "Knowledge"}

#: Onboarding's outcomes are internal identifiers. Spelled out here rather than
#: left to the model, so "manual_review" cannot become "manual review process"
#: one turn and "flagged for review" the next.
ONBOARDING_OUTCOMES = {
    "application_created": "Application created",
    "manual_review": "Sent to manual review",
    "rejected": "Rejected",
    "customer_not_found": "No such customer on file",
}


def _words(value: Any) -> str:
    """`under_review` -> `Under review`. Column values are not handler language."""
    return str(value or "unknown").replace("_", " ").capitalize()


def _money(amount: Any) -> str:
    """12750.0 -> "12,750.00".

    No currency symbol and no code: there is no currency column, and inventing
    one is the kind of confident detail that survives into a handler's email.
    """
    try:
        return f"{float(amount):,.2f}"
    except (TypeError, ValueError):
        return "not recorded"


def _day(iso: Any) -> str:
    """"2026-09-01" -> "1 Sep 2026".

    Built as `f"{d.day} {d:%b %Y}"` rather than `%-d`, which does not exist on
    Windows (`%-d` raises ValueError there, and this formats on every answer).
    """
    try:
        d = date.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return "not recorded"
    return f"{d.day} {d:%b %Y}"


def claims_facts(ws: dict[str, Any]) -> list[str]:
    """Operator-facing lines for the claims slice.

    Public, and imported by the claims workflow's `summarise`: the summary and
    the final answer are then written from the *same* lines, so the two can never
    describe one claim differently. That shared use is the reason this lives here
    rather than in the workflow — presentation has one home.

    Tolerates a claim with no customer name: `get_active_claims` does not join
    the customer table, only `get_claim` does.
    """
    claims = ws.get("claims") or []
    if not claims:
        return ["No active claims were found for this customer."]

    lines: list[str] = []

    received = ws.get("received") or {}
    if received:
        lines.append(
            "Received from the operator this turn: "
            + "; ".join(f"{_words(k).lower()} {v}" for k, v in received.items())
        )
    if ws.get("reply_unreadable"):
        lines.append("The operator's reply could not be read, so nothing was recorded from it.")

    actions = ws.get("next_actions") or {}
    for claim in claims:
        ref = claim.get("claim_ref")
        name, customer_ref = claim.get("customer_name"), claim.get("customer_ref")
        owner = f" for {name} ({customer_ref})" if name and customer_ref else ""
        lines.append(f"Claim {ref}: {claim.get('claim_type')} claim{owner}")
        lines.append(f"Status: {_words(claim.get('status'))}")
        lines.append(f"Amount claimed: {_money(claim.get('amount'))}")
        lines.append(f"Incident date: {_day(claim.get('incident_date'))}")
        if claim.get("missing_fields"):
            lines.append(
                "Still missing: "
                + ", ".join(f.replace("_", " ") for f in claim["missing_fields"])
            )
        for action in actions.get(ref, []):
            lines.append(f"Next step: {action}")
    return lines


def onboarding_facts(ws: dict[str, Any]) -> list[str]:
    """Operator-facing lines for the onboarding slice.

    Never `customer["id"]` or `application["id"]`: those are UUIDs, and a UUID in
    an answer is the single clearest sign the handler is reading state rather
    than being told something.
    """
    customer = ws.get("customer") or {}
    outcome = ws.get("outcome")
    lines = [f"Outcome: {ONBOARDING_OUTCOMES.get(str(outcome), _words(outcome))}"]

    if customer.get("full_name"):
        lines.insert(0, f"Customer: {customer['full_name']} ({customer.get('external_ref')})")
    elif ws.get("customer_ref"):
        lines.insert(0, f"Customer: {ws['customer_ref']}")

    if ws.get("review_reason"):
        lines.append(f"Reason: {ws['review_reason']}")
    for reason in ws.get("ineligible_reasons") or ws.get("reasons") or []:
        lines.append(f"Reason: {reason}")

    application = ws.get("application") or {}
    if application:
        lines.append(
            f"Application: {application.get('product')}, status "
            f"{_words(application.get('status'))}"
        )
    return lines


#: Workflows whose slice becomes fact lines. Knowledge is absent on purpose: its
#: `answer` is passed through verbatim, never re-described.
FACTS = {"claims": claims_facts, "onboarding": onboarding_facts}


def _turn_tasks(state: ControlTowerState) -> list[PlanTask]:
    """THIS turn's tasks, not the session's.

    `.get`, and the fall-back to the whole plan, are for threads checkpointed
    before `turn_task_ids` existed — new state fields must be optional (see
    state.py, schema_version).
    """
    turn_ids = state.get("turn_task_ids")
    return [t for t in state.get("plan", []) if turn_ids is None or t.id in turn_ids]


def _text_of(ws: dict[str, Any]) -> str | None:
    """Operator-facing text a workflow wrote itself, if any."""
    return ws.get("answer") or ws.get("summary")


def _done_workflows(state: ControlTowerState, turn: list[PlanTask]) -> list[str]:
    """Workflows that completed a task THIS turn, in plan order.

    Both halves matter. `workflow_states` slices are merged, not replaced across
    turns, so a workflow that is not in this turn still holds an older request's
    `answer` — reported, it reads as a fresh result. And a task that failed has
    no result to report, only an error (handled separately).
    """
    return list(dict.fromkeys(t.workflow for t in turn if t.status is TaskStatus.DONE))


def written_texts(state: ControlTowerState) -> list[str]:
    """Text the workflows wrote this turn, in plan order, to append verbatim."""
    slices = state.get("workflow_states", {})
    turn = _turn_tasks(state)
    return [
        text
        for workflow in _done_workflows(state, turn)
        if (text := _text_of(slices.get(workflow, {})))
    ]


def _request(state: ControlTowerState) -> str:
    """The operator's latest request.

    On a resume turn `/chat` sends `Command(resume=...)` and adds no message, so
    this is still the original request — which is the thing the answer should
    address, not the two words the operator typed to unblock it.
    """
    for message in reversed(state.get("messages", [])):
        if isinstance(message, HumanMessage):
            return str(message.content)
    return ""


def model_input(state: ControlTowerState) -> str | None:
    """Everything the model is allowed to see — or None when it is not needed.

    None means every workflow that ran this turn wrote its own operator-facing
    text, so there is nothing left to narrate and no model call is made.
    """
    slices = state.get("workflow_states", {})
    turn = _turn_tasks(state)

    sections: list[str] = []
    for workflow in _done_workflows(state, turn):
        ws = slices.get(workflow, {})
        builder = FACTS.get(workflow)
        if _text_of(ws) or builder is None:
            continue
        lines = builder(ws)
        if lines:
            sections.append(
                TITLES[workflow] + "\n" + "\n".join(f"- {line}" for line in lines)
            )

    # Failed and skipped tasks become a plain line with the error. The workflow
    # name survives here and nowhere else: "a claims task" is how a handler would
    # say it, and without it a mixed turn cannot say WHICH half failed.
    problems = [
        f"- A {t.workflow} task {'was skipped' if t.status is TaskStatus.SKIPPED else 'failed'}"
        f": {t.error or 'no reason recorded'}"
        for t in turn
        if t.status in (TaskStatus.FAILED, TaskStatus.SKIPPED)
    ]
    if problems:
        sections.append("Could not complete\n" + "\n".join(problems))

    if not sections:
        return None
    return f'Operator\'s request: "{_request(state)}"\n\n' + "\n\n".join(sections)


#: Written in code, not by the model. This path used to hand the model a prompt
#: saying "Nothing ran — no tasks were planned" under a rule to "refer to things
#: by CLM- and CUST- references". Given no references, Nova Pro invented them:
#: observed live, "Nothing was done to escalate the situation for CLM-123456 and
#: CUST-789101112". The model can only leak what it is shown, and here it was
#: shown an instruction with nothing to fill it. So when nothing ran there is no
#: model call at all — the same rule as a workflow that wrote its own answer.
NOTHING_PLANNED = (
    "I could not tell which workflow this request needs, so nothing was done. "
    "Ask about a customer (CUST-…), a claim (CLM-…), or a policy or procedure question."
)


async def compose_response(state: ControlTowerState, runtime: Runtime[Deps]) -> dict[str, Any]:
    prompt = model_input(state)
    written = written_texts(state)

    if not _turn_tasks(state):
        text = NOTHING_PLANNED
    elif prompt is None:
        # The turn's only work was a workflow that writes its own text. Calling
        # the model here would paraphrase an answer that is already correct, and
        # the frontend would show it twice — streamed once from the workflow's
        # node, then replaced by a worse version of itself.
        text = "\n\n".join(written)
    else:
        # HumanMessage, NOT AIMessage. Putting the results in an assistant turn
        # makes the model read them as its own half-finished output and CONTINUE
        # the list — observed inventing `[t2] email.send_onboarding_failed ->
        # skipped` and a fabricated Slack channel id for workflows that do not
        # exist. As a user turn it is data to report on, not a draft to extend.
        response = await runtime.context.model.ainvoke(
            [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)]
        )
        text = "\n\n".join([message_text(response), *written])

    return {
        "final_response": text,
        # Append to messages so the next turn has this as conversation context —
        # this is what makes a follow-up like "and the other one?" resolvable.
        "messages": [AIMessage(content=text)],
        "active_workflow": None,
        # Cleared unconditionally: a turn that reaches compose is a turn that is
        # not paused. The old NEEDS_INPUT branch here was dead — nothing in the
        # codebase ever sets that status, and an interrupted turn never reaches
        # this node at all, so the question it tried to preserve never existed.
        "pending_question": None,
    }
