# AI Operations Control Tower

A stateful AI operations orchestration platform. One conversational interface
coordinates multiple business workflows, preserves context across workflow
switches, exposes task progress, and **resumes reliably across service restarts**.

Built with LangGraph (orchestration), FastAPI (backend), Next.js (frontend), and
Terraform-provisioned AWS infrastructure on ECS Fargate.

> **Status: scaffolding.** Day 1 of a one-week build. Durable execution is
> implemented and verified; the planner, supervisor, and three workflow
> subgraphs are not yet built. See
> [`docs/plans/2026-09-17-feat-ai-operations-control-tower-plan.md`](docs/plans/2026-09-17-feat-ai-operations-control-tower-plan.md)
> for the full design and the day-by-day build order.

---

## Quick start

```bash
make setup      # create the venv, install backend deps
make up         # start Postgres (pgvector) in Docker
make migrate    # create checkpointer tables (advisory-locked, single process)
make test       # 20 tests
```

### Prove durable execution

The central claim of the architecture is that a workflow paused mid-run can be
resumed by a **different process**. Verify it rather than trusting it:

```bash
make verify-resume
```

This runs a graph until it hits a human-in-the-loop `interrupt()`, **exits the
process**, then starts a second process that recovers the checkpoint from
Postgres and completes the run. Expected output ends with:

```
PAUSED at interrupt, thread='verify-resume', steps=['stage_one']
State is now only in Postgres. This process is exiting.

RESUMED AND COMPLETED: steps=['stage_one', 'ask_human', 'stage_two']
stage_one ran in a process that no longer exists.
```

This is what makes ECS tasks disposable, and it is the third beat of the demo.

---

## Architecture at a glance

```
Browser ──TLS──▶ Public ALB ──▶ Next.js (ECS Fargate)   ← only public target
                                     │
                                     │  BFF proxy, private DNS
                                     ▼
                          http://api.internal:8000
                                     │
                                Internal ALB
                                     ▼
                            FastAPI (ECS Fargate)        ← zero public ingress
                                     │
                    ┌────────────────┼────────────────┐
                    ▼                ▼                ▼
                 Planner   ──▶  Supervisor  ──▶  Workflow subgraphs
                                     │           (onboarding / claims / knowledge)
                                     ▼
                      RDS PostgreSQL + pgvector
              (domain tables · LangGraph checkpoints · embeddings)
```

**The frontend never exposes the backend.** Client components call a relative
`/bff/*` route; a Next.js route handler forwards it over a private domain name
inside the VPC. This satisfies the assignment's "private communication, no direct
IP" requirement — and because the browser only ever sees a relative URL, there is
no public API origin baked into the bundle, so one image promotes across every
environment. See ADR-001 in the plan.

---

## Repository layout

| Path | Contents |
|---|---|
| `backend/app/graph/` | LangGraph state, planner, supervisor, workflow subgraphs |
| `backend/app/llm/` | Provider abstraction — Anthropic locally, Bedrock on ECS |
| `backend/app/db/` | Connection pool and checkpointer wiring |
| `backend/app/scripts/` | One-off migration and verification entrypoints |
| `frontend/app/bff/` | The BFF proxy (ADR-001) |
| `terraform/` | Modules + `environments/dev` |
| `docs/` | Architecture, networking, state management, tradeoffs |

**Start reading at `backend/app/graph/state.py`.** Everything else is a function
of the shape defined there.

---

## Local development notes

**Windows:** psycopg's async mode cannot run on the default `ProactorEventLoop`.
`app/core/eventloop.py` selects a compatible loop; it is called from every async
entrypoint and is a no-op on Linux. Without it the pool retries silently and
fails as `PoolTimeout`, which looks like Postgres is down when it isn't.

**Python 3.12, not 3.13** — some LangGraph/psycopg extras still lag on 3.13 wheels.

**LLM provider:** defaults to the Anthropic API locally (natively async, no AWS
model-access gating). Set `LLM_PROVIDER=bedrock` for the AWS path. Note the model
id differs by client — `ChatBedrockConverse` requires a geo inference-profile
prefix (`us.anthropic.claude-opus-4-8`); the bare id is rejected.

Copy `.env.example` to `.env` and fill in `ANTHROPIC_API_KEY` to exercise the LLM
paths. The tests and `make verify-resume` need no API key.

---

## Documentation

| Document | Covers |
|---|---|
| `docs/plans/2026-09-17-...-plan.md` | Full design, ADRs, build order, risks |
| `docs/architecture.md` | Solution architecture |
| `docs/langgraph-design.md` | Graph topology, planner/supervisor, subgraphs |
| `docs/state-management.md` | State schema, reducers, checkpointing, resume |
| `docs/networking.md` | Network topology, private communication, alternatives |
| `docs/terraform.md` · `docs/ecs.md` · `docs/ci-cd.md` | Infrastructure |
| `docs/assumptions.md` · `docs/tradeoffs.md` · `docs/future-improvements.md` | Decisions and their costs |
