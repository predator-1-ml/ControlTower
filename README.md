# AI Operations Control Tower

A stateful AI operations orchestration platform. One conversational interface
coordinates multiple business workflows, preserves context across workflow
switches, exposes task progress, and **resumes reliably across service restarts**.

Built with LangGraph (orchestration), FastAPI (backend), Next.js (frontend), and
Terraform-provisioned AWS infrastructure on ECS Fargate.

## What is built

| | |
|---|---|
| **Orchestration** | Planner → supervisor → three workflow subgraphs → compose |
| **Workflows** | Onboarding (deterministic), Claims (tool-driven), Knowledge (RAG) |
| **Durability** | Postgres checkpointing; workflows resume after the process dies |
| **API** | SSE streaming, session reconnect |
| **Frontend** | Chat, live plan DAG, activity log |
| **Infrastructure** | Terraform: VPC, 2 ALBs, ECS Fargate, RDS+pgvector, Route 53 private zone |
| **CI/CD** | 5 GitHub Actions workflows, OIDC, no long-lived keys |

**54 tests · ruff clean · typecheck clean · `terraform validate` clean across 8 stacks ·
both Docker images build and run**

Verified rather than asserted: durable execution across real process death, and
the full container-to-container path over the private DNS name the Route 53 zone
serves.

Not yet deployed to AWS — see [Deploying](#deploying).

---

## Quick start

```bash
make setup      # create the venv, install backend deps
make up         # start Postgres (pgvector) in Docker
make migrate    # domain schema + checkpointer tables (advisory-locked)
make seed       # demo fixtures
make test       # unit tests (no database needed)
make test-db    # + integration tests against Postgres
```

Then run it:

```bash
make dev                      # backend on :8000
cd frontend && npm run dev    # frontend on :3000
```

Needs an LLM credential: either `ANTHROPIC_API_KEY` in `backend/.env`, or
`LLM_PROVIDER=bedrock` with a current `aws login` session. The tests and
`make verify-resume` need neither.

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
model-access gating). Set `LLM_PROVIDER=bedrock` for the AWS path.

Two Bedrock details that cost time if unknown:

- **Model IDs need an inference-profile prefix, and it is regional.** In
  `ap-southeast-1` every current model is `global.`-prefixed
  (`global.anthropic.claude-sonnet-4-6`); `apac.` covers only legacy models and
  `us.` does not resolve at all. Check with
  `aws bedrock list-inference-profiles` — never guess.
- **Opus is not invocable on a fresh AWS account.** Bedrock returns *"not
  available for this account"* for Opus 4.8 and Sonnet 5. Production therefore
  uses Sonnet 4.6; local dev keeps Opus via the Anthropic API. This divergence is
  what the provider abstraction exists for.

Copy `.env.example` to `.env` and fill in `ANTHROPIC_API_KEY` to exercise the LLM
paths. The tests and `make verify-resume` need no API key.

---

## Deploying

```bash
export AWS_PROFILE=<your-profile>

cd terraform/bootstrap      && terraform init && terraform apply   # state bucket, once
cd ../environments/dev      && terraform init && terraform apply
terraform output                                                   # role ARNs, subnet ids
```

Set the five repository variables from those outputs (listed in
[`docs/ci-cd.md`](docs/ci-cd.md)), then push to `main` — the deploy workflows take
over from there.

Roughly **$4.40/day** in `ap-southeast-1`. Intended lifecycle is deploy → record
the demo → `terraform destroy`.

See [`docs/demo.md`](docs/demo.md) for the three-beat walkthrough.

---

## Documentation

| Document | Covers |
|---|---|
| [`architecture.md`](docs/architecture.md) | Solution architecture, AWS architecture |
| [`langgraph-design.md`](docs/langgraph-design.md) | Graph topology, orchestration, where the LLM is and is not |
| [`state-management.md`](docs/state-management.md) | Reducers, checkpointing, resume, schema versioning |
| [`networking.md`](docs/networking.md) | **Private communication, alternatives, tradeoffs** |
| [`ecs.md`](docs/ecs.md) | Task definitions, roles, shutdown timing |
| [`terraform.md`](docs/terraform.md) | Modules, state, promotion path |
| [`ci-cd.md`](docs/ci-cd.md) | Pipelines, OIDC, the Terraform/CI boundary |
| [`assumptions.md`](docs/assumptions.md) | What was assumed, and what was deliberately not built |
| [`tradeoffs.md`](docs/tradeoffs.md) | Every decision with its cost stated |
| [`future-improvements.md`](docs/future-improvements.md) | Ordered by what I would do first |
| [`demo.md`](docs/demo.md) | Three-beat walkthrough |
| [`plans/`](docs/plans/) | Original design plans and ADRs |

If you read one, read [`networking.md`](docs/networking.md) — it covers the
Additional Challenge and the reasoning behind the only architecturally unusual
decision in the system.
