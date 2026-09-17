variable "name" {
  description = "Service name; also the container name and task family"
  type        = string
}

variable "region" {
  description = "Needed by the awslogs driver"
  type        = string
}

variable "cluster_id" {
  type = string
}

variable "image" {
  description = "Full image reference. Prefer a digest over a tag so a rollback is deterministic."
  type        = string
}

variable "container_port" {
  type = number
}

variable "cpu" {
  description = "Fargate CPU units. 512 = 0.5 vCPU."
  type        = number
  default     = 512
}

variable "memory" {
  description = "MiB. Must be a valid pairing with cpu or the task definition is rejected."
  type        = number
  default     = 1024
}

variable "cpu_architecture" {
  type    = string
  default = "ARM64"
}

variable "desired_count" {
  type    = number
  default = 1
}

variable "subnet_ids" {
  description = "Private APP subnets"
  type        = list(string)
}

variable "security_group_id" {
  type = string
}

variable "target_group_arn" {
  type = string
}

variable "execution_role_arn" {
  type = string
}

variable "task_role_arn" {
  type = string
}

variable "log_group_name" {
  type = string
}

variable "environment" {
  description = "Plain environment variables. Nothing sensitive."
  type        = map(string)
  default     = {}
}

variable "secrets" {
  description = "name -> Secrets Manager ARN. Non-rotating secrets only; see main.tf."
  type        = map(string)
  default     = {}
}

variable "health_check_command" {
  description = "Container healthcheck. Fargate slim images have no curl."
  type        = list(string)
  default     = ["CMD-SHELL", "exit 0"]
}

variable "health_check_grace_period" {
  type    = number
  default = 60
}

variable "tags" {
  type    = map(string)
  default = {}
}
