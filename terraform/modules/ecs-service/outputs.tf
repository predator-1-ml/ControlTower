output "service_name" {
  value = aws_ecs_service.this.name
}

output "task_definition_arn" {
  value = aws_ecs_task_definition.this.arn
}

output "task_definition_family" {
  description = "CI renders a new revision from this family on each deploy"
  value       = aws_ecs_task_definition.this.family
}
