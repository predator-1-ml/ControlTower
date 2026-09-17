variable "name" {
  type = string
}

variable "subnet_ids" {
  description = "Private DATA subnets. These have no route to the NAT gateway."
  type        = list(string)
}

variable "security_group_id" {
  description = "Accepts 5432 only from the backend task security group"
  type        = string
}

variable "engine_version" {
  description = "Verified orderable in ap-southeast-1 with db.t4g.micro"
  type        = string
  default     = "17.11"
}

variable "instance_class" {
  description = "t4g = Graviton, cheapest class that supports this engine"
  type        = string
  default     = "db.t4g.micro"
}

variable "database_name" {
  type    = string
  default = "control_tower"
}

variable "master_username" {
  type    = string
  default = "control_tower"
}

variable "allocated_storage" {
  type    = number
  default = 20
}

variable "max_allocated_storage" {
  description = "Storage autoscaling ceiling. Prevents a runaway from becoming a bill."
  type        = number
  default     = 50
}

variable "multi_az" {
  type    = bool
  default = false
}

variable "backup_retention_period" {
  type    = number
  default = 1
}

variable "skip_final_snapshot" {
  description = "true in dev so terraform destroy does not leave a snapshot billing monthly"
  type        = bool
  default     = true
}

variable "deletion_protection" {
  type    = bool
  default = false
}

variable "tags" {
  type    = map(string)
  default = {}
}
