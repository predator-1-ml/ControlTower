output "deploy_role_arn" {
  description = "Set as AWS_DEPLOY_ROLE_ARN in GitHub repository variables"
  value       = aws_iam_role.deploy.arn
}

output "terraform_plan_role_arn" {
  value = aws_iam_role.terraform_plan.arn
}

output "terraform_apply_role_arn" {
  value = aws_iam_role.terraform_apply.arn
}
