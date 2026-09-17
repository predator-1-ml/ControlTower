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
  description = "Inference profile id. The prefix is REGIONAL: ap-southeast-1 needs global., not us."
  type        = string
  default     = "global.anthropic.claude-sonnet-4-6"
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
