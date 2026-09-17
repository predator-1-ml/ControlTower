---
title: Orchestration Core and Delivery — Remaining Work
type: feat
status: active
date: 2026-09-17
origin: docs/plans/2026-09-17-feat-ai-operations-control-tower-plan.md
---

# Orchestration Core and Delivery

Second plan. The [first one](2026-09-17-feat-ai-operations-control-tower-plan.md)
covers architecture and ADRs and is still the reference for *why*. This one covers
*what is left*, in the order it should be built, with the design decisions for the
next block settled rather than deferred.

## Where we are

| Shipped | Commit |
|---|---|
| Durable execution, proven across real process death | `10ca242` |
| Domain schema, seed fixtures, repository | `ce88a00` |
| Claims workflow, 6 branches | `4e9e9f9` |
| Terraform state bootstrap + networking | `fa7912f` |
| Bedrock/embedding corrections for ap-southeast-1 | `0546615` |

35 tests, ruff clean. Terraform validates. Nothing applied to AWS yet.

## Blockers, and what they actually block

| Blocker | Blocks | Does **not** block |
|---|---|---|
| Root AWS credentials still in use | `terraform apply` | Writing/validating any Terraform |
| Deploy cost not yet confirmed | `terraform apply` | Everything else |
| Bedrock has no Opus (Sonnet 4.6 only) | nothing — config already corrected | — |

**Consequence for sequencing:** Blocks 1–5 below are entirely unblocked. Block 6
is the only one that needs you. That is why the graph work comes first here, even
though the first plan argued for front-loading infrastructure — the argument still
holds, but it is outranked by "do not sit idle waiting on a credential change".

---

## Block 1 — Planner and Supervisor  ← next

The heart of the assignment. Requirements 6 (multi-agent coordination), 9
(planning before execution) and 11 (switch workflows mid-session) are all
satisfied here, and none of them are satisfied yet.

### Design decisions, settled

**Planner emits a task DAG, not a sequence.** `Plan` (already defined in
`state.py`) via `with_structured_output(Plan, include_raw=True)`. `include_raw`
matters: the planner runs on every turn, and one malformed plan must degrade to a
handled error, not kill the run.

**The planner extends; it does not replace.** On a second user turn it receives
the existing plan and appends. This is the entire mechanism for "a user may move
between workflows during a session": new tasks join the DAG, old `PENDING` tasks
stay put, and the supervisor picks them up again when the user returns to them.
No special-casing of workflow switches anywhere.

**Supervisor is pure selection + routing.** `ready_tasks()` already implements
dependency resolution and is unit-tested. The node marks exactly one task
`RUNNING` and returns `Command(goto=<workflow>, update=...)`. Annotate the return
`Command[Literal[...]]` so LangGraph can infer edges for the visualisation.

**Entry routing: resume beats plan.** On a new turn, if `aget_state().interrupts`
is non-empty, the caller sends `Command(resume=...)` and the planner never runs.
Otherwise the planner runs. Getting this backwards would re-plan over a paused
workflow and lose the human's answer.

**⚠️ Raise `recursion_limit`.** A supervisor loop costs 2 super-steps per task
(supervisor → subgraph → supervisor). LangGraph's default limit is **25**, so the
graph dies at roughly 11 tasks with `GraphRecursionError`. Set `recursion_limit`
to 100 in the invoke config and cap plan length in the planner prompt. This is a
silent cliff that will not appear in a 3-task demo and will appear in a 12-task
one.

**Failure propagates as SKIPPED, not FAILED.** When a task fails, its dependents
become `SKIPPED` rather than `FAILED` — they did not fail, they never ran. This
is why `is_plan_complete` already treats a plan containing SKIPPED as complete.
`compose_response` must therefore handle partial success, which is the normal
case in operations work.

**Retries live on the node, not in the loop.** `add_node(..., retry_policy=
RetryPolicy(max_attempts=3))` for tool nodes. Hand-rolling retry in the supervisor
would double-count against `recursion_limit`.

### Files

| File | Purpose |
|---|---|
| `app/graph/planner.py` | goal → `Plan`; extends an existing plan |
| `app/graph/supervisor.py` | select next ready task, route via `Command` |
| `app/graph/compose.py` | synthesise final answer from `plan` + `workflow_states` |
| `app/graph/build.py` | assemble main graph + 3 subgraphs, replaces `smoke.py` |
| `app/prompts/planner.py` | planner system prompt + few-shot |

### Acceptance

- [ ] *"Onboard CUST-1002 and check whether they already have an active claim"*
      produces a 3-task DAG with a dependency edge, **before** any work runs
- [ ] Both workflows execute, correct order, dependency respected
- [ ] A second turn switching workflow extends the plan; earlier tasks survive
- [ ] A failing task marks dependents SKIPPED; final response reports partially
- [ ] A 12-task plan completes (proves the recursion limit is actually raised)
- [ ] `smoke.py` deleted — it was scaffolding and has served its purpose

---

## Block 2 — Onboarding workflow

Same shape as claims, more branches, so it exercises conditional routing harder.

```
load_customer -> validate_information -> [gaps]     -> request_information (interrupt)
                                      -> [complete] -> verify_identity
                                              -> [failed]     -> manual_review
                                              -> [passed]     -> check_eligibility
                                                      -> [eligible]   -> create_application
                                                      -> [ineligible] -> reject
```

