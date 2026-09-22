# ECS cluster, log groups, and the two IAM roles every task needs.
#
# The role split is the part worth understanding, because getting it wrong is how
# secrets end up reachable from application code:
#
#   execution role — used by the ECS AGENT, before the container starts. Pulls
#                    the image and writes logs. The container never holds these
#                    permissions.
#   task role      — used by the APPLICATION at runtime. This is what a compromised
#                    container could use, so it gets the narrowest grant possible.
#                    Only the backend is given one.

resource "aws_ecs_cluster" "this" {
  name = var.name

  setting {
    name  = "containerInsights"
    value = var.container_insights ? "enabled" : "disabled"
  }

  tags = var.tags
}

resource "aws_cloudwatch_log_group" "services" {
  for_each = toset(var.services)

  name              = "/ecs/${var.name}/${each.value}"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

# ------------------------------------------------------------ execution role

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${var.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# No Secrets Manager grant on the execution role. It would only be needed to
# inject secrets through the task definition, and nothing is injected: the one
# secret (the rotating RDS password) is read by the application under the task
# role below.

# ----------------------------------------------------------------- task role

resource "aws_iam_role" "task" {
  name               = "${var.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = var.tags
}

# What the application itself may do. Two things only:
#
#  - invoke Bedrock models, scoped to inference profiles and foundation models
#    rather than "*"
#  - read the RDS-managed password at connect time, which is mandatory because a
#    rotating secret cannot be injected as an environment variable
data "aws_iam_policy_document" "task" {
  statement {
    sid       = "InvokeBedrockModels"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = var.bedrock_model_arns
  }

  dynamic "statement" {
    for_each = length(var.secret_arns) == 0 ? [] : [1]
    content {
      sid       = "ReadRotatingSecrets"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = var.secret_arns
    }
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "${var.name}-task"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}
