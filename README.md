# Final Fantasy Moogle Slack Bot

A serverless Slack bot that answers Final Fantasy questions with the personality of a Moogle, built with AWS Lambda, API Gateway, SQS, and Claude on Amazon Bedrock. Claude uses tool calling to fetch FFXI item data directly from FFXIclopedia, saving inference tokens when users ask about specific items.

## Architecture

```
Slack → API Gateway → Lambda Authorizer (API key validation - prevents abuse)
                         ↓
              Initial Lambda (Slack signature validation + enqueue + immediate "thinking" response via chat.postMessage)
                         ↓
                         SQS
                         ↓
               Processing Lambda (idempotency check + Claude on Bedrock + ffxi_item_lookup tool + Slack response via chat.postMessage)
                         ↓
                        Slack
```

**Security Layers:**
1. **Lambda Authorizer**: Validates API key query parameter (prevents non-Slack callers from hitting the endpoint)
2. **Initial Lambda**: Validates Slack signature in headers (ensures request actually came from Slack)
3. **Processing Lambda**: Idempotency check prevents duplicate processing from Slack retries

**Key Features:**
- **Multi-turn conversations**: Bot remembers conversation context within a 30-minute idle window. Works in Slack threads and top-level @mentions. Powered by Amazon Bedrock AgentCore Memory.
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
- Amazon Bedrock model access enabled for Anthropic Claude models (AWS Console > Bedrock > Model access)
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
- `lambda_functions/layer.zip` - Python dependencies (boto3, requests, beautifulsoup4)
- `lambda_functions/authorizer.zip` - API key validator
- `lambda_functions/initial_lambda.zip` - Parse request, post thinking message, enqueue to SQS
- `lambda_functions/processing_lambda.zip` - Idempotency check, Claude-on-Bedrock + tool use, post final response

### 2. Configure Secrets

```bash
export TF_VAR_slack_signing_secret='your-slack-signing-secret'
export TF_VAR_slack_bot_token='xoxb-your-bot-token'

# Optional: override the default Claude Haiku 4.5 model
# export TF_VAR_bedrock_model_id='anthropic.claude-haiku-4-5-20251001-v1:0'
```

> The Lambda IAM role handles Bedrock + AgentCore auth — no API keys needed.
> Ensure Bedrock model access is enabled in the deployment region.

### 2.5 Bump Terraform provider

The `hashicorp/aws` provider must be `~> 6.21` for `aws_bedrockagentcore_memory`.
Run `terraform init -upgrade` once to upgrade from `~> 5.x`.

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

### 3. Continue a conversation (multi-turn):
Reply in a thread or send another top-level @mention within 30 minutes:
```
@GoogleMoogle who is Cloud Strife?
... (bot replies) ...
@GoogleMoogle what about Tifa?   ← bot remembers the Cloud context
```
Conversations are scoped per user per channel. A 30-minute idle period starts a fresh context.

### 4. Reset conversation memory:
```
/moogle reset
```
The bot will immediately clear its memory of the current conversation and confirm with a message.

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
├── ffxi_item_lookup.py               # Standalone CLI version of the wiki scraper
├── lambda_functions/
│   ├── authorizer.py                 # API key validator
│   ├── initial_lambda.py             # Parse request, post thinking message, enqueue to SQS
│   ├── processing/                   # Processing Lambda package
│   │   ├── handler.py                # Lambda orchestration (thin layer)
│   │   ├── llm_client.py             # Claude on Bedrock + tool-use loop
│   │   ├── ffxi_item_lookup.py       # FFXIclopedia scraper (Claude tool implementation)
│   │   ├── slack_client.py           # Slack Web API wrapper
│   │   └── utils.py                  # Shared helpers
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
Edit `DEFAULT_PERSONALITY` in `lambda_functions/processing/llm_client.py`

### Change Claude Model on Bedrock
Set `TF_VAR_bedrock_model_id` (or the `bedrock_model_id` Terraform variable)
to another Anthropic model ID supported by Bedrock — e.g.
`anthropic.claude-sonnet-4-6`. Default is Claude Haiku 4.5
(`anthropic.claude-haiku-4-5-20251001-v1:0`).

### Tune Tool Use
The `ffxi_item_lookup` tool definition lives in
`lambda_functions/processing/llm_client.py` (`FFXI_ITEM_LOOKUP_TOOL`). Adjust
the description to tighten/loosen when Claude decides to call it. The lookup
itself, including the response trimming (`max_vendors`, `max_drops`), is in
`lambda_functions/processing/ffxi_item_lookup.py`.

### Change Error Message
Edit `ERROR_MESSAGE` in `lambda_functions/processing/slack_client.py`

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

**Bedrock errors:**
- `AccessDeniedException` on `bedrock:InvokeModel`: enable the chosen Claude model in AWS Console > Bedrock > Model access, in the deployment region
- `ValidationException` about model ID: verify `BEDROCK_MODEL_ID` env var on the Processing Lambda matches an active Anthropic model
- Throttling: Bedrock has per-account/region quotas; check CloudWatch and request a quota increase if needed
- Tool loop errors: check Processing Lambda logs for `Tool call:` / `Tool error:` entries; ffxi_item_lookup hits ffxiclopedia.fandom.com — transient network failures appear as tool errors

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
