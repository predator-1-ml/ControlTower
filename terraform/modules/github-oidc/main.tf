# GitHub Actions authentication via OIDC. No long-lived access keys anywhere.
#
# Two roles, because a workflow that only needs to read should not be able to
# write: `plan` runs on every pull request including from forks, `apply` runs
# only behind an environment gate.

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
  subjects = flatten([
    for env in var.environments : [
      "repo:${var.owner}/${var.repository}:environment:${env}",
      "repo:${var.owner}@${var.owner_id}/${var.repository}@${var.repository_id}:environment:${env}",
    ]
  ])
}

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.provider_arn]
    }

    # Scoped to `environment:`, not a branch ref. The environment subject is only
    # minted when the job declares `environment:`, which makes GitHub's
    # required-reviewer approval a precondition for the CREDENTIAL EXISTING —
    # not merely a UI gate someone can skip.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = local.subjects
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
  assume_role_policy = data.aws_iam_policy_document.assume.json
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
  assume_role_policy = data.aws_iam_policy_document.assume.json
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
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "terraform_apply" {
  role       = aws_iam_role.terraform_apply.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}
