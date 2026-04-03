terraform {
  required_version = ">= 1.0"

  # NOTE: To enable remote state:
  # 1. First run 'terraform apply' to create the backend resources (S3 bucket + DynamoDB table)
  # 2. Then uncomment the backend block below
  # 3. Run 'terraform init -migrate-state' to move state to S3
  #
  # backend "s3" {
  #   bucket         = "ff-moogle-bot-terraform-state"  # Must match var.terraform_state_bucket
  #   key            = "terraform.tfstate"
  #   region         = "us-east-1"
  #   encrypt        = true
  #   dynamodb_table = "ff-moogle-bot-terraform-locks"  # Must match var.terraform_locks_table
  # }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

locals {
  common_tags = {
    appname = "slackbotGoogleMoogle"
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.common_tags
  }
}

# Data sources

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# Random suffix for unique S3 bucket names
resource "random_id" "bucket_suffix" {
  byte_length = 8
}

# S3 Bucket for Idempotency
resource "aws_s3_bucket" "idempotency" {
  bucket = "${var.project_name}-idempotency-${random_id.bucket_suffix.hex}"
}

resource "aws_s3_bucket_versioning" "idempotency" {
  bucket = aws_s3_bucket.idempotency.id
  versioning_configuration {
    status = "Disabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "idempotency" {
  bucket = aws_s3_bucket.idempotency.id

  rule {
    id     = "expire-old-keys"
    status = "Enabled"

    expiration {
      days = 1
    }
  }
}

# S3 Bucket for Terraform State
resource "aws_s3_bucket" "terraform_state" {
  bucket = var.terraform_state_bucket
}

resource "aws_s3_bucket_versioning" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# DynamoDB Table for Terraform State Locking
resource "aws_dynamodb_table" "terraform_locks" {
  name         = var.terraform_locks_table
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }
}

# SQS Queue
resource "aws_sqs_queue" "processing_queue" {
  name                      = "${var.project_name}-processing-queue"
  message_retention_seconds = 3600
  receive_wait_time_seconds = 5

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue" "dlq" {
  name = "${var.project_name}-processing-dlq"
}

# IAM Role for Lambda functions
resource "aws_iam_role" "lambda_role" {
  name = "${var.project_name}-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy" "lambda_policy" {
  name = "${var.project_name}-lambda-policy"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:HeadObject"
        ]
        Resource = "${aws_s3_bucket.idempotency.arn}/*"
      },
      {
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl"
        ]
        Resource = [
          aws_sqs_queue.processing_queue.arn,
          aws_sqs_queue.dlq.arn
        ]
      }
    ]
  })
}

# Lambda Layer for dependencies
resource "aws_lambda_layer_version" "dependencies" {
  filename         = "lambda_functions/layer.zip"
  layer_name       = "${var.project_name}-dependencies"
  source_code_hash = filebase64sha256("lambda_functions/layer.zip")

  compatible_runtimes = ["python3.11"]
}

# Initial Lambda Function (API Gateway handler)
resource "aws_lambda_function" "initial" {
  function_name = "${var.project_name}-initial"
  role          = aws_iam_role.lambda_role.arn
  handler       = "initial_lambda.handler"
  runtime       = "python3.11"
  timeout       = 10
  memory_size   = 256

  filename         = "lambda_functions/initial_lambda.zip"
  source_code_hash = filebase64sha256("lambda_functions/initial_lambda.zip")

  layers = [aws_lambda_layer_version.dependencies.arn]

  environment {
    variables = {
      SQS_QUEUE_URL        = aws_sqs_queue.processing_queue.url
      SLACK_SIGNING_SECRET = var.slack_signing_secret
    }
  }
}

