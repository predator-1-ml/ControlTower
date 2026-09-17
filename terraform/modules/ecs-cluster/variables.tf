variable "name" {
  type = string
}

variable "services" {
  description = "Service names; one log group each"
  type        = list(string)
  default     = ["backend", "frontend"]
}

variable "log_retention_days" {
  description = "Short in dev: CloudWatch storage is billed and a demo needs days, not months"
  type        = number
  default     = 7
}

variable "container_insights" {
  description = "Extra per-task metrics. Costs money; off by default in dev."
  type        = bool
  default     = false
}

variable "secret_arns" {
  description = "Secrets both roles may read. Scoped, never wildcarded."
  type        = list(string)
  default     = []
}

variable "bedrock_model_arns" {
  description = "Inference profile and foundation model ARNs the task may invoke"
  type        = list(string)
  default     = []
}

variable "tags" {
  type    = map(string)
  default = {}
}
