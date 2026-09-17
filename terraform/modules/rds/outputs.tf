output "address" {
  value = aws_db_instance.this.address
}

output "port" {
  value = aws_db_instance.this.port
}

output "database_name" {
  value = aws_db_instance.this.db_name
}

output "master_user_secret_arn" {
  description = "Secrets Manager secret RDS manages and rotates. Fetched at connect time, never injected."
  value       = aws_db_instance.this.master_user_secret[0].secret_arn
}
