# GoogleMoogle Decision Log

Record of technical decisions, alternatives considered, and rationale.

---

## Decision 1: Framework & Deployment Platform

**Date**: 2026-03-28

### Question
How should we build and deploy the Slack bot?

### Options Considered

**Option A: Flask + Traditional Server**
- Pros: Simple, familiar framework, easy debugging
- Cons: Need to run 24/7, pay for always-on compute, harder to scale
- Cost: ~$5-20/month (server)

**Option B: AWS Lambda + API Gateway** ✅ CHOSEN
- Pros: Pay per use, auto-scaling, serverless, Slack-native integration
- Cons: Cold starts, event-driven model requires different thinking
- Cost: ~$0.20 per 1M requests

**Option C: Google Cloud Functions / Azure Functions**
- Pros: Similar to Lambda, multi-cloud flexibility
- Cons: Similar costs, no strong reason to switch from AWS

### Decision
**AWS Lambda + API Gateway**

**Rationale**: 
- Slack is event-driven; Lambda is event-driven
- Cost scales with usage, not uptime
- No maintenance overhead
- Industry standard for Slack bots

---

## Decision 2: Python Runtime Version

**Date**: 2026-03-29

### Question
Which Python version should we use for Lambda?

### Options Considered

**Option A: Python 3.8**
- Pros: Available on Ubuntu 20.04 locally
- Cons: AWS deprecated it; dependency conflicts; older
- Risk: Won't work with modern libraries

**Option B: Python 3.9/3.10**
- Pros: Supported by AWS, good dependency support
- Cons: Need to install separately on Ubuntu 20.04
- Risk: None

**Option C: Python 3.11+** ✅ CHOSEN (for Lambda)
- Pros: Latest, best performance, full support
- Cons: Required separate installation locally
- Risk: None

### Decision
**Python 3.10 for production, 3.11 locally for development**

**Rationale**:
- AWS supports 3.10+ (3.8 deprecated)
- Dependencies (slack-sdk, openai) target 3.10+
- Used Miniconda for isolated local dev environment
- Packaged dependencies target 3.10

---

## Decision 3: Slack Bot Authentication

**Date**: 2026-03-29

### Question
How should we secure the API Gateway endpoint?

### Options Considered

**Option A: API Key in Query Parameter**
```
?x-api-key=YOUR_KEY
```
- Pros: Simple, visible in URL
- Cons: Less secure, visible in logs, API Gateway validation failed
- Status: ❌ Attempted, didn't work

**Option B: API Key in Request Header**
```
x-api-key: YOUR_KEY
```
- Pros: Standard practice, more secure
- Cons: Slack doesn't support custom headers in Event Subscriptions
- Status: ❌ Not feasible with Slack

**Option C: Slack Signature Validation Only** ✅ CHOSEN
- Pros: Built into slack-sdk, cryptographically signed, no custom headers needed
- Cons: Only secures against non-Slack sources
- Status: ✅ Working

**Option D: Lambda Authorizer**
- Pros: Full control, any auth method
- Cons: More complex, additional Lambda invocations = cost
- Status: ⏸ Deferred for future

### Decision
**Slack Signature Validation Only (No API Key)**

**Rationale**:
- Slack signs every request with a secret
- slack-sdk validates signature automatically
- Only requests from Slack will be accepted
- Simpler than API key management
- No additional cost
- Sufficient security for internal bot

**Future Enhancement**: Can add Lambda Authorizer + API Key later if needed for stricter access control.

---

## Decision 4: Slack Interaction Library

**Date**: 2026-03-29

### Question
Should we use slack-bolt or slack-sdk only?

### Options Considered

**Option A: slack-bolt (Full Framework)**
- Pros: Handles routing, middleware, built-in features
- Cons: Overkill for simple bot, handler issues with API Gateway
- Status: ❌ Attempted, handler had 404 issues

**Option B: slack-sdk Only** ✅ CHOSEN
- Pros: Lightweight, direct control, works perfectly
- Cons: More manual code, less abstraction
- Status: ✅ Working

### Decision
**slack-sdk only, manual event handling**

**Rationale**:
- slack-bolt's handler didn't work with API Gateway format
- Manual event handling is ~50 lines of code
- Full control over request/response flow
- Smaller dependency, faster cold starts
- Signature validation still included in slack-sdk

**Note**: If bot complexity grows (multiple handlers, middleware), can refactor back to slack-bolt.

---

## Decision 5: Error Handling & Retry Logic

**Date**: 2026-03-29

### Question
How should we handle OpenAI API failures?

### Options Considered

**Option A: Fail Silently**
- Pros: Simple
- Cons: User doesn't know why bot didn't respond
- Status: ❌ Poor UX

**Option B: Post Error Message to Slack** ✅ CHOSEN
- Pros: User knows there was an issue, can troubleshoot
- Cons: Reveals error details (could be a security risk)
- Status: ✅ Implemented with limited error messages

