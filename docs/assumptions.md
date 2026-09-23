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
an operator is trusted to look up any customer.

**One static operator credential, not identity management.** The tool is internal
but its load balancer is public, so there is a door: a sign-in checked on the
Next.js server against a credential from the environment, remembered in an
HMAC-signed httpOnly cookie, with a guard in front of both the pages and `/bff/*`
(`frontend/lib/session.ts`, `frontend/proxy.ts`). There is deliberately no user
store, no sign-up, no lockout and no server-side revocation — a copied cookie is
valid until its 8-hour expiry. Production would put OIDC at the ALB listener
(`authenticate-oidc`) and make the ALB internal behind a VPN; the app-level check
would then be defence in depth rather than the only door. A customer-facing version would
need per-user authorisation on every tool call, which is a different design.

**Business rules are simplified but not invented.** Age ≥ 18; a motor claim of
5000 or more needs a second review; a claim over 10000 goes to the duty manager;
an unsettled claim requires manual review. They are quoted from the seeded policy
documents, so the RAG workflow and the deterministic workflow cannot give a
handler two different answers — and that agreement is *checked*, not asserted:
`test_claims_workflow.py::test_the_thresholds_in_code_are_the_thresholds_in_the_policy_text`
reads `backend/seed/seed.sql` and fails if either number is tuned in only one place.

The second-review rule is restricted to **motor** claims because the policy
sentence sits under the *Motor claims* heading, directly after "Motor claims under
5000 are settled by a single assessor". Reading it as a rule for all claim types
would over-escalate every large travel and property claim.

## Scale

**Demo scale: tens of sessions, thousands of rows, sixteen policy passages across
three documents.**
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

**Bedrock serves Nova Pro, not Claude.** Verified by direct invocation: every
Anthropic model returns `ResourceNotFoundException: Model use case details have
not been submitted for this account` — an account-level form in the Bedrock
console, not per-model access. Nova Pro needs no form; the whole graph is verified
on it, and switching to Claude afterwards is one config value.

**Embeddings are Cohere, not Titan.** Amazon Titan embeddings are not offered in
ap-southeast-1. `cohere.embed-english-v3` is 1024-dimensional (verified, and
unit-normalised), which is why `vector(1024)` is unchanged.

Both were checked against the live account rather than assumed, because
`list-foundation-models` returning a model does **not** mean you can invoke it.

## Data

**Seed fixtures are load-bearing, not decoration.** The first four customers and
first three claims are the ones the tests and the demo runbook name; the other
eight customers cover the remaining branches (the age rejection, a pending KYC
status, more than two open claims, closed-only history) and give the book enough
shape that a wrong retrieval or a wrong active-claims filter is visible. Each
row's comment in `backend/seed/seed.sql` says which branch it is for. Changing
the seed can silently stop exercising a branch.

**Registering a claim asks once, then decides.** The claim type is the only
detail refused over — it is NOT NULL and the handling rules key on it. An amount
or incident date not yet known is recorded as unknown, which is what first
notice of loss looks like; the required incident report is recorded as
outstanding rather than chased in the same turn. Quoted from the seeded policy
("Registering a claim") and checked by the same test as the thresholds.

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
