# From a laptop to a verified deployment

The record of the first real deployment, 2026-09-23. `terraform.md` and
`ci-cd.md` say how the pieces are meant to work; this is what happened when they
were followed for the first time, including the four things that broke and how
each was pinned down. It is kept because the assignment asks that every step be
explainable, and "what went wrong on the first apply" is the question that
actually gets asked.

Times are UTC. Account `187880375508`, region `ap-southeast-1`.

## 0. Starting point

The repository had been built and tested locally only. The AWS account held the
default VPC and nothing else: no state bucket, no OIDC provider, no roles. Every
`terraform` workflow run since the 17th had failed in seven seconds at the
credentials step, because the roles it tries to assume did not exist yet. That
is the bootstrapping order in `ci-cd.md` stated as a symptom: CI cannot plan
until a human has applied once.

## 1. Local checks first

Nothing is deployed that has not passed locally.

```bash
make lint                # ruff: clean
make test                # 70 passed, 24 skipped (the database suite)
make up && make migrate && make seed
make test-db             # 94 passed
make verify-resume       # paused in process 1, resumed and completed in process 2
```

`verify-resume` is the one that matters after touching `graph/` or `db/`: it is
the only check that proves state survives a process that no longer exists.

## 2. Bootstrap: the state bucket

```bash
cd terraform/bootstrap && terraform init && terraform apply
```

**It failed.** `CreateBucket` returned `409 BucketAlreadyExists` for
`control-tower-tfstate` even though this account owns no buckets: S3 names are
one namespace shared by every AWS account, and someone else has that one. The
bucket is now `control-tower-tfstate-187880375508`; the account-id suffix is the
conventional way to make a name unique without inventing one. Four files
referenced the name (`bootstrap/main.tf`, `dev/backend.tf`, `dev/variables.tf`,
`docs/terraform.md`) and the reason is recorded at the bootstrap variable.

Bootstrap state stays local and gitignored, as `terraform.md` explains.

## 3. Applying the dev environment

The two operator secrets have no default and cannot sit in the committed
tfvars, so they come from the environment:

```bash
export TF_VAR_operator_password=$(openssl rand -base64 18 | tr -d '/+=')
export TF_VAR_session_secret=$(openssl rand -base64 32)
cd terraform/environments/dev
terraform init            # against the S3 backend, with use_lockfile
terraform plan -out=tfplan
terraform apply tfplan
```

Plan: **69 to add, 0 to change, 0 to destroy.** The apply took about seven
minutes; RDS is the long pole. Delete the `tfplan` file afterwards: it holds
resource attributes, including the two secrets, in cleartext.

At this point the ECS services exist but run the busybox placeholder from
`terraform.tfvars`, fail their health checks, and the public ALB answers 503.
That is by design: CI owns which image runs, and nothing has been pushed yet.

### The manual steps Terraform cannot do

Terraform creates the roles; GitHub has to be told about them. Everything below
was done with `gh` so it is reproducible.

| Setting | Value | Command |
|---|---|---|
| `AWS_DEPLOY_ROLE_ARN` | `terraform output -raw github_deploy_role_arn` | `gh variable set` |
| `AWS_TF_PLAN_ROLE_ARN` | `… github_terraform_plan_role_arn` | `gh variable set` |
| `AWS_TF_APPLY_ROLE_ARN` | `… github_terraform_apply_role_arn` | `gh variable set` |
| `PRIVATE_SUBNET_IDS` | `… private_subnet_ids`, comma-joined | `gh variable set` |
| `BACKEND_SG_ID` | `… backend_security_group_id` | `gh variable set` |
| `OPERATOR_PASSWORD` | the `TF_VAR` above | `gh secret set` |
| `SESSION_SECRET` | the `TF_VAR` above | `gh secret set` |

And the environment gate, which `ci-cd.md` calls the actual security control:

```bash
# required reviewer on the dev environment, deployments only from main
gh api -X PUT repos/OWNER/REPO/environments/dev \
  --input - <<'EOF'
{"reviewers":[{"type":"User","id":<your user id>}],
 "deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}
EOF
gh api -X POST repos/OWNER/REPO/environments/dev/deployment-branch-policies \
  -f name=main -f type=branch
```

Until that rule exists, the `environment: dev` line on the apply and deploy jobs
is a label. With it, the OIDC subject claim the roles trust is only minted
after a person approves.

## 4. The first green pipeline

