terraform {
  required_version = ">= 1.0"

  backend "s3" {
    bucket       = "ff-moogle-bot-terraform-state"
    key          = "terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.21"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }
}

locals {
  common_tags = {
    appname    = "slackbotGoogleMoogle"
    aws-apn-id = "pc:9spfrcxqecofxzcgzjjzsf3o3"
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

    # Scoped to the idempotency prefix only. Notes (notes/notes.json) live in
    # this same bucket and must NOT be expired.
    filter {
      prefix = "idempotency/"
    }

    expiration {
      days = 1
    }
  }
}

# S3 Bucket Policy to allow Lambda role access
resource "aws_s3_bucket_policy" "idempotency" {
  bucket = aws_s3_bucket.idempotency.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          AWS = aws_iam_role.lambda_role.arn
        }
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject"
        ]
        Resource = "${aws_s3_bucket.idempotency.arn}/*"
      },
      {
        Effect = "Allow"
        Principal = {
          AWS = aws_iam_role.lambda_role.arn
        }
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.idempotency.arn
      }
    ]
  })
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

# AgentCore Memory for multi-turn conversation state
resource "aws_bedrockagentcore_memory" "moogle" {
  name                  = "${replace(var.project_name, "-", "_")}_memory"
  event_expiry_duration = 7
}

# SQS Queue
resource "aws_sqs_queue" "processing_queue" {
  name                       = "${var.project_name}-processing-queue"
  message_retention_seconds  = 3600
  receive_wait_time_seconds  = 5
  visibility_timeout_seconds = 360 # Must be >= processing Lambda timeout (300s)
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
        Resource = aws_sqs_queue.processing_queue.arn
      },
      {
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream"
        ]
        Resource = [
          "arn:aws:bedrock:*::foundation-model/anthropic.*",
          "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["aws-marketplace:ViewSubscriptions", "aws-marketplace:Subscribe"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:CreateEvent",
          "bedrock-agentcore:ListEvents",
          "bedrock-agentcore:GetEvent",
          "bedrock-agentcore:DeleteEvent",
          "bedrock-agentcore:ListSessions"
        ]
        Resource = [
          aws_bedrockagentcore_memory.moogle.arn,
          "${aws_bedrockagentcore_memory.moogle.arn}/*"
        ]
      }
    ]
  })
}

# IAM Role for API Gateway to invoke Lambda Authorizer
resource "aws_iam_role" "api_gateway_authorizer_role" {
  name = "${var.project_name}-api-gateway-authorizer-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "apigateway.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_role_policy" "api_gateway_authorizer_policy" {
  name = "${var.project_name}-api-gateway-authorizer-policy"
  role = aws_iam_role.api_gateway_authorizer_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.authorizer.arn
    }]
  })

  depends_on = [aws_lambda_function.authorizer]
}

# Lambda Layer for dependencies
resource "aws_lambda_layer_version" "dependencies" {
  filename         = "../lambda_functions/layer.zip"
  layer_name       = "${var.project_name}-dependencies"
  source_code_hash = filebase64sha256("../lambda_functions/layer.zip")

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

  filename         = "../lambda_functions/initial_lambda.zip"
  source_code_hash = filebase64sha256("../lambda_functions/initial_lambda.zip")

  layers = [aws_lambda_layer_version.dependencies.arn]

  environment {
    variables = {
      SQS_QUEUE_URL         = aws_sqs_queue.processing_queue.url
      SLACK_SIGNING_SECRET  = var.slack_signing_secret
      SLACK_BOT_TOKEN       = var.slack_bot_token
      LOG_LEVEL             = var.log_level
      AGENTCORE_MEMORY_ID   = aws_bedrockagentcore_memory.moogle.id
      SESSION_IDLE_MINUTES  = "10"
      S3_BUCKET_IDEMPOTENCY = aws_s3_bucket.idempotency.bucket
      EASTER_EGG_USER_ID    = var.easter_egg_user_id
    }
  }

  # Ensure IAM policy is attached before Lambda is created/updated
  depends_on = [aws_iam_role_policy.lambda_policy]
}

