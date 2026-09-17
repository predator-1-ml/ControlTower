# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Prime directive: every line must be interview-defensible

This is a take-home assignment. The brief states that a candidate who cannot
explain any code, infrastructure, workflow, or design decision **may be
disqualified**. That makes explainability the binding constraint on everything
here — not test coverage, not feature count, not elegance.

Two rules follow, and they override normal instincts:

**1. Think twice, then write once.** Before creating or editing a file, work out
what it is for, what breaks without it, and what the alternative was. If that
can't be answered in a sentence, the code isn't ready to write. Do not generate
a file to see whether it works out — prototype in a scratch probe, then write
the real thing deliberately.

**2. Volume is a liability.** Every file, dependency, helper, and abstraction is
something that has to be defended out loud. Prefer the smaller version. Delete
speculative surface on sight: unused helpers, endpoints nothing calls, config
knobs with one value, convenience layers with one caller.

### What "explainable" means in practice

- **Comment the *why*, never the *what*.** `# increments the counter` is noise.
  `# prepare=False forces the simple query protocol; the extended protocol
  accepts one statement per execute` is the interview answer, pre-written.
- **Record rejected alternatives at the decision site.** When a choice had a
  plausible competitor (HNSW vs IVFFlat, one pool vs two, BFF proxy vs ALB
  path-routing), say what lost and why in a comment or an ADR. "Why not X?" is
  the question that gets asked.
- **Name the failure mode a defence prevents.** A guard whose purpose isn't
  stated reads as cargo-culting. Say what goes wrong without it.
- **Pin surprising behaviour with a test.** If something was learned the hard
  way, a test is how it stays learned. Several tests here exist to document a
  footgun rather than to catch a regression, and say so in the docstring.

Minimal means *less code*, not *less explanation*. Prose is cheap and is the
deliverable; code is expensive and is the liability.

---

## Commands

```bash
make setup     # create venv (Python 3.12), install backend deps
make up        # start Postgres + pgvector in Docker
make migrate   # apply SQL migrations + checkpointer setup (advisory-locked)
make seed      # load demo fixtures
make test      # unit tests, no database needed
make test-db   # + integration tests against Postgres
make lint      # ruff
make verify-resume   # prove durable execution across process death
```

Run a single test: `cd backend && .venv/Scripts/python.exe -m pytest tests/graph/test_state.py::TestMergeTasks -q`

`make verify-resume` is the one to run after touching anything in `graph/` or
`db/` — it is the only check that exercises real cross-process durability.

---

## Architecture

A conversational interface coordinates three business workflows, keeps context
across workflow switches, and resumes after a process dies.

```
Browser → public ALB → Next.js ─(private DNS)→ internal ALB → FastAPI
                          │                                      │
                    BFF proxy, /bff/*              planner → supervisor → subgraphs
                                                              │
                                                  Postgres: domain + checkpoints + vectors
```

**Read `backend/app/graph/state.py` first.** Everything else is a function of the
shape defined there. Then `docs/plans/2026-09-17-*-plan.md` for the ADRs.

### Load-bearing decisions

