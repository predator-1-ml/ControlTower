"""Graph state for the Control Tower.

Read this file before any other graph module — everything else is a function of
the shape defined here.

Three ideas carry the whole design:

1. **The graph state is execution context, not the business record.**  Domain
   truth lives in the `customers` / `claims` / `applications` tables.  Message
   history is useful context but is NOT the source of truth for workflow
   progress — `plan` is.  That distinction is what separates an orchestrator
   from a chatbot with extra steps.

2. **Reducers decide how concurrent writes merge.**  A channel with no reducer
   is last-write-wins.  Picking the right reducer per channel is most of the
   correctness of a multi-agent graph — see `merge_tasks` below.

3. **State is checkpointed, so it must survive the process.**  Anything here is
   serialized to Postgres after every step and may be rehydrated by a *different*
   ECS task running a *newer* build.  Hence `schema_version`, and hence the rule
   that new fields are added as optional.
"""

from __future__ import annotations

import operator
from enum import StrEnum
from typing import Annotated, Any, Literal

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

WorkflowName = Literal["onboarding", "claims", "knowledge"]


class TaskStatus(StrEnum):
    """Lifecycle of a single planned task.

    NEEDS_INPUT is distinct from BLOCKED on purpose: BLOCKED means "waiting on
    another task", NEEDS_INPUT means "waiting on a human".  The supervisor can
    resolve the first on its own; only a `Command(resume=...)` clears the second.
    """

    PENDING = "pending"        # planned, dependencies not yet evaluated
    READY = "ready"            # dependencies satisfied, awaiting dispatch
    RUNNING = "running"
    BLOCKED = "blocked"        # waiting on another task
    NEEDS_INPUT = "needs_input"  # waiting on a human (an interrupt is open)
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"        # unreachable because a dependency failed


#: Statuses from which a task will never move again without re-planning.
TERMINAL_STATUSES = frozenset({TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.SKIPPED})


class PlanTask(BaseModel):
    """One unit of work in the plan.

    Pydantic rather than TypedDict here because this IS the planner's structured
    output — `with_structured_output(Plan)` validates against this model.  The
    graph *state* stays a TypedDict (documented as more performant, and the
    prebuilt agent factory rejects Pydantic state), but LLM output should be
    validated, so the two differ deliberately.
    """

    id: str = Field(description="Stable short id, e.g. '1'. Referenced by depends_on.")
    workflow: WorkflowName = Field(description="Which subgraph owns this task.")
    action: str = Field(description="Action within that workflow, e.g. 'retrieve_customer'.")
    args: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(
        default_factory=list,
        description="Task ids that must reach DONE before this one may run.",
    )

    # --- runtime fields: set by the supervisor/subgraphs, not by the planner ---
    status: TaskStatus = TaskStatus.PENDING
    attempts: int = 0
    result_ref: str | None = None
    error: str | None = None


class Plan(BaseModel):
    """Planner structured output. Planning happens *before* execution."""

    goal: str
    tasks: list[PlanTask]


class ToolResult(TypedDict):
    task_id: str
    tool: str
    ok: bool
    payload: dict[str, Any]
    latency_ms: int


class ErrorRecord(TypedDict):
    task_id: str | None
    node: str
    kind: str
    message: str
    retryable: bool


class PendingQuestion(TypedDict):
    """Surfaced to the UI when a workflow raises `interrupt()`."""

    task_id: str
    workflow: WorkflowName
    question: str
    fields: list[str]


def merge_tasks(left: list[PlanTask], right: list[PlanTask]) -> list[PlanTask]:
    """Merge task lists by id, letting the right-hand (newer) write win per task.

    ⚠️ **CONTRACT — a node MUST return only the tasks it changed.**

        return {"plan": [updated_task]}          # correct
        return {"plan": whole_plan_with_one_edit}  # WRONG — see below

    This reducer is `(accumulated, node_update) -> accumulated`. If a node hands
    back the *entire* plan, every task it did not touch is a stale write, and
    "right wins per id" faithfully applies that staleness: a task another node
    just advanced to DONE gets stamped back to its older status, and the
    supervisor dispatches it a second time. Returning only the delta makes that
    impossible by construction. There is a regression test pinning both halves
    of this in `tests/graph/test_state.py`.

    Why not the two obvious alternatives:

    * **No reducer (last-write-wins on the whole list).** Two subgraphs finishing
      in the same superstep each replace the list wholesale; one silently
      overwrites the other.

    * **`operator.add`.** Concatenates, so the list grows without bound and ends
      up holding several copies of one id in conflicting states. Whichever copy
      is read first wins — a genuinely horrible bug to chase.

    Merging by id keeps the list stable in size and lets independent subgraphs
    update their own tasks concurrently, which is the entire point of a plan DAG.

    Order is preserved from `left` so the UI's task timeline does not reshuffle
    between renders; tasks appearing only in `right` (a re-plan adding work) are
    appended.
    """
    if not left:
        return list(right)
    if not right:
        return list(left)

    by_id = {task.id: task for task in right}
    merged = [by_id.pop(task.id, task) for task in left]
    merged.extend(by_id.values())  # newly planned tasks, in planner order
    return merged