# Processing Lambda Function (SQS consumer)
resource "aws_lambda_function" "processing" {
  function_name = "${var.project_name}-processing"
  role          = aws_iam_role.lambda_role.arn
  handler       = "processing.handler"
  runtime       = "python3.11"
  # 300s to accommodate the deep-planning tier, which runs a more capable model
  # through a longer multi-step tool loop. Must stay <= the SQS queue's
  # visibility_timeout_seconds so a slow run isn't redelivered mid-flight.
  timeout     = 300
  memory_size = 512

  filename         = "../lambda_functions/processing_lambda.zip"
  source_code_hash = filebase64sha256("../lambda_functions/processing_lambda.zip")

  layers = [aws_lambda_layer_version.dependencies.arn]

  environment {
    variables = {
      BEDROCK_MODEL_ID          = var.bedrock_model_id
      PLANNER_MODEL_ID          = var.planner_model_id
      ENABLE_PLANNER_ESCALATION = tostring(var.enable_planner_escalation)
      BEDROCK_REGION            = var.aws_region
      SLACK_BOT_TOKEN           = var.slack_bot_token
      S3_BUCKET_IDEMPOTENCY     = aws_s3_bucket.idempotency.bucket
      SQS_QUEUE_URL             = aws_sqs_queue.processing_queue.url
      LOG_LEVEL                 = var.log_level
      AGENTCORE_MEMORY_ID       = aws_bedrockagentcore_memory.moogle.id
      SESSION_IDLE_MINUTES      = "10"
      SCRAPINGANT_API_KEY       = var.scrapingant_api_key
    }
  }

  # Ensure IAM policy is attached before Lambda is created/updated
  depends_on = [aws_iam_role_policy.lambda_policy]
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

  filename         = "../lambda_functions/authorizer.zip"
  source_code_hash = filebase64sha256("../lambda_functions/authorizer.zip")

  environment {
    variables = {
      # Authorizer only validates API key - Slack signatures validated in initial Lambda
      API_GATEWAY_API_KEY = aws_api_gateway_api_key.slack_api_key.value
      LOG_LEVEL           = var.log_level
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
  name           = "api-key-authorizer"
  rest_api_id    = aws_api_gateway_rest_api.slack_api.id
  authorizer_uri = aws_lambda_function.authorizer.invoke_arn
  # Use IAM role that allows API Gateway to assume it (not the Lambda execution role)
  authorizer_credentials = aws_iam_role.api_gateway_authorizer_role.arn
  # Authorizer only validates API key - Slack signatures validated by initial Lambda
  identity_source                  = "method.request.querystring.api_key"
  type                             = "REQUEST"
  authorizer_result_ttl_in_seconds = 0

  # Ensure Lambda permission and IAM role exist before creating authorizer
  depends_on = [
    aws_lambda_permission.authorizer_invoke,
    aws_iam_role_policy.api_gateway_authorizer_policy
  ]
}

# API Gateway Resources - All under /googlemoogle
# Parent resource: /googlemoogle
resource "aws_api_gateway_resource" "googlemoogle" {
  rest_api_id = aws_api_gateway_rest_api.slack_api.id
  parent_id   = aws_api_gateway_rest_api.slack_api.root_resource_id
  path_part   = "googlemoogle"
}

# Child resource: /googlemoogle/events (for Slack Events API / @mentions)
resource "aws_api_gateway_resource" "events" {
  rest_api_id = aws_api_gateway_rest_api.slack_api.id
  parent_id   = aws_api_gateway_resource.googlemoogle.id
  path_part   = "events"
}

resource "aws_api_gateway_method" "events_post" {
  rest_api_id   = aws_api_gateway_rest_api.slack_api.id
  resource_id   = aws_api_gateway_resource.events.id
  http_method   = "POST"
  authorization = "CUSTOM"
  authorizer_id = aws_api_gateway_authorizer.slack_authorizer.id
}

resource "aws_api_gateway_integration" "events_lambda_integration" {
  rest_api_id = aws_api_gateway_rest_api.slack_api.id
  resource_id = aws_api_gateway_resource.events.id
  http_method = aws_api_gateway_method.events_post.http_method

  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.initial.invoke_arn
}

# Child resource: /googlemoogle/slash (for Slack Slash Commands)
resource "aws_api_gateway_resource" "slash" {
  rest_api_id = aws_api_gateway_rest_api.slack_api.id
  parent_id   = aws_api_gateway_resource.googlemoogle.id
  path_part   = "slash"
}

resource "aws_api_gateway_method" "slash_post" {
  rest_api_id   = aws_api_gateway_rest_api.slack_api.id
  resource_id   = aws_api_gateway_resource.slash.id
  http_method   = "POST"
  authorization = "CUSTOM"
  authorizer_id = aws_api_gateway_authorizer.slack_authorizer.id
}

resource "aws_api_gateway_integration" "slash_lambda_integration" {
  rest_api_id = aws_api_gateway_rest_api.slack_api.id
  resource_id = aws_api_gateway_resource.slash.id
  http_method = aws_api_gateway_method.slash_post.http_method

  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.initial.invoke_arn
}

# Child resource: /googlemoogle/health (Health Check - Mock integration)
resource "aws_api_gateway_resource" "health" {
  rest_api_id = aws_api_gateway_rest_api.slack_api.id
  parent_id   = aws_api_gateway_resource.googlemoogle.id
  path_part   = "health"
}

resource "aws_api_gateway_method" "health_get" {
  rest_api_id   = aws_api_gateway_rest_api.slack_api.id
  resource_id   = aws_api_gateway_resource.health.id
  http_method   = "GET"
  authorization = "CUSTOM"
  authorizer_id = aws_api_gateway_authorizer.slack_authorizer.id
}

# Mock integration - returns static response without invoking Lambda
resource "aws_api_gateway_integration" "health_mock" {
  rest_api_id = aws_api_gateway_rest_api.slack_api.id
  resource_id = aws_api_gateway_resource.health.id
  http_method = aws_api_gateway_method.health_get.http_method

  type = "MOCK"
  request_templates = {
    "application/json" = jsonencode({
      statusCode = 200
    })
  }
}

resource "aws_api_gateway_method_response" "health_200" {
  rest_api_id = aws_api_gateway_rest_api.slack_api.id
  resource_id = aws_api_gateway_resource.health.id
  http_method = aws_api_gateway_method.health_get.http_method
  status_code = "200"

  response_models = {
    "application/json" = "Empty"
  }
}

resource "aws_api_gateway_integration_response" "health_mock_response" {
  rest_api_id = aws_api_gateway_rest_api.slack_api.id
  resource_id = aws_api_gateway_resource.health.id
  http_method = aws_api_gateway_method.health_get.http_method
  status_code = aws_api_gateway_method_response.health_200.status_code

  response_templates = {
    "application/json" = jsonencode({
      message = "hello world"
      status  = "ok"
    })
  }
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
    aws_api_gateway_authorizer.slack_authorizer,
    aws_api_gateway_integration.events_lambda_integration,
    aws_api_gateway_method.events_post,
    aws_api_gateway_integration.slash_lambda_integration,
    aws_api_gateway_method.slash_post,
    aws_api_gateway_integration.health_mock,
    aws_api_gateway_method.health_get,
    aws_api_gateway_integration_response.health_mock_response,
    aws_api_gateway_method_response.health_200
  ]

  rest_api_id = aws_api_gateway_rest_api.slack_api.id

  # Force redeployment when authorizer or methods change
  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_authorizer.slack_authorizer.id,
      aws_api_gateway_authorizer.slack_authorizer.authorizer_uri,
      aws_api_gateway_authorizer.slack_authorizer.authorizer_credentials,
      aws_api_gateway_resource.googlemoogle.id,
      aws_api_gateway_resource.events.id,
      aws_api_gateway_resource.slash.id,
      aws_api_gateway_resource.health.id,
      aws_api_gateway_method.events_post.id,
      aws_api_gateway_method.slash_post.id,
      aws_api_gateway_method.health_get.id,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }
}

