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

1. Turn 1 — "onboard CUST-1001 and check their claims" → tasks `t1`, `t2`
2. Turn 2 — "do they have any other open claims?" → planner appends `t3`; `t1`
   and `t2` are untouched, and the claims workflow finds the customer in
   `customer_id`, which onboarding published a turn earlier
3. Turn 3 — "when does a motor claim need a second review?" → `t4`, a knowledge
   task; the onboarding and claims slices of `workflow_states` are still intact

There is **no workflow-switch branch anywhere in the code**. Three channels carry
the context between turns: `plan`, `workflow_states` (keyed per workflow, so each
keeps its own scratch space) and `customer_id`.

Two details make that true rather than merely intended:

- **`/chat` seeds state only on a brand-new thread.** Graph input is a write like
  any other, and `workflow_states` / `customer_id` have no reducer, so seeding them
  on every turn erased exactly the context this section describes. Pinned by
  `test_second_turn_keeps_the_first_turns_context`.
- **A workflow's first node replaces its own slice.** Because the slice now
  outlives the turn, a second onboarding would otherwise inherit the first one's
  `application` and compose would report both.

**What it does not do:** while a workflow is paused on `interrupt()`, the next
message is delivered as `Command(resume=...)` — it is the answer, not a new
request. A user cannot park a question, switch workflow, and return to it. See
`tradeoffs.md`.

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
| Claims | whether a claim is complete, whether a supplied value is real, what the next step is | reads the operator's free-text reply, writes the summary |
| Knowledge | which excerpts are relevant | answers from them |

Onboarding uses no LLM whatsoever. Every decision it makes has a legal or
financial consequence and belongs where it can be read, tested, and pointed at
during an audit.

Claims has two model calls, each placed where the ambiguity genuinely is. The
first, `read_reply`, turns "Incident report IR-2291, police ref PR-77431" into
name/value pairs — and **code then verifies each one**: a pair is kept only if its
name was asked for *and* its value occurs in the operator's own words. That stops
invention; it does not validate a reference format, and the docstring says so,
because a guard that reads as stronger than it is gets trusted for something it
never did. The second, `summarise`, writes prose from facts that are already
decided — `next_actions` chose the next step, the model only phrases it.

A test asserts the claims model is **never called** on the incomplete branch: a
fluent summary of an incomplete claim is exactly the confident-but-wrong output
this split exists to prevent.

The final answer follows the same line. `compose` is handed operator-facing fact
lines built in code, never workflow state — it cannot narrate a UUID or a task
name it was never shown. And text a workflow wrote itself (knowledge's cited
answer, claims' summary) is passed through **verbatim**; when it was the turn's
only task, `compose` makes zero model calls, so a citation cannot be damaged by a
second pass over it.

## Human-in-the-loop

`interrupt()`, not `interrupt_before` — the latter is positioned as a debugging
breakpoint, not an HITL mechanism.

⚠️ **Resuming re-runs the node from the top**, not from the `interrupt()` line.
Anything above it executes twice. The rule followed everywhere here: `interrupt()`
is the **first statement** in its node, and nodes that pause perform no writes.

`create_application` — onboarding's only write — lives in its own node for exactly
this reason. An interrupt sharing that node would create two applications for one
customer.

The claims pause splits three ways for three different reasons:

| Node | Why it is its own node |
|---|---|
| `request_information` | holds `interrupt()` as its first statement and does nothing else. It asks about **one** claim (`incomplete[0]`): a value supplied against a union of several claims' missing fields has no owning claim to be recorded on |
| `read_reply` | so the operator's words are **checkpointed before** the fallible model call. A process death here resumes with the reply intact and re-runs only the extraction. Every failure is caught — uncaught, the interrupt is already consumed, the task is still `running`, and the operator's next message gets planned as a new request with their answer gone |
| `record_information` | the write. One statement, idempotent (the node can re-run if the process dies before the checkpoint), and it returns the post-write row so state and database cannot disagree |

Re-asking is bounded by **progress, not a counter**: the workflow asks again only
if this reply supplied at least one new field and something is still outstanding.
`missing_fields` is finite and strictly shrinks, so the loop terminates. A reply
that supplies nothing ends the pause and the claim truthfully stays
`awaiting_information`.

`summarise` is the **single owner** of `done` and `outcome`, on every path. Two
owners means a path that misses one leaves the task `running`, and the
supervisor's guard retries it three times before failing it.

## Error handling

| Situation | Behaviour |
|---|---|
| A node raises (Bedrock throttled, database down) | the turn ends with an SSE `error` event. That step's writes are never checkpointed, so the task is still `running` in Postgres. On the next turn the supervisor finds a `running` task nobody claimed, counts an attempt and re-dispatches it — the row below |
| Workflow returns without claiming its task | retried up to 3 times, then `FAILED` with the error recorded on the task |
| A `FAILED` task's dependents | `SKIPPED`, not `FAILED` — they never ran |
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
