import json
import os
import logging

# Configure logging based on environment variable
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'ERROR').upper()
valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
if LOG_LEVEL not in valid_levels:
    LOG_LEVEL = 'ERROR'

logger = logging.getLogger()
logger.setLevel(getattr(logging, LOG_LEVEL))

API_GATEWAY_KEY = os.environ.get('API_GATEWAY_API_KEY', '')

def handler(event, context):
    """
    Lambda Authorizer - validates API key for both GET and POST methods.
    Default deny - only allows GET and POST with valid API key.
    """
    # Only log full event at DEBUG level
    if LOG_LEVEL == 'DEBUG':
        logger.debug(f"Full event: {json.dumps(event)}")

    query_params = event.get('queryStringParameters') or {}
    http_method = event.get('httpMethod', '')
    method_arn = event.get('methodArn')

    logger.info(f"Authorizer called: method={http_method}")

    # Only GET and POST are authorized methods
    if http_method not in ('GET', 'POST'):
        logger.warning(f"Method {http_method} not allowed - only GET and POST authorized")
        return generate_policy('user', 'Deny', method_arn)

    # Validate API key for ALL allowed methods (GET and POST)
    api_key = query_params.get('api_key') or query_params.get('X-API-Key') or query_params.get('x-api-key')

    if not api_key:
        logger.error(f"No API key provided for {http_method} request")
        return generate_policy('user', 'Deny', method_arn)

    # Log key lengths for debugging without exposing values
    if LOG_LEVEL == 'DEBUG':
        expected_len = len(API_GATEWAY_KEY)
        received_len = len(api_key)
        logger.debug(f"API key length check - expected: {expected_len}, received: {received_len}")

    if api_key != API_GATEWAY_KEY:
        logger.error(f"API key validation failed for {http_method} request")
        return generate_policy('user', 'Deny', method_arn)

    logger.info(f"API key validated successfully for {http_method} request")
    return generate_policy('user', 'Allow', method_arn)

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