# API Gateway Stage
resource "aws_api_gateway_stage" "prod" {
  deployment_id = aws_api_gateway_deployment.prod.id
  rest_api_id   = aws_api_gateway_rest_api.slack_api.id
  stage_name    = "prod"

  lifecycle {
    # Ignore deployment_id changes to prevent stage destruction
    # The deployment updates via triggers, stage just points to it
    ignore_changes = [deployment_id]
  }
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

# ─── Runaway protection ────────────────────────────────────────────────────────
# CloudWatch alarm fires when the initial Lambda is invoked too frequently,
# SNS delivers the notification to the throttle Lambda, which sets reserved
# concurrency to 0. API Gateway then returns 429 without invoking the bot.
# Recovery: aws lambda delete-function-concurrency --function-name <initial>

data "archive_file" "throttle_lambda" {
  type        = "zip"
  output_path = "${path.module}/throttle_lambda.zip"
  source {
    content  = file("${path.module}/../lambda_functions/throttle/throttle.py")
    filename = "throttle.py"
  }
}

resource "aws_iam_role" "throttle_lambda_role" {
  name = "${var.project_name}-throttle-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "throttle_lambda_policy" {
  name = "${var.project_name}-throttle-lambda-policy"
  role = aws_iam_role.throttle_lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Effect   = "Allow"
        Action   = "lambda:PutFunctionConcurrency"
        Resource = aws_lambda_function.initial.arn
      }
    ]
  })
}

resource "aws_lambda_function" "throttle" {
  function_name    = "${var.project_name}-runaway-throttle"
  role             = aws_iam_role.throttle_lambda_role.arn
  handler          = "throttle.handler"
  runtime          = "python3.11"
  timeout          = 10
  memory_size      = 128
  filename         = data.archive_file.throttle_lambda.output_path
  source_code_hash = data.archive_file.throttle_lambda.output_base64sha256

  environment {
    variables = {
      TARGET_FUNCTION_NAME = aws_lambda_function.initial.function_name
    }
  }

  depends_on = [aws_iam_role_policy.throttle_lambda_policy]
}

