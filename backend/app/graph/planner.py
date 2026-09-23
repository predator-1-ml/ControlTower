"""Planner: turn a user goal into a task DAG, before any work happens.

Two properties matter more than the prompt:

**It extends, it never replaces.** On a second turn the planner appends to the
existing plan, so earlier tasks and their results stay in the DAG. Together with
`workflow_states` (keyed per workflow) and `customer_id` / `customer_ref`
persisting in the checkpoint, that is the whole mechanism for "a user may move
between workflows during a session" — there is no special case for switching
anywhere in the code. The limit: a session paused on `interrupt()` never reaches
this node; `/chat` delivers the next message as the answer (see
`docs/tradeoffs.md`).

**Task ids are renumbered on merge.** The model emits local ids ("1", "2") every
turn, which would collide with the previous turn's. Ids are rewritten to t1, t2…
and `depends_on` is remapped with them. Without this, turn two's "task 1" silently
overwrites turn one's.

**It is shown both sides of the conversation, and the customer in focus.** The
previous version handed the model the last two *human* turns only. Observed live:
"Summarise CLM-5003" was answered with "…for Tom Baker (CUST-1004)", and the
follow-up "register a new claim for 100,000" was planned with no customer at all
— the reference had been said by the assistant, and the assistant's side never
reached the planner. A handler's follow-ups lean on the answer they just read, so
the planner has to read it too. The customer in focus is listed separately, from
state rather than from prose, because a reply that names the customer only by
name ("Tom Baker's claim…") would otherwise leave the model guessing at a
reference it was never shown.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.runtime import Runtime

from app.graph.deps import Deps
from app.graph.state import ControlTowerState, Plan, PlanTask, TaskStatus
from app.llm.provider import message_text

SYSTEM_PROMPT = """You plan work for an insurance operations control tower.

Three workflows are available. Each one runs END TO END on its own — it is a
complete capability, not a step:

- onboarding : looks up the customer, verifies identity, checks eligibility and
               creates an application. Action: onboard_customer
- claims     : two actions.
               retrieve_claims — finds a claim by reference, or a customer's
               active claims; checks for missing information (asking the handler
               for it if needed), applies the handling rules and summarises.
               register_claim — records a NEW claim for a customer (first notice
               of loss) and applies the handling rules to it. Use it when the
               handler wants to open, register, log, create or "onboard" a claim.
- knowledge  : answers a policy or procedure question from internal documents.
               Action: answer_question

Rules:
- **EMIT AT MOST ONE TASK PER WORKFLOW.** Never split a workflow into steps.
  "Look up the customer, then onboard them" is ONE onboarding task, because
  onboarding already looks the customer up. Two onboarding tasks would run the
  whole workflow twice and ask the user the same question twice.
- Use depends_on only when a task needs another's RESULT. "Onboard this customer
  and check their claims" is two tasks, and the claims task depends on the
  onboarding task because it needs the customer identified first.
- Do not invent dependencies between unrelated tasks.
- Put references in args exactly as the handler typed them: a CUST- reference as
  customer_ref, a CLM- reference as claim_ref. A customer named without a
  reference goes in customer_name. For a knowledge task, the question itself as
  `question`.
- "This customer", "they" or "them" means the customer in focus below. Put their
  customer_ref in args. If no customer is in focus, leave customer_ref out — the
  workflow will say what it needs.
- register_claim args are customer_ref, claim_ref, claim_type (motor, property
  or travel), amount (a number) and incident_date (YYYY-MM-DD). Fill in ONLY what
  this request states, plus the customer in focus. A new claim has its own type,
  amount and date — never take them from an earlier claim in the conversation.
  Never guess a value; the workflow asks for the rest.
- A plain question about policy or procedure is a single knowledge task.
- A request none of the three workflows can do (send an email, change a policy,
  delete a record) gets NO task. Return an empty task list.
- Most requests need one or two tasks. Never emit more than four.

Known context:
- Customer in focus: {customer}

