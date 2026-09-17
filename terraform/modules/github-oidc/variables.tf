variable "name" {
  type = string
}

variable "owner" {
  description = "GitHub owner login"
  type        = string
}

variable "repository" {
  description = "Repository name"
  type        = string
}

variable "owner_id" {
  description = <<-EOT
    Numeric GitHub owner id, for the immutable subject claim.
    Find with: gh api repos/OWNER/REPO --jq .owner.id
  EOT
  type        = string
}

variable "repository_id" {
  description = <<-EOT
    Numeric GitHub repository id, for the immutable subject claim.
    Find with: gh api repos/OWNER/REPO --jq .id
  EOT
  type        = string
}

variable "environments" {
  description = "GitHub Environments allowed to assume these roles"
  type        = list(string)
  default     = ["dev"]
}

variable "create_provider" {
  description = "false if the account already has a GitHub OIDC provider (only one is permitted per account)"
  type        = bool
  default     = true
}

variable "existing_provider_arn" {
  type    = string
  default = null
}

variable "ecr_repository_arns" {
  type    = list(string)
  default = []
}

variable "task_role_arns" {
  description = "Roles the deploy role may pass to ECS: execution and task"
  type        = list(string)
  default     = []
}

variable "state_bucket_arn" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}