resource "aws_sns_topic" "runaway_alarm" {
  name = "${var.project_name}-runaway-alarm"
}

resource "aws_sns_topic_policy" "runaway_alarm" {
  arn = aws_sns_topic.runaway_alarm.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowCloudWatchPublish"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = "SNS:Publish"
        Resource  = aws_sns_topic.runaway_alarm.arn
        Condition = {
          StringEquals = { "AWS:SourceAccount" = data.aws_caller_identity.current.account_id }
        }
      },
      {
        Sid       = "AllowBudgetsPublish"
        Effect    = "Allow"
        Principal = { Service = "budgets.amazonaws.com" }
        Action    = "SNS:Publish"
        Resource  = aws_sns_topic.runaway_alarm.arn
        Condition = {
          StringEquals = { "AWS:SourceAccount" = data.aws_caller_identity.current.account_id }
        }
      }
    ]
  })
}

resource "aws_lambda_permission" "throttle_sns" {
  statement_id  = "AllowSNSInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.throttle.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.runaway_alarm.arn
}

resource "aws_sns_topic_subscription" "throttle_lambda" {
  topic_arn = aws_sns_topic.runaway_alarm.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.throttle.arn

  depends_on = [aws_lambda_permission.throttle_sns]
}

resource "aws_cloudwatch_metric_alarm" "runaway_protection" {
  alarm_name        = "${var.project_name}-runaway-protection"
  alarm_description = "Throttles the bot to 0 concurrency when invocations exceed ${var.runaway_alarm_threshold}/min (runaway loop or abuse)"

  namespace           = "AWS/Lambda"
  metric_name         = "Invocations"
  dimensions          = { FunctionName = aws_lambda_function.initial.function_name }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = var.runaway_alarm_threshold
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [
    aws_sns_topic.runaway_alarm.arn, # triggers throttle Lambda
    aws_sns_topic.notifications.arn, # sends to Slack via Chatbot
  ]
}

# ─── AWS Chatbot / Slack notifications ────────────────────────────────────────

resource "aws_sns_topic" "notifications" {
  name = "${var.project_name}-notifications"
}

resource "aws_sns_topic_policy" "notifications" {
  arn = aws_sns_topic.notifications.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowAccountPublish"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "SNS:Publish"
        Resource  = aws_sns_topic.notifications.arn
      },
      {
        Sid       = "AllowCloudWatchPublish"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = "SNS:Publish"
        Resource  = aws_sns_topic.notifications.arn
        Condition = {
          StringEquals = {
            "AWS:SourceAccount" = data.aws_caller_identity.current.account_id
          }
        }
      }
    ]
  })
}

resource "aws_iam_role" "chatbot" {
  name = "${var.project_name}-chatbot-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "chatbot.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "chatbot_readonly" {
  role       = aws_iam_role.chatbot.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

resource "aws_chatbot_slack_channel_configuration" "code_testing" {
  configuration_name = "${var.project_name}-code-testing"
  iam_role_arn       = aws_iam_role.chatbot.arn
  slack_team_id      = var.slack_chatbot_workspace_id
  slack_channel_id   = var.slack_chatbot_channel_id
  sns_topic_arns     = [aws_sns_topic.notifications.arn, aws_sns_topic.runaway_alarm.arn]

  depends_on = [aws_iam_role_policy_attachment.chatbot_readonly]
}

# Outputs
output "api_gateway_events_url" {
  value       = "${aws_api_gateway_stage.prod.invoke_url}/googlemoogle/events"
  description = "API Gateway endpoint URL for Slack Events API (@mentions)"
}

output "api_gateway_slash_url" {
  value       = "${aws_api_gateway_stage.prod.invoke_url}/googlemoogle/slash"
  description = "API Gateway endpoint URL for Slash Commands"
}

output "api_gateway_health_url" {
  value       = "${aws_api_gateway_stage.prod.invoke_url}/googlemoogle/health"
  description = "API Gateway Health check endpoint (GET)"
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

output "agentcore_memory_id" {
  value       = aws_bedrockagentcore_memory.moogle.id
  description = "AgentCore Memory ID for multi-turn conversation state"
}

output "runaway_recovery_command" {
  value       = "aws lambda delete-function-concurrency --function-name ${aws_lambda_function.initial.function_name} --region ${var.aws_region}"
  description = "Run this command to re-enable the bot after a runaway protection throttle"
}

output "notifications_sns_arn" {
  value       = aws_sns_topic.notifications.arn
  description = "Add this SNS ARN to your existing billing alarms so they notify #code-testing via Chatbot"
}
