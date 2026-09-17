variable "name" {
  description = "Load balancer and target group name"
  type        = string
}

variable "internal" {
  description = "true for the backend's private ALB, false for the public one"
  type        = bool
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  description = "Public subnets for the public ALB; private app subnets for the internal one"
  type        = list(string)
}

variable "security_group_id" {
  type = string
}

variable "target_port" {
  description = "Container port this ALB forwards to"
  type        = number
}

variable "health_check_path" {
  type    = string
  default = "/health"
}

variable "idle_timeout" {
  description = "Seconds. Must exceed the longest expected streaming response."
  type        = number
  default     = 300
}

variable "deregistration_delay" {
  description = "Seconds ECS waits before SIGTERM. The real in-flight budget."
  type        = number
  default     = 180
}

variable "certificate_arn" {
  description = "ACM certificate. Null means HTTP only, which is the demo posture."
  type        = string
  default     = null
}

variable "tags" {
  type    = map(string)
  default = {}
}
