# CI/CD

Five workflows. Two verify, three deploy.

| Workflow | Trigger | Does |
|---|---|---|
| `backend-ci` | backend changes | ruff, migrate, seed, pytest against real Postgres+pgvector |
| `frontend-ci` | frontend changes | typecheck, production build |
| `deploy-backend` | push to main | build → ECR → register revision → **migrate on that revision** → ECS rolling update → confirm no rollback |
| `deploy-frontend` | push to main | build → ECR → ECS rolling update |
| `terraform` | terraform changes | plan on PR, apply on main behind approval |

## The Terraform / CI boundary

The one rule that keeps these from fighting each other:

> **Terraform owns everything with a lifecycle longer than one deploy.
> CI owns exactly one mutable thing: which image digest is running.**

Which is why the ECS service carries:

```hcl
lifecycle {
  ignore_changes = [task_definition, desired_count]
}
```

Without it, the next `terraform apply` reverts whatever CI last deployed — and it
does so quietly, because from Terraform's point of view it is simply restoring
the declared state.

## Authentication

OIDC. There are no AWS access keys in GitHub, and nothing to rotate or leak.

Three roles, because a workflow that only reads should not be able to write:

| Role | Used by | Permissions |
|---|---|---|
| `…-github-deploy` | deploy workflows | ECR push, ECS update, scoped `PassRole` |
| `…-github-tf-plan` | plan on PR | `ReadOnlyAccess` + state bucket |
| `…-github-tf-apply` | apply on main | `AdministratorAccess`, gated by environment |

`AdministratorAccess` on the apply role is deliberate. Terraform creates IAM, VPC,
RDS and ECS resources, and enumerating that surface produces a policy that breaks
silently the next time a resource type is added. **The control is the environment
gate, not a narrower policy** — see below.

### The environment gate is the actual security control

There are **two trust policies**, not one. The deploy and apply roles share the
*gated* policy: every deploying job declares `environment: dev`, and the policy
requires `…:sub` to equal `repo:OWNER/REPO:environment:dev`. The read-only plan
role has its own, trusting `…:pull_request` and `…:ref:refs/heads/main`, because
the plan job declares no environment — a plan that needs approval before anyone
can read it is not a review aid. Sharing one policy would fail both ways: plan
could never authenticate, and anything that could assume the read-only role could
equally assume `AdministratorAccess`.

That subject claim is **only minted when the job declares the environment**. So a
required reviewer on the `dev` environment is a precondition for the AWS
credential existing at all — not a button someone can route around. This is why
the gated trust policy is scoped to `environment:` rather than to a branch `ref:`.

**This only holds once the rule exists.** Terraform cannot configure GitHub, so it
is a manual step after the first apply: *Settings → Environments → dev →* add a
required reviewer and restrict deployment branches to `main`. Until then the
`dev` environment has no protection rules and the gate is a label, not a control.

### ⚠️ Immutable subject claims

GitHub is migrating to subject claims that embed numeric ids:

```
classic   : repo:predator-1-ml/ControlTower:environment:dev
immutable : repo:predator-1-ml@66988630/ControlTower@1373064236:environment:dev
```

Repositories created, renamed, or transferred after **2026-07-15** get the new
format automatically. **This repository was created 2026-09-16**, so it is in
scope, and a trust policy written only against the classic form will not match —
producing an `AssumeRoleWithWebIdentity` denial that reads like a permissions
problem rather than a string-matching one.

`modules/github-oidc` lists **both** forms in `StringEquals`, which is an OR of
exact values. No wildcard, so the trust surface is not widened.

If a deploy ever fails to assume the role, read the real claim before changing
anything else — add a step that prints `github.job_workflow_sha` context, or
decode the token from the runner.

## Why deploy by digest, not tag

```bash
DIGEST=$(docker inspect --format='{{index .RepoDigests 0}}' "$IMAGE")
```

A task definition that stores a *tag* cannot be rolled back deterministically:
rolling back to revision N-1 re-resolves that tag to whatever it points at **now**,
which after a bad deploy is the bad image. ECR repositories are also set to
`IMMUTABLE` tags so a tag can never be repointed in the first place.

## Migrations run before the service update

As a standalone `RunTask`, not at app startup and not as an init container.

`AsyncPostgresSaver.setup()` holds no advisory lock and cannot be wrapped in a
transaction. If several tasks boot against a stale schema, the losers crash on a
`UniqueViolation`, and concurrent `CREATE INDEX CONCURRENTLY` can leave an
`INVALID` index behind that queries silently stop using. One process, one
advisory lock, before anything else starts.

The workflow checks the container exit code explicitly:

```bash
aws ecs wait tasks-stopped ...   # returns when it stops — including on failure
EXIT=$(aws ecs describe-tasks ... --query 'tasks[0].containers[0].exitCode')
[ "$EXIT" = "0" ] || exit 1
```

`wait tasks-stopped` returns for a *failed* task too. Without the exit-code check
a broken migration deploys silently.

### Migrations run on the NEW image

`run-task` can override a container's command but not its image. So the workflow
registers the new task-definition revision first and runs the migration on
*that*. Running it on the service's current revision would execute the previous
image's migrations — a new `.sql` file would land one deploy late, after the code
that needs it is already serving.

The consequence: during the rolling update old code runs against the new schema,
so a migration must be backward compatible — add, never rename or drop, in one
deploy.

### "Stable" is not "deployed"

The service has a deployment circuit breaker with rollback. A rolled-back service
is perfectly stable, so `aws ecs wait services-stable` succeeds after a failed
deploy. The workflow's last step compares the service's live task definition with
the revision it registered and fails if they differ.

Deploys are **not** gated on the CI workflows passing; they trigger on the same
push. At this scale a bad push is caught by the circuit breaker rather than
prevented. The production answer is `workflow_run` on a green CI, or a single
pipeline.

## Concurrency, and why it differs per workflow

```yaml
# CI: newest run wins
cancel-in-progress: true

# deploy and terraform: queue, never cancel
cancel-in-progress: false
```

Cancelling an apply mid-flight can leave state describing infrastructure that does
not exist. Cancelling a deploy leaves ECS partway through a rolling update.
Neither is worth the saved minute.

## Repository variables to set

After `terraform apply`, from its outputs:

| Variable | From |
|---|---|
| `AWS_DEPLOY_ROLE_ARN` | `github_deploy_role_arn` |
| `AWS_TF_PLAN_ROLE_ARN` | `github_terraform_plan_role_arn` |
| `AWS_TF_APPLY_ROLE_ARN` | `github_terraform_apply_role_arn` |
| `PRIVATE_SUBNET_IDS` | `private_subnet_ids`, comma-separated |
| `BACKEND_SG_ID` | `backend_security_group_id` |

Repository **variables**, not secrets — none of these are sensitive, and role ARNs
are useless without the OIDC trust relationship.

## Bootstrapping order

There is a chicken-and-egg: the workflows need roles that Terraform creates, and
Terraform needs a state bucket that does not exist yet.

```bash
cd terraform/bootstrap && terraform init && terraform apply   # state bucket
cd ../environments/dev && terraform init && terraform apply   # everything else
# then set the repository variables above
```

The first `terraform apply` is run by a human. Everything after can be CI.
