# GoogleMoogle - Final Fantasy XI Slack Bot

A cheerful Moogle companion that answers Final Fantasy XI questions in Slack, powered by OpenAI's ChatGPT.

## Overview

GoogleMoogle is a serverless Slack bot that:
- ✨ Responds only when explicitly mentioned: `@GoogleMoogle [question]`
- 🧙 Focuses exclusively on Final Fantasy XI topics
- 💬 Uses OpenAI ChatGPT with a Moogle personality
- 🚀 Deployed on AWS Lambda (pay-per-use, auto-scaling)
- 🔐 Secured with Slack request signature validation
- 🏗️ Infrastructure managed with Terraform (IaC ready)

## Architecture

```
Slack Workspace
       ↓
   (mentions bot)
       ↓
API Gateway
       ↓
AWS Lambda (Python 3.10)
       ↓
OpenAI ChatGPT API
       ↓
Slack Workspace (bot posts response)
```

## Current Features (v1.0)

✅ Responds to @mentions in Slack
✅ FF11-focused responses with Moogle personality
✅ Powered by GPT-3.5-turbo
✅ Serverless architecture (AWS Lambda)
✅ Request signature validation (Slack security)
✅ Error handling and logging

## Prerequisites

### For Deployment

- **AWS Account** with appropriate permissions
- **Slack Workspace** (admin access)
- **OpenAI Account** with API access
- **Terraform** installed locally (v1.0+)
- **Python 3.10+** for packaging

### For Development

- Python 3.10+ (or use Miniconda for isolated environment)
- pip or conda
- Git
- AWS CLI configured (optional, for direct Lambda updates)

## Quick Start

### 1. Create Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps)
2. Click **Create New App** → **From scratch** → Name: `GoogleMoogle`
3. Go to **OAuth & Permissions**
   - Add scopes: `chat:write`, `messages:read`
   - Install to Workspace
   - Copy **Bot User OAuth Token** (xoxb-...)
4. Go to **Settings → Basic Information**
   - Copy **Signing Secret**
