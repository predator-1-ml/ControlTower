output "cluster_id" {
  value = aws_ecs_cluster.this.id
}

output "cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "execution_role_arn" {
  description = "Used by the ECS agent to pull images and resolve secrets"
  value       = aws_iam_role.execution.arn
}

output "task_role_arn" {
  description = "Used by the application at runtime"
  value       = aws_iam_role.task.arn
}

output "log_group_names" {
  value = { for k, g in aws_cloudwatch_log_group.services : k => g.name }
}
