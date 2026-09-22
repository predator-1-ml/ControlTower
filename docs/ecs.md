# ECS Architecture

Two Fargate services in one cluster, deployed independently as the assignment
requires.

| Service | Subnets | Reached by | Public? |
|---|---|---|---|
| `control-tower-dev-frontend` | private app | public ALB | via ALB only |
| `control-tower-dev-backend` | private app | internal ALB | **never** |

Both run ARM64/Graviton — roughly 20% cheaper for identical work. Images are built
on GitHub's `ubuntu-24.04-arm` runners (free for public repositories) rather than
under QEMU emulation on x64, which is slow enough to notice. The architecture must
match: an x64 image registers without complaint and then dies on ECS with
`exec format error`.

## The role split

This is the part that determines whether a compromised container can read your
secrets.

| Role | Used by | When | Grants |
|---|---|---|---|
| **execution** | the ECS agent | before the container starts | pull image, write logs |
| **task** (backend only) | the application | at runtime | invoke Bedrock, read ONE secret: the rotating DB password |

The usual pattern is to inject secrets through the task definition's `secrets`
block, resolved by the **execution** role so the container never touches the
secret store. That pattern is deliberately NOT used here, and the reason is
rotation: ECS resolves an injected secret once, at task start, and RDS rotates the
master password every 7 days — so it would work for a week and then fail to
authenticate. Instead the backend's **task** role may call `GetSecretValue` on
that one secret ARN, and the connection pool reads it each time it opens a
connection (`backend/app/db/checkpointer.py::conninfo`). The cost is that a
compromised backend container can read the DB password — which it could already
use, since it holds open connections.

The **frontend has no task role at all**. It calls no AWS API; giving the only
internet-facing container the backend's role would hand it Bedrock and the
database password for nothing.

Bedrock permissions are scoped to inference-profile and foundation-model ARNs
rather than `"*"`. **Both are required**: Converse resolves an inference profile,
which in turn invokes the underlying foundation model, and a policy naming only
one fails with an opaque `AccessDenied`.

## Shutdown timing, and the number that actually matters

The intuitive answer is wrong here, and it matters because LangGraph runs can be
long.

```
ECS deregisters the target
  └─ waits out deregistration_delay   (180s)   ← the real in-flight budget
       └─ sends SIGTERM
            └─ waits stopTimeout      (120s max on Fargate)
                 └─ SIGKILL
```

**`stopTimeout` caps at 120 seconds on Fargate** — the API rejects more. So the
common advice "set `stopTimeout` ≥ your drain time" is *impossible* beyond two
minutes.

The ordering saves you: ECS waits out the target group's `deregistration_delay`
**before** sending SIGTERM. That value, not `stopTimeout`, is the budget for
finishing in-flight requests.

**And for work exceeding roughly two minutes, no shutdown tuning is sufficient.**
That is the architectural argument for the checkpointer rather than a footnote
about it: the task *will* be killed mid-run eventually, so steps are idempotent,
state is durable, and the client reconnects and resumes. Designing for the task
dying is cheaper than trying to prevent it.

## Health checks — two of them, for different jobs

| Check | Gates | Endpoint |
|---|---|---|
| ALB target group | whether traffic is routed | `/health` |
| Container `healthCheck` | whether ECS considers the task healthy | backend `/health`, frontend `/` |

`/health` **deliberately does not touch the database**. If it did, a brief RDS
blip would make ECS kill every task at once — turning a recoverable dependency
wobble into an outage, and a self-inflicted one.

Fargate slim images ship no `curl`, so the usual `curl -f` healthcheck fails in a
way that looks exactly like the app being down. Both services use their own
runtime instead:

```bash
# backend
python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"
# frontend
node -e "fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
```

## Deployment safety

```hcl
deployment_circuit_breaker {
  enable   = true
  rollback = true      # both are required; enabling one alone does nothing
}
health_check_grace_period_seconds = 60
```

Without the circuit breaker a broken image sits failing health checks until
someone notices.

```hcl
lifecycle {
  ignore_changes = [task_definition, desired_count]
}
```

Terraform owns everything with a lifecycle longer than one deploy; CI owns exactly
one mutable thing — which image digest runs. Without this the next
`terraform apply` quietly reverts whatever CI last deployed.

## Logging

```hcl
"mode"            = "non-blocking"
"max-buffer-size" = "4m"
```

The default `awslogs` driver is **blocking**: if CloudWatch is slow, it applies
backpressure to the container and the application stalls. Non-blocking with a
bounded buffer trades a few dropped log lines under pressure for an application
that keeps serving — the right trade for an operations tool.

Retention is 7 days in dev. CloudWatch storage is billed, and a demo needs days.

## Migrations

A standalone `RunTask` before the service update, reusing the backend image with
an overridden command. Not at app startup (N tasks race the same migration) and
not as an init container (runs on every scale-out).

The reasoning is in [`state-management.md`](state-management.md); the workflow
mechanics are in [`ci-cd.md`](ci-cd.md).

## Sizing

| | CPU | Memory | Count |
|---|---|---|---|
| backend | 0.5 vCPU | 1 GB | 1 |
| frontend | 0.5 vCPU | 1 GB | 1 |

One task each because it is a demo. Note the honest consequence: **a single
backend task means the in-process concurrency guard on sessions is sufficient
today and would not be after scaling out** — see the known limitation in
[`state-management.md`](state-management.md).

Fargate quota on this account is 30 vCPU, so scaling is a `desired_count` change,
not a quota request.
