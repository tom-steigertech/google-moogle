import json
import os
import time
import hmac
import hashlib
import boto3
import requests
import logging
from urllib.parse import parse_qs

# Configure logging based on environment variable
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'ERROR').upper()
valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
if LOG_LEVEL not in valid_levels:
    LOG_LEVEL = 'ERROR'

logger = logging.getLogger()
logger.setLevel(getattr(logging, LOG_LEVEL))

sqs = boto3.client('sqs')

SQS_QUEUE_URL = os.environ['SQS_QUEUE_URL']
SLACK_SIGNING_SECRET = os.environ['SLACK_SIGNING_SECRET']
SLACK_BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN', '')

def handler(event, context):
    """
    Initial Lambda - validates Slack signature, enqueues to SQS, posts immediate "thinking" message via Web API.
    Both @mentions and slash commands follow the same flow.
    """
    if LOG_LEVEL == 'DEBUG':
        logger.debug(f"Received event: {json.dumps(event)}")

    body_str = event.get('body', '{}')
    headers = event.get('headers', {})

    # Validate Slack signature
    timestamp = headers.get('X-Slack-Request-Timestamp') or headers.get('x-slack-request-timestamp')
    signature = headers.get('X-Slack-Signature') or headers.get('x-slack-signature')

    if not timestamp or not signature:
        logger.error("Missing Slack signature headers")
        return {'statusCode': 401, 'body': json.dumps({'error': 'Unauthorized'})}

    # Create base string and calculate signature
    base_string = f"v0:{timestamp}:{body_str}"
    my_signature = 'v0=' + hmac.new(
        SLACK_SIGNING_SECRET.encode('utf-8'),
        base_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(my_signature, signature):
        logger.error("Slack signature validation failed")
        return {'statusCode': 401, 'body': json.dumps({'error': 'Unauthorized'})}

    logger.info("Slack signature validation passed")

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

    if LOG_LEVEL == 'DEBUG':
        logger.debug(f"Parsed payload keys: {list(payload.keys())}")

    # Handle Slack URL verification challenge
    if payload.get('type') == 'url_verification' and 'challenge' in payload:
        logger.info("Handling Slack URL verification challenge")
        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'challenge': payload['challenge']})
        }

    # Determine if this is an @mention event or slash command
    event_data = payload.get('event', {})
    is_mention = event_data.get('type') == 'app_mention'
    is_slash_command = payload.get('command') is not None

    logger.info(f"Request type - is_mention: {is_mention}, is_slash_command: {is_slash_command}")

    # Generate request ID
    request_id = generate_request_id(payload)

    # Get channel info
    channel_id = payload.get('channel_id') or event_data.get('channel')
    thread_ts = event_data.get('thread_ts') if is_mention else None

    if LOG_LEVEL == 'DEBUG':
        logger.debug(f"Channel info - channel_id: {channel_id}, thread_ts: {thread_ts}")

    if not channel_id:
        logger.error("No channel_id found in payload")
        return {'statusCode': 200, 'body': json.dumps({'error': 'No channel found'})}

    # Prepare message for SQS
    message = {
        'request_id': request_id,
        'payload': payload,
        'timestamp': time.time(),
        'channel_id': channel_id,
        'thread_ts': thread_ts,
        'is_mention': is_mention,
        'is_slash_command': is_slash_command
    }

    # Send to SQS
    try:
        sqs_response = sqs.send_message(
            QueueUrl=SQS_QUEUE_URL,
            MessageBody=json.dumps(message),
            MessageAttributes={
                'RequestId': {
                    'StringValue': request_id,
                    'DataType': 'String'
                }
            }
        )
        logger.info(f"Message sent to SQS, MessageId: {sqs_response.get('MessageId')}")
    except Exception as e:
        logger.error(f"Failed to send message to SQS: {e}")
        raise

    # Post thinking message
    if channel_id and SLACK_BOT_TOKEN:
        thinking_text = generate_moogle_thinking_text()
        logger.info(f"Posting thinking message to channel {channel_id}")
        result = post_slack_message(channel_id, thinking_text, thread_ts)
        if result:
            logger.info("Thinking message posted successfully")
        else:
            logger.error("Failed to post thinking message")

    # Return response
    if is_slash_command:
        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({
                'text': "Kupo! I'm consulting my crystal ball... check back in a moment for my answer!",
                'response_type': 'ephemeral'
            })
        }
    else:
        return {'statusCode': 200, 'body': json.dumps({'ok': True})}

def generate_request_id(payload):
    """Generate unique request ID using Slack's stable identifiers."""
    import hashlib

    slack_id = None
    id_source = "unknown"

    if payload.get('event', {}).get('event_id'):
        slack_id = payload['event']['event_id']
        id_source = "event_id"
    elif payload.get('event_id'):
        slack_id = payload['event_id']
        id_source = "event_id"
    elif payload.get('trigger_id'):
        slack_id = payload['trigger_id']
        id_source = "trigger_id"
    else:
        slack_id = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:32]
        id_source = "payload_hash"

    logger.info(f"Request ID generated using {id_source}")

    channel = payload.get('channel_id') or payload.get('channel') or payload.get('event', {}).get('channel') or 'unknown'
    user = payload.get('user_id') or payload.get('user') or payload.get('event', {}).get('user') or 'unknown'

    unique_string = f"{slack_id}:{channel}:{user}"
    return hashlib.sha256(unique_string.encode()).hexdigest()[:32]

def generate_moogle_thinking_text():
    """Generate a Moogle-style thinking message text."""
    import random
    moogle_phrases = [
        "Kupo! Let me search through my memories of Final Fantasy for you!",
        "One moment, kupo! Consulting the ancient tomes...",
        "Hmm, let me think about that one, kupo!",
        "Kupo kupo! Searching my crystal ball for answers...",
        "Just a second, kupo! I'll find that information for you!"
    ]
    return random.choice(moogle_phrases)

def post_slack_message(channel_id, text, thread_ts=None):
    """Post a message to Slack using chat.postMessage API."""
    url = 'https://slack.com/api/chat.postMessage'
    headers = {
        'Authorization': f'Bearer {SLACK_BOT_TOKEN}',
        'Content-Type': 'application/json'
    }
    data = {'channel': channel_id, 'text': text}
    if thread_ts:
        data['thread_ts'] = thread_ts

    try:
        logger.info(f"Sending POST to Slack chat.postMessage")
        response = requests.post(url, headers=headers, json=data, timeout=5)
        response_data = response.json()
        
        if response_data.get('ok'):
            logger.info("Slack message posted successfully")
            return True
        else:
            logger.error(f"Slack API error: {response_data.get('error')}")
            return False
    except Exception as e:
        logger.error(f"Exception posting to Slack: {e}")
        return False
