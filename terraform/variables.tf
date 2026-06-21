variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "bedrock_model_id" {
  description = "Bedrock model ID (default: Amazon Nova Lite). Switch to us.anthropic.claude-haiku-4-5-20251001-v1:0 (cross-region inference profile) once Claude Haiku 4.5 Marketplace access is approved in Bedrock."
  type        = string
  default     = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "planner_model_id" {
  description = "Bedrock model ID for the deep-planning tier. Complex, multi-step questions are escalated from bedrock_model_id to this more capable model. Uses the us. cross-region inference profile (ACTIVE in this account)."
  type        = string
  default     = "us.anthropic.claude-sonnet-4-6"
}

variable "enable_planner_escalation" {
  description = "Kill-switch for the more expensive Sonnet planner tier. Temporarily false while cost-monitoring strategies are built; set true to re-enable escalation."
  type        = bool
  default     = false
}

variable "slack_signing_secret" {
  description = "Slack signing secret for request validation"
  type        = string
  sensitive   = true
}

variable "slack_bot_token" {
  description = "Slack bot OAuth token"
  type        = string
  sensitive   = true
}

variable "api_gateway_api_key" {
  description = "Optional: Custom API Gateway API key. If not provided, AWS will auto-generate one."
  type        = string
  sensitive   = true
  default     = "" # Empty means AWS will auto-generate
}

variable "project_name" {
  description = "Project name for resource naming"
  type        = string
  default     = "ff-moogle-bot"
}

variable "idempotency_ttl_minutes" {
  description = "TTL for idempotency keys in S3 (in minutes)"
  type        = number
  default     = 5
}

variable "terraform_state_bucket" {
  description = "S3 bucket name for storing Terraform state (must be globally unique)"
  type        = string
  default     = "ff-moogle-bot-terraform-state"
}

variable "terraform_locks_table" {
  description = "DynamoDB table name for Terraform state locking"
  type        = string
  default     = "ff-moogle-bot-terraform-locks"
}

variable "slack_chatbot_workspace_id" {
  description = "Slack workspace ID for AWS Chatbot (starts with T — found in workspace URL)"
  type        = string
  default     = "T1HUKSP2M"
}

variable "slack_chatbot_channel_id" {
  description = "Slack channel ID for AWS Chatbot notifications (right-click channel → View channel details)"
  type        = string
  default     = "C03HE46DXC7"
}

variable "runaway_alarm_threshold" {
  description = "Initial Lambda invocations per minute that triggers runaway protection (throttles the bot to zero). Tune based on expected peak traffic."
  type        = number
  default     = 30
}

variable "log_level" {
  description = "Logging level for Lambda functions (DEBUG, INFO, WARNING, ERROR, CRITICAL). Default is ERROR for minimal logging."
  type        = string
  default     = "ERROR"

  validation {
    condition     = contains(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], upper(var.log_level))
    error_message = "LOG_LEVEL must be one of: DEBUG, INFO, WARNING, ERROR, CRITICAL."
  }
}

variable "easter_egg_user_id" {
  description = "Slack user ID that receives the easter egg reply instead of a normal response. Leave empty to disable."
  type        = string
  default     = ""
}

variable "scrapingant_api_key" {
  description = "ScrapingAnt API key used by the r/ffxi last-resort Reddit search tool. Leave empty to keep the tool dormant."
  type        = string
  default     = ""
  sensitive   = true
}
