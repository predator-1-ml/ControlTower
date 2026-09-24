# AI Operations Control Tower

One conversational interface for insurance operations staff. It **plans** a
request into tasks, **coordinates** three business workflows (onboarding,
claims, policy knowledge), **keeps context** when the conversation moves between
them, and **resumes after the process running it dies**.

LangGraph · FastAPI · Next.js · PostgreSQL + pgvector · AWS ECS Fargate ·
Terraform · GitHub Actions

---

## At a glance

- **Planning is visible before anything runs.** The planner returns a task graph
  (DAG), which is streamed to the UI before the first task starts.
- **Routing is code, not a model.** The supervisor that dispatches tasks has no
  LLM. It is a pure function over the plan, and it is unit-tested.
- **State lives in Postgres, not in the process.** Every step is checkpointed.
  Tested by killing the process locally and the ECS task in AWS.
- **The backend has no public ingress.** Every call from the frontend to the
  backend goes through a private DNS name inside the VPC (the Additional
  Challenge).
- **Deployed and verified end to end** on AWS on 2026-09-23. Live traffic found
  five faults that no local run could have found. They are listed in
  [`deployment-walkthrough.md`](docs/deployment-walkthrough.md).

**If you read one file of code, read [`backend/app/graph/state.py`](backend/app/graph/state.py).**
The rest of the backend is built around the shape defined there.

---

## Requirements → where they are met

| Requirement | How it is met | Read |
|---|---|---|
| One interface, many workflows | Planner → supervisor → three workflow subgraphs → compose | [`langgraph-design.md`](docs/langgraph-design.md) |
| Context across workflow switches | The plan is extended, never replaced. Each workflow keeps its own state. The customer in focus is shared. | [`demo.md`](docs/demo.md) beat 2 |
| Visible task progress | `plan` and task-status SSE events drive a live timeline | [`frontend/DESIGN.md`](frontend/DESIGN.md) |
| Resume after restarts | Postgres checkpointer. `interrupt()` for human-in-the-loop. | [`state-management.md`](docs/state-management.md) |
| AWS infrastructure as code | Terraform: 8 modules + a `dev` environment | [`terraform.md`](docs/terraform.md), [`ecs.md`](docs/ecs.md) |
| CI/CD | 5 GitHub Actions workflows. OIDC, no long-lived AWS keys. | [`ci-cd.md`](docs/ci-cd.md) |
| **Additional Challenge:** private frontend → backend communication over a domain name | Next.js BFF proxy → Route 53 private zone → internal ALB | [`networking.md`](docs/networking.md) |

---

## Architecture

```
Browser ─HTTP─▶ Public ALB ──▶ Next.js (ECS Fargate)      ← the only public target
                                     │
                                     │  /bff/* proxy over private DNS
                                     ▼
                   http://api.control-tower.internal:8000  (Route 53 private zone)
                                     │
                                Internal ALB
                                     ▼
                            FastAPI (ECS Fargate)          ← no public ingress
                                     │
                 planner ─▶ supervisor ─▶ onboarding / claims / knowledge ─▶ compose
                                     │
                                     ▼
                        RDS PostgreSQL + pgvector
              (domain tables · LangGraph checkpoints · embeddings)
```

### How one turn runs

1. **Planner (LLM)** turns the message into a task graph, e.g. `t2 claims` waits
   for `t1 onboarding`. The plan is streamed to the UI straight away.
2. **Supervisor (no LLM)** picks the tasks whose dependencies are done and
   dispatches them. When nothing is left, it hands off to compose.
3. **Workflow subgraphs** do the work. A subgraph can pause on `interrupt()` to
   ask the operator for missing information.
4. **Compose (LLM)** writes one answer for the turn. Citations are built from the
   retrieved chunks, never parsed from the model's output.

LangGraph checkpoints every step to Postgres. Because of this, a paused workflow
can be resumed by a different process, on a different ECS task.

### Where the LLM is used, and where it is not

