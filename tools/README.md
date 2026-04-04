# Testing Tools for pyGoogleMoogle

This directory contains testing utilities for the pyGoogleMoogle Slack Bot.

## test_api.sh

A bash script to test the API Gateway endpoints locally.

### Prerequisites

- `curl` installed
- `jq` (optional, for pretty-printing JSON responses)
- Your API Gateway URL and API key (from Terraform outputs)

### Usage

```bash
./test_api.sh <api_gateway_url> <api_key> [test_type]
```

### Examples

```bash
# Run all tests
./test_api.sh https://xxx.execute-api.us-east-1.amazonaws.com/prod myapikey123

# Test specific scenarios (start with health!)
./test_api.sh https://xxx.execute-api.us-east-1.amazonaws.com/prod myapikey123 health     # Simplest test first
./test_api.sh https://xxx.execute-api.us-east-1.amazonaws.com/prod myapikey123 challenge
./test_api.sh https://xxx.execute-api.us-east-1.amazonaws.com/prod myapikey123 command
./test_api.sh https://xxx.execute-api.us-east-1.amazonaws.com/prod myapikey123 mention
```

### Test Types

| Test | Description | Expected Result |
|------|-------------|-----------------|
| `health` | Health check endpoint (GET) | Returns `{"message": "hello world"}` - 200 (API Gateway MOCK) |
| `challenge` | Slack URL verification | Returns challenge value |
| `command` | Slash command (/googlemoogle) | Returns Moogle "thinking" message |
| `event` | Generic event callback | 200 or 401 (signature check) |
| `mention` | App mention event | 200 or 401 (signature check) |
| `signature` | Missing signature test | 401 Unauthorized |
| `all` | All tests (default) | Mixed results |

**Start with `health`** - It's the simplest test that uses API Gateway MOCK integration (no Lambda invoked) to validate only the API Gateway + Authorizer layer.

### Understanding Results

- **200 + challenge**: URL verification working
- **200 + Moogle message**: Slash command responding
- **401**: Signature validation working (requests without valid Slack signatures are rejected)

### Important Notes

1. **Architecture - Two-Layer Security**:
   - **API Gateway Authorizer**: Validates only the **API key** (sent as `?api_key=...` query parameter)
   - **Initial Lambda**: Validates **Slack request signatures** (using `X-Slack-Signature` and `X-Slack-Request-Timestamp` headers)

   This separation of concerns allows:
   - API-level protection at the gateway
   - Application-level Slack-specific validation in the Lambda
   - Better error handling and logging

2. **Why 401 Errors?**: The test script sends dummy Slack headers that fail signature validation in the **initial Lambda**. This is **expected behavior** - the Lambda correctly validates Slack signatures. Only real Slack requests with valid signatures will pass.

   - **403** = API Gateway rejected (bad API key) - check your key
   - **401** = Initial Lambda rejected (bad Slack signature) - expected for local tests
   - **200** = Both validations passed - working correctly!

3. **Real Testing**: For full end-to-end testing, use the actual Slack app:
   - Add the bot to your workspace
   - Send `/googlemoogle` commands
   - @mention the bot in channels

