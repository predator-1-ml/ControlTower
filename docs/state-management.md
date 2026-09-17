# State Management

Three ideas carry the whole design. Everything else follows from them.

## 1. Graph state is execution context, not the business record

Domain truth lives in `customers`, `claims`, `applications`. Message history is
useful conversational context but is **not** the source of truth for workflow
progress — `plan` is.

That distinction is what separates an orchestrator from a chatbot with database
access. If you can answer "what has happened to this request?" only by re-reading
the conversation, you have a transcript, not a workflow engine.

## 2. Reducers decide how concurrent writes merge

A channel with no reducer is last-write-wins. Choosing the right reducer per
channel is most of the correctness of a multi-agent graph.

| Channel | Reducer | Why |
|---|---|---|
| `messages` | `add_messages` | appends, dedupes by id |
| `plan` | `merge_tasks` | **custom — see below** |
| `tool_results`, `errors` | `operator.add` | append-only logs; nothing is ever revised |
| everything else | none | last write wins, which is correct for scalars |

### `merge_tasks`, and the contract it depends on

> **A node must return only the tasks it changed.**

```python
return {"plan": task_delta(task, status=TaskStatus.DONE)}   # correct
return {"plan": whole_plan_with_one_edit}                   # wrong
```

The reducer is `(accumulated, node_update) -> accumulated`. If a node returns the
*entire* plan, every task it did not touch is a stale write — and "right wins per
id" faithfully applies that staleness. A task another node just advanced to
`DONE` gets stamped back to its older status, and the supervisor dispatches it a
second time.

Why not the two obvious alternatives:

- **No reducer.** Two subgraphs finishing in the same super-step each replace the
  list wholesale; one silently overwrites the other.
- **`operator.add`.** Concatenates, so the list grows without bound and holds
  several copies of one id in conflicting states. Whichever is read first wins.

`task_delta()` exists to make the contract hard to violate, and two tests pin
it — one of which deliberately asserts the *broken* behaviour so nobody changes
the convention without noticing.

This was found by a test failing during scaffolding, which is the only reason the
contract is written down at all.

## 3. State is checkpointed, so it must survive the process

Everything in `ControlTowerState` is serialised to Postgres after every step and
may be rehydrated by a **different ECS task running a newer build**.

```python
class ControlTowerState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    session_id: str          # also the checkpointer thread_id
    user_id: str
    trace_id: str            # correlates API → graph → node → tool → audit row
    customer_id: str | None
    current_intent: str | None
    plan: Annotated[list[PlanTask], merge_tasks]
    active_workflow: WorkflowName | None
    workflow_states: dict[str, dict]     # keyed per workflow
    tool_results: Annotated[list[ToolResult], operator.add]
    errors: Annotated[list[ErrorRecord], operator.add]
    pending_question: PendingQuestion | None
    final_response: str | None
    schema_version: int
```

`workflow_states` being keyed per workflow is what lets a user leave onboarding,
handle a claim, and return with onboarding state intact.

TypedDict rather than Pydantic for the state itself — the docs note Pydantic is
less performant and the prebuilt agent factory rejects it. `PlanTask` *is*
Pydantic, because it is LLM structured output and should be validated.

## Checkpointing

`AsyncPostgresSaver` over a connection pool, built inside the FastAPI lifespan.
Its `__init__` captures the running event loop, so constructing it at module
import raises `RuntimeError: no running event loop`. Consequence: **there is no
importable module-level graph**; handlers read `request.app.state.graph`.

Three connection kwargs are mandatory and none are guessable:

| kwarg | Without it |
|---|---|
| `autocommit=True` | Migrations use `CREATE INDEX CONCURRENTLY`, forbidden inside a transaction — `.setup()` fails or silently does not commit |
| `row_factory=dict_row` | The saver indexes rows by name; the default gives `TypeError: tuple indices must be integers` |
| `prepare_threshold=0` | Behind PgBouncer or RDS Proxy: `prepared statement "_pg3_0" does not exist` once connections are re-multiplexed |

The third is insurance today — we connect straight to RDS — but adding a proxy
later would otherwise break production in a way that looks nothing like its cause.

### Custom types must be registered for msgpack

`PlanTask` and `TaskStatus` are listed in `ALLOWED_MSGPACK_MODULES`. Unregistered
types warn today and will be **blocked** later — and blocking **does not raise**.
It logs and returns the raw dict, so a node expecting a `PlanTask` gets a plain
dict and fails with an `AttributeError` far from the cause, or a `.get()` quietly
takes the wrong branch.

An integration test asserts a `PlanTask` survives a real Postgres round trip *as a
PlanTask*. The first version of that test guarded nothing — it passed with the fix
removed, because the warning is emitted through `logging`, not `warnings`, so
`recwarn` never saw it. It uses `caplog` now, verified by deleting the fix and
confirming the test fails.

## Migrations: one process, one lock

`AsyncPostgresSaver.setup()` holds **no advisory lock**, does no `SELECT … FOR
UPDATE`, and cannot be wrapped in a transaction (its own migrations use `CREATE
INDEX CONCURRENTLY`). It reads `MAX(v)` and applies the tail.

So if N ECS tasks boot simultaneously against a stale schema:

- all read version *k*, all apply *k+1…n*, all insert the same rows — losers hit a
  `UniqueViolation` and **crash at boot**
- concurrent `CREATE INDEX CONCURRENTLY` on one table can abort, leaving an
  `INVALID` index that queries quietly stop using

That window opens on the first boot after a checkpointer upgrade — exactly the
rolling-deploy moment. Running `setup()` in the app lifespan works right up until
the deploy where it doesn't.

Migrations therefore run as a **standalone ECS task before the service update**,
wrapped in `pg_advisory_lock`. Detect a bad index with:

```sql
SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;
```

## Resume, and why it is the point

The architecture's central claim is that a workflow paused mid-run can be resumed
by a **different process**. `make verify-resume` proves it rather than asserting
it:

```
$ make verify-resume
PAUSED at interrupt, thread='mk-ajeya', steps=['stage_one']
State is now only in Postgres. This process is exiting.

RESUMED AND COMPLETED: steps=['stage_one', 'ask_human', 'stage_two']
stage_one ran in a process that no longer exists.
```

This is what makes ECS tasks disposable, and it is the third beat of the demo.

⚠️ **A missing checkpointer fails late, not early.** `interrupt()` without one
pauses and returns a well-formed interrupt; only the *resume* raises. In
production that means a user answers a question and *then* it breaks, with nothing
persisted. "It paused correctly" is not evidence checkpointing works — only a
resume is.

## Schema versioning across deploys

LangGraph applies the **latest code to every thread**, including threads resuming
from checkpoints written by an older build. During a rolling deploy both versions
run at once.

`schema_version` is stamped at thread start. The rules that follow:

- add new fields as optional
- rename via add-then-remove across **two** deploys, never one
- renaming or removing a *node* breaks threads currently paused at it, because
  `StateSnapshot.next` holds the node name
- edge and topology changes are safe — routing is not persisted

## Known limitation

Two concurrent turns on one `session_id` race the checkpointer. The API rejects a
second turn while one is in flight, using an **in-process** guard — which holds
for a single backend task and not across a scaled-out service.

A correct implementation would take a Postgres advisory lock keyed on the session.
It is documented here rather than half-solved, because a guard that looks
distributed and isn't is worse than one that is honestly local.
