# Assumptions

What was assumed, and what would change if the assumption were wrong.

## Domain

**Insurance operations.** The assignment lists example workflows without naming an
industry; claims processing implies insurance, so the domain model follows that.
Nothing in the orchestration depends on it — the three workflows are deliberately
different *shapes* (deterministic, tool-driven, retrieval), not three variations of
one domain.

**Internal operators, not customers.** The assignment says "intended for internal
operational users". So: no customer-facing authentication, no multi-tenancy, and
an operator is trusted to look up any customer. A customer-facing version would
need per-user authorisation on every tool call, which is a different design.

**Business rules are simplified but not invented.** Age ≥ 18, claims ≥ 5000 need a
second review, an unsettled claim requires manual review. They come from the
seeded policy documents so the RAG workflow and the deterministic workflow agree
with each other.

## Scale

**Demo scale: tens of sessions, thousands of rows, five knowledge documents.**
Consequences that are honest rather than hidden:

- The HNSW index is not load-bearing. Below ~50k vectors exact search is 100%
  recall in single-digit milliseconds. It exists to demonstrate the production
  shape.
- One task per service, so the in-process session guard is sufficient today and
  would not be after scaling out.
- No caching layer. Redis appears in the original HLD as optional; nothing here
  justified it.

## Operations

**Single region (ap-southeast-1)**, chosen because it is where the AWS account
has Bedrock access. Multi-region is a different exercise.

**The environment is short-lived.** Deploy, record the demo, `terraform destroy`.
That is why `skip_final_snapshot`, `force_delete`, and disabled deletion
protection are set — they would all be wrong in production and are flagged as such
in [`terraform.md`](terraform.md).

**No HTTPS on the public ALB.** A demo has no registered domain against which to
validate a certificate. The listener block exists to show where TLS terminates.

## Model availability

**Bedrock serves Sonnet 4.6, not Opus.** Verified by direct invocation: Opus 4.8
and Sonnet 5 both return *"not available for this account"* — a fresh AWS account
does not get the newest models without contacting AWS Sales.

**Embeddings are Cohere, not Titan.** Amazon Titan embeddings are not offered in
ap-southeast-1. `cohere.embed-english-v3` is 1024-dimensional (verified, and
unit-normalised), which is why `vector(1024)` is unchanged.

Both were checked against the live account rather than assumed, because
`list-foundation-models` returning a model does **not** mean you can invoke it.

## Data

**Seed fixtures are load-bearing, not decoration.** Each of the four customers
drives a different workflow branch, and integration tests assert against them.
Changing the seed can silently stop exercising a branch.

**`missing_fields` is persisted on the claim** rather than re-derived at runtime,
so the reason a workflow paused is auditable after the fact.

## What a reviewer should NOT assume was overlooked

Explicitly out of scope, each for a stated reason:

| Not built | Why |
|---|---|
| Authentication / authorisation | Internal tool; the assignment does not ask for it |
| Multi-tenancy | Single operator organisation |
| Rate limiting | No untrusted callers |
| Kubernetes, Kafka, Neo4j, MongoDB, dedicated vector DB | Complexity without a requirement to justify it |
| `prod/` Terraform environment | An environment never applied is decoration |
| Query rewriting in RAG | A round trip that changes nothing against five documents |
| Reranking | Same |
| WAF / Shield | Appropriate for a real internet-facing tool; cost a demo does not justify |
