import json
import os
import time
import hmac
import hashlib
import boto3
from urllib.parse import parse_qs

sqs = boto3.client('sqs')

SQS_QUEUE_URL = os.environ['SQS_QUEUE_URL']
SLACK_SIGNING_SECRET = os.environ['SLACK_SIGNING_SECRET']

def handler(event, context):
    """
    Initial Lambda - validates Slack signature, enqueues to SQS, returns immediate response.
    Idempotency check happens in the processing lambda.
    """
    body_str = event.get('body', '{}')
    headers = event.get('headers', {})
    
    # Validate Slack signature
    timestamp = headers.get('X-Slack-Request-Timestamp') or headers.get('x-slack-request-timestamp')
    signature = headers.get('X-Slack-Signature') or headers.get('x-slack-signature')
    
    if not timestamp or not signature:
        print("Missing Slack signature headers")
        return {'statusCode': 401, 'body': json.dumps({'error': 'Unauthorized'})}
    
    # Create base string and calculate signature
    base_string = f"v0:{timestamp}:{body_str}"
    my_signature = 'v0=' + hmac.new(
        SLACK_SIGNING_SECRET.encode('utf-8'),
        base_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    
    # Compare signatures
    if not hmac.compare_digest(my_signature, signature):
        print("Slack signature validation failed")
        return {'statusCode': 401, 'body': json.dumps({'error': 'Unauthorized'})}
    
    # Parse the body based on content type
    content_type = headers.get('Content-Type') or headers.get('content-type', '')
    
    if 'application/x-www-form-urlencoded' in content_type:
        parsed_body = parse_qs(body_str)
        payload = {k: v[0] for k, v in parsed_body.items()}
    else:
        try:
            payload = json.loads(body_str)
        except json.JSONDecodeError:
            payload = {}
    
    # Generate request ID from timestamp + channel + user for idempotency
    request_id = generate_request_id(payload)
    
    # Prepare message for SQS
    message = {
        'request_id': request_id,
        'payload': payload,
        'response_url': payload.get('response_url'),
        'timestamp': time.time()
    }
    
    # Send to SQS
    sqs.send_message(
        QueueUrl=SQS_QUEUE_URL,
        MessageBody=json.dumps(message),
        MessageAttributes={
            'RequestId': {
                'StringValue': request_id,
                'DataType': 'String'
            }
        }
    )
    
    # Return immediate "thinking" response
    moogle_response = generate_moogle_thinking_message(payload)
    
    return {
        'statusCode': 200,
        'headers': {
            'Content-Type': 'application/json'
        },
        'body': json.dumps(moogle_response)
    }

def generate_request_id(payload):
    """Generate unique request ID from key payload fields."""
    import hashlib
    
    # Use combination of timestamp, channel, user, and text
    timestamp = payload.get('event', {}).get('ts') or payload.get('trigger_id') or str(time.time())
    channel = payload.get('channel_id') or payload.get('channel') or 'unknown'
    user = payload.get('user_id') or payload.get('user') or 'unknown'
    text = payload.get('text') or payload.get('command') or ''
    
    unique_string = f"{timestamp}:{channel}:{user}:{text}"
    return hashlib.sha256(unique_string.encode()).hexdigest()[:32]

def generate_moogle_thinking_message(payload):
    """Generate a Moogle-style thinking message."""
    moogle_phrases = [
        "Kupo! Let me search through my memories of Final Fantasy for you!",
        "One moment, kupo! Consulting the ancient tomes...",
        "Hmm, let me think about that one, kupo!",
        "Kupo kupo! Searching my crystal ball for answers...",
        "Just a second, kupo! I'll find that information for you!"
    ]
    
    import random
    thinking_message = random.choice(moogle_phrases)
    
    # Check if this is a slash command or event
    if payload.get('command'):
        return {
            'text': thinking_message,
            'response_type': 'ephemeral'
        }
    else:
        return {
            'text': thinking_message
        }