Pushing the branch and opening a PR ran the `terraform` workflow's plan job
through the read-only role: **"No changes. Your infrastructure matches the
configuration."** That line is the proof that the state in S3, the code in the
PR, and the account agree.

Merging to `main` triggered `deploy-backend` and `deploy-frontend`. Both paused
at the `dev` gate, were approved, built an image tagged by commit SHA, pushed it
to ECR, registered a task-definition revision by **digest**, and rolled the
service. The backend deploy ran migrations as a one-off task on the new
revision first; its log shows `0001_init`, `0002_embedding_model`, and the
checkpointer setup, then the service booted with
`provider=bedrock, embeddings=cohere.embed-english-v3`.

Sign-in through the public ALB worked on the first try.

## 5. What live traffic found

Every one of these passed every local run, every unit test, and CI. They only
exist where the network is real.

| # | Symptom | Cause | Pinned by | Fix |
|---|---|---|---|---|
| 1 | Every `/bff/*` call returned 502 `backend_unreachable: fetch failed`. DNS resolved; the backend target was healthy. | The internal ALB listener was fixed at port 80, while the frontend is handed `http://api.control-tower.internal:8000` and the frontend-to-ALB security-group rule only opens 8000. Connection refused one hop short of the backend. | `describe-listeners` showed 80; `describe-target-health` showed the backend healthy on 8000. | The ALB module takes `listener_port`; the internal ALB sets it to the backend port. PR #2. |
| 2 | Frontend tasks cycled; the public target group reported `Target.ResponseCodeMismatch`. The site still served, because an ALB with no healthy targets fails open. | The health check hit `/`, which since operator sign-in answers `307` to `/sign-in`; the matcher accepts only 200. | ECS service events: `Health checks failed with these codes: [307]`. | Health check path `/sign-in`, the one page that returns 200 with no session. PR #2. |
| 3 | The CI run for fix 1 planned `2 to change` and then **skipped** the apply job while showing green. | `hashicorp/setup-terraform` installs a wrapper that always exits 0 and exposes the real code only as its own step output, so the workflow's `$?` never saw the `-detailed-exitcode` value of 2. | The plan step log showed `Plan: 0 to add, 2 to change`; the `Upload plan` step was skipped. | `terraform_wrapper: false` on both setup steps. PR #3. This is the first apply that ran through CI. |
| 4 | With 1 and 2 fixed, ECS still stopped every frontend task: `Task failed container health checks`, while the ALB saw them healthy. | Fargate injects `HOSTNAME=<task hostname>` into the container and it overrides the Dockerfile's `HOSTNAME=0.0.0.0`. Next's standalone server binds to `HOSTNAME`, so it listened only on the task IP. The ALB targets that IP; the container check on `127.0.0.1` got `ECONNREFUSED`. | The ECS log banner read `Local: http://ip-10-0-11-152…:3000` where a local run prints `localhost`. Reproduced by pulling the deployed image and running it with `-e HOSTNAME=ip-10-0-11-152`: same check, `ECONNREFUSED`. | `HOSTNAME=0.0.0.0` in the frontend task environment. PR #4, and a CLAUDE.md gotcha. |

| 5 | Sign-in succeeded, then the workspace showed "This page couldn't load". | `crypto.randomUUID()` exists only in a secure context (https or localhost). The demo ALB is plain http, so the first render threw `crypto.randomUUID is not a function`. localhost counts as secure, so every local run passed. | Browser console on the deployed site, on the first end-to-end run. | The session id falls back to `getRandomValues`, which has no such restriction. PR #6. |

Fault 3 deserves a sentence: a deployment pipeline that reports success while
doing nothing is worse than one that fails, and the only reason it was caught
is that the change it skipped was being watched for.

Fault 5 is the argument for running the demo against the deployed URL rather
than localhost before recording anything: it cannot occur locally.

After PR #4 and a `deploy-frontend` dispatch, the frontend rollout reached
`COMPLETED` on revision 5 with the task `HEALTHY` and zero failed tasks.

## 6. Seeding the deployed database

