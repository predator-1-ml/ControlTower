output "repository_urls" {
  description = "Keyed by service name, e.g. backend -> 1234.dkr.ecr.ap-southeast-1.amazonaws.com/control-tower-dev/backend"
  value       = { for k, r in aws_ecr_repository.this : k => r.repository_url }
}

output "repository_arns" {
  value = { for k, r in aws_ecr_repository.this : k => r.arn }
}
