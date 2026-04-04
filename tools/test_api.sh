#!/bin/bash

# Test script for pyGoogleMoogle Slack Bot API endpoints
# Usage: ./test_api.sh <api_gateway_url> <api_key> [test_type]

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Help function
show_help() {
    cat << EOF
Usage: $0 <api_gateway_url> <api_key> [test_type]

Test the pyGoogleMoogle Slack Bot API endpoints

Arguments:
  api_gateway_url    The API Gateway URL (e.g., https://xxx.execute-api.us-east-1.amazonaws.com/prod)
  api_key            The API key for authentication
  test_type          Optional: specific test to run (all|health|challenge|command|event|mention|signature)
                     Default: all

Examples:
  $0 https://xxx.execute-api.us-east-1.amazonaws.com/prod myapikey123
  $0 https://xxx.execute-api.us-east-1.amazonaws.com/prod myapikey123 health     # Start here!
  $0 https://xxx.execute-api.us-east-1.amazonaws.com/prod myapikey123 challenge  # Tests /googlemoogle/events
  $0 https://xxx.execute-api.us-east-1.amazonaws.com/prod myapikey123 command    # Tests /googlemoogle/slash

Tests:
  all       - Run all tests (default)
  health    - Test health check endpoint (GET, simplest test - uses API Gateway MOCK)
  challenge - Test Slack URL verification challenge
  command   - Test slash command (/googlemoogle)
  event     - Test generic event
  mention   - Test app mention event
  signature - Test signature validation failure

EOF
}

# Check arguments
if [ $# -lt 2 ]; then
    show_help
    exit 1
fi

API_URL="$1"
API_KEY="$2"
TEST_TYPE="${3:-all}"

# Validate URL format
if [[ ! "$API_URL" =~ ^https?:// ]]; then
    echo -e "${RED}Error: API URL must start with http:// or https://${NC}"
    exit 1
fi

# Remove trailing slash from URL
API_URL="${API_URL%/}"

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  pyGoogleMoogle API Test Script${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo "API URL: $API_URL"
echo "Test Type: $TEST_TYPE"
echo ""

# Test 0: Health Check (Simple GET test - tests API Gateway + Authorizer only)
test_health() {
    echo -e "${YELLOW}Test 0: Health Check (GET) - API Gateway MOCK${NC}"
    echo "----------------------------------------"
    echo "This uses API Gateway MOCK integration - no Lambda is invoked."
    echo "Purpose: Test API Gateway + Authorizer layer independently."
    echo ""
    
    local endpoint="$API_URL/googlemoogle/health?api_key=$API_KEY"
    
    echo "Endpoint: $endpoint"
    echo "Method: GET"
    echo "Integration: API Gateway MOCK (static response)"
    echo ""
    echo "Sending request..."
    echo ""
    
    local response
    response=$(curl -s -w "\n%{http_code}" -X GET "$endpoint" 2>&1)
    
    local http_code=$(echo "$response" | tail -n1)
    local body=$(echo "$response" | sed '$d')
    
    echo "HTTP Status: $http_code"
    echo "Response:"
    echo "$body" | jq '.' 2>/dev/null || echo "$body"
    
    if [ "$http_code" -eq 200 ]; then
        if echo "$body" | grep -q "hello world"; then
            echo -e "${GREEN}✓ PASS: Health check returned hello world${NC}"
            echo -e "${GREEN}✓ API Gateway + Authorizer working correctly${NC}"
        else
            echo -e "${YELLOW}⚠ Got 200 but unexpected body${NC}"
        fi
    elif [ "$http_code" -eq 403 ]; then
        echo -e "${RED}✗ FAIL: API Gateway rejected (403) - Invalid API key${NC}"
        echo -e "${YELLOW}Check that your API key is correct${NC}"
    else
        echo -e "${RED}✗ FAIL: Expected 200, got $http_code${NC}"
    fi
    echo ""
}

# Test 1: Slack URL Verification Challenge
test_challenge() {
    echo -e "${YELLOW}Test 1: Slack URL Verification Challenge${NC}"
    echo "----------------------------------------"
    
    local endpoint="$API_URL/googlemoogle/events?api_key=$API_KEY"
    local challenge_value="test-challenge-12345"
    local timestamp=$(date +%s)
    
    local payload=$(cat <<EOF
{
  "type": "url_verification",
  "challenge": "$challenge_value",
  "token": "test-token"
}
EOF
)
    
    echo "Endpoint: $endpoint"
    echo "Payload:"
    echo "$payload" | jq '.' 2>/dev/null || echo "$payload"
    echo ""
    echo "Note: We send dummy Slack headers. The initial Lambda will reject (401)"
    echo "because it can't validate Slack signatures without the signing secret."
    echo "The API Gateway authorizer only checks the API key (which is valid)."
    echo ""
    echo "Sending request..."
    echo ""
    
    local response
    response=$(curl -s -w "\n%{http_code}" -X POST "$endpoint" \
        -H "Content-Type: application/json" \
        -H "X-Slack-Request-Timestamp: $timestamp" \
        -H "X-Slack-Signature: v0=dummysignature" \
        -d "$payload" 2>&1)
    
    local http_code=$(echo "$response" | tail -n1)
    local body=$(echo "$response" | sed '$d')
    
    echo "HTTP Status: $http_code"
    echo "Response:"
    echo "$body" | jq '.' 2>/dev/null || echo "$body"
    
    # API authorizer: 403 = bad API key (rejected at gateway)
    # Initial Lambda: 401 = bad Slack signature (rejected by app logic)
    if [ "$http_code" -eq 401 ]; then
        echo -e "${YELLOW}⚠ Expected 401 - Initial Lambda rejected (invalid Slack signature)${NC}"
        echo -e "${GREEN}✓ API key accepted by authorizer, Slack validation works in Lambda${NC}"
    elif [ "$http_code" -eq 403 ]; then
        echo -e "${RED}✗ FAIL: API Gateway rejected request (check API key)${NC}"
    elif [ "$http_code" -eq 200 ]; then
        if echo "$body" | grep -q "$challenge_value"; then
            echo -e "${GREEN}✓ PASS: Challenge returned correctly${NC}"
        else
            echo -e "${RED}✗ FAIL: Challenge not found in response${NC}"
        fi
    else
        echo -e "${YELLOW}⚠ Unexpected status: $http_code${NC}"
    fi
    echo ""
}

# Test 2: Slash Command (/googlemoogle)
test_command() {
    echo -e "${YELLOW}Test 2: Slash Command (/googlemoogle)${NC}"
    echo "----------------------------------------"
    
    local endpoint="$API_URL/googlemoogle/slash?api_key=$API_KEY"
    local timestamp=$(date +%s)
    local body="token=test-token&team_id=T123&team_domain=test&channel_id=C123&channel_name=general&user_id=U123&user_name=testuser&command=/googlemoogle&text=chocobo&response_url=https://hooks.slack.com/commands/123&trigger_id=test.trigger.id"
    
    echo "Endpoint: $endpoint"
    echo "Content-Type: application/x-www-form-urlencoded"
    echo "Body: $body"
    echo ""
    echo "Note: Dummy Slack headers will be rejected by initial Lambda (401)"
    echo "because Slack signature validation requires the signing secret."
    echo ""
    echo "Sending request..."
    echo ""
    
    local response
    response=$(curl -s -w "\n%{http_code}" -X POST "$endpoint" \
        -H "Content-Type: application/x-www-form-urlencoded" \
        -H "X-Slack-Request-Timestamp: $timestamp" \
        -H "X-Slack-Signature: v0=dummysignature" \
        -d "$body" 2>&1)
    
    local http_code=$(echo "$response" | tail -n1)
    local body_response=$(echo "$response" | sed '$d')
    
    echo "HTTP Status: $http_code"
    echo "Response:"
    echo "$body_response" | jq '.' 2>/dev/null || echo "$body_response"
    
    # 403 = API Gateway rejected (bad API key)
    # 401 = Initial Lambda rejected (bad Slack signature)
    if [ "$http_code" -eq 401 ]; then
        echo -e "${YELLOW}⚠ Expected 401 - Initial Lambda rejected (invalid Slack signature)${NC}"
        echo -e "${GREEN}✓ API key accepted by authorizer, Slack validation in Lambda works${NC}"
    elif [ "$http_code" -eq 403 ]; then
        echo -e "${RED}✗ FAIL: API Gateway rejected (check API key)${NC}"
    elif [ "$http_code" -eq 200 ]; then
        echo -e "${GREEN}✓ PASS: Got 200 response${NC}"
        if echo "$body_response" | grep -qi "moogle\|kupo"; then
            echo -e "${GREEN}✓ PASS: Contains Moogle response${NC}"
        fi
    else
        echo -e "${YELLOW}⚠ Unexpected status: $http_code${NC}"
    fi
    echo ""
}

# Test 3: Generic Event
test_event() {
    echo -e "${YELLOW}Test 3: Generic Event${NC}"
    echo "----------------------------------------"
    
    local endpoint="$API_URL/googlemoogle/events?api_key=$API_KEY"
    local timestamp=$(date +%s)
    local event_id="evt-$(date +%s%N)"
    
    local payload=$(cat <<EOF
{
  "token": "test-token",
  "team_id": "T123",
  "api_app_id": "A123",
  "event": {
    "type": "message",
    "channel": "C123",
    "user": "U123",
    "text": "Hello test",
    "ts": "$timestamp",
    "event_id": "$event_id"
  },
  "type": "event_callback",
  "authed_users": ["U123"]
}
EOF
)
    
    echo "Endpoint: $endpoint"
    echo "Payload:"
    echo "$payload" | jq '.' 2>/dev/null || echo "$payload"
    echo ""
    echo "Sending request..."
    echo ""
    
    local response
    response=$(curl -s -w "\n%{http_code}" -X POST "$endpoint" \
        -H "Content-Type: application/json" \
        -H "X-Slack-Request-Timestamp: $timestamp" \
        -H "X-Slack-Signature: v0=dummysignature" \
        -d "$payload" 2>&1)
    
    local http_code=$(echo "$response" | tail -n1)
    local body=$(echo "$response" | sed '$d')
    
    echo "HTTP Status: $http_code"
    echo "Response:"
    echo "$body" | jq '.' 2>/dev/null || echo "$body"
    
    # 403 = API Gateway rejected (bad API key)
    # 401 = Initial Lambda rejected (bad Slack signature)
    if [ "$http_code" -eq 401 ]; then
        echo -e "${YELLOW}⚠ Expected 401 - Initial Lambda rejected (invalid Slack signature)${NC}"
        echo -e "${GREEN}✓ API authorizer works (accepted key), Lambda validates signatures${NC}"
    elif [ "$http_code" -eq 403 ]; then
        echo -e "${RED}✗ FAIL: API Gateway rejected (check API key)${NC}"
    elif [ "$http_code" -eq 200 ]; then
        echo -e "${GREEN}✓ PASS: Endpoint responding${NC}"
    else
        echo -e "${YELLOW}⚠ Unexpected status code $http_code${NC}"
    fi
    echo ""
}

# Test 4: App Mention Event
test_mention() {
    echo -e "${YELLOW}Test 4: App Mention Event${NC}"
    echo "----------------------------------------"
    
    local endpoint="$API_URL/googlemoogle/events?api_key=$API_KEY"
    local timestamp=$(date +%s)
    local event_id="evt-$(date +%s%N)"
    
    local payload=$(cat <<EOF
{
  "token": "test-token",
  "team_id": "T123",
  "api_app_id": "A123",
  "event": {
    "type": "app_mention",
    "channel": "C123",
    "user": "U123",
    "text": "<@BOTID> Tell me about moogles",
    "ts": "$timestamp",
    "event_id": "$event_id",
    "thread_ts": "$timestamp"
  },
  "type": "event_callback",
  "authed_users": ["U123"]
}
EOF
)
    
    echo "Endpoint: $endpoint"
    echo "Payload:"
    echo "$payload" | jq '.' 2>/dev/null || echo "$payload"
    echo ""
    echo "Sending request..."
    echo ""
    
    local response
    response=$(curl -s -w "\n%{http_code}" -X POST "$endpoint" \
        -H "Content-Type: application/json" \
        -H "X-Slack-Request-Timestamp: $timestamp" \
        -H "X-Slack-Signature: v0=dummysignature" \
        -d "$payload" 2>&1)
    
    local http_code=$(echo "$response" | tail -n1)
    local body=$(echo "$response" | sed '$d')
    
    echo "HTTP Status: $http_code"
    echo "Response:"
    echo "$body" | jq '.' 2>/dev/null || echo "$body"
    
    # 403 = API Gateway rejected (bad API key)
    # 401 = Initial Lambda rejected (bad Slack signature)
    if [ "$http_code" -eq 401 ]; then
        echo -e "${YELLOW}⚠ Expected 401 - Initial Lambda rejected (invalid Slack signature)${NC}"
        echo -e "${GREEN}✓ API authorizer works (accepted key), Lambda validates signatures${NC}"
    elif [ "$http_code" -eq 403 ]; then
        echo -e "${RED}✗ FAIL: API Gateway rejected (check API key)${NC}"
    elif [ "$http_code" -eq 200 ]; then
        echo -e "${GREEN}✓ PASS: Endpoint responding${NC}"
    else
        echo -e "${YELLOW}⚠ WARN: Unexpected status code $http_code${NC}"
    fi
    echo ""
}

# Test 5: Signature Validation Test
test_signature() {
    echo -e "${YELLOW}Test 5: Signature Validation (API Key Only)${NC}"
    echo "----------------------------------------"
    
    local endpoint="$API_URL/googlemoogle/events?api_key=$API_KEY"
    local timestamp=$(date +%s)
    
    local payload='{"type":"test","text":"signature test"}'
    
    echo "Endpoint: $endpoint"
    echo "Testing without Slack signature headers..."
    echo ""
    
    local response
    response=$(curl -s -w "\n%{http_code}" -X POST "$endpoint" \
        -H "Content-Type: application/json" \
        -d "$payload" 2>&1)
    
    local http_code=$(echo "$response" | tail -n1)
    local body=$(echo "$response" | sed '$d')
    
    echo "HTTP Status: $http_code"
    echo "Response:"
    echo "$body" | jq '.' 2>/dev/null || echo "$body"
    
    # 403 = API Gateway rejected (bad API key)
    # 401 = Initial Lambda rejected (missing Slack signature headers)
    if [ "$http_code" -eq 401 ]; then
        echo -e "${GREEN}✓ PASS: Initial Lambda correctly rejected request without Slack signature headers (401)${NC}"
        echo -e "${GREEN}✓ API authorizer only checks API key (which was valid)${NC}"
    elif [ "$http_code" -eq 403 ]; then
        echo -e "${RED}✗ FAIL: API Gateway rejected (check API key)${NC}"
    else
        echo -e "${YELLOW}⚠ WARN: Expected 401 for missing signature, got $http_code${NC}"
    fi
    echo ""
}

# Run tests based on test_type
case "$TEST_TYPE" in
    challenge)
        test_challenge
        ;;
    command)
        test_command
        ;;
    health)
        test_health
        ;;
    event)
        test_event
        ;;
    mention)
        test_mention
        ;;
    signature)
        test_signature
        ;;
    all)
        test_health
        test_challenge
        test_command
        test_event
        test_mention
        test_signature
        ;;
    *)
        echo -e "${RED}Unknown test type: $TEST_TYPE${NC}"
        show_help
        exit 1
        ;;
esac

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  Testing Complete${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo "Architecture:"
echo "  - /googlemoogle/health (MOCK): API Gateway + Authorizer only (no Lambda)"
echo "  - /googlemoogle/events: Slack Events API (@mentions)"
echo "  - /googlemoogle/slash: Slack Slash Commands"
echo "  - API Gateway Authorizer: Validates API key only (403 if invalid)"
echo "  - Initial Lambda: Validates Slack signatures (401 if invalid)"
echo ""
echo "Expected Results:"
echo "  - 200 (health) = API Gateway + Authorizer working ✓"
echo "  - 401 (other) = Initial Lambda rejected dummy signatures (expected)"
echo "  - 403 = API Gateway rejected invalid API key (check your key)"
echo "  - 200 (real) = Both validations passed (use real Slack app)"
echo ""
echo "For full testing with valid Slack signatures, use the actual Slack app."
