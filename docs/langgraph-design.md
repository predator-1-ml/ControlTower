# LangGraph Design

How the orchestration works: the graph topology, what decides what, and where the
LLM is and is not used.

## Topology

```
START ─▶ planner ─▶ supervisor ─┬─▶ onboarding ─┐
                                ├─▶ claims     ─┼─▶ supervisor  (loop)
                                ├─▶ knowledge  ─┘
                                └─▶ compose ─▶ END
```

Four node types, with sharply different jobs:

| Node | Decides | Uses an LLM? |
|---|---|---|
| **planner** | what work is needed | yes — this is the ambiguous part |
| **supervisor** | what runs next | **no** |
| **workflow subgraphs** | how a capability executes | only where language is the task |
| **compose** | how to report it | yes |

## The supervisor has no LLM, deliberately

Which tasks are ready, what happens when one fails, and when the plan is finished
are all **derivable from the plan**. Making that a model call would buy latency,
cost, and nondeterminism in exchange for nothing.

It also makes the routing logic a pure function over state, which is why
`ready_tasks()` is unit-testable without a database, an API key, or a graph:

```python
def ready_tasks(plan: list[PlanTask]) -> list[PlanTask]:
    done = {t.id for t in plan if t.status is TaskStatus.DONE}
    return [t for t in plan
            if t.status in (PENDING, READY, BLOCKED)
            and all(dep in done for dep in t.depends_on)]
```

The intelligence is in the planner. The supervisor is control flow.

## Planning before execution

The planner emits a **task DAG**, not a sequence:

```json
{
  "goal": "Onboard CUST-1002 and check for active claims",
  "tasks": [
    {"id": "t1", "workflow": "onboarding", "action": "onboard_customer",
     "args": {"customer_ref": "CUST-1002"}, "depends_on": []},
    {"id": "t2", "workflow": "claims", "action": "retrieve_claims",
     "args": {"customer_ref": "CUST-1002"}, "depends_on": ["t1"]}
  ]
}
```

That plan is streamed to the UI as a `plan` event **the moment the planner
returns and before any workflow runs** — which is what makes "planning before
execution" visible in the product rather than merely true in the code.

`with_structured_output(Plan, include_raw=True)` is used so a malformed plan
becomes a handled error rather than an exception: the planner runs on every turn,
and one bad parse must not kill a session that already has work in flight.

## Moving between workflows mid-session

**The planner extends the plan; it never replaces it.** That single choice is the
entire mechanism:

1. Turn 1 — "onboard CUST-1002" → tasks `t1`
2. Turn 2 — "actually, summarise CLM-5003" → planner appends `t2`; `t1` is
   untouched
3. Turn 3 — "back to the onboarding" → the supervisor picks up `t1` again,
   because it was never discarded

There is **no workflow-switch branch anywhere in the code**. Context survives
because `workflow_states` is keyed per workflow, so each keeps its own scratch
space.

One necessary detail: the model emits local ids (`"1"`, `"2"`) every turn, which
would collide across turns. New tasks are renumbered to `t1, t2, …` on merge and
`depends_on` is remapped with them. Without it, turn two's "task 1" silently
overwrites turn one's.

## Subgraphs

Each workflow is a compiled `StateGraph` added **directly as a node**. They share
`ControlTowerState`'s channel names, so no wrapper is needed to translate inputs.

No checkpointer is passed to them. A subgraph added as a node **inherits the
parent's**, which is what lets an `interrupt()` raised three levels deep
propagate to the top-level graph. Compiling one with `checkpointer=False` would
silently disable interrupts inside that workflow — the failure would only show up
when a human was expected to answer a question.

## Where the LLM is, and is not

The line is drawn the same way in all three workflows:

> **Business rules stay in code. Language work goes to the model.**

| Workflow | Code decides | Model does |
|---|---|---|
| Onboarding | identity, eligibility, approval — everything | nothing at all |
| Claims | whether a claim is complete | writes the summary |
| Knowledge | which excerpts are relevant | answers from them |

Onboarding uses no LLM whatsoever. Every decision it makes has a legal or
financial consequence and belongs where it can be read, tested, and pointed at
during an audit.

A test asserts the claims model is **never called** on the incomplete branch: a
fluent summary of an incomplete claim is exactly the confident-but-wrong output
this split exists to prevent.

## Human-in-the-loop

`interrupt()`, not `interrupt_before` — the latter is positioned as a debugging
breakpoint, not an HITL mechanism.

⚠️ **Resuming re-runs the node from the top**, not from the `interrupt()` line.
Anything above it executes twice. The rule followed everywhere here: `interrupt()`
is the **first statement** in its node, and nodes that pause perform no writes.

`create_application` — the only write in the system — lives in its own node for
exactly this reason. An interrupt sharing that node would create two applications
for one customer.

## Error handling

| Situation | Behaviour |
|---|---|
| Task fails | `FAILED`, error recorded on the task |
| Its dependents | `SKIPPED`, not `FAILED` — they never ran |
| Workflow returns without claiming its task | retried up to 3 times, then failed |
| Plan unparseable | error recorded, session continues |
| No task runnable | compose what exists |

`SKIPPED` vs `FAILED` is what lets the final response say *"I could not do X, so I
did not attempt Y"* rather than reporting two unrelated failures. It is also why
`is_plan_complete()` treats a plan containing skipped tasks as finished — partial
success is the normal case in operations work, not an edge case.

## ⚠️ The recursion ceiling

A supervisor loop costs **2 super-steps per task** (supervisor → workflow →
supervisor). LangGraph's default `recursion_limit` is **25**, so the graph dies at
roughly 11 tasks with `GraphRecursionError`.

Verified directly: a 12-task plan raises at the default and completes at 100.
`RECURSION_LIMIT = 100` is passed in the invoke config, and a 12-task test pins
it.

This is a silent cliff — a 3-task demo never reaches it, a realistic plan does.

## Versions

LangGraph 1.x differs substantially from 0.x, and most published examples are
stale:

| Stale | Current |
|---|---|
| `config_schema` | `context_schema` |
| `checkpoint_during` | `durability` |
| `add_conditional_edges(..., then=)` | removed |
| `create_react_agent` | `create_agent` |
| `.stream()` shape varies | `version="v2"` gives a uniform `StreamPart` |

Two pins are **security floors**, not preferences: `langgraph-checkpoint>=4.2.0`
(CVE-2026-48775) and `langgraph-checkpoint-postgres==3.1.2` (CVE-2026-71433). The
trap is that the Postgres checkpointer only requires `>=4.1.0`, which resolves to
a vulnerable version — so the floor is pinned explicitly.
