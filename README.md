# Final Fantasy Moogle Slack Bot

A serverless Slack bot that answers Final Fantasy questions with the personality of a Moogle, built with AWS Lambda, API Gateway, SQS, and OpenAI.

## Architecture

```
Slack → API Gateway → Lambda Authorizer (API key validation - prevents abuse)
                         ↓
              Initial Lambda (Slack signature validation + enqueue + immediate "thinking" response via chat.postMessage)
                         ↓
                         SQS
                         ↓
               Processing Lambda (idempotency check + OpenAI call + Slack response via chat.postMessage)
                         ↓
                        Slack
```

**Security Layers:**
1. **Lambda Authorizer**: Validates API key query parameter (prevents non-Slack callers from hitting the endpoint)
2. **Initial Lambda**: Validates Slack signature in headers (ensures request actually came from Slack)
3. **Processing Lambda**: Idempotency check prevents duplicate processing from Slack retries

**Key Features:**
- **Unified Flow**: Both @mentions and slash commands follow the same pattern: immediate "thinking" message via Web API, then async processing
- **Async Processing**: Initial Lambda responds immediately by posting a "thinking" Moogle message via `chat.postMessage`, processing happens asynchronously
- **Error Handling**: Processing Lambda catches all errors and posts a friendly Moogle-style error message to Slack
- **Idempotency**: S3-based deduplication with 1-day TTL in the processing Lambda prevents duplicate work from Slack retries
- **Security**: Lambda Authorizer validates API Gateway API key (sent as query parameter)
- **Personality**: Responds as a cheerful Moogle from the Final Fantasy series

## Prerequisites

- AWS CLI configured with appropriate credentials
- Terraform >= 1.0
- Python 3.11
- Slack app with:
  - Bot token (`xoxb-...`)
  - Signing secret
  - Event subscriptions AND/OR slash commands enabled

## Setup

### 1. Build Lambda Packages

```bash
chmod +x build.sh
./build.sh
```

This creates:
- `lambda_functions/layer.zip` - Python dependencies (boto3, requests, openai)
- `lambda_functions/authorizer.zip` - API key validator
- `lambda_functions/initial_lambda.zip` - Parse request, post thinking message, enqueue to SQS
- `lambda_functions/processing_lambda.zip` - Idempotency check, OpenAI integration, post final response

### 2. Configure Secrets

```bash
export TF_VAR_openai_api_key='your-openai-api-key'
export TF_VAR_slack_signing_secret='your-slack-signing-secret'
export TF_VAR_slack_bot_token='xoxb-your-bot-token'
```

### 3. Deploy Infrastructure

```bash
cd terraform
terraform init
terraform plan
terraform apply
```

### 4. Configure Slack App

After deployment, Terraform will output the API Gateway URLs. You must append your API key as a query parameter:

#### For @mentions (Events API):
```
https://xxxxx.execute-api.us-east-1.amazonaws.com/prod/googlemoogle/events?api_key=YOUR_API_KEY
```

Configure in Slack:
1. **Event Subscriptions**: Enable and set Request URL to the above
2. **Subscribe to bot events**: Add `app_mention`
3. **OAuth & Permissions**: Ensure your bot has `chat:write` and `app_mentions:read` scopes

#### For Slash Commands:
```
https://xxxxx.execute-api.us-east-1.amazonaws.com/prod/googlemoogle/slash?api_key=YOUR_API_KEY
```

Configure in Slack:
1. **Slash Commands**: Create a new command (e.g., `/moogle`)
2. Set Request URL to the above
3. Add usage hint: "Ask a Final Fantasy question"

**Important**: The API key must be provided as a query parameter (`?api_key=your-key`) since Slack can only send custom data via URL parameters, not custom headers.

## Usage

Once configured, users can:

### 1. Mention the bot:
```
@GoogleMoogle Who is Cloud Strife?
```
The bot will immediately post a "thinking" message in the channel, then post the final answer below it.

### 2. Use slash commands:
```
/moogle How do I beat Emerald Weapon?
```
The bot will immediately post a "thinking" message in the channel, then post the final answer below it.

The bot responds with Moogle personality:
- "Kupo! Cloud Strife is the main protagonist of Final Fantasy VII..."
- "Kupo kupo! To beat Emerald Weapon, you'll need..."

### Error Handling

If something goes wrong during processing, the bot will post a friendly error message:
```
Kupo... I ran into an issue with the Moogle Magic! 

The crystal ball is a bit cloudy right now. Please try asking your question again in a moment, kupo!
```

## Project Structure

```
.
├── build.sh                          # Build script for Lambda packages
├── lambda_functions/
│   ├── authorizer.py                 # API key validator
│   ├── initial_lambda.py             # Parse request, post thinking message, enqueue to SQS
│   ├── processing_lambda.py          # Idempotency check, OpenAI integration, error handling
│   ├── requirements.txt              # Python dependencies
│   └── *.zip                         # Built Lambda packages
└── terraform/
    ├── main.tf                       # All AWS resources
    └── variables.tf                  # Terraform variables
```

