# Networking Design

This document answers the assignment's Additional Challenge: how the frontend and
backend communicate privately using a domain name, why that design was chosen,
what else was considered, and what it costs.

## Topology

```
                            internet
                               │
                          ┌────▼────┐
   public subnets         │  ALB    │  ← the only internet-facing component
   10.0.0.0/24            └────┬────┘
   10.0.1.0/24                 │ :3000
                               │
   ┌───────────────────────────▼───────────────────────────┐
   │ private app subnets    Next.js (Fargate)              │
   │ 10.0.10.0/24                │                          │
   │ 10.0.11.0/24                │ http://api.control-tower.internal:8000
   │                             │      ↑ private DNS       │
   │                        ┌────▼────┐                     │
   │                        │ internal│                     │
   │                        │   ALB   │                     │
   │                        └────┬────┘                     │
   │                             │ :8000                    │
   │                        FastAPI (Fargate)               │
   └─────────────────────────────┬─────────────────────────┘
                                 │ :5432
   ┌─────────────────────────────▼─────────────────────────┐
   │ private data subnets   RDS PostgreSQL + pgvector      │
   │ 10.0.20.0/24  10.0.21.0/24    (no route to the NAT)   │
   └────────────────────────────────────────────────────────┘
```

Three tiers, because **"private subnet" and "private communication" are different
claims** and the assignment asks for the second.

## Security group chain

Each tier accepts traffic only from the tier above it, referenced **by security
group id, not by CIDR**:

```
internet ──▶ alb_public ──▶ frontend ──▶ alb_internal ──▶ backend ──▶ rds
   :443          :3000          :8000         :8000         :5432
```

The distinction matters: a CIDR rule trusts an address range, which grows as
subnets are added and quietly keeps trusting things it shouldn't. An SG-to-SG
rule trusts *a specific set of tasks*, and stays correct when the network changes
underneath it.

Nothing can skip a link. **The backend has no path from the internet at all** —
it is in no public target group and its security group admits only the internal
ALB. That is what makes the BFF proxy a real boundary rather than a convention.

## How the frontend finds the backend

A Route 53 **private hosted zone** (`control-tower.internal`) holds an A record
aliased at the internal ALB. Private hosted zones resolve only inside their
associated VPC, so the name is meaningless outside it.

The frontend task reads `BACKEND_INTERNAL_URL=http://api.control-tower.internal:8000`
at **request time** and calls it. No IP addresses appear anywhere in the
configuration.

Two VPC settings are required for any of this to work, and their absence is
silent:

```hcl
enable_dns_support   = true
enable_dns_hostnames = true
```

Without them the private zone does not resolve and the frontend cannot reach the
backend by name.

## The part that is easy to get wrong

A browser **cannot resolve a private VPC domain**. That is not a limitation to
work around — it is the crux of the exercise.

Next.js splits across that boundary:

| Runs where | Can resolve `api.control-tower.internal`? |
|---|---|
| Server components, route handlers (in-container) | ✅ yes |
| Client components (in the browser) | ❌ no |

A chat UI is inherently browser-side and streaming, so the obvious fix — an ALB
rule routing `/api/*` straight to the backend — *defeats the requirement*. Under
that design the frontend never calls the backend at all; the **browser** does,
over the public internet, and `https://app…/api/*` is reachable by any curl or
scanner. Private subnets are not private communication.

**The resolution: the frontend proxies.** A Next.js route handler at
`/bff/[...path]` receives the browser's request and forwards it over private DNS:

```
browser ──▶ public ALB ──▶ Next.js ──▶ api.control-tower.internal ──▶ FastAPI
```

Every frontend→backend byte now crosses a private domain name inside the VPC, and
the backend keeps zero public ingress.

### A second benefit that is easy to miss

`NEXT_PUBLIC_*` variables are **inlined by the bundler at `next build`**, not read
at runtime. With the BFF, client components call the *relative* URL `/bff/...`, so
there is no public API origin to bake in — and one image promotes unchanged from
local to dev to prod. Same-origin also means no CORS and no `SameSite=None`.

### Two traps worth recording

**Do not implement the hop with `next.config.js` rewrites.** Rewrites are
evaluated at build time and frozen into `routes-manifest.json`; `next start` never
re-reads them. A `destination` read from the environment is therefore fixed at
build, reproducing the exact problem the BFF exists to avoid — somewhere nobody
thinks to look. Rewrites have also historically dropped `text/event-stream`
bodies.

**Mount it at `/bff`, not `/api`.** An ALB rule for `/api/*` would shadow Next's
own `app/api/*` handlers, intercepting before the request reaches the container.

## Alternatives considered

### Decision A — how browser traffic reaches the backend

