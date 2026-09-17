variable "name" {
  description = "Prefix for repository names, e.g. control-tower-dev"
  type        = string
}

variable "repositories" {
  description = "Service names to create repositories for"
  type        = list(string)
  default     = ["backend", "frontend"]
}

variable "force_delete" {
  description = "Allow terraform destroy to remove repositories that still hold images. Dev only."
  type        = bool
  default     = false
}

variable "tags" {
  type    = map(string)
  default = {}
}
