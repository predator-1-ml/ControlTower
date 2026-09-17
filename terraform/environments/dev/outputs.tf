output "public_url" {
  description = "The only internet-facing entry point"
  value       = "http://${module.alb_public.dns_name}"
}

output "backend_private_url" {
  description = "Private DNS the frontend uses. Resolves only inside the VPC — this is the assignment's core networking requirement."
  value       = "http://api.${var.internal_domain}:${var.backend_port}"
}

output "ecr_repository_urls" {
  value = module.ecr.repository_urls
}

output "cluster_name" {
  value = module.cluster.cluster_name
}

output "backend_service_name" {
  value = module.backend.service_name
}

output "frontend_service_name" {
  value = module.frontend.service_name
}

output "db_secret_arn" {
  description = "RDS-managed, rotated every 7 days. Fetched at connect time, never injected."
  value       = module.rds.master_user_secret_arn
}

output "private_subnet_ids" {
  description = "Needed to run the migration task before a service update"
  value       = module.networking.private_app_subnet_ids
}

output "backend_security_group_id" {
  value = module.networking.backend_sg_id
}

output "github_deploy_role_arn" {
  description = "Set as repository variable AWS_DEPLOY_ROLE_ARN"
  value       = module.github_oidc.deploy_role_arn
}

output "github_terraform_plan_role_arn" {
  value = module.github_oidc.terraform_plan_role_arn
}

output "github_terraform_apply_role_arn" {
  value = module.github_oidc.terraform_apply_role_arn
}