| Option | Meets the requirement? | Backend exposure | Verdict |
|---|---|---|---|
| **BFF proxy through Next.js** | ✅ every hop is private DNS | none | **Chosen** |
| ALB path-routing `/api/*` | ❌ browser uses the public ALB | full HTTP surface | Rejected |

The honest cost of the BFF: the frontend fleet now carries API throughput as well
as rendering, and streaming correctness becomes its problem (see below).

### Decision B — what the private name resolves to

| Option | Cost | Why not chosen |
|---|---|---|
| **Internal ALB + private hosted zone** | ~$20/mo | **Chosen** |
| AWS Cloud Map | free | Resolves to **task IPs**, and undici (Node's `fetch`) caches DNS. Replacing a backend task can leave the frontend holding a stale address — and the crash-recovery demo replaces backend tasks *on purpose*. |
| ECS Service Connect | free | Two disqualifiers: its default `perRequestTimeout` is **15 seconds** and an SSE stream never "completes", so streams die mid-flight regardless of heartbeats; and AWS states it *"doesn't use or create DNS hosted zones in Amazon Route 53"* — names are resolved by an injected Envoy sidecar, not DNS, so it does not literally satisfy "a domain name". |

Cloud Map is a legitimate answer and is explicitly named in the assignment. It was
rejected on reliability, not correctness: **$20/mo buys a stable endpoint that
survives task replacement**, and the demo depends on exactly that.

Service Connect is AWS's general recommendation for ECS in 2026, which is why it
is worth naming the specific reasons it loses here rather than ignoring it. The
timeout is fixable (`per_request_timeout_seconds = 0`); the Envoy sidecar's
configuration is not editable, and reports of it buffering token streams could not
be ruled out.

## Streaming across the hops

SSE must survive three hops — public ALB → Next.js → internal ALB → backend — and
each one can silently buffer.

| Setting | Value | Why |
|---|---|---|
| ALB `idle_timeout` | 300s | Default 60s is below a long run. Any byte resets it, so the backend's 15s heartbeat holds it open. |
| ALB `client_keep_alive` | 3600s (default) | **Hard wall-clock cap that does NOT reset with traffic** — the real ceiling on one stream. |
| `load_balancing_algorithm_type` | `round_robin` | **Not `least_outstanding_requests`**: an open stream counts as outstanding for its whole life, so LOR starves tasks. |
| `deregistration_delay` | 180s | ECS waits this out *before* SIGTERM, so it — not `stopTimeout` — is the in-flight budget. |

At the proxy, response headers are **allowlisted, never spread**. Undici
transparently decompresses the body but leaves a stale `content-encoding: gzip`
and the original `content-length` on the headers object; copying those onto an
already-decoded stream gives the browser `ERR_CONTENT_DECODING_FAILED` or a silent
truncation with nothing logged server-side.

Verified locally with `curl -N`, which is the only way to check this — a browser
hides the framing:

```
content-type: text/event-stream; charset=utf-8
cache-control: no-cache, no-store, no-transform, must-revalidate
Transfer-Encoding: chunked
```

The **absent** headers are the evidence: no `content-length` alongside chunked
encoding proves the response is not buffered; no `content-encoding` proves the
allowlist worked.

> Note: the widespread claim that "ALB buffers SSE" comes from ALB with **Lambda**
> targets, which is a genuinely buffered path. IP targets on Fargate stream fine.
> `X-Accel-Buffering: no` is an nginx directive the ALB ignores; it is set anyway
> as insurance for any future hop.

## Egress and cost

- **One NAT gateway, not one per AZ.** ~$35/mo against ~$70. The tradeoff is real
  and stated: if that AZ fails, tasks in the other AZ lose outbound internet.
  Production would run one per AZ.
- **The data tier has no route to the NAT at all.** RDS needs no outbound
  internet, and not granting a route is cheaper and stronger than writing a rule
  to deny one.
- **S3 gateway endpoint — free, and the highest-value line in the stack.** ECR
  serves image layers from S3, so without it every image pull is billed as NAT
  data processing at $0.045/GB.
- **Interface endpoints were evaluated and rejected.** At ~$7.30/mo each per AZ,
  the four needed (`ecr.api`, `ecr.dkr`, `logs`, `secretsmanager`) across two AZs
  is ~$58/mo against ~$35 for one NAT. They only win once NAT *data processing*
  dominates, which needs sustained high egress.

## What this design does not do

- **No HTTPS on the public ALB.** A demo has no registered domain to validate a
  certificate against. The listener block exists to show where TLS terminates; a
  real deployment adds an ACM certificate and redirects `:80 → :443`.
- **No WAF**, no Shield Advanced. Both are appropriate for a genuinely
  internet-facing operations tool and both cost money a demo does not justify.
- **Single region.** Multi-region is a different exercise.
