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
  five faults that no local run could have found. See
  [Deployment on AWS](#deployment-on-aws).

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
| Works on AWS | Deployed on 2026-09-23 through the CI pipeline. The full demo was run against the public URL, including stopping the only backend task mid-workflow. See [Deployment on AWS](#deployment-on-aws). |
| Private networking | Every turn went browser → public ALB → Next.js → `api.control-tower.internal` → internal ALB → FastAPI. The backend was never reachable from outside the VPC. |

`make verify-resume` ends with:

```
RESUMED AND COMPLETED: steps=['stage_one', 'ask_human', 'stage_two']
stage_one ran in a process that no longer exists.
```

---

## Deployment on AWS

The stack was deployed to AWS on **2026-09-23**, in `ap-southeast-1`. The full
demo was run against the public URL. Everything below links to real pipeline
runs and pull requests. The long-form record is
[`deployment-walkthrough.md`](docs/deployment-walkthrough.md).

### What was deployed

`terraform apply` created **69 resources in about 7 minutes**. RDS was the
slowest part.

| Layer | Resources |
|---|---|
| Network | VPC `10.0.0.0/16`: public subnets, private app subnets, private DB subnets, one NAT gateway |
| Entry | Public ALB → Next.js. This is the only thing reachable from the internet. |
| Private hop | Route 53 private zone `control-tower.internal` → internal ALB on `:8000` → FastAPI |
| Compute | ECS Fargate cluster with two services: frontend and backend |
| Data | RDS PostgreSQL 17 (`db.t4g.micro`) with pgvector: domain tables, checkpoints and embeddings |
| Models | Bedrock `apac.amazon.nova-pro-v1:0` (chat) and `cohere.embed-english-v3` (embeddings), called with the task role. No keys are involved. |
| Delivery | ECR repositories, three GitHub OIDC roles (plan, apply, deploy), a $20/month budget alert |

### How code reaches production

```
PR ──▶ terraform plan (read-only role, no approval needed)
merge to main ──▶ approval gate on the `dev` environment ──▶ build image, tag = commit SHA
      ──▶ push to ECR ──▶ run migrations as a one-off ECS task (exit code checked)
      ──▶ register the task definition by image DIGEST ──▶ roll the ECS service
```

- **No AWS keys are stored in GitHub.** Jobs use OIDC to assume a role. The
  role trusts only jobs running in the `dev` environment, so a human approval
  is required before the credential can exist at all.
- **Deploys use the image digest, not the tag.** A tag can be moved, so
  rolling back to a tag might pull the bad image again. A digest cannot change.
- **Terraform and CI each own different things.** Terraform owns the
  infrastructure. CI owns only one thing: which image is running.

### Timeline (UTC, 2026-09-23)

| Time | Event | Run / PR |
|---|---|---|
| 02:13 | Bootstrap failed: the S3 bucket name was already taken by another AWS account. Fixed by adding the account ID to the name. The PR plan passed. | [plan](https://github.com/predator-1-ml/ControlTower/actions/runs/35809512438) |
| 02:30 | First merge to `main`. Both images built, migrations ran, both services rolled out. | [#1](https://github.com/predator-1-ml/ControlTower/pull/1) · [backend](https://github.com/predator-1-ml/ControlTower/actions/runs/35810624920) · [frontend](https://github.com/predator-1-ml/ControlTower/actions/runs/35810624877) |
| 02:38 | Fixed faults 1 and 2 below | [#2](https://github.com/predator-1-ml/ControlTower/pull/2) · [terraform](https://github.com/predator-1-ml/ControlTower/actions/runs/35811133122) |
| 02:40 | Fixed fault 3. This was the first `apply` that actually ran through CI. | [#3](https://github.com/predator-1-ml/ControlTower/pull/3) · [terraform](https://github.com/predator-1-ml/ControlTower/actions/runs/35811324193) |
| 03:05 | Fixed fault 4. The frontend became healthy. | [#4](https://github.com/predator-1-ml/ControlTower/pull/4) · [terraform](https://github.com/predator-1-ml/ControlTower/actions/runs/35812980097) · [frontend](https://github.com/predator-1-ml/ControlTower/actions/runs/35813067020) |
| 04:30 | The seed script now ships in the backend image, so the private RDS could be seeded | [#5](https://github.com/predator-1-ml/ControlTower/pull/5) · [backend](https://github.com/predator-1-ml/ControlTower/actions/runs/35818592300) |
| 04:41 | Fixed fault 5. The end-to-end browser run was done after this. | [#6](https://github.com/predator-1-ml/ControlTower/pull/6) · [frontend](https://github.com/predator-1-ml/ControlTower/actions/runs/35819328768) |
| 10:50 | Current `main` (`9d67784`) deployed through the same pipeline | [#11](https://github.com/predator-1-ml/ControlTower/pull/11) · [backend](https://github.com/predator-1-ml/ControlTower/actions/runs/35851127345) |

The failed runs from 09-17 and 09-18 in the Actions tab happened before the
first `apply`. The IAM roles did not exist yet, so every run failed at the
credentials step.

### Five faults found only by live traffic

All five passed every local run, every unit test and CI.

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| 1 | Every `/bff/*` call returned 502, although DNS resolved and the backend target was healthy | The internal ALB listened on `:80`, but the frontend called `:8000`, which is the only port its security group opens | The listener port became a module input. [#2](https://github.com/predator-1-ml/ControlTower/pull/2) |
| 2 | Frontend tasks kept being replaced | The health check hit `/`, which redirects (307) to sign-in. The check only accepts 200. | Health check on `/sign-in`. [#2](https://github.com/predator-1-ml/ControlTower/pull/2) |
| 3 | The CI plan showed "2 to change", yet the apply job was **skipped** and the run showed green | The `setup-terraform` wrapper always exits 0, so the `-detailed-exitcode` value was lost | `terraform_wrapper: false`. [#3](https://github.com/predator-1-ml/ControlTower/pull/3) |
| 4 | ECS killed every frontend task, while the ALB saw them as healthy | Fargate overrides `HOSTNAME`, so Next.js bound only to the task IP. The container's own check on `127.0.0.1` was refused. | Set `HOSTNAME=0.0.0.0` in the task definition. [#4](https://github.com/predator-1-ml/ControlTower/pull/4) |
| 5 | After sign-in, the page failed to load | `crypto.randomUUID()` only works in a secure context. localhost counts as secure; a plain-HTTP ALB does not. | Fall back to `getRandomValues`. [#6](https://github.com/predator-1-ml/ControlTower/pull/6) |

### End-to-end verification on the deployed stack

This was driven in a real browser against the public URL, after the seed and
ingest tasks ran.

| Check | Observed |
|---|---|
| Planning before execution | A two-task plan appeared before either task ran. Tasks moved `pending → running → done` in dependency order. |
| Context across a switch | "Do they have any other open claims?" was answered for CUST-1001. The customer came from state, not from the message. |
| RAG on Bedrock | A policy question was answered with the citation `[claims-handling-policy.md, Motor claims]` |
| A pause that reads the reply | For CLM-5003, the operator typed the two missing references in plain words. A one-off task queried RDS: `status=under_review`, plus one `audit_events` row. |
| **Crash mid-workflow** | Onboarding for CUST-1002 was paused, then `aws ecs stop-task` stopped **the only backend task**. ECS replaced it in about a minute and the private DNS name did not change. After a page refresh, the paused question was restored from Postgres. Answering it completed the workflow. |

Every request went browser → public ALB → Next.js → `api.control-tower.internal:8000`
→ internal ALB → FastAPI → Bedrock / RDS. The backend was never reachable from
outside the VPC.

### Reproduce it

```bash
export AWS_PROFILE=<profile>
cd terraform/bootstrap   && terraform init && terraform apply   # state bucket, once
cd ../environments/dev   && terraform init && terraform apply   # 69 resources, about 7 min
# set the 5 repository variables and 2 secrets from `terraform output` (walkthrough §3)
# merge to main → approve the `dev` gate → CI builds, migrates and deploys
# seed + ingest as one-off ECS tasks (walkthrough §6)
terraform destroy                                                # afterwards
```

**Cost:** about **$4/day** (NAT gateway, two ALBs, `db.t4g.micro`, two Fargate
tasks). The lifecycle is deploy → demo → `terraform destroy`.

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
