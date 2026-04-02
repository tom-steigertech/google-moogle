terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
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
      API_GATEWAY_API_KEY  = var.api_gateway_api_key
    }
  }
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
    aws_api_gateway_method.events_post
  ]

  rest_api_id = aws_api_gateway_rest_api.slack_api.id
}

# API Gateway Stage
resource "aws_api_gateway_stage" "prod" {
  deployment_id = aws_api_gateway_deployment.prod.id
  rest_api_id   = aws_api_gateway_rest_api.slack_api.id
  stage_name    = "prod"
}

# Outputs
output "api_gateway_invoke_url" {
  value       = "${aws_api_gateway_stage.prod.invoke_url}/slack/events"
  description = "API Gateway endpoint URL for Slack"
}

output "idempotency_bucket" {
  value       = aws_s3_bucket.idempotency.bucket
  description = "S3 bucket for idempotency checking"
}

output "sqs_queue_url" {
  value       = aws_sqs_queue.processing_queue.url
  description = "SQS queue URL for processing"
}