# Processing Lambda Function (SQS consumer)
resource "aws_lambda_function" "processing" {
  function_name = "${var.project_name}-processing"
  role          = aws_iam_role.lambda_role.arn
  handler       = "processing_lambda.handler"
  runtime       = "python3.11"
  timeout       = 60
  memory_size   = 512

  filename         = "lambda_functions/processing_lambda.zip"
  source_code_hash = filebase64sha256("lambda_functions/processing_lambda.zip")

  layers = [aws_lambda_layer_version.dependencies.arn]

  environment {
    variables = {
      OPENAI_API_KEY        = var.openai_api_key
      SLACK_BOT_TOKEN       = var.slack_bot_token
      S3_BUCKET_IDEMPOTENCY = aws_s3_bucket.idempotency.bucket
      SQS_QUEUE_URL         = aws_sqs_queue.processing_queue.url
    }
  }
}

# SQS trigger for Processing Lambda
resource "aws_lambda_event_source_mapping" "sqs_trigger" {
  event_source_arn = aws_sqs_queue.processing_queue.arn
  function_name    = aws_lambda_function.processing.arn
  batch_size       = 1
}

# API Gateway
resource "aws_api_gateway_rest_api" "slack_api" {
  name        = "${var.project_name}-api"
  description = "Slack bot API for Final Fantasy Moogle"

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

# Lambda Authorizer
resource "aws_lambda_function" "authorizer" {
  function_name = "${var.project_name}-authorizer"
  role          = aws_iam_role.lambda_role.arn
  handler       = "authorizer.handler"
  runtime       = "python3.11"
  timeout       = 10
  memory_size   = 128

  filename         = "lambda_functions/authorizer.zip"
  source_code_hash = filebase64sha256("lambda_functions/authorizer.zip")

  environment {
    variables = {
      SLACK_SIGNING_SECRET = var.slack_signing_secret
      API_GATEWAY_API_KEY  = aws_api_gateway_api_key.slack_api_key.value
    }
  }

  # Ensure the API key is created before the Lambda is updated
  depends_on = [aws_api_gateway_api_key.slack_api_key]
}

resource "aws_lambda_permission" "authorizer_invoke" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.authorizer.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.slack_api.execution_arn}/authorizers/*"
}

resource "aws_api_gateway_authorizer" "slack_authorizer" {
  name                             = "slack-signature-authorizer"
  rest_api_id                      = aws_api_gateway_rest_api.slack_api.id
  authorizer_uri                   = aws_lambda_function.authorizer.invoke_arn
  authorizer_credentials           = aws_iam_role.lambda_role.arn
  identity_source                  = "method.request.header.X-Slack-Request-Timestamp,method.request.header.X-Slack-Signature,method.request.querystring.api_key"
  type                             = "REQUEST"
  authorizer_result_ttl_in_seconds = 0
}

# API Gateway Resources and Methods
resource "aws_api_gateway_resource" "slack" {
  rest_api_id = aws_api_gateway_rest_api.slack_api.id
  parent_id   = aws_api_gateway_rest_api.slack_api.root_resource_id
  path_part   = "slack"
}

resource "aws_api_gateway_resource" "events" {
  rest_api_id = aws_api_gateway_rest_api.slack_api.id
  parent_id   = aws_api_gateway_resource.slack.id
  path_part   = "events"
}

resource "aws_api_gateway_method" "events_post" {
  rest_api_id   = aws_api_gateway_rest_api.slack_api.id
  resource_id   = aws_api_gateway_resource.events.id
  http_method   = "POST"
  authorization = "CUSTOM"
  authorizer_id = aws_api_gateway_authorizer.slack_authorizer.id
}

resource "aws_api_gateway_integration" "lambda_integration" {
  rest_api_id = aws_api_gateway_rest_api.slack_api.id
  resource_id = aws_api_gateway_resource.events.id
  http_method = aws_api_gateway_method.events_post.http_method

  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.initial.invoke_arn
}

# /googlemoogle path for POST endpoint
resource "aws_api_gateway_resource" "googlemoogle" {
  rest_api_id = aws_api_gateway_rest_api.slack_api.id
  parent_id   = aws_api_gateway_rest_api.slack_api.root_resource_id
  path_part   = "googlemoogle"
}

resource "aws_api_gateway_method" "googlemoogle_post" {
  rest_api_id   = aws_api_gateway_rest_api.slack_api.id
  resource_id   = aws_api_gateway_resource.googlemoogle.id
  http_method   = "POST"
  authorization = "CUSTOM"
  authorizer_id = aws_api_gateway_authorizer.slack_authorizer.id
}

resource "aws_api_gateway_integration" "googlemoogle_lambda_integration" {
  rest_api_id = aws_api_gateway_rest_api.slack_api.id
  resource_id = aws_api_gateway_resource.googlemoogle.id
  http_method = aws_api_gateway_method.googlemoogle_post.http_method

  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.initial.invoke_arn
}

resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.initial.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.slack_api.execution_arn}/*/*"
}

