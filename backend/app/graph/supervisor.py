"""Supervisor: pick the next executable task and route it to its workflow.

Deliberately has no LLM. Every decision it makes — which tasks are ready, what
happens when one fails, when the plan is finished — is derivable from the plan,
so making it a model call would add latency, cost, and nondeterminism in exchange
for nothing. The intelligence is in the *planner*; the supervisor is control flow.

That also makes it unit-testable without a database or an API key, which is why
`ready_tasks` was written as a pure function over the plan.
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.types import Command

from app.graph.state import (
    TERMINAL_STATUSES,
    ControlTowerState,
    PlanTask,
    TaskStatus,
    is_plan_complete,
    ready_tasks,
)

#: Where the supervisor may send work. Declared as a Literal so LangGraph can
#: infer the graph edges for visualisation, and so a typo is a type error rather
#: than a runtime dead-end.
Destination = Literal["onboarding", "claims", "knowledge", "compose"]

MAX_ATTEMPTS = 3


def _unreachable(plan: list[PlanTask]) -> list[PlanTask]:
    """Tasks that can never run because a dependency failed or was skipped.

    They become SKIPPED, not FAILED: they did not fail, they never ran. That
    distinction is what lets the final response say "I could not do X, so I did
    not attempt Y" instead of reporting two unrelated failures.
    """
    blocked = {t.id for t in plan if t.status in (TaskStatus.FAILED, TaskStatus.SKIPPED)}
    if not blocked:
        return []
    return [
        t.model_copy(update={"status": TaskStatus.SKIPPED, "error": "dependency did not complete"})
        for t in plan
        if t.status not in TERMINAL_STATUSES and blocked.intersection(t.depends_on)
    ]


def supervise(state: ControlTowerState) -> Command[Destination]:
    plan = state.get("plan", [])

    # A task returning to the supervisor still marked RUNNING means its subgraph
    # ended without claiming it — a routing bug or an unimplemented workflow.
    # Fail it rather than looping forever on the same task.
    for task in plan:
        if task.status is TaskStatus.RUNNING:
            attempts = task.attempts + 1
            status = TaskStatus.FAILED if attempts >= MAX_ATTEMPTS else TaskStatus.PENDING
            return Command(
                goto="supervisor",
                update={
                    "plan": [
                        task.model_copy(
                            update={
                                "status": status,
                                "attempts": attempts,
                                "error": "workflow returned without completing the task",
                            }
                        )
                    ]
                },
            )

    skipped = _unreachable(plan)
    if skipped:
        return Command(goto="supervisor", update={"plan": skipped})

    if is_plan_complete(plan) or not plan:
        return Command(goto="compose")

    ready = ready_tasks(plan)
    if not ready:
        # Nothing runnable and nothing terminal: every remaining task is waiting
        # on a human (NEEDS_INPUT). Compose what we have rather than spinning.
        return Command(goto="compose")

    task = ready[0]
    return Command(
        goto=task.workflow,
        update={
            "plan": [task.model_copy(update={"status": TaskStatus.RUNNING})],
            "active_workflow": task.workflow,
        },
    )


def unimplemented_workflow(state: ControlTowerState) -> dict[str, Any]:
    """Placeholder for workflows not yet built.

    Fails the task with a clear reason instead of leaving it RUNNING forever.
    Replaced as each workflow lands.
    """
    for task in state.get("plan", []):
        if task.status is TaskStatus.RUNNING:
            return {
                "plan": [
                    task.model_copy(
                        update={
                            "status": TaskStatus.FAILED,
                            "error": f"workflow '{task.workflow}' is not implemented yet",
                        }
                    )
                ],
                "errors": [
                    {
                        "task_id": task.id,
                        "node": task.workflow,
                        "kind": "not_implemented",
                        "message": f"workflow '{task.workflow}' is not implemented yet",
                        "retryable": False,
                    }
                ],
            }
    return {}
