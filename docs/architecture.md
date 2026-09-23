# Solution Architecture

## One sentence

A Next.js operations dashboard talks privately to a FastAPI service on ECS
Fargate; FastAPI runs a LangGraph supervisor that plans and coordinates
onboarding, claims and knowledge subgraphs, which execute controlled tools against
PostgreSQL, with state checkpointed in RDS so any task can be replaced mid-run.

## The shape

```
Browser
  │  HTTPS
┌─▼──────────────┐
│  Public ALB    │  the only internet-facing component
└─┬──────────────┘
  │  :3000
┌─▼──────────────┐
│  Next.js       │  chat · plan DAG · task timeline
│  (Fargate)     │  /bff/* proxies over private DNS
└─┬──────────────┘
  │  http://api.control-tower.internal:8000
┌─▼──────────────┐
│  FastAPI       │  SSE · session API
│  (Fargate)     │
│   └── LangGraph: planner → supervisor → subgraphs → compose
└─┬──────────────┘
  │  :5432
┌─▼──────────────────────────────────────────┐
│  RDS PostgreSQL + pgvector                 │
│  domain tables · checkpoints · embeddings  │
└────────────────────────────────────────────┘
```

## Core principle

> **Agent decides · Tool does · Workflow controls sequence · State remembers ·
> Graph controls transitions**

In practice that means one line drawn consistently: **business rules stay in code,
language work goes to the model.** Onboarding — where every decision has a legal
or financial consequence — uses no LLM at all.

## Technology, and why

| Layer | Choice | Reason |
|---|---|---|
| Orchestration | LangGraph 1.2.11 | Required. Explicit graphs, subgraphs, durable checkpoints |
| Backend | FastAPI + Python 3.12 | Natural fit for LangGraph; typed async APIs |
| Frontend | Next.js 16 / React 19 | The BFF route handler is what makes private-DNS communication work |
| Database | PostgreSQL 17 + pgvector | One datastore for domain data, checkpoints **and** vectors |
| LLM | Bedrock (prod) / Anthropic API (dev) | Thin provider interface; see below |
| Compute | ECS Fargate | Required. No EC2 hosts to operate |
| IaC | Terraform 1.16 | Required |
| CI/CD | GitHub Actions + OIDC | Required. No long-lived keys |

### Deliberately out of scope

Kubernetes, Kafka, Neo4j, MongoDB, a dedicated vector database, Step Functions,
microservice-per-agent. Each would be defensible at a scale this system is not at,
and none would make the assignment's requirements more satisfied.

**PostgreSQL does most of the persistence work on purpose.** One datastore to
operate, one connection pool to explain, one backup story.

## Three workflows, three patterns

Chosen so they demonstrate *different* architectural shapes rather than three
variations of one:

| Workflow | Pattern | LLM used for |
|---|---|---|
| **Onboarding** | deterministic rules + conditional routing | nothing |
| **Claims** | tool/API-driven | reading the operator's free-text reply, and writing the summary |
| **Knowledge** | retrieval-augmented (pgvector) | answering from retrieved excerpts only |

## Request flow

```
user message
  → resume? ─ yes ─▶ Command(resume=…)     ← an open interrupt means this
                                              message is the ANSWER
  → no ─▶ planner ─▶ task DAG ─▶ streamed to the UI before any work runs
                       │
                  supervisor ─▶ next ready task ─▶ workflow subgraph
                       ▲                                │
                       └────────────────────────────────┘
                       │
                  no work left ─▶ compose ─▶ final response
```

Resume taking precedence over planning is the most important ordering in the
system. Planning while a workflow is paused would re-plan over it and discard the
answer the user just typed.

## LLM provider abstraction

Graph code never imports a concrete model class; it calls `get_chat_model()`.

| Environment | Provider | Model |
|---|---|---|
| Local dev | Anthropic API | `claude-opus-4-8` |
| Production | Bedrock | `apac.amazon.nova-pro-v1:0` |
| Embeddings, everywhere | Bedrock | `cohere.embed-english-v3` |

The split is not stylistic. **No Anthropic model is invocable on this AWS
account**: Bedrock gates them all behind an account-level use-case form and
returns `ResourceNotFoundException` until it is submitted. Nova Pro needs no form,
and the full graph — planning, dependencies, interrupt and resume, RAG — is
verified on it. Moving to Claude once the form clears is one config value. The
abstraction earned its place on day one.

Embeddings are deliberately *not* tied to the chat provider: they are Cohere on
Bedrock in local development as well as production, so there is one vector space
everywhere and local retrieval exercises the vectors production uses.

Two Bedrock details that cost time if unknown:

- **Model IDs need an inference-profile prefix, and it is per-model.** In
  ap-southeast-1 Nova is `apac.`-prefixed while Claude is `global.`-prefixed;
  `us.` does not resolve at all, and the bare id is rejected. List them with
  `aws bedrock list-inference-profiles` rather than guessing.
- **`ChatBedrockConverse` has no native async.** `ainvoke`/`astream` bridge
  blocking boto3 onto a thread pool, holding a thread for the whole call including
  a streamed response. The default executor caps at `min(32, cpu_count + 4)`, so
  FastAPI concurrency silently ceilings there. The executor **and** boto3's
  `max_pool_connections` are raised together — raising one alone just moves the
  bottleneck.

## Observability

One `trace_id` per turn, carried through the API, the graph, every node, every
tool, and written to `audit_events`. It rides on **every SSE frame**, so a failure
a user reports on screen joins directly to its rows rather than being guessed at
from timestamps.

## Further reading

| Document | Covers |
|---|---|
| [`langgraph-design.md`](langgraph-design.md) | Graph topology, planner/supervisor, where the LLM is |
| [`state-management.md`](state-management.md) | Reducers, checkpointing, resume, schema versioning |
| [`networking.md`](networking.md) | Private communication, alternatives, streaming |
| [`ecs.md`](ecs.md) | Task definitions, deployment, shutdown timing |
| [`terraform.md`](terraform.md) | Module structure, state, promotion path |
| [`ci-cd.md`](ci-cd.md) | Pipelines, OIDC, the Terraform/CI boundary |
| [`assumptions.md`](assumptions.md) · [`tradeoffs.md`](tradeoffs.md) · [`future-improvements.md`](future-improvements.md) | What was assumed, what was traded, what is next |
