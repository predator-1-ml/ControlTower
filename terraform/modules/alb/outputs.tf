output "dns_name" {
  value = aws_lb.this.dns_name
}

output "zone_id" {
  description = "For the Route 53 alias record that fronts the internal ALB"
  value       = aws_lb.this.zone_id
}

output "target_group_arn" {
  value = aws_lb_target_group.this.arn
}

output "arn" {
  value = aws_lb.this.arn
}
