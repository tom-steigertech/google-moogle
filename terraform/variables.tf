variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "openai_api_key" {
  description = "OpenAI API key"
  type        = string
  sensitive   = true
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

variable "log_level" {
  description = "Logging level for Lambda functions (DEBUG, INFO, WARNING, ERROR, CRITICAL). Default is ERROR for minimal logging."
  type        = string
  default     = "ERROR"

  validation {
    condition     = contains(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], upper(var.log_level))
    error_message = "LOG_LEVEL must be one of: DEBUG, INFO, WARNING, ERROR, CRITICAL."
  }
}
