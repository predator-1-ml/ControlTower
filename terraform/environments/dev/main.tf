# Dev environment. The only environment that exists, deliberately: one applied
# environment plus a documented promotion path is more defensible than a prod/
# directory that has never been run.
#
# Deploy order matters and is enforced by dependencies below:
#   networking -> rds + ecr -> cluster -> internal alb -> backend -> frontend
#
# Backend before frontend, because the frontend resolves api.internal at request
# time and a missing target produces a confusing 502 rather than an obvious
# failure.

locals {
  name = "${var.project}-${var.environment}"

  tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
  }

  # The private DNS name the frontend uses to reach the backend. THIS is the
  # assignment's "communicate privately using a domain name" requirement, and the
  # reason there is an internal ALB at all.
  backend_host = "api.${var.internal_domain}"
}

data "aws_caller_identity" "current" {}

# --------------------------------------------------------------- networking

module "networking" {
  source = "../../modules/networking"

  name          = local.name
  region        = var.region
  vpc_cidr      = var.vpc_cidr
  frontend_port = var.frontend_port
  backend_port  = var.backend_port
  tags          = local.tags
}

# ---------------------------------------------------------------- registries

module "ecr" {
  source = "../../modules/ecr"

  name         = local.name
  repositories = ["backend", "frontend"]
  force_delete = true # dev: let destroy clean up without emptying repos first
  tags         = local.tags
}

# --------------------------------------------------------------------- data

module "rds" {
  source = "../../modules/rds"

  name              = local.name
  subnet_ids        = module.networking.private_data_subnet_ids
  security_group_id = module.networking.rds_sg_id
  engine_version    = var.postgres_version
  instance_class    = var.db_instance_class
  tags              = local.tags
}

# ------------------------------------------------------------------- compute

module "cluster" {
  source = "../../modules/ecs-cluster"

  name        = local.name
  services    = ["backend", "frontend"]
  secret_arns = [module.rds.master_user_secret_arn]

  # Scoped to inference profiles and foundation models rather than "*".
  # Both are needed: the Converse API resolves an inference profile, which in
  # turn invokes the underlying foundation model, and a policy naming only one
  # of them fails with an opaque AccessDenied.
  bedrock_model_arns = [
    "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:inference-profile/*",
    "arn:aws:bedrock:*::foundation-model/*",
  ]

  tags = local.tags
}

# ---------------------------------------------------------- load balancing

module "alb_public" {
  source = "../../modules/alb"

  name              = "${local.name}-public"
  internal          = false
  vpc_id            = module.networking.vpc_id
  subnet_ids        = module.networking.public_subnet_ids
  security_group_id = module.networking.alb_public_sg_id
  target_port       = var.frontend_port
  health_check_path = "/"
  tags              = local.tags
}

module "alb_internal" {
  source = "../../modules/alb"

  name              = "${local.name}-internal"
  internal          = true
  vpc_id            = module.networking.vpc_id
  subnet_ids        = module.networking.private_app_subnet_ids
  security_group_id = module.networking.alb_internal_sg_id
  target_port       = var.backend_port
  health_check_path = "/health"
  tags              = local.tags
}

# ------------------------------------------------- private DNS (ADR-001b)

# A private hosted zone resolves only inside its associated VPC. Combined with
# the internal ALB it gives a real, stable DNS name for the backend.
#
# Why not Cloud Map, which is free and explicitly named in the assignment: Cloud
# Map resolves to task IPs, and undici (Node's fetch) caches DNS — so replacing
# a backend task can leave the frontend holding a stale address. The
# crash-recovery demo replaces backend tasks on purpose, so a stable endpoint is
# worth ~$20/mo. Documented in docs/networking.md as the tradeoff it is.
resource "aws_route53_zone" "internal" {
  name = var.internal_domain

  # The presence of this block is what makes the zone private.
  vpc {
    vpc_id = module.networking.vpc_id
  }

  tags = local.tags
}

resource "aws_route53_record" "backend" {
  zone_id = aws_route53_zone.internal.zone_id
  name    = local.backend_host
  type    = "A"

  alias {
    name                   = module.alb_internal.dns_name
    zone_id                = module.alb_internal.zone_id
    evaluate_target_health = true
  }
}