| Component | LLM? | Why |
|---|---|---|
| Planner | Yes | Reading intent from free text is the ambiguous part |
| Supervisor | **No** | Readiness, failure handling and completion can all be derived from the plan |
| Onboarding | **No** | Eligibility is a business rule. It belongs in code. |
| Claims | Only to read free-text replies | Code checks that every extracted value actually appears in what the operator typed |
| Knowledge | Yes (RAG) | Answering from policy documents is a language task |
| Compose | Yes | Writing the report |

---

## Key design decisions

| Decision | Why | Rejected alternative |
|---|---|---|
| Next.js BFF proxy for every API call | Every frontend → backend hop crosses private DNS. No API URL is built into the bundle, so one image works in every environment. | ALB `/api/*` routing: the backend would become public |
| Internal ALB + Route 53 private zone | The DNS name stays the same when tasks are replaced, which crash recovery depends on | Service Connect (its 15s request timeout kills SSE), Cloud Map (resolves to task IPs that get cached) |
| Hand-built planner/supervisor graph | The assignment asks to *demonstrate* routing and coordination | `langgraph-supervisor` and `create_agent`, which hide exactly that |
| Postgres for everything | One datastore to run and explain: domain data, checkpoints, vectors | A separate vector DB, a queue, a graph DB |
| One psycopg pool, plain SQL | The checkpointer needs psycopg anyway. A second pool would have no good reason to exist. | SQLAlchemy + Alembic |
| Bedrock Nova Pro in production | Every Anthropic model on this account is blocked by an account-level use-case form. Switching back is one config value. | Waiting on the form |

Each decision's cost is in [`tradeoffs.md`](docs/tradeoffs.md).

---

## Evidence

| Claim | How it was checked |
|---|---|
| Code quality | 119 backend tests (91 run without a database) · `ruff` clean · both Docker images build |
| Durable execution | `make verify-resume` pauses a graph, **exits the process**, and completes the run from a brand-new process |
| Works on AWS | The full demo was run in a browser against the public URL, including `aws ecs stop-task` on the only backend task while a workflow was paused. The replacement task resumed it from Postgres. |
| Private networking | Every turn went browser → public ALB → Next.js → `api.control-tower.internal` → internal ALB → FastAPI. The backend was never reachable from outside the VPC. |

`make verify-resume` ends with:

```
RESUMED AND COMPLETED: steps=['stage_one', 'ask_human', 'stage_two']
stage_one ran in a process that no longer exists.
```

---

## The demo in three beats

Full script with talking points: [`docs/demo.md`](docs/demo.md).

| Beat | Say | What it shows |
|---|---|---|
| 1. Planning | *"Onboard CUST-1001 and check whether they already have an active claim."* | A two-task plan with a dependency appears before any task runs |
| 2. Context | *"Do they have any other open claims?"*, then a policy question | "They" is resolved from shared state. The plan extends across three workflows. |
| 3. Crash | *"Onboard CUST-1002"* → kill the backend → restart → answer | The paused workflow resumes where it stopped |

---

## Run it locally

