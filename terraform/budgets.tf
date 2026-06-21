# ─── Cost governance: AWS Budgets + Cost Anomaly Detection ────────────────────
#
# Replaces the manually-created billing alarms. All alerts route to the existing
# `notifications` SNS topic, which fans out to #code-testing via AWS Chatbot.
#
# Note: Budgets and Cost Anomaly Detection are global services managed in
# us-east-1 (this provider's region) and require Cost Explorer to be enabled
# on the account (it already is — the prior manual billing alarms relied on it).

# ─── Total-account monthly cost budget ────────────────────────────────────────
# Notify-only (Slack + email). No auto-shutdown: account-wide overage may be
# driven by non-bot spend, where throttling the bot wouldn't help.
resource "aws_budgets_budget" "account_monthly" {
  name         = "${var.project_name}-account-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_limit)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_sns_topic_arns  = [aws_sns_topic.notifications.arn]
    subscriber_email_addresses = var.budget_alert_emails
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_sns_topic_arns  = [aws_sns_topic.notifications.arn]
    subscriber_email_addresses = var.budget_alert_emails
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_sns_topic_arns  = [aws_sns_topic.notifications.arn]
    subscriber_email_addresses = var.budget_alert_emails
  }

  depends_on = [aws_sns_topic_policy.notifications]
}

# ─── Amazon Bedrock-scoped monthly cost budget ────────────────────────────────
# Bedrock spend is the bot's runaway signal. The FORECASTED 100% threshold also
# publishes to the runaway_alarm topic, which invokes the throttle Lambda and
# sets the bot's reserved concurrency to 0 — an automatic kill-switch. Recover
# with the runaway_recovery_command output (delete-function-concurrency).
resource "aws_budgets_budget" "bedrock_monthly" {
  name         = "${var.project_name}-bedrock-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.bedrock_monthly_budget_limit)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "Service"
    values = ["Amazon Bedrock"]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_sns_topic_arns  = [aws_sns_topic.notifications.arn]
    subscriber_email_addresses = var.budget_alert_emails
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_sns_topic_arns  = [aws_sns_topic.notifications.arn]
    subscriber_email_addresses = var.budget_alert_emails
  }

  # Forecasted overage → notify (Slack + email) AND trigger the runaway shutdown.
  # AWS Budgets permits only one SNS subscriber per notification, so we target
  # the runaway_alarm topic: it invokes the throttle Lambda (concurrency → 0) AND
  # reaches Slack, since AWS Chatbot subscribes to runaway_alarm too (see main.tf).
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = var.budget_alert_emails
    subscriber_sns_topic_arns  = [aws_sns_topic.runaway_alarm.arn]
  }

  depends_on = [
    aws_sns_topic_policy.notifications,
    aws_sns_topic_policy.runaway_alarm,
  ]
}

# ─── Cost Anomaly Detection (per-service, includes Amazon Bedrock) ─────────────
# Catches sudden, abnormal spend faster than a monthly budget threshold (e.g. a
# token-loop or an accidental planner re-enable). AWS only allows CUSTOM monitors
# to filter by LINKED_ACCOUNT, and exactly ONE DIMENSIONAL/SERVICE monitor per
# account. AWS auto-provisions that monitor as "Default-Services-Monitor"; this
# resource adopts it (imported into state) so Terraform fully manages it. It
# tracks every service independently and flags per-service anomalies — Bedrock
# among them. Free, and broader coverage than a Bedrock-only monitor.
resource "aws_ce_anomaly_monitor" "bedrock" {
  name              = "Default-Services-Monitor"
  monitor_type      = "DIMENSIONAL"
  monitor_dimension = "SERVICE"
}

resource "aws_ce_anomaly_subscription" "bedrock" {
  name             = "${var.project_name}-service-anomaly-alerts"
  frequency        = "IMMEDIATE" # IMMEDIATE requires an SNS subscriber
  monitor_arn_list = [aws_ce_anomaly_monitor.bedrock.arn]

  subscriber {
    type    = "SNS"
    address = aws_sns_topic.notifications.arn
  }

  threshold_expression {
    dimension {
      key           = "ANOMALY_TOTAL_IMPACT_ABSOLUTE"
      values        = [tostring(var.anomaly_alert_threshold)]
      match_options = ["GREATER_THAN_OR_EQUAL"]
    }
  }

  depends_on = [aws_sns_topic_policy.notifications]
}

# ─── Outputs ──────────────────────────────────────────────────────────────────
output "account_budget_name" {
  value       = aws_budgets_budget.account_monthly.name
  description = "Total-account monthly cost budget (Terraform-managed)"
}

output "bedrock_budget_name" {
  value       = aws_budgets_budget.bedrock_monthly.name
  description = "Amazon Bedrock-scoped monthly cost budget (Terraform-managed)"
}

output "bedrock_anomaly_monitor_arn" {
  value       = aws_ce_anomaly_monitor.bedrock.arn
  description = "Per-service Cost Anomaly Detection monitor (flags Bedrock and all other services)"
}