# ------------------------------------------------------------------ services

module "backend" {
  source = "../../modules/ecs-service"

  name               = "${local.name}-backend"
  region             = var.region
  cluster_id         = module.cluster.cluster_id
  image              = var.backend_image
  container_port     = var.backend_port
  subnet_ids         = module.networking.private_app_subnet_ids
  security_group_id  = module.networking.backend_sg_id
  target_group_arn   = module.alb_internal.target_group_arn
  execution_role_arn = module.cluster.execution_role_arn
  task_role_arn      = module.cluster.task_role_arn
  log_group_name     = module.cluster.log_group_names["backend"]

  environment = {
    APP_ENV   = var.environment
    LOG_LEVEL = "INFO"

    LLM_PROVIDER       = "bedrock"
    AWS_REGION         = var.region
    BEDROCK_MODEL_ID   = var.bedrock_model_id
    EMBEDDING_MODEL_ID = var.embedding_model_id

    # Host and secret ARN, not a connection string: the password rotates every
    # 7 days and the application fetches it at connect time.
    DB_HOST       = module.rds.address
    DB_PORT       = tostring(module.rds.port)
    DB_NAME       = module.rds.database_name
    DB_SECRET_ARN = module.rds.master_user_secret_arn
  }

  # Deliberately empty. The only secret here is the DB password, and an injected
  # secret is not refreshed when it rotates — it would work for a week and then
  # fail. See modules/ecs-service/main.tf.
  secrets = {}

  health_check_command = [
    "CMD-SHELL",
    "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:${var.backend_port}/health').status==200 else 1)\"",
  ]

  tags = local.tags
}

module "frontend" {
  source = "../../modules/ecs-service"

  name               = "${local.name}-frontend"
  region             = var.region
  cluster_id         = module.cluster.cluster_id
  image              = var.frontend_image
  container_port     = var.frontend_port
  subnet_ids         = module.networking.private_app_subnet_ids
  security_group_id  = module.networking.frontend_sg_id
  target_group_arn   = module.alb_public.target_group_arn
  execution_role_arn = module.cluster.execution_role_arn
  task_role_arn      = module.cluster.task_role_arn
  log_group_name     = module.cluster.log_group_names["frontend"]

  environment = {
    NODE_ENV = "production"

    # The private domain name. Read at REQUEST time by the BFF route handler, so
    # the same image runs unchanged in every environment. There is deliberately
    # no NEXT_PUBLIC_API_URL: that would be inlined into the browser bundle at
    # build time and require a publicly reachable backend (ADR-001).
    BACKEND_INTERNAL_URL = "http://${local.backend_host}:${var.backend_port}"
  }

  health_check_command = [
    "CMD-SHELL",
    "node -e \"fetch('http://127.0.0.1:${var.frontend_port}/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))\"",
  ]

  # Explicit: the frontend resolving api.internal before the backend has targets
  # produces a 502 that looks like a code fault rather than an ordering one.
  depends_on = [module.backend, aws_route53_record.backend]

  tags = local.tags
}

# ----------------------------------------------------------- CI/CD identity

module "github_oidc" {
  source = "../../modules/github-oidc"

  name       = local.name
  owner      = var.github_owner
  repository = var.github_repository

  # Numeric ids for the immutable subject claim. This repository was created
  # after 2026-07-15, so GitHub mints the new claim format and a trust policy
  # written only against the classic form will not match.
  owner_id      = var.github_owner_id
  repository_id = var.github_repository_id

  environments        = [var.environment]
  ecr_repository_arns = values(module.ecr.repository_arns)
  task_role_arns      = [module.cluster.execution_role_arn, module.cluster.task_role_arn]
  state_bucket_arn    = "arn:aws:s3:::${var.state_bucket}"

  tags = local.tags
}

# ------------------------------------------------------------ cost guardrail

# Created FIRST in dependency terms — it is the cheapest thing in the stack and
# the only one that protects you from the rest. The real risk here is not any
# single resource; it is forgetting to run `terraform destroy`.
module "budget" {
  source = "../../modules/budget"

  name               = local.name
  notification_email = var.budget_email
  monthly_limit_usd  = var.budget_limit_usd
}
