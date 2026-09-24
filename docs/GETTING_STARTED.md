# Getting started

How to run the system locally, what it deliberately does not do yet, where the
code lives, and which document covers what. The [README](../README.md) explains
how the system works; this page is for running and navigating it.

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
- **`make seed` empties the embedded corpus**, so always run `make ingest` after
  it.
- **No credentials are needed for the tests or for `make verify-resume`.**

## Test it

```bash
make test            # unit tests; the 28 database tests skip without Postgres
make test-db         # + integration tests against Postgres (needs up, migrate, seed)
make lint            # ruff
make verify-resume   # durable execution across process death
```

`make verify-resume` is the most important check. It runs a graph until it
pauses on `interrupt()`, **exits the process**, then resumes and finishes the
run from a brand-new process. It ends with:

```
RESUMED AND COMPLETED: steps=['stage_one', 'ask_human', 'stage_two']
stage_one ran in a process that no longer exists.
```

To walk through the demo by hand, follow [`demo.md`](demo.md).

---

## Known limitations

These are stated on purpose rather than hidden. Each one has a written reason in
[`tradeoffs.md`](tradeoffs.md).

- **While a workflow is paused, the next message is treated as the answer.** You
  cannot park a question and come back to it later.
- **The session concurrency guard only works inside one process.** It is correct
  for a single backend task, but not for a scaled-out service. The fix (a
  Postgres advisory lock) is item 1 in
  [`future-improvements.md`](future-improvements.md).
- **Long runs share the request path.** A queue and a worker would move them off
  it (item 2 in [`future-improvements.md`](future-improvements.md)).
- **This is a dev-tier environment:** HTTP only (there is no domain for a
  certificate), one NAT gateway, single-AZ RDS, and the operator password is a
  plain task environment variable rather than a Secrets Manager reference.
- **Bedrock serves Nova Pro, not Claude.** Every Anthropic model on this AWS
  account is blocked until an account-level use-case form is submitted.
  Switching back is one config value.

See [`assumptions.md`](assumptions.md) for what was assumed and what was
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
  components/       Workspace, ChatPanel, TaskTimeline, WorkflowPanel, Status
  lib/              SSE client, signed session cookie
  proxy.ts          operator sign-in guard
terraform/
  bootstrap/        remote state bucket
  modules/          networking, alb, ecs-cluster, ecs-service, rds, ecr, github-oidc, budget
  environments/dev/
.github/workflows/  backend CI, frontend CI, terraform plan/apply, two deploys
docs/               design documents (below)
```

**If you read one file of code, read
[`backend/app/graph/state.py`](../backend/app/graph/state.py).** The rest of the
backend is built around the shape defined there.

---

## Documentation

| Document | Covers |
|---|---|
| [`networking.md`](networking.md) | The Additional Challenge: private communication, the alternatives, the tradeoffs |
| [`langgraph-design.md`](langgraph-design.md) | Graph topology, and where the LLM is and is not used |
| [`state-management.md`](state-management.md) | Reducers, checkpointing, resume, schema versioning |
| [`deployment-walkthrough.md`](deployment-walkthrough.md) | The first deploy, and the five faults only live traffic found |
| [`tradeoffs.md`](tradeoffs.md) | Every decision, with its cost |
| [`architecture.md`](architecture.md) | Solution and AWS architecture |
| [`ecs.md`](ecs.md) | Task definitions, IAM roles, shutdown timing |
| [`terraform.md`](terraform.md) | Modules, state, promotion path |
| [`ci-cd.md`](ci-cd.md) | Pipelines, OIDC, where Terraform ends and CI begins |
| [`assumptions.md`](assumptions.md) | What was assumed, and what was not built |
| [`future-improvements.md`](future-improvements.md) | What I would do next, in order |
| [`demo.md`](demo.md) | The demo runbook |
| [`../frontend/DESIGN.md`](../frontend/DESIGN.md) | The UI's layout, status vocabulary and design rules |
| [`plans/`](plans/) | Original design plans and ADRs |
