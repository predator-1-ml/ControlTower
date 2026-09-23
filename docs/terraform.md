# Terraform Structure

```
terraform/
├── bootstrap/                 state bucket — run once, state kept local
├── modules/
│   ├── networking/            VPC, 3 subnet tiers, SG chain, NAT, S3 endpoint
│   ├── ecr/                   immutable-tag registries
│   ├── alb/                   used twice: public and internal
│   ├── rds/                   PostgreSQL + managed password
│   ├── ecs-cluster/           cluster, log groups, the two IAM roles
│   ├── ecs-service/           used twice: backend and frontend
│   └── github-oidc/           CI roles, no long-lived keys
└── environments/
    └── dev/                   composes all of the above
```

## The bootstrap chicken-and-egg

Every stack keeps state in S3, but the bucket must exist before a backend can
point at it. So `bootstrap/` keeps its own state **locally**, on the machine that
ran it (`*.tfstate` is gitignored, so it is not in the repository).

That is normally a smell. It is acceptable here because the stack is ~20 lines,
changes essentially never, and losing its state costs one `terraform import`.

```bash
cd terraform/bootstrap && terraform init && terraform apply
cd ../environments/dev && terraform init && terraform apply
```

## State

```hcl
backend "s3" {
  bucket       = "control-tower-tfstate-187880375508"
  key          = "dev/terraform.tfstate"
  region       = "ap-southeast-1"
  encrypt      = true
  use_lockfile = true      # S3 native locking — NO DynamoDB table
}
```

**`use_lockfile` replaced DynamoDB-based locking**, which is deprecated and slated
for removal. Most tutorials still show a lock table; it is no longer needed and is
one less resource to create, pay for, and explain.

The bucket is versioned and encrypted, with `prevent_destroy`. State is the source
of truth for what the account contains — losing it is worse than losing the
infrastructure, because Terraform then no longer knows what it owns.

## Directories, not workspaces

Workspaces share one backend key and one configuration, so environments can differ
only by variable value. That is exactly wrong when prod needs Multi-AZ RDS,
deletion protection, more tasks, and a different blast radius.

Separate directories give separate state files, separate IAM roles, and a plan
diff that cannot accidentally target the wrong environment.

Terragrunt earns its keep at many-environments-many-accounts scale. For two
environments it is ceremony.

## Only `dev` exists

Deliberately. **One applied environment plus a documented promotion path is more
defensible than a `prod/` directory that has never been run** — an unapplied
environment is decoration that has never been proven to work.

Promotion would be: copy `environments/dev`, change the state key, and flip the
variables that encode the dev-vs-prod tradeoffs:

| Variable | dev | prod |
|---|---|---|
| `multi_az` | `false` | `true` |
| `deletion_protection` | `false` | `true` |
| `skip_final_snapshot` | `true` | `false` |
| `backup_retention_period` | 1 | 7+ |
| NAT gateways | 1 | one per AZ |
| `desired_count` | 1 | ≥2 |
| `force_delete` (ECR) | `true` | `false` |
| `enable_deletion_protection` (ALB) | `false` | `true` |

Every one of those is a cost-versus-resilience decision made deliberately for a
demo, not an oversight.

## Module conventions

- Each module has `main.tf`, `variables.tf`, `outputs.tf`, `versions.tf`.
- Security groups are exported **individually**, not as a map — wiring the wrong
  tier together then fails at plan time with an unknown attribute, rather than at
  runtime with a connection timeout.
- Security group rules are **separate resources**, never inline `ingress`/`egress`
  blocks. Inline blocks take exclusive ownership of a group's rules, so mixing the
  two styles makes Terraform delete rules it did not create on every apply.
- No `profile` in the provider block. Locally it comes from `AWS_PROFILE`; in CI
  authentication is OIDC and there is no profile at all. Hardcoding one breaks CI.

## Provider version

`hashicorp/aws ~> 6.0`, resolving to 6.65.0. v6's headline breaking change was
region handling becoming a provider-level argument — read the v6 upgrade guide
before widening the constraint.

`required_version = ">= 1.11"` because write-only arguments landed in 1.11.

## Running it

```bash
export AWS_PROFILE=Nyomad

cd terraform/environments/dev
terraform init
terraform plan          # review before every apply
terraform apply

terraform output        # role ARNs and subnet ids for the CI variables
```

To tear down after recording the demo:

```bash
terraform destroy
```

The state bucket in `bootstrap/` is deliberately NOT part of that: it has
`prevent_destroy` and versioning, so `terraform destroy` there fails by design.
An empty versioned bucket costs nothing; to remove it, empty every object version
in the console, drop `prevent_destroy`, then destroy.

`force_delete = true` on ECR and `skip_final_snapshot = true` on RDS exist so
`destroy` completes without manual cleanup or a snapshot that keeps billing.

## Validation

All eight stacks pass `terraform validate` and `terraform fmt -check`:

```bash
terraform fmt -check -recursive terraform/
cd terraform/environments/dev && terraform init -backend=false && terraform validate
```

CI enforces both, and `plan` runs with `-detailed-exitcode` so a failure to
produce a plan is distinguishable from a plan with no changes.
