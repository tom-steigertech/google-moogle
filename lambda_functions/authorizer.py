import json
import hmac
import hashlib
import os

SLACK_SIGNING_SECRET = os.environ['SLACK_SIGNING_SECRET']
API_GATEWAY_KEY = os.environ.get('API_GATEWAY_API_KEY', '')

def handler(event, context):
    """
    Lambda Authorizer to validate Slack request signatures and API Gateway API key.
    For POST methods, API key must be provided as a query parameter.
    """
    headers = event.get('headers', {})
    query_params = event.get('queryStringParameters') or {}
    http_method = event.get('httpMethod', '')
    
    # Validate API Key for POST methods (sent as query parameter since Slack can't send custom headers)
    if http_method == 'POST':
        api_key = query_params.get('api_key') or query_params.get('X-API-Key') or query_params.get('x-api-key')
        if not api_key or api_key != API_GATEWAY_KEY:
            print(f"API key validation failed for {http_method} request")
            return generate_policy('user', 'Deny', event.get('methodArn'))
    
    # Validate Slack signature
    timestamp = headers.get('X-Slack-Request-Timestamp') or headers.get('x-slack-request-timestamp')
    signature = headers.get('X-Slack-Signature') or headers.get('x-slack-signature')
    body = event.get('body', '')
    
    if not timestamp or not signature:
        print("Missing Slack signature headers")
        return generate_policy('user', 'Deny', event.get('methodArn'))
    
    # Create base string
    base_string = f"v0:{timestamp}:{body}"
    
    # Calculate signature
    my_signature = 'v0=' + hmac.new(
        SLACK_SIGNING_SECRET.encode('utf-8'),
        base_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    
    # Compare signatures
    if hmac.compare_digest(my_signature, signature):
        return generate_policy('user', 'Allow', event.get('methodArn'))
    else:
        print("Slack signature validation failed")
        return generate_policy('user', 'Deny', event.get('methodArn'))

def generate_policy(principal_id, effect, resource):
    auth_response = {
        'principalId': principal_id
    }
    
    if effect and resource:
        auth_response['policyDocument'] = {
            'Version': '2012-10-17',
            'Statement': [{
                'Action': 'execute-api:Invoke',
                'Effect': effect,
                'Resource': resource
            }]
        }
    
    return auth_response
