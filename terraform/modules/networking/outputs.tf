output "vpc_id" {
  value = aws_vpc.main.id
}

output "public_subnet_ids" {
  description = "Public ALB only"
  value       = aws_subnet.public[*].id
}

output "private_app_subnet_ids" {
  description = "ECS tasks and the internal ALB"
  value       = aws_subnet.private_app[*].id
}

output "private_data_subnet_ids" {
  description = "RDS subnet group"
  value       = aws_subnet.private_data[*].id
}

# Security groups are exported individually rather than as a map so that a
# consumer wiring the wrong tier together fails at plan time with an unknown
# attribute, instead of at runtime with a connection timeout.
output "alb_public_sg_id" {
  value = aws_security_group.alb_public.id
}

output "alb_internal_sg_id" {
  value = aws_security_group.alb_internal.id
}

output "frontend_sg_id" {
  value = aws_security_group.frontend.id
}

output "backend_sg_id" {
  value = aws_security_group.backend.id
}

output "rds_sg_id" {
  value = aws_security_group.rds.id
}