## API Gateway Endpoints

After deployment, the following endpoints are available under `/googlemoogle`:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/googlemoogle/events` | POST | Slack Events API (@mentions) |
| `/googlemoogle/slash` | POST | Slack Slash Commands |
| `/googlemoogle/health` | GET | Health check endpoint (tests API Gateway + Authorizer) |

All endpoints require the `api_key` query parameter (e.g., `?api_key=YOUR_KEY`).

## Customization

### Change Idempotency Window
Edit the S3 lifecycle configuration in `terraform/main.tf` (default: 1 day)

### Modify Moogle Personality
Edit the `system_prompt` in `lambda_functions/processing_lambda.py`

### Change OpenAI Model
Update the `model` parameter in `call_openai()` function (default: gpt-4o-mini)

### Change Error Message
Edit `MOOGLE_ERROR_MESSAGE` in `lambda_functions/processing_lambda.py`

## Monitoring

View logs in CloudWatch:
- `/aws/lambda/ff-moogle-bot-initial-lambda`
- `/aws/lambda/ff-moogle-bot-processing-lambda`
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
- S3 idempotency bucket has lifecycle rules to auto-delete old markers (1 day)
- Slack signatures are validated before processing
- API Gateway API key required as query parameter for POST requests (since Slack can't send custom headers)
- No sensitive data is logged

## Testing

### Health Check Endpoint

A simple GET endpoint is available for testing the API Gateway and Authorizer using **API Gateway MOCK integration** (no Lambda invoked):

```bash
# Get the health endpoint URL
curl "$(terraform output -raw api_gateway_health_url)?api_key=$(terraform output -raw api_key_value)"

# Or manually
curl "https://xxx.execute-api.us-east-1.amazonaws.com/prod/googlemoogle/health?api_key=YOUR_KEY"
```

**Expected response:**
```json
{"message": "hello world", "status": "ok"}
```

This uses API Gateway's MOCK integration to return a static response. It tests **only** the API Gateway + Authorizer layer without invoking any Lambda. Use this to verify your API key and basic connectivity before testing Slack integration.

### Testing Tools

See the `tools/` directory for a comprehensive test script:

```bash
cd tools
./test_api.sh <api_gateway_url> <api_key> health
```

## Troubleshooting

**500 Internal Server Error:**
- Check CloudWatch logs: `/aws/lambda/ff-moogle-bot-*`
- Verify Lambda environment variables are set: `terraform show`
- Check for Python syntax errors in deployed code

**Bot not responding:**
- Check CloudWatch logs for errors
- Verify Slack signing secret matches
- Ensure API Gateway URL is correctly configured in Slack
- **Verify the `api_key` query parameter is included in the URL** (e.g., `?api_key=your-key`)

**"Unauthorized" or "Forbidden" errors:**
- Check that the API key query parameter matches the value from `terraform output api_key_value`
- Verify the URL includes `?api_key=YOUR_KEY` at the end

**Duplicate responses:**
- Check S3 idempotency bucket for request markers
- Verify S3 lifecycle rules are configured

**OpenAI errors:**
- Verify `OPENAI_API_KEY` is set correctly
- Check OpenAI API status and rate limits

**API Gateway / Authorizer not working:**
- Authorizer Lambda not invoked (no CloudWatch logs): API Gateway deployment may be stale
  - The deployment has automatic triggers to redeploy when resources change
  - If needed, run `terraform apply` again to trigger a new deployment
- Getting 403 errors: Check API key query parameter is included: `?api_key=YOUR_KEY`
- Methods returning 500 without hitting authorizer: Check deployment succeeded and stage is active
- **"API Gateway does not have permission to assume the provided role"**:
  - The API Gateway authorizer needs a separate IAM role that API Gateway can assume
  - This role must have a trust policy allowing `apigateway.amazonaws.com`
  - The role needs `lambda:InvokeFunction` permission on the authorizer Lambda
  - See `aws_iam_role.api_gateway_authorizer_role` in main.tf
- **"Active stages pointing to this deployment" error**:
  - Don't use `terraform taint` on API Gateway deployments
  - The deployment uses `create_before_destroy` lifecycle to handle updates
  - The stage ignores deployment_id changes to prevent destruction
  - Just run `terraform apply` normally

**Lambda Permission Errors:**
- Check `aws_lambda_permission` resources are created before authorizer
- Verify authorizer credentials IAM role has correct trust policy (apigateway.amazonaws.com)
- CloudWatch logs for authorizer: `/aws/lambda/ff-moogle-bot-authorizer`

**Slash commands not working:**
- Ensure the `/slash` endpoint URL is configured in the Slack app
- Verify the bot token has `chat:write` scope
- Check CloudWatch logs for Initial Lambda to see if requests are being received

## License

MIT License - feel free to modify and use for your own projects, kupo!