Seed fixtures already cover every branch: CUST-1002 clean, CUST-1003 `kyc_status
= failed` → manual review, CUST-1001 verified with an active claim → the
eligibility rule in the seeded policy says that needs manual review.

**The one real trap:** `create_application` is the only write in the system. It
must not sit in a node that can `interrupt()`, because resume re-runs a node from
the top and would create the application twice. Keep it in its own node.

---

## Block 3 — Knowledge / RAG

```
query_rewrite -> embed -> pgvector_retrieve -> assemble_context -> generate (+ citations)
```

- `cohere.embed-english-v3` via Bedrock. **Verified: 1024 dims, unit-normalised**
  — so `vector_cosine_ops` and `<=>` are the correct pairing.
- Ingestion script embeds `data/seed` policy text into `knowledge_chunks`.
- ⚠️ `SET hnsw.iterative_scan = relaxed_order` before any filtered query, or a
  `WHERE` clause silently returns too few rows.
- Retrieval must tolerate `embedding IS NULL` — seed rows start un-embedded.
- Citations come from `source` + `section`; answers must not cite what was not
  retrieved.

---

## Block 4 — API and streaming

| Endpoint | Purpose |
|---|---|
| `POST /chat` | new turn; SSE stream of graph progress |
| `POST /sessions/{id}/resume` | answer an open interrupt |
| `GET /sessions/{id}` | plan + task states, for reconnect |
| `GET /health` | done |

`astream(stream_mode=["updates","messages","custom"], subgraphs=True,
version="v2")`. `get_stream_writer()` emits per-node progress for the timeline.
`EventSourceResponse(..., ping=15)`.

**Verify SSE through all three hops with `curl -N -i`, not a browser.** The
failure modes are in ADR-002 and every one is self-inflicted.

---

## Block 5 — Frontend

Three components against the existing BFF proxy: `ChatPanel`, `WorkflowPanel`
(the plan DAG), `TaskTimeline` (per-node progress). Client components call
relative `/bff/*` only — an absolute URL silently destroys the private-domain
property.

Minimum bar: the plan DAG must be visible *before* execution starts. That is the
screenshot that proves "planning before execution".

---

## Block 6 — Infrastructure and deploy  ⛔ needs credentials

Modules: `alb` (public + internal), `ecr`, `rds`, `ecs-cluster`, `ecs-service`,
`service-discovery` (Route 53 private zone), `secrets`, then
`environments/dev`.

Settled already, carried from the first plan: `stopTimeout` caps at 120s on
Fargate so `deregistration_delay` is the real in-flight budget; migrations run as
a one-off `RunTask` before the service update; `manage_master_user_password` for
DB creds because an injected password dies at the first rotation;
`lifecycle { ignore_changes = [task_definition, desired_count] }` so Terraform
does not revert CI's image.

**Deploy order matters:** bootstrap → dev infra → migration task → backend →
frontend. Backend before frontend, because the frontend resolves
`api.internal` at request time and a missing target is a confusing 502.

---

## Block 7 — CI/CD

OIDC (no long-lived keys, no thumbprint). Five workflows: backend/frontend CI,
backend/frontend deploy, terraform plan/apply.

🔴 **The trust policy will not match a textbook subject claim.** This repo was
created 2026-09-16, after the 2026-07-15 cutoff, so it uses immutable subject
claims with numeric IDs — owner `66988630`, repo `1373064236`. Read the real
claim from a token before writing the policy.

---

## Block 8 — Documentation and demo

Ten files (requirement 22). Most of the content already exists as ADRs and code
comments; this is assembly, not authorship.

Demo, three beats: plan DAG appears before execution → workflow switch preserves
context → **kill the backend mid-interrupt and resume**. Beat three is the one
that separates this from a chatbot.

---

## Gaps found reviewing the flow end to end

Things neither plan had addressed, now assigned:

1. **`recursion_limit`** — silent cliff at ~11 tasks. Block 1.
2. **Re-plan vs resume ambiguity** — resume must win, or a paused workflow gets
   re-planned and the human's answer is lost. Block 1.
3. **Concurrent turns on one session** — two requests on the same `thread_id`
   race the checkpointer. Reject a second turn while a session is `running`;
   document as a known limitation rather than solving it.
4. **Un-embedded knowledge rows** — retrieval must not assume `embedding` is
   populated. Block 3.
5. **`create_application` idempotency** — the only write, must not share a node
   with an interrupt. Block 2.
6. **Trace correlation** — `trace_id` is in state and `audit_events` but is not
   yet returned to the client. Add it to the SSE envelope in Block 4 so a demo
   failure is traceable to a row.

## Sequencing

```
Block 1 (planner/supervisor)  ← start here, unblocked
Block 2 (onboarding)          ┐
Block 3 (knowledge/RAG)       ├ unblocked
Block 4 (API + SSE)           │
Block 5 (frontend)            ┘
Block 6 (infra + deploy)      ⛔ credentials + cost sign-off
Block 7 (CI/CD)               depends on 6
Block 8 (docs + demo)         depends on all
```

If time runs short, cut in this order: RAG reranking → prod Terraform env →
frontend polish → knowledge workflow depth. **Never cut:** the private-DNS proof,
checkpoint resume, or the docs.