# API Gateway Deployment
resource "aws_api_gateway_deployment" "prod" {
  depends_on = [
    aws_api_gateway_integration.lambda_integration,
    aws_api_gateway_method.events_post,
    aws_api_gateway_integration.googlemoogle_lambda_integration,
    aws_api_gateway_method.googlemoogle_post
  ]

  rest_api_id = aws_api_gateway_rest_api.slack_api.id
}

# API Gateway Stage
resource "aws_api_gateway_stage" "prod" {
  deployment_id = aws_api_gateway_deployment.prod.id
  rest_api_id   = aws_api_gateway_rest_api.slack_api.id
  stage_name    = "prod"
}

# API Key (Auto-generated by AWS)
resource "aws_api_gateway_api_key" "slack_api_key" {
  name        = "${var.project_name}-api-key"
  description = "API Key for Slack bot authentication"
  enabled     = true
}

# Usage Plan for the API
resource "aws_api_gateway_usage_plan" "slack_usage_plan" {
  name        = "${var.project_name}-usage-plan"
  description = "Usage plan for Slack bot API"

  api_stages {
    api_id = aws_api_gateway_rest_api.slack_api.id
    stage  = aws_api_gateway_stage.prod.stage_name
  }

  # Optional: Add throttling limits
  throttle_settings {
    burst_limit = 100
    rate_limit  = 50
  }

  # Optional: Add quota limits
  quota_settings {
    limit  = 10000
    period = "DAY"
  }
}

# Associate the API Key with the Usage Plan
resource "aws_api_gateway_usage_plan_key" "slack_usage_plan_key" {
  key_id        = aws_api_gateway_api_key.slack_api_key.id
  key_type      = "API_KEY"
  usage_plan_id = aws_api_gateway_usage_plan.slack_usage_plan.id
}

# Outputs
output "api_gateway_invoke_url" {
  value       = "${aws_api_gateway_stage.prod.invoke_url}/slack/events"
  description = "API Gateway endpoint URL for Slack (/slack/events)"
}

output "api_gateway_googlemoogle_url" {
  value       = "${aws_api_gateway_stage.prod.invoke_url}/googlemoogle"
  description = "API Gateway POST endpoint URL for GoogleMoogle"
}

output "api_key_value" {
  value       = aws_api_gateway_api_key.slack_api_key.value
  description = "The auto-generated API key value (add as ?api_key=... query parameter)"
  sensitive   = true
}

output "idempotency_bucket" {
  value       = aws_s3_bucket.idempotency.bucket
  description = "S3 bucket for idempotency checking"
}

output "sqs_queue_url" {
  value       = aws_sqs_queue.processing_queue.url
  description = "SQS queue URL for processing"
}

output "terraform_state_bucket" {
  value       = aws_s3_bucket.terraform_state.bucket
  description = "S3 bucket for Terraform state storage"
}

output "terraform_locks_table" {
  value       = aws_dynamodb_table.terraform_locks.name
  description = "DynamoDB table for Terraform state locking"
}
