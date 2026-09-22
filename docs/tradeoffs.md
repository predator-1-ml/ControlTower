# Tradeoffs

Every entry is a decision with a real cost, not a free win. The cost is stated.

## Architecture

### BFF proxy instead of ALB path-routing
**Gained:** every frontend→backend hop crosses a private domain name; the backend
has zero public ingress; no API origin baked into the browser bundle, so one image
promotes across environments.
**Cost:** the frontend fleet now carries API throughput as well as rendering, and
SSE correctness becomes its problem — header allowlisting, no buffering, no
compression middleware in front of it.
**Rejected alternative:** ALB `/api/*` routing is simpler and scales the two
independently, but it makes the backend publicly reachable and the frontend never
uses the private name — which does not satisfy the requirement.

### Internal ALB instead of Cloud Map
**Gained:** a stable DNS name that survives task replacement, health-checked
targets.
**Cost:** ~$20/month, and one more hop.
**Rejected alternative:** Cloud Map is free and explicitly named in the
assignment, but resolves to task IPs — and undici caches DNS, so replacing a
backend task can leave the frontend holding a stale address. The crash-recovery
demo replaces backend tasks *on purpose*.

### Explicit planner/supervisor instead of a prebuilt agent
**Gained:** routing, coordination and state transitions are visible in the code —
which is what the assignment asks to see demonstrated.
**Cost:** more code to write and defend than `create_agent(...)`.

### Supervisor has no LLM
**Gained:** deterministic routing, no latency or token cost per hop, and routing
logic that is unit-testable as a pure function.
**Cost:** it cannot handle a situation the planner did not anticipate. A model
supervisor could improvise; this one fails the task and reports it.

### One Postgres for everything
**Gained:** one datastore to operate, back up, and explain; domain data and
checkpoints commit together.
**Cost:** vector search competes with transactional load on one instance. At
serious RAG scale they would separate.

## Implementation

### Plain SQL instead of an ORM
**Gained:** one database library and one connection pool. The checkpointer
requires psycopg regardless, so an ORM would have meant a second pool — and "why
do you have two connection pools?" has no good answer.
**Cost:** hand-written SQL, no compile-time schema checking.

### Numbered .sql migrations instead of Alembic
**Gained:** migrations are readable SQL applied under the same advisory lock the
checkpointer setup uses; two fewer dependencies.
**Cost:** no autogeneration, no downgrade path. Acceptable for a schema this size.

### `merge_tasks` requires a calling convention
**Gained:** concurrent subgraphs can update their own tasks without clobbering
each other, and the plan list stays bounded.
**Cost:** a node returning the whole plan breaks it subtly. Mitigated by
`task_delta()` and two tests, one of which asserts the broken behaviour
deliberately — but it is a convention, not a compiler guarantee.

### Bedrock in prod, Anthropic API in dev
**Gained:** fast local iteration with no AWS gating; an AWS-native production
story; and it absorbed a genuine constraint — no Anthropic model is invocable on
this account until a use-case form is approved, so production runs Nova Pro.
**Cost:** two providers to keep working, and dev exercises a different model than
prod. Meaningful behaviour differences would be found late.

### `ChatBedrockConverse` despite no native async
**Gained:** the standard Bedrock surface — Guardrails, Knowledge Bases,
invocation logging.
**Cost:** every model call holds a worker thread for its full duration, including
streamed responses. Mitigated by raising the executor and boto3 pool together;
the ceiling is higher, not absent.
**Rejected alternative:** the Anthropic-SDK-backed Bedrock client is genuinely
async but has fewer regions and no Guardrails.

## Infrastructure

### One NAT gateway
**Gained:** ~$35/month instead of ~$70.
**Cost:** if that AZ fails, tasks in the other AZ lose outbound internet.
Production runs one per AZ.

### Single-AZ RDS, no deletion protection, no final snapshot
**Gained:** ~$16/month instead of roughly double, and `terraform destroy`
completes without manual cleanup.
**Cost:** a failure means downtime and a restore, not a failover. All three are
wrong for production and are listed in the promotion table.

### `AdministratorAccess` on the Terraform apply role
**Gained:** Terraform can create IAM, VPC, RDS and ECS resources without a policy
that silently breaks the next time a resource type is added.
**Cost:** a broad role. The control is the environment gate — the OIDC subject
only carries `environment:dev` when the job declares it, so a required reviewer is
a precondition for the credential existing.

### No `prod/` environment
**Gained:** nothing shipped that has never been run.
**Cost:** the promotion path is documented rather than demonstrated.

## Product

### Onboarding uses no LLM at all
**Gained:** every decision with a legal or financial consequence is readable,
testable, and auditable.
**Cost:** it cannot handle anything outside its branches. A genuinely novel case
goes to manual review rather than being reasoned about.

### Citations built from retrieved chunks, never parsed from the model
**Gained:** a citation always points at text that was actually retrieved.
**Cost:** citations are chunk-level, not sentence-level. The answer may cite a
chunk it drew only one clause from.

### No query rewriting in RAG
**Gained:** one less round trip and one less token bill.
**Cost:** terse or badly-phrased queries retrieve worse. This is the first thing
to add when the corpus grows — see [`future-improvements.md`](future-improvements.md).

### A paused session treats the next message as the answer
**Gained:** the answer a user types can never be discarded. While a workflow is
parked on `interrupt()`, `/chat` delivers the message as `Command(resume=...)`
without consulting the planner — re-planning over a paused workflow would throw
away what was just typed.
**Cost:** a question cannot be parked. "Actually, do something else first" is
consumed as the answer to the open question. The fix is a classifier in front of
the resume (answer vs. new request), which is an LLM judgement on the one path
that is currently deterministic — not worth it at this scale.

### Free-text answers to interrupts are recorded, not parsed
**Gained:** the human-in-the-loop path has no LLM in it and cannot misread an
answer into the wrong field.
**Cost:** the supplied details are stored as the operator typed them
(`workflow_states[...]["supplied"]`), not validated. Supplying "anything"
satisfies the pause; a real system would attach structured fields to the prompt.

### In-process session concurrency guard
**Gained:** two concurrent turns on one session cannot race the checkpointer,
today.
**Cost:** it holds for a single backend task and not across a scaled-out service.
Documented as a known limitation rather than half-solved, because a guard that
looks distributed and isn't is worse than one that is honestly local.