{existing}"""

# "HANDLED — never plan them again" was the previous wording, and it named the
# WORKFLOW as handled rather than the request: with `knowledge.answer_question {}
# (done)` in the list, a second policy question got an empty plan about half the
# time (probed twice against the live checkpoint, 2026-09-22: one run planned
# it, the next planned nothing). The args are listed for the same reason — a
# task whose args are empty cannot be told apart from a repeat of itself.
EXISTING_PLAN_NOTE = """These tasks are already done in this session. Do not repeat them:
{tasks}

A new question, a new customer or a new claim is NEW work and needs a task,
even in a workflow that has already run. Only an identical request is a repeat."""

# The planner sees the conversation so a follow-up ("and their claims?", "register
# one for them") can be resolved, but it is fenced off from the one request it
# must plan. Handing the model bare human turns with no fence — an earlier
# version — made it plan all of them: observed live, "Onboard CUST-1002" (already
# done) was planned a second time alongside the new request, and the operator
# was asked for the same documents twice.
REQUEST = """Conversation so far, for context only — do NOT plan anything in it:
{transcript}

Plan ONLY this request:
{latest}"""

#: How much conversation the planner is shown. Six messages is three exchanges:
#: enough to resolve "them" and "the other one", short enough that an old,
#: already-planned request cannot read as the current one.
TRANSCRIPT_MESSAGES = 6

#: An assistant answer can run to a dozen lines of facts and citations; the
#: planner needs the references in it, not the prose. Cut, not summarised —
#: a summary would be another model call on every turn.
TRANSCRIPT_CHARS = 600


def _renumber(new_tasks: list[PlanTask], offset: int) -> list[PlanTask]:
    """Give new tasks globally unique ids and remap depends_on to match."""
    mapping = {t.id: f"t{offset + i + 1}" for i, t in enumerate(new_tasks)}
    return [
        t.model_copy(
            update={
                "id": mapping[t.id],
                # A dependency on a task from an earlier turn is already a global
                # id and passes through unchanged.
                "depends_on": [mapping.get(d, d) for d in t.depends_on],
                "status": TaskStatus.PENDING,
            }
        )
        for t in new_tasks
    ]


def _transcript(messages: list[Any]) -> str:
    """Both sides of the conversation before the latest request, as a fenced block."""
    lines: list[str] = []
    for message in messages[-TRANSCRIPT_MESSAGES - 1 : -1]:
        if isinstance(message, HumanMessage):
            lines.append(f"Handler: {message_text(message)}")
        elif isinstance(message, AIMessage):
            lines.append(f"Assistant: {message_text(message)[:TRANSCRIPT_CHARS]}")
    return "\n".join(lines) or "(none)"


async def plan_node(state: ControlTowerState, runtime: Runtime[Deps]) -> dict[str, Any]:
    existing = state.get("plan", [])

    existing_note = ""
    if existing:
        # args included: without them "onboarding.onboard_customer (done)" does
        # not say WHICH customer was onboarded, so the model cannot tell a
        # repeat from new work.
        listed = "\n".join(
            f"- {t.id}: {t.workflow}.{t.action} {t.args} ({t.status.value})" for t in existing
        )
        existing_note = EXISTING_PLAN_NOTE.format(tasks=listed)

    # include_raw=True so a malformed plan is a handled error rather than an
    # exception: the planner runs on every turn, and one bad parse must not kill
    # a session that already has work in flight.
    planner = runtime.context.model.with_structured_output(Plan, include_raw=True)
    messages = state["messages"]
    result = await planner.ainvoke(
        [
            SystemMessage(
                content=SYSTEM_PROMPT.format(
                    customer=state.get("customer_ref") or "none yet",
                    existing=existing_note,
                )
            ),
            HumanMessage(
                content=REQUEST.format(
                    transcript=_transcript(messages),
                    latest=message_text(messages[-1]),
                )
            ),
        ]
    )

    parsed: Plan | None = result.get("parsed")
    if parsed is None:
        return {
            "errors": [
                {
                    "task_id": None,
                    "node": "planner",
                    "kind": "plan_parse_failed",
                    "message": str(result.get("parsing_error"))[:500],
                    "retryable": True,
                }
            ]
        }

    tasks = _renumber(parsed.tasks, offset=len(existing))
    return {
        "plan": tasks,  # merge_tasks appends; existing tasks are untouched
        "turn_task_ids": [t.id for t in tasks],
        "current_intent": parsed.goal,
    }