class ControlTowerState(TypedDict):
    """State shared by the main graph and all workflow subgraphs.

    Subgraphs share these channel names, so a compiled subgraph can be added as a
    node directly with no wrapper function. Keys a subgraph does NOT declare stay
    private to it and never surface here — that is how `workflow_states` stays
    the only cross-workflow surface.
    """

    # --- conversation -------------------------------------------------------
    # add_messages appends and de-duplicates by id, rather than replacing.
    messages: Annotated[list[AnyMessage], add_messages]

    # --- identity / tracing -------------------------------------------------
    session_id: str   # also the checkpointer thread_id — this is what makes resume work
    user_id: str
    trace_id: str     # one id correlating API -> graph -> node -> tool -> audit row

    # --- business context ---------------------------------------------------
    customer_id: str | None
    current_intent: str | None

    # --- orchestration ------------------------------------------------------
    plan: Annotated[list[PlanTask], merge_tasks]
    active_workflow: WorkflowName | None
    # Per-workflow scratch space. Keyed by workflow name so a user can leave
    # onboarding, handle a claim, and come back with onboarding state intact —
    # the "move between workflows during a session" requirement.
    workflow_states: dict[str, dict[str, Any]]

    # --- accumulating channels ---------------------------------------------
    # operator.add is correct for these two: they are append-only logs where
    # every entry is distinct and nothing is ever revised.
    tool_results: Annotated[list[ToolResult], operator.add]
    errors: Annotated[list[ErrorRecord], operator.add]

    # --- human-in-the-loop --------------------------------------------------
    pending_question: PendingQuestion | None

    # --- output -------------------------------------------------------------
    final_response: str | None

    # --- compatibility ------------------------------------------------------
    # LangGraph applies the LATEST code to every thread, including threads
    # resuming from checkpoints written by an older build. During a rolling ECS
    # deploy both versions run at once. Stamp the version at thread start and
    # branch on it if semantics ever change. Corollary: add new fields as
    # optional, and rename via add-then-remove across two deploys — never in one.
    schema_version: int


CURRENT_SCHEMA_VERSION = 1


def new_state(session_id: str, user_id: str, trace_id: str) -> ControlTowerState:
    """Initial state for a fresh session. Every channel is explicitly seeded."""
    return ControlTowerState(
        messages=[],
        session_id=session_id,
        user_id=user_id,
        trace_id=trace_id,
        customer_id=None,
        current_intent=None,
        plan=[],
        active_workflow=None,
        workflow_states={},
        tool_results=[],
        errors=[],
        pending_question=None,
        final_response=None,
        schema_version=CURRENT_SCHEMA_VERSION,
    )


def ready_tasks(plan: list[PlanTask]) -> list[PlanTask]:
    """Tasks whose dependencies are all DONE — the supervisor's dispatch set.

    Pure function over the plan, deliberately: the routing decision is testable
    without a graph, a database, or an LLM. Most of the supervisor's behaviour
    can be covered by unit tests over this one function.
    """
    done = {t.id for t in plan if t.status is TaskStatus.DONE}
    return [
        t
        for t in plan
        if t.status in (TaskStatus.PENDING, TaskStatus.READY, TaskStatus.BLOCKED)
        and all(dep in done for dep in t.depends_on)
    ]


def is_plan_complete(plan: list[PlanTask]) -> bool:
    """True when no task can make further progress.

    Note this is *not* "all tasks DONE" — a plan with a FAILED task whose
    dependents are SKIPPED is complete too. Composing the final response must
    handle partial success, because partial success is the normal case in
    operations work.
    """
    return bool(plan) and all(t.status in TERMINAL_STATUSES for t in plan)
