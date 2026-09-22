"""Planner: turn a user goal into a task DAG, before any work happens.

Two properties matter more than the prompt:

**It extends, it never replaces.** On a second turn the planner appends to the
existing plan, so earlier tasks and their results stay in the DAG. Together with
`workflow_states` (keyed per workflow) and `customer_id` persisting in the
checkpoint, that is the whole mechanism for "a user may move between workflows
during a session" — there is no special case for switching anywhere in the code.
The limit: a session paused on `interrupt()` never reaches this node; `/chat`
delivers the next message as the answer (see `docs/tradeoffs.md`).

**Task ids are renumbered on merge.** The model emits local ids ("1", "2") every
turn, which would collide with the previous turn's. Ids are rewritten to t1, t2…
and `depends_on` is remapped with them. Without this, turn two's "task 1" silently
overwrites turn one's.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.runtime import Runtime

from app.graph.deps import Deps
from app.graph.state import ControlTowerState, Plan, PlanTask, TaskStatus

SYSTEM_PROMPT = """You plan work for an insurance operations control tower.

Three workflows are available. Each one runs END TO END on its own — it is a
complete capability, not a step:

- onboarding : looks up the customer, verifies identity, checks eligibility and
               creates an application. Action: onboard_customer
- claims     : finds the customer's claims, validates and summarises them.
               Action: retrieve_claims
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
- Put references in args: customer_ref for CUST-1001, claim_ref for CLM-5001.
- A plain question about policy or procedure is a single knowledge task.
- Most requests need one or two tasks. Never emit more than four.

{existing}"""

EXISTING_PLAN_NOTE = """The session already has these tasks. They are HANDLED — never plan
them again, whatever the earlier requests say:
{tasks}"""

# The planner sees earlier requests so a follow-up ("and their claims?") can be
# resolved, but they are fenced off from the one request it must plan. Handing
# the model three bare human turns — the previous version — made it plan all
# three: observed live, "Onboard CUST-1002" (already done) was planned a second
# time alongside the new request, and the operator was asked for the same
# documents twice.
REQUEST = """Earlier requests in this session, for context only — do NOT plan these:
{earlier}

Plan ONLY this request:
{latest}"""


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
    requests = [str(m.content) for m in state["messages"] if isinstance(m, HumanMessage)]
    result = await planner.ainvoke(
        [
            SystemMessage(content=SYSTEM_PROMPT.format(existing=existing_note)),
            HumanMessage(
                content=REQUEST.format(
                    earlier="\n".join(f"- {r}" for r in requests[-3:-1]) or "(none)",
                    latest=requests[-1],
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