4. **LocalStack**: For full local testing with AWS services, consider using [LocalStack](https://localstack.cloud/).

### Getting Your API Details

After Terraform deployment:

```bash
cd ../terraform
terraform output api_gateway_googlemoogle_url
terraform output -raw api_key_value
```

### Troubleshooting

**401 vs 403 - Understanding the Response**

**403 "Forbidden" - API Key Issue**
- The **API Gateway Authorizer** rejected the request
- Check that your API key is correct: `terraform output -raw api_key_value`
- Ensure the key is in the query string: `?api_key=YOUR_KEY`

**401 "Unauthorized" - Slack Signature Issue (Expected for Local Testing)**
- The **Initial Lambda** rejected the request (not the API Gateway)
- Your API key is correct (otherwise you'd get 403)
- The Lambda validates Slack signatures using `SLACK_SIGNING_SECRET`
- Local test scripts can't generate valid signatures without the secret
- **This is expected behavior!** The architecture is working correctly.

**Solution**: Test with the actual Slack app for full validation:
- Add the bot to your workspace
- Configure Slack endpoints with your API key
- Slack automatically generates valid signatures
- Both API key and Slack signature will pass

### Debugging Workflow

When troubleshooting API issues, follow this sequence:

1. **Start with `health` test** (uses API Gateway MOCK, no Lambda):
   ```bash
   ./test_api.sh <url> <key> health
   ```
   - **200 + hello world** = API Gateway + Authorizer working ✓
   - **403** = API key issue (check key value)
   - If health fails, the problem is in API Gateway or Authorizer

2. **Then test `challenge`** (adds Lambda + Slack signature validation):
   ```bash
   ./test_api.sh <url> <key> challenge
   ```
   - **401** = Initial Lambda rejecting signature (expected without Slack secret)
   - **200 + challenge** = Full flow working (use real Slack to achieve this)
   - **500** = Error in initial Lambda (check CloudWatch)

3. **Check CloudWatch Logs**:
   ```bash
   aws logs tail /aws/lambda/ff-moogle-bot-initial-lambda --follow
   aws logs tail /aws/lambda/ff-moogle-bot-authorizer --follow
   ```

**"curl: command not found"**
- Install curl: `sudo apt-get install curl` (Ubuntu/Debian) or `brew install curl` (macOS)

**"jq: command not found"**
- Script works without jq, but install for prettier output: `sudo apt-get install jq` or `brew install jq`

**"Connection refused"**
- Check that the API Gateway URL is correct
- Ensure the `api_key` query parameter is included
- Verify the API is deployed to the `prod` stage

**Authorizer Lambda not invoked (no CloudWatch logs)**
- API Gateway deployment may be stale with old configuration
- The deployment automatically redeploys when resources change via Terraform triggers
- Run `terraform apply` again to ensure latest deployment is active
- Check API Gateway Console > Stages > prod to see active deployment
- Verify authorizer is attached to methods in API Gateway Console
- **Don't use `terraform taint`** on API Gateway deployments - it causes dependency errors

**"403 Forbidden" on health endpoint**
- API key query parameter missing or incorrect
- Authorizer rejecting the request
- Verify: `curl "<url>/health?api_key=$(terraform output -raw api_key_value)"`

**"500 Internal Server Error"**
- This indicates an error in the Lambda code itself
- Check CloudWatch Logs: `/aws/lambda/ff-moogle-bot-*`
- Common causes:
  - Missing environment variables (check `terraform show`)
  - Lambda code errors (check logs for stack traces)
  - IAM permission issues

## Additional Testing

### Manual curl commands

```bash
# Test health endpoint (API Gateway MOCK - no Lambda invoked, tests Auth only)
curl -X GET "https://xxx.execute-api.us-east-1.amazonaws.com/prod/health?api_key=YOUR_KEY"

# Test URL verification (Events API) - expects 401 (no valid Slack signature)
curl -X POST "https://xxx.execute-api.us-east-1.amazonaws.com/prod/slack/events?api_key=YOUR_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Slack-Request-Timestamp: $(date +%s)" \
  -H "X-Slack-Signature: v0=dummysignature" \
  -d '{"type":"url_verification","challenge":"test123"}'

# Test slash command - expects 401 (no valid Slack signature)
curl -X POST "https://xxx.execute-api.us-east-1.amazonaws.com/prod/googlemoogle?api_key=YOUR_KEY" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -H "X-Slack-Request-Timestamp: $(date +%s)" \
  -H "X-Slack-Signature: v0=dummysignature" \
  -d 'command=/googlemoogle&text=chocobo&user_id=U123&channel_id=C123'
```

### AWS CLI Testing

```bash
# Invoke Lambda directly
aws lambda invoke \
  --function-name ff-moogle-bot-initial-lambda \
  --payload '{"body": "{\"type\": \"url_verification\", \"challenge\": \"test\"}"}' \
  response.json
```

## CI/CD Integration

This script can be integrated into CI/CD pipelines:

```yaml
# GitHub Actions example
- name: Test API endpoints
  run: |
    ./tools/test_api.sh \
      $(terraform output -raw api_gateway_url) \
      $(terraform output -raw api_key) \
      all
```