**Option C: Retry with Exponential Backoff**
- Pros: Handles temporary API outages
- Cons: Adds complexity, Slack waits 30 seconds (timeout)
- Status: ⏸ Deferred

### Decision
**Post error message to Slack, no retries**

**Rationale**:
- User sees issue and can report it
- Transient errors: User can ask again
- Persistent errors: Logged in CloudWatch for debugging
- Retry logic would hit Lambda timeout (30 seconds)

---

## Decision 6: Content Enrichment (Future)

**Date**: 2026-03-29

### Question
Should we add web scraping for richer responses?

### Options Considered

**Option A: No Web Scraping (MVP)** ✅ CHOSEN
- Pros: Simple, fast, low cost, works now
- Cons: Responses are generic, not detailed
- Status: ✅ Implemented

**Option B: Real-time Web Scraping**
- Pros: Always current
- Cons: Slow (3-5s per request), timeout risk, higher cost
- Status: ⏸ Deferred

**Option C: Cached Web Scraping**
- Pros: Fast, detailed, reasonable cost
- Cons: Data can be stale, requires DynamoDB
- Status: ⏸ Future enhancement

**Option D: Pre-scraped Data Store**
- Pros: Instant responses, zero timeout risk
- Cons: Limited to pre-scraped data, requires maintenance
- Status: ⏸ Future enhancement

### Decision
**No web scraping in MVP**

**Rationale**:
- Bot works and is useful with just ChatGPT
- Adding scraping adds complexity and cost
- Can add later with caching strategy
- Helps us understand usage patterns first

**Planned for v2**: Cached web scraping with DynamoDB

---

## Decision 7: Infrastructure as Code

**Date**: 2026-03-29

### Question
Should we manage AWS infrastructure with IaC?

### Options Considered

**Option A: Manual AWS Console** ❌ Current State
- Pros: Visual, no learning curve for non-engineers
- Cons: Error-prone, hard to reproduce, no version control
- Risk: Drift over time

**Option B: Terraform** ✅ CHOSEN
- Pros: Version controlled, reproducible, standard tool, CI/CD ready
- Cons: Learning curve, requires AWS credentials
- Status: ✅ Implemented

**Option C: AWS CloudFormation**
- Pros: Native AWS tool, integrated
- Cons: YAML/JSON syntax, less readable than Terraform
- Status: ⏸ Alternative

### Decision
**Terraform**

**Rationale**:
- Industry standard for multi-cloud IaC
- Easy CI/CD integration
- Version control + code review friendly
- Reproducible deployments
- Enables future automation

---

## Decision 8: CI/CD Pipeline

**Date**: 2026-03-29

### Question
How should we set up deployment automation?

### Options Considered

**Option A: Manual deployment**
- Current state
- Cons: Error-prone, slow, hard to track changes

**Option B: GitHub Actions** ✅ PLANNED
- Pros: Free, integrated with GitHub, easy setup
- Plan:
  1. Push to main → runs tests
  2. Tests pass → Terraform plan
  3. Approve → Terraform apply + deploy Lambda

**Option C: GitLab CI / Jenkins**
- Cons: More setup, not needed for this scale

### Decision
**GitHub Actions (planned for next phase)**

**Rationale**:
- Free tier sufficient for this project
- Close to code (GitHub)
- Terraform integration is straightforward

---

## Summary: Current State vs. MVP vs. Future

### Current Implementation (v1.0)
✅ Slack bot responds to @mentions
✅ Calls OpenAI ChatGPT for FF11 advice
✅ Posts responses back to Slack
✅ Deployed on AWS Lambda
✅ Event-driven, pay-per-use
✅ Signature validation for security

### Planned v2.0
⏳ Web scraping for rich content
⏳ Cached data with DynamoDB
⏳ Image support (zone maps, item icons)
⏳ Block Kit formatting for rich messages
⏳ CI/CD with GitHub Actions + Terraform

### Potential Future Enhancements
- API key authentication (Lambda Authorizer)
- Rate limiting per user/channel
- Analytics and usage tracking
- Multi-language support
- Thread support for conversations
- Persistent conversation history

---

## Architecture Decision Records

### Why Lambda over containerized solutions?
- Slack events are bursty (not constant)
- Serverless = pay only for invocations
- No cold start concerns for user-facing bot
- Easier deployment than ECS/Fargate

### Why not use slack-bolt?
- Initial attempts had handler routing issues
- Direct control over event flow is simpler
- slack-sdk provides all needed functionality
- slack-bolt can be added back if complexity grows

### Why no API key protection currently?
- Slack signs every request (cryptographic)
- API Gateway API key had authentication issues
- Signature validation is sufficient for MVP
- Can add Lambda Authorizer later without changing architecture

### Why no web scraping in v1?
- Adds latency and cost
- FFXIclopedia changes infrequently
- ChatGPT can answer most questions
- Caching strategy will be better than real-time scraping