5. Go to **Event Subscriptions**
   - Enable Events
   - Subscribe to: `app_mention`
   - Save (we'll add the URL after deployment)

### 2. Get OpenAI API Key

1. Go to [platform.openai.com/account/api-keys](https://platform.openai.com/account/api-keys)
2. Create new secret key
3. Copy it (sk-...)

### 3. Prepare Lambda Package

```bash
# Create project directory
mkdir slack-googleMoogle && cd slack-googleMoogle

# Copy these files:
# - lambda_function.py
# - requirements.txt

# Create deployment package
mkdir package
cd package
pip install -r ../requirements.txt -t .
cp ../lambda_function.py .
zip -r ../lambda_function.zip .
cd ..
```

### 4. Deploy with Terraform

```bash
# Copy and configure variables
cp terraform.tfvars.example terraform.tfvars

# Edit terraform.tfvars with your credentials
# aws_region, slack_bot_token, slack_signing_secret, openai_api_key

# Initialize Terraform
terraform init

# Review changes
terraform plan

# Deploy
terraform apply

# Get the API Gateway URL (shown in output)
# Copy the api_gateway_url output
```

### 5. Configure Slack

1. Go back to your Slack app → **Event Subscriptions**
2. Set **Request URL** to the output from Terraform:
   ```
   https://your-api-gateway-url/slack
   ```
3. Wait for green checkmark
4. Save Changes

### 6. Invite Bot to Channel

In Slack:
```
/invite @GoogleMoogle
```

### 7. Test

```
@GoogleMoogle Where do I get Pearlescent Silk?
```

## Project Structure

```
slack-googleMoogle/
├── lambda_function.py          # Main bot code
├── requirements.txt            # Python dependencies
├── lambda_function.zip         # Packaged Lambda (generated)
├── main.tf                      # Terraform main config
├── variables.tf                 # Terraform variables
├── terraform.tfvars.example     # Terraform variable template
├── terraform.tfstate            # Terraform state (don't commit)
├── README.md                    # This file
├── DECISION_LOG.md              # Technical decisions made
├── DEPLOYMENT.md                # Detailed deployment guide
├── TROUBLESHOOTING.md           # Common issues and fixes
└── .gitignore                   # Git ignore rules
```

## Cost Profile

**Monthly Estimated Costs** (light to moderate usage):

- **Lambda**: $0.20 per 1M requests → ~$1-5/month
- **API Gateway**: $3.50 per 1M requests → ~$1-3/month
- **OpenAI API**: ~$0.002 per 1K tokens → ~$5-20/month (depends on usage)

**Total**: ~$10-30/month

Free tier often covers Lambda and API Gateway completely.

## Configuration

### Environment Variables

Set in Lambda via Terraform `terraform.tfvars`:

- `SLACK_BOT_TOKEN` - Bot token from Slack app
- `OPENAI_API_KEY` - OpenAI API key

### Lambda Settings (in `variables.tf`)

- `lambda_timeout` - Request timeout (default: 30s)
- `lambda_memory` - Memory allocation (default: 256MB)
- `aws_region` - AWS region (default: us-east-2)

## Monitoring

### CloudWatch Logs

```bash
# View Lambda logs
aws logs tail /aws/lambda/slack-googleMoogle --follow
```

Or in AWS Console:
1. Lambda → Your function → Monitor → Logs
2. Click latest log stream

### CloudWatch Alarms

Terraform creates two alarms:
- **Error count** - Alerts if 5+ errors in 5 minutes
- **Duration** - Alerts if avg duration > 15 seconds

Optional: Set `alarm_sns_topic` in `terraform.tfvars` to send alerts to SNS.

## Updating the Bot

### Code Changes

```bash
# 1. Update lambda_function.py

# 2. Rebuild package
cd package
rm -rf *
pip install -r ../requirements.txt -t .
cp ../lambda_function.py .
zip -r ../lambda_function.zip .
cd ..

# 3. Redeploy with Terraform
terraform apply
```

### Infrastructure Changes

```bash
# Edit variables.tf or terraform.tfvars
# Then redeploy
terraform plan
terraform apply
```

## Security Considerations

- ✅ Slack requests are cryptographically signed (validated by slack-sdk)
- ✅ Sensitive data (tokens, keys) stored in Lambda env vars (not in code)
- ✅ Terraform stores state with sensitive values (use remote backend in production)
- ⚠️ OpenAI API key: Keep secure, rotate periodically
- ⚠️ Terraform state: Use S3 remote backend + DynamoDB locking for team use

## Troubleshooting

See `TROUBLESHOOTING.md` for common issues and solutions.

## Future Roadmap

### v2.0 (Planned)
- [ ] Web scraping for rich FFXIclopedia content
- [ ] Cached item/zone data with DynamoDB
- [ ] Image support (zone maps, item icons)
- [ ] Rich message formatting with Block Kit
- [ ] CI/CD pipeline with GitHub Actions

### Future Enhancements
- [ ] API key authentication (Lambda Authorizer)
- [ ] Per-user rate limiting
- [ ] Usage analytics and tracking
- [ ] Multi-language support
- [ ] Thread-based conversations
- [ ] Persistent chat history

## Technical Decisions

See `DECISION_LOG.md` for:
- Why we chose Lambda + API Gateway
- Why we use slack-sdk instead of slack-bolt
- Why signature validation instead of API keys
- Future enhancement considerations

## References

- [Final Fantasy XI](https://www.ffxiah.com/)
- [FFXIclopedia](https://ffxiclopedia.fandom.com/)
- [Slack API Documentation](https://api.slack.com/)
- [OpenAI API Documentation](https://platform.openai.com/docs/)
- [AWS Lambda Documentation](https://docs.aws.amazon.com/lambda/)
- [Terraform AWS Provider](https://registry.terraform.io/providers/hashicorp/aws/latest)

## Support

For issues:
1. Check `TROUBLESHOOTING.md`
2. Review CloudWatch logs
3. Check `DECISION_LOG.md` for architecture details
4. Refer to error messages in Slack channel

## License

Created for Final Fantasy XI enthusiasts. Enjoy, kupo!