**Prerequisites:** Docker, Python 3.12 with [`uv`](https://docs.astral.sh/uv/),
Node 22. AWS credentials are optional (see below).

```bash
cp .env.example backend/.env   # set ANTHROPIC_API_KEY, or LLM_PROVIDER=gemini / bedrock
make setup                     # venv + backend deps
make up                        # Postgres, backend :8000, frontend :3000 (Docker)
make migrate                   # schema + checkpointer tables, under an advisory lock
make seed                      # demo fixtures
make ingest                    # embed the policy corpus (needs AWS credentials)
```

Open <http://localhost:3000> and sign in as `operator` / `tower-local-only`.

- **LLM:** the Anthropic API or Gemini locally. Bedrock is used on AWS.
- **Embeddings always use Bedrock Cohere, even locally.** This is deliberate:
  local retrieval then runs in the same vector space as production. Without
  `make ingest`, the knowledge workflow answers that no document covers the
  question.
- **No credentials are needed for the tests or for `make verify-resume`.**

```bash
make test            # unit tests, no database
make test-db         # + integration tests (needs up, migrate, seed)
make lint
make verify-resume   # durable execution across process death
```

> The Makefile uses the Windows venv path (`.venv/Scripts/python.exe`). On macOS
> or Linux, replace it with `.venv/bin/python`.

---

## Deploy to AWS

```bash
export AWS_PROFILE=<profile>
cd terraform/bootstrap        && terraform init && terraform apply   # state bucket, once
cd ../environments/dev        && terraform init && terraform apply   # ~7 min, RDS is the slowest part
```

Then set the GitHub repository variables from `terraform output` and push to
`main`. From there, CI builds, migrates and deploys. The exact steps, including
the manual ones Terraform cannot do, are in
[`deployment-walkthrough.md`](docs/deployment-walkthrough.md).

The stack costs about **$4/day** in `ap-southeast-1`. The intended lifecycle is
deploy → demo → `terraform destroy`.

---

## Known limitations

These are stated on purpose rather than hidden. Each one has a written reason.

- **While a workflow is paused, the next message is treated as the answer.** You
  cannot park a question and come back to it later.
- **The session concurrency guard only works inside one process.** It is correct
  for a single backend task, but not for a scaled-out service. The fix (a
  Postgres advisory lock) is item 1 in
  [`future-improvements.md`](docs/future-improvements.md).
- **This is a dev-tier environment:** HTTP only (there is no domain for a
  certificate), one NAT gateway, single-AZ RDS.
- **Long runs share the request path.** A queue and a worker would move them off
  it. See [`future-improvements.md`](docs/future-improvements.md) item 2.

See [`assumptions.md`](docs/assumptions.md) for what was assumed and what was
deliberately not built.

---

## Repository layout

```
backend/
  app/graph/        state, planner, supervisor, compose, workflow subgraphs
  app/llm/          provider abstraction: Anthropic / Gemini locally, Bedrock on AWS
  app/db/           connection pool, checkpointer wiring, repositories
  app/api/          FastAPI routes, SSE streaming
  app/scripts/      migrate, seed, ingest, verify_resume (one-off entrypoints)
  migrations/       numbered .sql files, append-only
  tests/            unit + integration (CONTROL_TOWER_DB_TESTS=1)
frontend/
  app/bff/          the BFF proxy: the only path to the backend
  proxy.ts          operator sign-in guard (signed httpOnly cookie)
terraform/
  bootstrap/        remote state bucket
  modules/          networking, alb, ecs-cluster, ecs-service, rds, ecr, github-oidc, budget
  environments/dev/
.github/workflows/  backend CI, frontend CI, terraform plan/apply, two deploys
docs/               design documents (below)
```

---

## Documentation

Suggested reading order for a reviewer:

1. [`networking.md`](docs/networking.md): the Additional Challenge, and the one
   unusual architectural decision
2. [`langgraph-design.md`](docs/langgraph-design.md): graph topology, and where
   the LLM is and is not used
3. [`state-management.md`](docs/state-management.md): reducers, checkpointing,
   resume
4. [`deployment-walkthrough.md`](docs/deployment-walkthrough.md): the first
   deploy, and the five faults only live traffic found
5. [`tradeoffs.md`](docs/tradeoffs.md): every decision, with its cost

| Also | Covers |
|---|---|
| [`architecture.md`](docs/architecture.md) | Solution and AWS architecture |
| [`ecs.md`](docs/ecs.md) | Task definitions, IAM roles, shutdown timing |
| [`terraform.md`](docs/terraform.md) | Modules, state, promotion path |
| [`ci-cd.md`](docs/ci-cd.md) | Pipelines, OIDC, where Terraform ends and CI begins |
| [`assumptions.md`](docs/assumptions.md) | What was assumed, and what was not built |
| [`future-improvements.md`](docs/future-improvements.md) | What I would do next, in order |
| [`demo.md`](docs/demo.md) | The demo runbook |
| [`plans/`](docs/plans/) | Original design plans and ADRs |
