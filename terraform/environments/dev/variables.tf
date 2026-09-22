variable "project" {
  type    = string
  default = "control-tower"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "region" {
  description = "Must be a region where the account has Bedrock model access"
  type        = string
  default     = "ap-southeast-1"
}

variable "vpc_cidr" {
  type    = string
  default = "10.0.0.0/16"
}

variable "internal_domain" {
  description = "Private hosted zone. Not a real registered domain; resolves only inside the VPC."
  type        = string
  default     = "control-tower.internal"
}

variable "backend_port" {
  type    = number
  default = 8000
}

variable "frontend_port" {
  type    = number
  default = 3000
}

variable "backend_image" {
  description = "Full image ref. Placeholder until CI has pushed one; deploy by digest for deterministic rollback."
  type        = string
}

variable "frontend_image" {
  type = string
}

variable "postgres_version" {
  type    = string
  default = "17.11"
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "bedrock_model_id" {
  # Nova, not Claude: every Anthropic model on this account is gated behind an
  # account-level use-case form and fails with ResourceNotFoundException until
  # it is submitted (probed 2026-09-18; see CLAUDE.md). This value reaches the
  # task as an env var, which OVERRIDES the app's own default — so a stale id
  # here breaks chat in the deployed stack even when the code is right.
  description = "Inference profile id. The prefix is per-model: Nova is apac., Claude is global.; us. does not resolve in ap-southeast-1."
  type        = string
  default     = "apac.amazon.nova-pro-v1:0"
}

variable "embedding_model_id" {
  description = "Titan is not offered in ap-southeast-1; Cohere is. 1024 dims, matching vector(1024)."
  type        = string
  default     = "cohere.embed-english-v3"
}

variable "github_owner" {
  type    = string
  default = "predator-1-ml"
}

variable "github_repository" {
  type    = string
  default = "ControlTower"
}

variable "github_owner_id" {
  description = "gh api repos/OWNER/REPO --jq .owner.id"
  type        = string
  default     = "66988630"
}

variable "github_repository_id" {
  description = "gh api repos/OWNER/REPO --jq .id"
  type        = string
  default     = "1373064236"
}

variable "state_bucket" {
  description = "Must match the bucket in backend.tf"
  type        = string
  default     = "control-tower-tfstate"
}

variable "budget_email" {
  description = "Budget alerts go here. AWS sends a confirmation email first."
  type        = string
  default     = "ajjukrish2000@gmail.com"
}

variable "budget_limit_usd" {
  description = "Monthly ceiling. ~$20 is about four days of continuous uptime."
  type        = string
  default     = "20"
}

# ------------------------------------------------------------ operator sign-in
#
# The static operator credential the Next.js server checks (frontend/lib/session.ts).
# No defaults for the two secrets, deliberately: terraform.tfvars for dev is
# COMMITTED, so they cannot live there. Supply them through the environment —
#   export TF_VAR_operator_password=... TF_VAR_session_secret=$(openssl rand -base64 32)
# locally, and as repository secrets in CI (.github/workflows/terraform.yml).

variable "operator_username" {
  type    = string
  default = "operator"
}

variable "operator_password" {
  description = "Unset, sign-in fails closed: nobody can get in."
  type        = string
  sensitive   = true
}

variable "session_secret" {
  description = "HMAC key for the session cookie. Changing it signs everyone out."
  type        = string
  sensitive   = true
}