| Decision | Because | Rejected |
|---|---|---|
| Next.js BFF proxy for all API calls | Only design where *every* frontend→backend hop crosses private DNS; also removes the build-time API URL problem | ALB `/api/*` routing — makes the backend public, defeating the requirement |
| Internal ALB + Route 53 private zone | Stable DNS name survives task replacement, which the crash-recovery demo depends on | ECS Service Connect (15s `perRequestTimeout` kills SSE; isn't real DNS). Cloud Map (resolves to task IPs; undici caches them) |
| One psycopg pool, plain SQL | The checkpointer needs psycopg regardless; a second pool via SQLAlchemy is a question with no good answer | SQLAlchemy ORM + Alembic |
| Hand-built planner/supervisor graph | The assignment asks to *demonstrate* routing and coordination; a prebuilt agent hides exactly that | `langgraph-supervisor`, `create_agent` |
| Postgres for everything | One datastore to explain and operate | Separate vector DB, Kafka, Neo4j |

---

## Gotchas already paid for

Each of these cost real time. Do not rediscover them.

**`merge_tasks` has a contract: a node returns only the tasks it changed.**
Returning the whole plan makes untouched tasks stale writes, reverting a
concurrent update so the supervisor dispatches a completed task twice. Pinned by
two tests, one of which asserts the *broken* behaviour deliberately.

**`interrupt()` re-runs its node from the top on resume** — not from the
interrupt line. Any side effect before it runs twice. Put `interrupt()` first in
the node, or isolate the write into its own node.

**A missing checkpointer fails late, not early.** `interrupt()` without one
pauses and returns a well-formed interrupt; only the *resume* raises. "It paused
correctly" is not evidence checkpointing works — only a resume is.

**psycopg async cannot use Windows' `ProactorEventLoop`.** It surfaces as
`PoolTimeout`, which looks exactly like Postgres being down. `app/core/eventloop.py`
fixes it; it must be called before the loop is created, which is why
`tests/conftest.py` exists. No-op on Linux.

**`conn.execute()` takes one statement.** Multi-statement SQL files need
`prepare=False` (simple query protocol), or you get "cannot insert multiple
commands into a prepared statement".

**`AsyncPostgresSaver.setup()` takes no lock and isn't transactional.** Parallel
ECS tasks booting against a stale schema crash on `UniqueViolation` and can leave
an `INVALID` index. Migrations run as one advisory-locked process, never at app
startup.

**Custom types in checkpointed state must be registered for msgpack.**
`PlanTask` and `TaskStatus` are listed in `ALLOWED_MSGPACK_MODULES`. Unregistered
types warn today and will be *blocked* later — and blocking does not raise, it
returns the raw dict, so `task.status` fails far from the cause. Add any new
custom type that enters state to that list.

**That warning goes through `logging`, not `warnings`.** A test built on
`recwarn` silently guards nothing; use `caplog`. Verified by removing the fix and
watching the test still pass.

**`ChatBedrockConverse` has no native async.** `ainvoke`/`astream` bridge blocking
boto3 onto a thread pool, holding a thread for the whole call including a streamed
response. Raise `executor_max_workers` **and** `boto_max_pool_connections`
together, or the bottleneck just moves.

**Bedrock needs an inference-profile ID, and the prefix is regional.** The bare
model ID is rejected for on-demand throughput. Verified in `ap-southeast-1`:
`apac.` profiles exist only for older models; every current model is
`global.`-prefixed (`global.anthropic.claude-opus-4-8`). `us.` does not resolve
there. Never assume the prefix — run
`aws bedrock list-inference-profiles --region <region>`.

**AWS account: `187880375508`, region `ap-southeast-1`, profile `Nyomad`.**
Terraform needs `AWS_PROFILE=Nyomad` in the environment. Deliberately NOT
hardcoded as `profile =` in any `.tf`, because GitHub Actions authenticates via
OIDC and has no profile.

**Nothing loop-bound may be built at import time.** `AsyncPostgresSaver.__init__`
captures the running loop, so the graph is compiled in the FastAPI lifespan.
There is no importable module-level `graph`; handlers read `request.app.state.graph`.

---

## Conventions

- **Python 3.12**, not 3.13 — some LangGraph/psycopg extras still lag on wheels.
- **Migrations are numbered `.sql` files** in `backend/migrations/`, applied in
  filename order and tracked in `schema_migrations`. Append only; never edit an
  applied file.
- **Repository functions return plain dicts.** Anything a tool returns lands in
  checkpointed state, so it must survive JSON — `_serialise` coerces `date` and
  `Decimal`, which round-trip through psycopg but not through JSON.
- **Seed fixtures are load-bearing.** Each of the four customers drives a
  different workflow branch; integration tests assert against them. Changing the
  seed can silently stop exercising a branch.
- **Source documents (`*.pdf`, `*.docx`) are gitignored.** The assignment PDF is
  marked "Classified as C2 - General Business" and this repo is public.