The migrations gave RDS a schema and nothing else. Locally the seed goes in with
`psql`; RDS is private, and the backend image did not carry the fixture, so
there was no way to load it. The seed now lives in the backend build context,
ships in the image, and loads through `app.scripts.seed`, a sibling of the
migrate script, so it can run the same way migrations do: as a one-off task on
the backend image (PR #5).

```bash
CLUSTER=control-tower-dev
SUBNETS=$(gh variable get PRIVATE_SUBNET_IDS)
SG=$(gh variable get BACKEND_SG_ID)

run_backend_task() {   # $1 = module to run
  aws ecs run-task --cluster "$CLUSTER" --task-definition control-tower-dev-backend \
    --launch-type FARGATE \
    --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=DISABLED}" \
    --overrides '{"containerOverrides":[{"name":"control-tower-dev-backend","command":["python","-m","'"$1"'"]}]}' \
    --query 'tasks[0].taskArn' --output text
}

T=$(run_backend_task app.scripts.seed);   aws ecs wait tasks-stopped --cluster $CLUSTER --tasks $T
T=$(run_backend_task app.scripts.ingest); aws ecs wait tasks-stopped --cluster $CLUSTER --tasks $T
```

Check the exit code of each with `describe-tasks`, exactly as the deploy
workflow does for migrations: `wait tasks-stopped` returns for a failed task
too. The seed truncates `knowledge_chunks`, so ingest must follow it or the
knowledge workflow answers "No internal policy document covers that question",
which reads as a broken demo rather than an unembedded corpus.

## 7. End-to-end verification

Driven in a real browser against the public URL on 2026-09-23, after the seed
and ingest tasks. This is `docs/demo.md` executed on the deployed stack, not
on localhost, and it is what found fault 5.

| Beat | What was done | What was observed |
|---|---|---|
| Sign-in | `operator` and the generated password | 303 to `/`, workspace rendered, status `Ready` |
| 1. Planning before execution | "Onboard CUST-1001 and check whether they already have an active claim." | Plan of two tasks appeared before either ran; `t1 onboarding` then `t2 claims` moved pending, running, done in that order. Answer: sent to manual review because of active claim CLM-5001. Session card: `Customer CUST-1001 Priya Raman · Claims CLM-5001`. |
| 2a. Context across a switch | "Do they have any other open claims?" with no customer named | `t3 claims` appended to the same plan; answer described CLM-5001 (motor, 4,820.00, open). The customer came from state, not from the message. |
| 2b. Third workflow | "When does a motor claim need a second review?" | `t4 knowledge`; answer cited `[claims-handling-policy.md, Motor claims]`, from the five chunks embedded by the ingest task through Bedrock. |
| 2c. A workflow that pauses | "Summarise claim CLM-5003." | Status `Waiting on you`; the composer became "Answer to resume claims"; the question named `incident report · police reference`. |
| 2d. The reply is read | "Incident report IR-2291, police ref PR-77431" | Answer: status under review, escalate to the duty manager because 12,750 exceeds 10,000, cited `[operations-runbook.md, Escalation]`. A one-off task queried RDS: `CLM-5003 status=under_review, missing_fields=[]`, and one `audit_events` row containing `PR-77431`. |
| 3. Crash mid-workflow | New session; "Onboard CUST-1002"; paused on photo ID and proof of address. Then `aws ecs stop-task` on the only backend task. | ECS replaced the task in about a minute; the internal ALB drained the old target and marked the new one healthy; the private DNS name never changed. After a page refresh the same session id restored the paused question from Postgres. Answering it completed the workflow: application submitted for Daniel Okafor. |

Every one of those turns went browser, public ALB, Next.js, BFF route handler,
`api.control-tower.internal:8000`, internal ALB, FastAPI, Bedrock and RDS. The
backend was never reachable from outside the VPC at any point.

Screenshots of each state are in `.playwright-mcp/e2e/` locally (gitignored).

## 8. Teardown and cost

The stack costs roughly $4 a day (NAT gateway, two ALBs, `db.t4g.micro`, two
Fargate tasks). The $20 monthly budget alert exists; AWS emails a confirmation
that has to be accepted before it fires.

```bash
cd terraform/environments/dev && terraform destroy
```

`force_delete` on ECR and `skip_final_snapshot` on RDS let that complete without
manual cleanup. The state bucket stays, by design.

## Appendix: the whole path, in order

```bash
# local
make setup && make up && make migrate && make seed && make test-db && make verify-resume
# bootstrap and apply (once, by a human)
cd terraform/bootstrap && terraform init && terraform apply
cd ../environments/dev && terraform init && terraform apply
terraform output
# tell GitHub about the roles (section 3), then
git push && gh pr create ...        # plan runs read-only on the PR
gh pr merge ...                     # deploys and apply wait at the dev gate
# seed the deployed database (section 6), then drive the demo (docs/demo.md)
# afterwards
terraform destroy
```
