# Final Fantasy Moogle Slack Bot

A serverless Slack bot that answers Final Fantasy XI questions with the personality of a Moogle. Built on AWS Lambda, API Gateway, SQS, and Claude on Amazon Bedrock. The bot uses tool calling to fetch live data from FFXI wikis and display structured item cards in Slack, rather than hallucinating from training data.

## Architecture

```
Slack → API Gateway → Lambda Authorizer (API key validation)
                         ↓
              Initial Lambda
              - Slack signature validation
              - Posts "thinking..." message immediately via chat.postMessage
              - Enqueues work to SQS
                         ↓
                        SQS
                         ↓
               Processing Lambda
               - Idempotency check (S3)
               - Claude Haiku 4.5 on Bedrock + tool-use loop
               - Posts final answer to Slack via chat.postMessage
                         ↓
                        Slack
```

**Security layers:**
1. **Lambda Authorizer** — validates `?api_key=` query parameter; rejects non-Slack callers before they reach any Lambda
2. **Initial Lambda** — validates Slack HMAC signature; ensures requests actually came from Slack
3. **Processing Lambda** — idempotency check prevents duplicate processing from Slack retries

**LLM tools (Claude decides when to call these):**
| Tool | When used | Source |
|------|-----------|--------|
| `ffxi_item_lookup` | User asks about a specific item's price, vendors, or drop sources | FFXIclopedia (Fandom wiki) |
| `ffxi_wiki_search` | Any FFXI question about quests, jobs, spells, zones, NPCs, mechanics | BG-Wiki → FFXIclopedia fallback |

When `ffxi_item_lookup` succeeds, a formatted item card is posted to Slack and the LLM's flavor text is suppressed. When it fails, the LLM replies in Moogle voice. Wiki search content is fed back to the LLM to answer the question.

**Cost controls:**
- **Runaway circuit breaker** — CloudWatch alarm fires when the initial Lambda is invoked ≥ 30 times/minute (runaway loop or abuse); sets reserved concurrency to 0, causing API Gateway to return 429 without invoking Lambda
- **Budget circuit breaker** — When monthly spend hits 100% of the $10 budget, the same throttle Lambda fires
- Both triggers also post a Slack notification to `#code-testing` via AWS Chatbot
- Recovery: `aws lambda delete-function-concurrency --function-name ff-moogle-bot-initial --region us-east-1`

## Key Features

- **Multi-turn conversations** — Bot remembers conversation context within a 10-minute idle window, scoped per user per channel. Powered by Amazon Bedrock AgentCore Memory.
- **Async processing** — Initial Lambda responds immediately with a "thinking" message; heavy work happens in the processing Lambda triggered by SQS
- **Two-wiki search** — BG-Wiki searched first; automatically falls back to FFXIclopedia when BG-Wiki has no content
- **Idempotency** — S3-based deduplication (1-day TTL) prevents duplicate responses from Slack retries
- **Runaway + budget protection** — Dual circuit breakers with Slack alerting via AWS Chatbot

## Prerequisites

- AWS CLI configured with appropriate credentials (us-east-1)
- Terraform >= 1.0
- Python 3.11
- Amazon Bedrock model access enabled for **Claude Haiku 4.5** via cross-region inference profile (`us.anthropic.claude-haiku-4-5-20251001-v1:0`) — enable in AWS Console > Bedrock > Model access
- Slack app with bot token (`xoxb-...`), signing secret, and Event Subscriptions + Slash Commands enabled

## Setup

### 1. Build Lambda packages

```bash
chmod +x build.sh
./build.sh
```

Creates:
- `lambda_functions/layer.zip` — Python dependencies (boto3, requests, beautifulsoup4)
- `lambda_functions/authorizer.zip` — API key validator
- `lambda_functions/initial_lambda.zip` — Slack request parsing + enqueue
- `lambda_functions/processing_lambda.zip` — Idempotency + LLM + tool use + Slack response

### 2. Configure secrets

Copy `env.sh.example` (or create `env.sh`) and fill in your values:

```bash
export TF_VAR_slack_signing_secret='your-slack-signing-secret'
export TF_VAR_slack_bot_token='xoxb-your-bot-token'

source env.sh
```

The Lambda IAM role handles Bedrock and AgentCore auth — no additional API keys needed.

### 3. Deploy

```bash
cd terraform
terraform init
terraform apply
```

On first run, `terraform init -upgrade` may be needed to pull `hashicorp/aws ~> 6.21` (required for `aws_bedrockagentcore_memory`).

### 4. Configure Slack app

After deployment, Terraform outputs the API Gateway URLs. Append your API key as a query parameter to each URL — Slack can only send custom data via URL parameters, not custom headers.

#### Event Subscriptions (@mentions)
```
https://xxxxx.execute-api.us-east-1.amazonaws.com/prod/googlemoogle/events?api_key=YOUR_KEY
```
- Enable Event Subscriptions in the Slack app settings
- Subscribe to bot event: `app_mention`
- Required OAuth scopes: `chat:write`, `app_mentions:read`

#### Slash Commands
```
https://xxxxx.execute-api.us-east-1.amazonaws.com/prod/googlemoogle/slash?api_key=YOUR_KEY
```
- Create a new slash command (e.g. `/moogle`) pointing to the above URL
- Required OAuth scope: `chat:write`

## Usage

### @mention
```
@GoogleMoogle Where do I get a Beehive Chip?
```
The bot posts a "thinking..." message immediately, then posts an item card plus a Moogle-flavored reply.

### Slash command
```
/moogle How do I unlock Blue Mage?
```
Same async flow — immediate acknowledgement, then answer.

