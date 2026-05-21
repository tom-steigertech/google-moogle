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
s3 = boto3.client('s3')
agentcore = boto3.client(
    'bedrock-agentcore',
    region_name=os.environ.get('BEDROCK_REGION') or os.environ.get('AWS_REGION', 'us-east-1')
)

SQS_QUEUE_URL = os.environ['SQS_QUEUE_URL']
SLACK_SIGNING_SECRET = os.environ['SLACK_SIGNING_SECRET']
SLACK_BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN', '')
AGENTCORE_MEMORY_ID = os.environ.get('AGENTCORE_MEMORY_ID', '')
IDLE_MINUTES = int(os.environ.get('SESSION_IDLE_MINUTES', '10'))
S3_BUCKET_IDEMPOTENCY = os.environ.get('S3_BUCKET_IDEMPOTENCY', '')

# Notes storage — kept in sync with lambda_functions/processing/notes_client.py
NOTES_KEY = "notes/notes.json"
MAX_NOTE_LENGTH = 500
MAX_NOTES_RETAINED = 200
NOTES_LISTING_LIMIT = 20

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

    # Drop any event the bot itself generated — bot_id is present on messages
    # sent via chat.postMessage with a bot token; subtype catches legacy format.
    if event_data.get('bot_id') or event_data.get('subtype') == 'bot_message':
        logger.debug("Ignoring bot-generated message event")
        return {'statusCode': 200, 'body': json.dumps({'ok': True})}

    is_mention = event_type == 'app_mention'
    is_thread_reply = (
        event_type == 'message'
        and event_data.get('thread_ts') is not None
        and event_data.get('bot_id') is None
        and event_data.get('subtype') is None
        and event_data.get('user') is not None
    )
    is_channel_message = (
        event_type == 'message'
        and event_data.get('thread_ts') is None
        and event_data.get('bot_id') is None
        and event_data.get('subtype') is None
        and event_data.get('user') is not None
        and '<@' not in event_data.get('text', '')  # @mentions fire app_mention separately
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
    user_session_id = f"{channel_id}_{slack_user_id}"
    if thread_ts:
        safe_ts = thread_ts.replace(".", "_")
        session_id = f"{channel_id}_{safe_ts}"
    else:
        session_id = user_session_id

    # Plain channel messages (non-thread, non-mention): only respond if sender has
    # an active session within the idle window. This prevents the bot from joining
    # general channel chatter.
    if is_channel_message:
        if not _session_is_active(actor_id, user_session_id, IDLE_MINUTES):
            logger.info(f"Channel message ignored — no active session for {actor_id}")
            return {'statusCode': 200, 'body': json.dumps({'ok': True})}
        session_id = user_session_id

    # For thread-based interactions, resolve the best session to use.
    # If the thread session has no memory, fall back to the user's main-channel
    # session — this handles the common case of @mention in the channel followed
    # by a thread reply to the bot's response (different thread_ts, same conversation).
    # Always process thread replies regardless of memory state — silently dropping
    # them when memory is unavailable (e.g. transient API failure) is worse than
    # occasionally responding in an unrelated thread.
    elif thread_ts:
        if not _session_has_memory(actor_id, session_id):
            if session_id != user_session_id and _session_has_memory(actor_id, user_session_id):
                logger.info(f"Thread resolved to user session {user_session_id!r}")
                session_id = user_session_id
            else:
                logger.info(f"No prior session for thread; starting fresh in {session_id!r}")

    # Drop anything that isn't a recognised command type — prevents message.channels
    # events that slipped through the earlier filters from reaching SQS.
    if not (is_mention or is_channel_message or is_thread_reply or is_slash_command):
        logger.info("Unactionable event — skipping SQS enqueue")
        return {'statusCode': 200, 'body': json.dumps({'ok': True})}

    # Easter egg: configurable user gets a special greeting instead of a real answer
    easter_egg_user_id = os.environ.get('EASTER_EGG_USER_ID', '')
    if easter_egg_user_id and slack_user_id == easter_egg_user_id:
        logger.info(f"Easter egg triggered for user {slack_user_id}")
        post_slack_message(channel_id, "What n00b? I couldn't hear you... please show yourself out!", thread_ts)
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

    # Handle /moogle takenote <text> and /moogle notes — inline, no SQS enqueue
    if is_slash_command:
        raw_text = payload.get('text', '').strip()
        text_lower = raw_text.lower()

        if text_lower.startswith('takenote'):
            note_text = raw_text[len('takenote'):].strip()
            if not note_text:
                return _ephemeral("Kupo? You forgot to tell me what to note, kupo!")
            try:
                note = _save_note_inline(note_text, slack_user_id, channel_id)
                return _ephemeral(
                    f"Kupo! I've noted it down: \"{note['text']}\" (id: {note['id']})"
                )
            except Exception as e:
                logger.error(f"Failed to save note: {e}", exc_info=True)
                return _ephemeral("Kupo... my pom-pom got tangled. Couldn't save that note!")

        if text_lower == 'notes':
            try:
                return _ephemeral(_format_notes_listing(_load_notes_inline()))
            except Exception as e:
                logger.error(f"Failed to list notes: {e}", exc_info=True)
                return _ephemeral("Kupo... my pom-pom got tangled. Couldn't read the notes!")

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


def _session_is_active(actor_id: str, session_id: str, idle_minutes: int = 10) -> bool:
    """Return True if the session has memory AND the last event is within idle_minutes."""
    if not AGENTCORE_MEMORY_ID:
        return False
    try:
        resp = agentcore.list_events(
            memoryId=AGENTCORE_MEMORY_ID,
            actorId=actor_id,
            sessionId=session_id,
        )
        events = resp.get('events') or resp.get('memoryEvents') or []
        if not events:
            return False
        from datetime import datetime, timezone

        def _ts(e):
            raw = e.get('eventTimestamp') or ''
            if isinstance(raw, datetime):
                return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
            try:
                return datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
            except Exception:
                return datetime.fromtimestamp(0, tz=timezone.utc)

        last_ts = max(_ts(e) for e in events)
        return (datetime.now(tz=timezone.utc) - last_ts).total_seconds() <= idle_minutes * 60
    except Exception as e:
        logger.error(f"Error checking session activity: {e}")
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


def _ephemeral(text: str) -> dict:
    """Build a Slack ephemeral slash-command response."""
    return {
        'statusCode': 200,
        'headers': {'Content-Type': 'application/json'},
        'body': json.dumps({'text': text, 'response_type': 'ephemeral'})
    }


def _save_note_inline(text: str, author_slack_id: str, channel_id: str) -> dict:
    """Append a user note to s3://<bucket>/notes/notes.json.

    Kept in sync with lambda_functions/processing/notes_client.save_note —
    the initial Lambda is packaged without the processing/ package, so the
    routine is duplicated here rather than imported.
    """
    import uuid
    from datetime import datetime, timezone

    if not S3_BUCKET_IDEMPOTENCY:
        raise RuntimeError("S3_BUCKET_IDEMPOTENCY env var is not set")

    text = (text or "").strip()
    if not text:
        raise ValueError("note text is empty")
    if len(text) > MAX_NOTE_LENGTH:
        text = text[:MAX_NOTE_LENGTH].rstrip() + "…"

    notes = []
    try:
        resp = s3.get_object(Bucket=S3_BUCKET_IDEMPOTENCY, Key=NOTES_KEY)
        loaded = json.loads(resp["Body"].read())
        if isinstance(loaded, list):
            notes = loaded
    except s3.exceptions.NoSuchKey:
        pass
    except Exception as e:
        # If the existing document is unreadable, fall back to starting fresh
        # rather than losing the user's input. The corrupt file will be
        # overwritten on the next write.
        logger.warning(f"Could not load existing notes (starting fresh): {e}")

    note = {
        "id": uuid.uuid4().hex[:12],
        "text": text,
        "author_id": f"slack_{author_slack_id}" if author_slack_id else "",
        "channel_id": channel_id or "",
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    notes.append(note)
    if len(notes) > MAX_NOTES_RETAINED:
        notes = notes[-MAX_NOTES_RETAINED:]

    s3.put_object(
        Bucket=S3_BUCKET_IDEMPOTENCY,
        Key=NOTES_KEY,
        Body=json.dumps(notes).encode("utf-8"),
        ContentType="application/json",
    )
    logger.info(f"Saved note {note['id']} (total now: {len(notes)})")
    return note


def _load_notes_inline() -> list:
    """Read s3://<bucket>/notes/notes.json. Returns [] if missing or unreadable."""
    if not S3_BUCKET_IDEMPOTENCY:
        raise RuntimeError("S3_BUCKET_IDEMPOTENCY env var is not set")
    try:
        resp = s3.get_object(Bucket=S3_BUCKET_IDEMPOTENCY, Key=NOTES_KEY)
        data = json.loads(resp["Body"].read())
        return data if isinstance(data, list) else []
    except s3.exceptions.NoSuchKey:
        return []


def _format_notes_listing(notes: list) -> str:
    """Render the global notes pool as ephemeral slash-command text, newest first."""
    if not notes:
        return ("Kupo! No notes saved yet, kupo! "
                "Use `/moogle takenote <something>` to add one.")

    total = len(notes)
    recent = list(reversed(notes[-NOTES_LISTING_LIMIT:]))  # newest first
    shown = len(recent)

    header = (f"*Moogle notes* ({shown} of {total}, newest first):"
              if shown < total
              else f"*Moogle notes* ({total} total):")

    lines = [header]
    for i, note in enumerate(recent, 1):
        text = (note.get('text') or '').strip()
        author_raw = (note.get('author_id') or '')
        author_slack_id = author_raw.removeprefix('slack_')
        created = (note.get('created_at') or '')[:10]  # YYYY-MM-DD
        author = f"<@{author_slack_id}>" if author_slack_id else "unknown"
        lines.append(f"{i}. \"{text}\" — {author}, {created}")

    return "\n".join(lines)


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
