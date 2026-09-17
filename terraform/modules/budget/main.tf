# Cost guardrail.
#
# AWS Budgets rather than a CloudWatch billing alarm, deliberately. Billing
# metrics (`AWS/Billing` → `EstimatedCharges`) are published ONLY in us-east-1,
# so a CloudWatch alarm needs a second provider aliased to that region even when
# everything else lives elsewhere. `aws_budgets_budget` is a global resource and
# works from any provider — one less piece of accidental complexity for the same
# outcome.
#
# Two notifications, because they answer different questions:
#
#   ACTUAL     — "you have already spent this."   Tells you it happened.
#   FORECASTED — "you are on track to spend this." Tells you while you can act.
#
# For infrastructure billed by the hour, forecasted is the one that matters: a
# stack left running is cheap for the first day and expensive by the second week,
# and the forecast catches it on day one.

resource "aws_budgets_budget" "monthly" {
  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = var.monthly_limit_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Fires while there is still time to do something about it.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = var.forecast_threshold_percent
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.notification_email]
  }

  # Fires when it has actually happened.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = var.actual_threshold_percent
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.notification_email]
  }
}
