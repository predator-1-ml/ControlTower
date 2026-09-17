# Future Improvements

Ordered by what I would actually do first, not by ambition.

## 1. Session concurrency, properly

The current guard is an in-process `set`. It holds for one backend task and not
across a scaled-out service — the moment `desired_count > 1`, two turns on one
session can land on different tasks and race the checkpointer.

**Fix:** a Postgres advisory lock keyed on `session_id`, taken for the duration of
a turn. Same mechanism already used for migrations.

This is first because it is the only known-incorrect thing in the system, and it
becomes incorrect at exactly the moment you'd want to scale.

## 2. Move long runs off the request path

Today a turn runs inside the HTTP request. A LangGraph run exceeding
`deregistration_delay` is killed mid-flight — survivable, because the checkpointer
means work resumes, but the client sees a dropped stream.

**Fix:** a queue and a worker service. The API enqueues and returns immediately;
the client reconnects to the event stream by `session_id`. The checkpointer
already makes the work resumable; what is missing is the client-side reconnect
contract and `Last-Event-ID` support.

## 3. Query rewriting and reranking in RAG

Deliberately skipped: against five policy documents, rewriting is a round trip
that changes nothing measurable.

Both become worth it once the corpus is large enough that terse queries miss.
Rewriting first — it is one call and helps most. Reranking second, and only with
an evaluation set, because a reranker you cannot measure is a slower retriever.

## 4. Structured evaluation

There is no eval harness. Tests assert *plumbing* — that citations come from
retrieval, that the model is not called on the incomplete branch — not answer
quality.

**Fix:** a fixture set of (question, expected behaviour) pairs, run against the
graph, scoring retrieval recall and whether the workflow reached the right
terminal node. Prompt changes are currently unmeasurable, which means they are
unsafe.

## 5. Prompt and graph versioning

`schema_version` is stamped but nothing versions the prompts. A prompt change
today silently alters the behaviour of sessions resuming from old checkpoints.

**Fix:** version prompts alongside the state schema; record which version produced
each `audit_events` row. Without it, "why did it do that last week?" is
unanswerable.

## 6. Observability beyond audit rows

`trace_id` flows through every layer and reaches the client, which is the
foundation. What is missing is anything to *look* at it with.

**Fix:** OpenTelemetry spans per node and per tool, exported to CloudWatch or
Langfuse; dashboards for workflow completion rate, pause rate, per-node latency
and token cost. LLM cost per session is currently unknown, which is a strange
thing not to know.

## 7. Production Terraform environment

`environments/prod/` with the promotion table in [`terraform.md`](terraform.md)
applied: Multi-AZ RDS, NAT per AZ, deletion protection, `desired_count ≥ 2`,
final snapshots.

Plus HTTPS — an ACM certificate and a `:80 → :443` redirect. The listener block
already exists; it needs a registered domain.

## 8. Tighten the Terraform apply role

`AdministratorAccess` gated by a GitHub environment is defensible but broad.

**Fix:** narrow it by observing what Terraform actually calls — CloudTrail over a
few applies gives the real action list. Worth doing once the infrastructure stops
changing shape, and not before, because a hand-written policy breaks silently the
next time a resource type is added.

## 9. Richer workflow visibility

The UI shows the plan and an activity log. It does not show the graph.

**Fix:** render the DAG with dependency edges drawn, node-level progress inside a
running workflow, and the ability to inspect a completed task's tool results.
`get_stream_writer()` already emits per-node progress; the frontend just
aggregates it into a list today.

## 10. Blue/green deployments

ECS native blue/green landed in AWS provider 6.4.0. Rolling updates plus the
circuit breaker are adequate for a demo; blue/green would give a clean cutover and
a faster rollback for a service holding long-lived streams — where draining
matters more than usual.

---

## Deliberately not planned

Things a reviewer might expect to see here, with the reason they are absent:

- **Kubernetes** — nothing here needs it, and it would replace one operational
  story with a larger one.
- **A dedicated vector database** — pgvector at this scale is not the constraint.
  Revisit past millions of vectors.
- **Microservice-per-agent** — the agents share state and a database. Splitting
  them would add network calls and distributed-transaction problems to buy
  independent deployability nobody has asked for.
- **Multi-region** — a different exercise with a different budget.
