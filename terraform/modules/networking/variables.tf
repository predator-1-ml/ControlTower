variable "name" {
  description = "Prefix for resource names, e.g. control-tower-dev"
  type        = string
}

variable "region" {
  description = "Region. Needed for the S3 gateway endpoint service name."
  type        = string
}

variable "vpc_cidr" {
  description = "VPC CIDR. /16 gives room for three /24 tiers across two AZs."
  type        = string
  default     = "10.0.0.0/16"
}

variable "frontend_port" {
  description = "Port the Next.js container listens on"
  type        = number
  default     = 3000
}

variable "backend_port" {
  description = "Port the FastAPI container listens on"
  type        = number
  default     = 8000
}

variable "tags" {
  description = "Tags applied to every resource in this module"
  type        = map(string)
  default     = {}
}
