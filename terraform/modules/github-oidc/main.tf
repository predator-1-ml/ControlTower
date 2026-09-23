# GitHub Actions authentication via OIDC. No long-lived access keys anywhere.
#
# Three roles and TWO trust policies, because who may assume a role matters as
# much as what it can do:
#
#   gated   deploy + terraform-apply. Assumable only by a job that declares
#           `environment: dev`, so the environment's required reviewer stands in
#           front of the credential.
#   plan    terraform-plan, read-only. Assumable from a pull request or from main
#           with no approval, because a plan nobody can see until someone approves
#           it is not a review aid.
#
# One shared trust policy would be wrong in both directions: the plan job declares
# no environment and could never assume its role, and anything allowed to assume
# the read-only role could equally assume AdministratorAccess.

data "aws_caller_identity" "current" {}

# The thumbprint is NOT set, and that is deliberate rather than an omission.
# AWS has validated GitHub's OIDC endpoint against its own trusted CA library
# since 2023; `thumbprint_list` is optional and is ignored for this provider.
# Most tutorials still carry a hardcoded SHA that rotates and eventually breaks.
resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_provider ? 1 : 0

  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]

  tags = var.tags
}

locals {
  provider_arn = var.create_provider ? aws_iam_openid_connect_provider.github[0].arn : var.existing_provider_arn

  # ⚠️ GitHub is migrating to IMMUTABLE SUBJECT CLAIMS, which embed numeric owner
  # and repository ids:
  #
  #   classic : repo:predator-1-ml/ControlTower:environment:dev
  #   immutable: repo:predator-1-ml@66988630/ControlTower@1373064236:environment:dev
  #
  # Repositories created, renamed, or transferred after 2026-07-15 get the new
  # format automatically. This repository was created 2026-09-16, so it is in
  # scope — and a textbook trust policy written against the classic form simply
  # will not match, producing an opaque AssumeRoleWithWebIdentity denial that
  # looks like a permissions problem rather than a string-matching one.
  #
  # Both forms are listed so the role works either way and survives the
  # transition. StringEquals with a list is an OR, and every entry is exact —
  # no wildcard, so this does not widen the trust surface.
  repo_prefixes = [
    "repo:${var.owner}/${var.repository}",
    "repo:${var.owner}@${var.owner_id}/${var.repository}@${var.repository_id}",
  ]

  # What follows the repo in the `sub` claim, per trust policy. A job that
  # declares `environment:` gets `environment:<name>` INSTEAD of its ref, which
  # is why the plan job (no environment) needs its own entries. Fork pull
  # requests are not a concern here: GitHub does not issue them an OIDC token.
  subject_suffixes = {
    gated = [for env in var.environments : "environment:${env}"]
    plan  = ["pull_request", "ref:refs/heads/main"]
  }

  subjects = {
    for trust, suffixes in local.subject_suffixes :
    trust => [for pair in setproduct(local.repo_prefixes, suffixes) : "${pair[0]}:${pair[1]}"]
  }
}

data "aws_iam_policy_document" "assume" {
  for_each = local.subjects

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.provider_arn]
    }

    # For the gated policy the subject is `environment:`, not a branch ref. That
    # subject is only minted when the job declares `environment:`, which makes
    # GitHub's required-reviewer approval a precondition for the CREDENTIAL
    # EXISTING — provided the reviewer rule is actually configured on the
    # environment in the repository settings. Terraform cannot see that; it is a
    # manual step in docs/ci-cd.md.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = each.value
    }

    # Without this, any GitHub Actions token from any repository could be
    # presented. Never omit it.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

# ------------------------------------------------------------- deploy role

# Pushes images and rolls ECS services. Cannot change infrastructure.
resource "aws_iam_role" "deploy" {
  name               = "${var.name}-github-deploy"
  assume_role_policy = data.aws_iam_policy_document.assume["gated"].json
  tags               = var.tags
}

data "aws_iam_policy_document" "deploy" {
  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # this action does not accept a resource restriction
  }

  statement {
    sid = "EcrPush"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = var.ecr_repository_arns
  }

  statement {
    sid = "DeployServices"
    actions = [
      "ecs:DescribeServices",
      "ecs:DescribeTaskDefinition",
      "ecs:DescribeTasks",
      "ecs:ListTasks",
      "ecs:RegisterTaskDefinition",
      "ecs:UpdateService",
      "ecs:RunTask",
    ]
    resources = ["*"] # several of these are not resource-scopable
  }

  # Registering a task definition means handing ECS the roles it references, so
  # the deploy role needs PassRole — narrowed to exactly those two roles and to
  # the ECS service, so it cannot pass an arbitrary privileged role.
  statement {
    sid       = "PassTaskRoles"
    actions   = ["iam:PassRole"]
    resources = var.task_role_arns
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "${var.name}-github-deploy"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}

# ---------------------------------------------------------- terraform roles

# Read-only. Used by plan-on-pull-request, which runs without approval.
resource "aws_iam_role" "terraform_plan" {
  name               = "${var.name}-github-tf-plan"
  assume_role_policy = data.aws_iam_policy_document.assume["plan"].json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "terraform_plan" {
  role       = aws_iam_role.terraform_plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

# Plan also needs to write the S3 lockfile and read/write state, which
# ReadOnlyAccess does not cover.
data "aws_iam_policy_document" "state_access" {
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
    resources = [var.state_bucket_arn, "${var.state_bucket_arn}/*"]
  }
}

resource "aws_iam_role_policy" "plan_state" {
  name   = "${var.name}-tf-state"
  role   = aws_iam_role.terraform_plan.id
  policy = data.aws_iam_policy_document.state_access.json
}

# Applies infrastructure changes. AdministratorAccess is broad and deliberately
# so — Terraform creates IAM, VPC, RDS and ECS resources, and enumerating that
# surface produces a policy that silently breaks on the next resource added.
# The control is the environment gate on the assume-role condition, not a
# narrower policy: this role cannot be assumed without an approved deployment.
resource "aws_iam_role" "terraform_apply" {
  name               = "${var.name}-github-tf-apply"
  assume_role_policy = data.aws_iam_policy_document.assume["gated"].json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "terraform_apply" {
  role       = aws_iam_role.terraform_apply.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}
