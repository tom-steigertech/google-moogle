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
  description = "API Gateway API key for request validation (sent as query parameter)"
  type        = string
  sensitive   = true
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
