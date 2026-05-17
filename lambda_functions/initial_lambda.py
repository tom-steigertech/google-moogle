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
agentcore = boto3.client(
    'bedrock-agentcore',
    region_name=os.environ.get('BEDROCK_REGION') or os.environ.get('AWS_REGION', 'us-east-1')
)

SQS_QUEUE_URL = os.environ['SQS_QUEUE_URL']
SLACK_SIGNING_SECRET = os.environ['SLACK_SIGNING_SECRET']
SLACK_BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN', '')
AGENTCORE_MEMORY_ID = os.environ.get('AGENTCORE_MEMORY_ID', '')

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

    # Determine if this is an @mention, thread reply, or slash command
    event_data = payload.get('event', {})
    event_type = event_data.get('type')
    is_mention = event_type == 'app_mention'
    is_thread_reply = (
        event_type == 'message'
        and event_data.get('thread_ts') is not None
        and event_data.get('bot_id') is None
        and event_data.get('subtype') is None
    )
    is_slash_command = payload.get('command') is not None

    logger.info(f"Request type - is_mention: {is_mention}, is_thread_reply: {is_thread_reply}, is_slash_command: {is_slash_command}")

    # Generate request ID
    request_id = generate_request_id(payload)

    # Get channel / user info
    channel_id = payload.get('channel_id') or event_data.get('channel')
    thread_ts = event_data.get('thread_ts') if (is_mention or is_thread_reply) else None
    slack_user_id = payload.get('user_id') or event_data.get('user') or 'unknown'

    if LOG_LEVEL == 'DEBUG':
        logger.debug(f"Channel info - channel_id: {channel_id}, thread_ts: {thread_ts}, user: {slack_user_id}")

    if not channel_id:
        logger.error("No channel_id found in payload")
        return {'statusCode': 200, 'body': json.dumps({'error': 'No channel found'})}

    # Compute AgentCore Memory session identifiers
    # IDs must match [a-zA-Z0-9][a-zA-Z0-9-_]* — replace colons and dots with underscores
    actor_id = f"slack_{slack_user_id}"
    if thread_ts:
        safe_ts = thread_ts.replace(".", "_")
        session_id = f"{channel_id}_{safe_ts}"
    else:
        session_id = f"{channel_id}_{slack_user_id}"

    # For thread replies, only respond if a prior conversation exists in this thread
    if is_thread_reply and not _session_has_memory(actor_id, session_id):
        logger.info(f"Thread reply ignored — no prior session in {session_id!r}")
        return {'statusCode': 200, 'body': json.dumps({'ok': True})}

    # Handle /moogle reset — clear session and return early (no SQS enqueue)
    if is_slash_command and payload.get('text', '').strip().lower() == 'reset':
        logger.info(f"Reset requested for session {session_id!r}")
        _clear_session_inline(actor_id, session_id)
        post_slack_message(channel_id, "Kupo! I've forgotten our conversation, kupo! Ask me anything fresh!")
        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'text': "Memory cleared, kupo!", 'response_type': 'ephemeral'})
        }

    # Prepare message for SQS
    message = {
        'request_id': request_id,
        'payload': payload,
        'timestamp': time.time(),
        'channel_id': channel_id,
        'thread_ts': thread_ts,
        'is_mention': is_mention,
        'is_slash_command': is_slash_command,
        'actor_id': actor_id,
        'session_id': session_id,
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

def _session_has_memory(actor_id: str, session_id: str) -> bool:
    """Return True if AgentCore Memory has at least one event for this session."""
    if not AGENTCORE_MEMORY_ID:
        return False
    try:
        resp = agentcore.list_events(
            memoryId=AGENTCORE_MEMORY_ID,
            actorId=actor_id,
            sessionId=session_id,
        )
        events = resp.get('events') or resp.get('memoryEvents') or []
        return len(events) > 0
    except Exception as e:
        logger.error(f"Error checking session memory: {e}")
        return False


def _clear_session_inline(actor_id: str, session_id: str, cap: int = 200) -> None:
    """Delete all AgentCore Memory events for a session (for /moogle reset)."""
    if not AGENTCORE_MEMORY_ID:
        logger.warning("AGENTCORE_MEMORY_ID not set; reset is a no-op")
        return
    try:
        events = []
        kwargs = dict(memoryId=AGENTCORE_MEMORY_ID, actorId=actor_id, sessionId=session_id)
        next_token = None
        while True:
            if next_token:
                kwargs['nextToken'] = next_token
            resp = agentcore.list_events(**kwargs)
            events.extend(resp.get('events') or resp.get('memoryEvents') or [])
            next_token = resp.get('nextToken')
            if not next_token:
                break

        if len(events) > cap:
            logger.warning(f"Session has {len(events)} events; deleting first {cap} only")
            events = events[:cap]

        for ev in events:
            event_id = ev.get('eventId') or ev.get('id')
            if event_id:
                agentcore.delete_event(
                    memoryId=AGENTCORE_MEMORY_ID,
                    actorId=actor_id,
                    sessionId=session_id,
                    eventId=event_id,
                )
        logger.info(f"Cleared {len(events)} events from session {session_id!r}")
    except Exception as e:
        logger.error(f"Error clearing session {session_id!r}: {e}")


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