### Multi-turn conversation
```
@GoogleMoogle Who is Shantotto?
... (bot replies) ...
@GoogleMoogle What job is she?    ← bot remembers the prior context
```
Context is per-user per-channel, scoped to a 10-minute idle window. Start a new conversation by waiting 10 minutes or using `/moogle reset`.

### Reset conversation memory
```
/moogle reset
```

## Project Structure

```
.
├── build.sh                              # Builds all Lambda zips + layer
├── env.sh                                # (gitignored) TF_VAR_* secrets
├── lambda_functions/
│   ├── authorizer.py                     # API key validator (Lambda Authorizer)
│   ├── initial_lambda.py                 # Slack request parsing, thinking msg, SQS enqueue
│   ├── throttle/
│   │   └── throttle.py                   # Sets reserved concurrency=0 (circuit breaker)
│   ├── processing/
│   │   ├── handler.py                    # Lambda entry point + orchestration
│   │   ├── llm_client.py                 # Bedrock Converse API + tool-use loop
│   │   ├── ffxi_item_lookup.py           # FFXIclopedia scraper (item tool)
│   │   ├── ffxi_wiki_search.py           # BG-Wiki search + FFXIclopedia fallback (wiki tool)
│   │   ├── memory_client.py              # AgentCore Memory (multi-turn state)
│   │   ├── slack_client.py               # Slack Web API wrapper
│   │   └── utils.py                      # Shared helpers
│   └── requirements.txt                  # boto3, requests, beautifulsoup4
└── terraform/
    ├── main.tf                           # All AWS resources
    └── variables.tf                      # Configurable variables
```

## API Gateway Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/googlemoogle/events` | POST | Slack Events API (@mentions) |
| `/googlemoogle/slash` | POST | Slack Slash Commands |
| `/googlemoogle/health` | GET | Health check — MOCK integration, no Lambda invoked |

All endpoints require `?api_key=YOUR_KEY`.

## Monitoring

CloudWatch log groups:
- `/aws/lambda/ff-moogle-bot-initial`
- `/aws/lambda/ff-moogle-bot-processing`
- `/aws/lambda/ff-moogle-bot-authorizer`
- `/aws/lambda/ff-moogle-bot-runaway-throttle`

AWS Chatbot sends alarm notifications to the configured Slack channel (`#code-testing` by default) when:
- The runaway alarm fires (≥ 30 invocations/min)
- Monthly spend reaches 85% or 100% of budget (forecasted or actual)

## Customization

### Change the LLM model
Set `TF_VAR_bedrock_model_id` before running `terraform apply`. Must be a cross-region inference profile ID for Anthropic models, e.g. `us.anthropic.claude-sonnet-4-6`. Default: `us.anthropic.claude-haiku-4-5-20251001-v1:0`.

### Modify Moogle personality
Edit `DEFAULT_PERSONALITY` in `lambda_functions/processing/llm_client.py`.

### Tune tool-use behavior
Tool descriptions live in `llm_client.py` (`FFXI_ITEM_LOOKUP_TOOL`, `FFXI_WIKI_SEARCH_TOOL`). Adjust the descriptions to tighten or loosen when Claude decides to call each tool.

### Adjust the runaway threshold
Set `TF_VAR_runaway_alarm_threshold` (default: 30 invocations/minute).

### Change the conversation idle window
Set `SESSION_IDLE_MINUTES` in the Lambda environment variables in `terraform/main.tf` (default: 10).

## Health Check

Test API Gateway and the Authorizer without invoking any processing Lambda:

```bash
curl "$(terraform -chdir=terraform output -raw api_gateway_health_url)?api_key=$(terraform -chdir=terraform output -raw api_key_value)"
```

Expected response:
```json
{"message": "hello world", "status": "ok"}
```

## Troubleshooting

**Bot not responding / 500 errors**
- Check CloudWatch logs: `/aws/lambda/ff-moogle-bot-initial` and `ff-moogle-bot-processing`
- Verify Slack signing secret and bot token are correct (`terraform show`)
- Confirm the `?api_key=` query parameter is present in the Slack app URL configuration

**"Unauthorized" / 403**
- API key mismatch — compare `?api_key=` value against `terraform output api_key_value`

**Duplicate responses**
- Check S3 idempotency bucket for stale markers
- Verify S3 lifecycle rules are in place (1-day expiry)

**Bedrock errors**
- `AccessDeniedException`: Enable Claude Haiku 4.5 in AWS Console > Bedrock > Model access. Use the cross-region profile ID (`us.anthropic.claude-haiku-4-5-20251001-v1:0`), not the bare model ID.
- `ValidationException` on model ID: Anthropic models require the `us.` cross-region inference profile prefix — the bare model ID is not accepted.
- Tool errors: `ffxi_item_lookup` and `ffxi_wiki_search` hit external sites; transient failures appear in logs as `Tool error:` entries.

**Circuit breaker fired (bot returns 429)**
- Check `#code-testing` in Slack for the alarm notification
- Re-enable the bot after investigating:
  ```bash
  aws lambda delete-function-concurrency --function-name ff-moogle-bot-initial --region us-east-1
  ```

**Wiki search returning nothing**
- BG-Wiki is tried first, FFXIclopedia second. If both return no content the LLM answers from its training data.
- Temporarily set `TF_VAR_log_level=DEBUG` and redeploy to see search queries and results in CloudWatch.

## Cleanup

```bash
cd terraform
terraform destroy
```

The idempotency S3 bucket may need to be manually emptied first if it contains objects.

## Security Notes

- Slack secrets are passed as Lambda environment variables (not logged)
- S3 idempotency bucket is private with a 1-day object lifecycle
- HMAC Slack signature validated on every request before any processing
- API key required as `?api_key=` query parameter (Slack cannot send custom headers)
- Circuit breakers limit runaway spend automatically

## License

MIT License — feel free to modify and use for your own projects, kupo!
