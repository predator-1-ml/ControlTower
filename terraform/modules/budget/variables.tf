variable "name" {
  type = string
}

variable "notification_email" {
  description = "Where budget alerts go. AWS sends a confirmation to this address."
  type        = string
}

variable "monthly_limit_usd" {
  description = <<-EOT
    Monthly ceiling in USD. The stack costs roughly $0.20/hour (~$145/month if
    left running), so $20 is about four days of continuous uptime — well above a
    deploy-demo-destroy cycle, and low enough to notice quickly if it is forgotten.
  EOT
  type        = string
  default     = "20"
}

variable "forecast_threshold_percent" {
  description = "Alert when FORECAST to exceed this percent of the limit. Early warning."
  type        = number
  default     = 50
}

variable "actual_threshold_percent" {
  description = "Alert when ACTUAL spend exceeds this percent of the limit."
  type        = number
  default     = 80
}
