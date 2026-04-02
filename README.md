# Final Fantasy Moogle Slack Bot

A serverless Slack bot that answers Final Fantasy questions with the personality of a Moogle, built with AWS Lambda, API Gateway, SQS, and OpenAI.

## Architecture

```
Slack → API Gateway → Lambda Authorizer (API key validation - prevents abuse)
                         ↓
              Initial Lambda (Slack signature validation + enqueue + immediate "thinking" response)
                         ↓
                         SQS
                         ↓
               Processing Lambda (idempotency check + OpenAI call + Slack response)
                         ↓
                        Slack
```

**Security Layers:**
1. **Lambda Authorizer**: Validates API key query parameter (prevents non-Slack callers from hitting the endpoint)
2. **Initial Lambda**: Validates Slack signature in headers (ensures request actually came from Slack)
3. **Processing Lambda**: Idempotency check prevents duplicate processing from Slack retries

**Key Features:**
- **Async Processing**: Initial Lambda responds immediately with a "thinking" Moogle message, processing happens asynchronously
- **Lightweight Initial Lambda**: No S3 calls for faster cold starts - just parse, enqueue, and respond
- **Idempotency**: S3-based deduplication with 5-minute TTL in the processing Lambda prevents duplicate work from Slack retries
- **Security**: Lambda Authorizer validates both Slack signatures AND API Gateway API key (sent as query parameter)
- **Personality**: Responds as a cheerful Moogle from the Final Fantasy series

## Prerequisites

- AWS CLI configured with appropriate credentials
- Terraform >= 1.0
- Python 3.11
- Slack app with:
  - Bot token (`xoxb-...`)
  - Signing secret
  - Event subscriptions or slash commands enabled

## Setup

### 1. Build Lambda Packages

```bash
chmod +x build.sh
./build.sh
```

This creates:
- `lambda_functions/layer.zip` - Python dependencies (boto3, requests, openai)
- `lambda_functions/authorizer.zip` - Slack signature validator
- `lambda_functions/initial_lambda.zip` - Lightweight enqueue (no S3 calls for fast cold start)
- `lambda_functions/processing_lambda.zip` - OpenAI integration

### 2. Configure Secrets

```bash
export TF_VAR_openai_api_key='your-openai-api-key'
export TF_VAR_slack_signing_secret='your-slack-signing-secret'
export TF_VAR_slack_bot_token='xoxb-your-bot-token'
export TF_VAR_api_gateway_api_key='your-custom-api-key'
```

The `api_gateway_api_key` is a custom secret you define. It will be required as a query parameter in all POST requests to validate requests.

### 3. Deploy Infrastructure

```bash
cd terraform
terraform init
terraform plan
terraform apply
```

### 4. Configure Slack App

After deployment, Terraform will output the API Gateway URL. You must append your API key as a query parameter:

```
https://xxxxx.execute-api.us-east-1.amazonaws.com/prod/slack/events?api_key=your-custom-api-key
```

Configure your Slack app:
1. **Event Subscriptions**: Enable and set the Request URL to the URL with the `api_key` query parameter
2. **Slash Commands**: Create commands pointing to the same URL with the `api_key` query parameter
3. **OAuth & Permissions**: Ensure your bot has `chat:write` scope

**Important**: The API key must be provided as a query parameter (`?api_key=your-key`) since Slack can only send custom data via URL parameters, not custom headers.

## Usage

Once configured, users can:

1. **Mention the bot**: `@MoogleBot Who is Cloud Strife?`
2. **Use slash commands**: `/ff ask How do I beat Emerald Weapon?`
3. **Send DMs**: Direct messages work automatically

The bot will respond with Moogle personality:
- "Kupo! Cloud Strife is the main protagonist of Final Fantasy VII..."
- "Kupo kupo! To beat Emerald Weapon, you'll need..."

## Project Structure

```
.
├── build.sh                          # Build script for Lambda packages
├── lambda_functions/
│   ├── authorizer.py                 # Slack signature validator
│   ├── initial_lambda.py             # Lightweight enqueue (no S3 calls)
│   ├── processing_lambda.py          # Idempotency check + OpenAI + Slack response
│   ├── requirements.txt              # Python dependencies
│   └── *.zip                         # Built Lambda packages
└── terraform/
    ├── main.tf                       # All AWS resources
    └── variables.tf                  # Terraform variables
```

## Customization

### Change Idempotency Window
Edit `IDEMPOTENCY_TTL_MINUTES` in `terraform/variables.tf` (default: 5 minutes)

### Modify Moogle Personality
Edit the `system_prompt` in `lambda_functions/processing_lambda.py`

### Change OpenAI Model
Update the `model` parameter in `call_openai()` function (default: gpt-5-nano-2025-08-07)

## Monitoring

View logs in CloudWatch:
- `/aws/lambda/ff-moogle-bot-initial`
- `/aws/lambda/ff-moogle-bot-processing`
- `/aws/lambda/ff-moogle-bot-authorizer`

Check SQS metrics for queue depth and processing times.

## Cleanup

```bash
cd terraform
terraform destroy
```

This will remove all AWS resources but won't delete the S3 bucket if it has objects. You may need to manually empty and delete the idempotency bucket.

## Security Notes

- Secrets are passed as environment variables to Lambdas
- S3 idempotency bucket has lifecycle rules to auto-delete old markers
- Slack signatures are validated before processing
- API Gateway API key required as query parameter for POST requests (since Slack can't send custom headers)
- No sensitive data is logged

## Troubleshooting

**Bot not responding:**
- Check CloudWatch logs for errors
- Verify Slack signing secret matches
- Ensure API Gateway URL is correctly configured in Slack
- **Verify the `api_key` query parameter is included in the URL** (e.g., `?api_key=your-key`)

**"Unauthorized" or "Forbidden" errors:**
- Check that the API key query parameter matches the `TF_VAR_api_gateway_api_key` value
- Verify the URL includes `?api_key=your-custom-api-key` at the end

**Duplicate responses:**
- Check S3 idempotency bucket for request markers
- Verify S3 lifecycle rules are configured

**OpenAI errors:**
- Verify `OPENAI_API_KEY` is set correctly
- Check OpenAI API status and rate limits

## License

MIT License - feel free to modify and use for your own projects, kupo!
