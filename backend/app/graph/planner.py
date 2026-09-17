"""Planner: turn a user goal into a task DAG, before any work happens.

Two properties matter more than the prompt:

**It extends, it never replaces.** On a second turn the planner appends to the
existing plan. That single choice is the entire mechanism for "a user may move
between workflows during a session": new tasks join the DAG, tasks the user
walked away from stay PENDING, and the supervisor picks them up again when they
come back. There is no special case for switching workflows anywhere in the code.

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

Break the user's request into tasks, each owned by exactly one workflow:

- onboarding : look up a customer, verify identity, check eligibility, create an
               application. Actions: retrieve_customer, onboard_customer.
- claims     : find a customer's claims, validate them, summarise them.
               Actions: retrieve_claims, summarise_claim.
- knowledge  : answer a policy or procedure question from internal documents.
               Actions: answer_question.

Rules:
- Use depends_on when a task genuinely needs another's result. "Onboard this
  customer and check their claims" means the claims task depends on the
  onboarding task, because the claims lookup needs the customer identified first.
- Do not invent dependencies between unrelated tasks; independent tasks should
  run independently.
- Put customer references (like CUST-1001) and claim references (like CLM-5001)
  in args, as customer_ref and claim_ref.
- Prefer the smallest plan that answers the request. Never emit more than 8 tasks.
- If the request is a plain question about policy or procedure, that is a single
  knowledge task.

{existing}"""

EXISTING_PLAN_NOTE = """The session already has these tasks. Plan ONLY the new
work the latest message asks for; do not repeat existing tasks:
{tasks}"""


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
        listed = "\n".join(
            f"- {t.id}: {t.workflow}.{t.action} ({t.status.value})" for t in existing
        )
        existing_note = EXISTING_PLAN_NOTE.format(tasks=listed)

    # include_raw=True so a malformed plan is a handled error rather than an
    # exception: the planner runs on every turn, and one bad parse must not kill
    # a session that already has work in flight.
    planner = runtime.context.model.with_structured_output(Plan, include_raw=True)
    result = await planner.ainvoke(
        [
            SystemMessage(content=SYSTEM_PROMPT.format(existing=existing_note)),
            *[m for m in state["messages"] if isinstance(m, HumanMessage)][-3:],
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
        "current_intent": parsed.goal,
    }
