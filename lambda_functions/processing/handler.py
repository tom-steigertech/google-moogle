"""Handler for the Processing Lambda - thin orchestration layer.

This module contains the Lambda handler entry point and orchestrates:
- SQS message consumption
- Idempotency checking (S3)
- LLM response generation (via MoogleLLMClient)
- Slack response posting (via MoogleSlackClient)
"""

import json
from datetime import datetime, timezone

import boto3

from .llm_client import MoogleLLMClient
from .memory_client import load_recent_turns, save_turn
from .notes_client import recent_notes
from .slack_client import MoogleSlackClient
from .utils import (
    setup_logging,
    get_ops_logger,
    extract_question,
    truncate_text,
    get_env_var,
)

# Initialize AWS clients
s3 = boto3.client('s3')
sqs = boto3.client('sqs')

# Initialize module-level logger (will be configured on first use)
logger = None
ops_logger = None
llm_client = None
slack_client = None


def _initialize_clients():
    """Initialize singleton clients if not already done.
    
    This is called lazily on first request to ensure environment is ready.
    """
    global logger, ops_logger, llm_client, slack_client

    if logger is None:
        logger = setup_logging()

    if ops_logger is None:
        ops_logger = get_ops_logger()

    if llm_client is None:
        llm_client = MoogleLLMClient(
            model_id=get_env_var('BEDROCK_MODEL_ID', required=False),
            region_name=get_env_var('BEDROCK_REGION', required=False),
            log_level=get_env_var('LOG_LEVEL', required=False, default='ERROR')
        )
    
    if slack_client is None:
        slack_client = MoogleSlackClient(
            bot_token=get_env_var('SLACK_BOT_TOKEN'),
            log_level=get_env_var('LOG_LEVEL', required=False, default='ERROR')
        )


def handler(event, context):
    """Lambda handler - orchestrates processing of SQS messages.
    
    Flow:
    1. Parse SQS message
    2. Check idempotency (S3 head_object)
    3. Write idempotency marker (S3 put_object)
    4. Extract question from payload
    5. Generate response via LLM
    6. Send response to Slack
    7. Delete SQS message
    
    On error: Sends error message to Slack and deletes SQS message
    """
    global logger
    
    # Initialize on first run
    _initialize_clients()
    
    S3_BUCKET = get_env_var('S3_BUCKET_IDEMPOTENCY')
    SQS_QUEUE_URL = get_env_var('SQS_QUEUE_URL')
    
    records = event.get('Records', [])
    logger.info(f"Processing Lambda invoked with {len(records)} records")
    
    for record in records:
        receipt_handle = record.get('receiptHandle')
        request_id = None
        channel_id = None
        thread_ts = None
        is_mention = False
        is_slash_command = False
        
        try:
            # Parse message
            message = json.loads(record['body'])
            payload = message['payload']
            request_id = message.get('request_id')
            
            logger.info(f"Processing request: {request_id}")

            # Extract message metadata
            channel_id = message.get('channel_id')
            thread_ts = message.get('thread_ts')
            is_mention = message.get('is_mention', False)
            is_slash_command = message.get('is_slash_command', False)
            actor_id = message.get('actor_id', '')
            session_id = message.get('session_id', '')
            
            logger.debug(
                f"Message info - channel_id: {channel_id}, "
                f"is_mention: {is_mention}, is_slash_command: {is_slash_command}"
            )
            
            if not channel_id:
                logger.error(f"No channel_id for request {request_id} - deleting malformed message")
                _delete_sqs_message(SQS_QUEUE_URL, receipt_handle)
                continue
            
            # Check idempotency
            if _is_duplicate(S3_BUCKET, request_id):
                logger.info(f"Duplicate request detected: {request_id}")
                _delete_sqs_message(SQS_QUEUE_URL, receipt_handle)
                continue
            
            # Write idempotency marker
            _write_idempotency_marker(S3_BUCKET, request_id)
            logger.info(f"Idempotency marker written for {request_id}")
            
            # Extract question
            question = extract_question(payload)
            logger.info(f"Extracted question: {truncate_text(question)}")

            # Build multi-turn messages array from AgentCore Memory
            MEMORY_ID = get_env_var('AGENTCORE_MEMORY_ID', required=False, default='')
            IDLE_MINUTES = int(get_env_var('SESSION_IDLE_MINUTES', required=False, default='30'))
            prior_turns = []
            if MEMORY_ID and actor_id and session_id:
                prior_turns = load_recent_turns(MEMORY_ID, actor_id, session_id, IDLE_MINUTES)
                logger.info(f"Loaded {len(prior_turns)} prior turn(s) from session {session_id!r}")
            messages = prior_turns + [{"role": "user", "content": question}]

            # Load user-contributed notes for context (best-effort).
            try:
                notes_for_context = recent_notes(S3_BUCKET, limit=50)
                if notes_for_context:
                    logger.info(f"Loaded {len(notes_for_context)} note(s) for context")
            except Exception as notes_err:
                logger.error(f"Failed to load notes: {notes_err}")
                notes_for_context = []

            # Post an interim "this will take a moment" message if the LLM
            # escalates a complex question to the deep-planning tier.
            def _notify_escalation(goal: str):
                try:
                    logger.info(f"Posting escalation notice for goal: {truncate_text(goal)}")
                    slack_client.send_response(
                        channel_id=channel_id,
                        text=_escalation_notice(goal),
                        thread_ts=thread_ts,
                        is_mention=is_mention,
                    )
                except Exception as notice_err:
                    logger.error(f"Failed to post escalation notice: {notice_err}")

            # Generate response via LLM (Claude on Bedrock + tool use)
            logger.info("Calling Bedrock Converse API")
            answer, item_lookups, escalated = llm_client.generate_response(
                messages,
                notes=notes_for_context,
                note_context={
                    "bucket": S3_BUCKET,
                    "author_id": actor_id,
                    "channel_id": channel_id,
                },
                escalation_notifier=_notify_escalation,
            )
            logger.info(
                f"Bedrock response received, length: {len(answer)}, "
                f"item_lookups: {len(item_lookups)}, escalated: {escalated}"
            )

            if escalated:
                # The planner tier returns a synthesized prose answer that already
                # weaves in any looked-up numbers, so we post it directly and skip
                # the item cards (they'd be redundant supplementary noise here).
                logger.info("Posting planner answer (escalated)")
                try:
                    slack_client.send_response(
                        channel_id=channel_id,
                        text=answer,
                        thread_ts=thread_ts,
                        is_mention=is_mention,
                    )
                except Exception as slack_err:
                    logger.error(f"Failed to post planner answer: {slack_err}")
            else:
                # Post item data cards first (found items only; LLM handles not-found).
                # Non-fatal: a card failure shouldn't block the flavor text or memory save.
                for item_data in item_lookups:
                    if _card_is_informative(item_data):
                        blocks = slack_client.format_item_card(item_data)
                        if blocks:
                            item_name = item_data.get("name", "Item")
                            logger.info(f"Posting item card for: {item_name}")
                            try:
                                card_resp = slack_client.send_blocks(
                                    channel_id=channel_id,
                                    blocks=blocks,
                                    text=f"Item data: {item_name}",
                                    thread_ts=thread_ts,
                                    is_mention=is_mention,
                                )
                                # If drops overflow the card, post the full list in a thread on the card
                                drops = item_data.get("drops", [])
                                if len(drops) > MoogleSlackClient.DROPS_INLINE or item_data.get("drops_truncated"):
                                    card_ts = card_resp.get("ts") if card_resp else None
                                    if card_ts:
                                        drop_blocks = slack_client.format_drops_thread(item_data)
                                        if drop_blocks:
                                            logger.info(f"Posting full drop list thread for: {item_name}")
                                            slack_client.send_blocks(
                                                channel_id=channel_id,
                                                blocks=drop_blocks,
                                                text=f"Full drop list for {item_name}",
                                                thread_ts=card_ts,
                                            )
                            except Exception as card_err:
                                logger.error(f"Failed to post item card for {item_name}: {card_err}")

                # Only suppress LLM flavor text when at least one item card actually
                # carries acquisition info (vendors, drops, crafting, or AH). A
                # "found" item with none of that (e.g. a craft-only page we couldn't
                # parse) yields a near-empty card, so we let the LLM's text answer
                # through to fill the gap instead of leaving the user with a bare card.
                any_informative_card = any(
                    _card_is_informative(il) for il in item_lookups
                )
                if not any_informative_card:
                    logger.info("Sending response to Slack")
                    try:
                        slack_client.send_response(
                            channel_id=channel_id,
                            text=answer,
                            thread_ts=thread_ts,
                            is_mention=is_mention
                        )
                    except Exception as slack_err:
                        logger.error(f"Failed to post flavor text: {slack_err}")
                else:
                    logger.info("Item card posted — suppressing LLM flavor text")

            # Persist turns to AgentCore Memory (best-effort; don't fail the request)
            if MEMORY_ID and actor_id and session_id:
                try:
                    save_turn(MEMORY_ID, actor_id, session_id, "user", question)
                    save_turn(MEMORY_ID, actor_id, session_id, "assistant", answer)
                    logger.info("Turns saved to AgentCore Memory")
                except Exception as mem_err:
                    logger.error(f"Failed to save turns to memory: {mem_err}")

            # Operational audit log: the question asked, the time, and which tools
            # were used to build the answer. Goes to CloudWatch and the ops Slack
            # channel. Best-effort — never lets a logging failure break the request.
            _log_operation(
                request_id=request_id,
                question=question,
                channel_id=channel_id,
                tool_calls=getattr(llm_client, "last_tool_calls", []),
                escalated=escalated,
            )

            logger.info(f"Successfully processed request {request_id}")
            _delete_sqs_message(SQS_QUEUE_URL, receipt_handle)
            
        except Exception as e:
            logger.error(
                f"Error processing request {request_id}: {str(e)}",
                exc_info=True
            )
            
            # Post error message to Slack
            if channel_id:
                try:
                    logger.info(f"Posting error message to Slack for request {request_id}")
                    slack_client.send_error_message(
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                        is_mention=is_mention
                    )
                except Exception as slack_error:
                    logger.error(f"Failed to post error message: {slack_error}")
            
            _delete_sqs_message(SQS_QUEUE_URL, receipt_handle)
    
    return {'statusCode': 200}


def _escalation_notice(goal: str) -> str:
    """Moogle-voice interim message shown while the planner tier works.

    Posted as soon as the front-line model escalates so the user knows a more
    careful (and slower) answer is coming rather than staring at silence.
    """
    goal = truncate_text((goal or "").strip(), 200)
    return (
        f"Ooh, that's a meaty one, kupo! Let me put my full pom-pom power to it "
        f"and work out: _{goal}_\n\nGive me a moment, kupo kupo!"
    )


def _log_operation(request_id, question, channel_id, tool_calls, escalated):
    """Emit an end-of-processing operation summary to CloudWatch and Slack.

    Records the initial question, the date/time, and the tools called while
    building the answer. CloudWatch gets a compact, greppable JSON line (filter
    on "MOOGLE_OPS" in Logs Insights); the ops Slack channel gets a readable
    card. Both paths are best-effort and never raise.

    Tool calls arrive as [{"name", "input"}, ...] from MoogleLLMClient.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    tool_calls = tool_calls or []

    # --- CloudWatch (always on, independent of LOG_LEVEL via the ops logger) ---
    try:
        ops_logger.info("MOOGLE_OPS " + json.dumps({
            "request_id": request_id,
            "timestamp": timestamp,
            "question": truncate_text(question or "", 500),
            "channel_id": channel_id,
            "escalated": bool(escalated),
            "tool_count": len(tool_calls),
            "tools": tool_calls,
        }))
    except Exception as cw_err:
        logger.error(f"Failed to write operation log to CloudWatch: {cw_err}")

    # --- Slack ops channel (optional; skipped when not configured) ---
    ops_channel = get_env_var("OPS_LOG_SLACK_CHANNEL_ID", required=False, default="")
    if not ops_channel:
        return
    try:
        if tool_calls:
            tools_str = ", ".join(
                f"`{t.get('name', '?')}`"
                + (f" ({t['input']})" if t.get("input") else "")
                for t in tool_calls
            )
        else:
            tools_str = "_none — answered directly_"
        text = (
            f"*Moogle op log* · {timestamp}\n"
            f"*Question:* {truncate_text(question or '(none)', 300)}\n"
            f"*Asked in:* <#{channel_id}>\n"
            f"*Tools called:* {tools_str}\n"
            f"*Planner escalation:* {'yes' if escalated else 'no'}"
        )
        slack_client.send_response(channel_id=ops_channel, text=text)
    except Exception as slack_err:
        logger.error(f"Failed to post operation log to Slack: {slack_err}")


def _card_is_informative(item_data: dict) -> bool:
    """True if an item lookup result has acquisition info worth a card.

    Used to decide whether to suppress the LLM's flavor text: a found item with
    no vendors, drops, crafting, or auction-house data produces a bare card, so
    we keep the flavor text in that case.
    """
    if not item_data.get("found"):
        return False
    return bool(
        item_data.get("vendors")
        or item_data.get("drops")
        or item_data.get("synthesis")
        or item_data.get("synthesis_crafts")
        or item_data.get("how_to_obtain")
        or item_data.get("auction_house")
    )


def _is_duplicate(s3_bucket: str, request_id: str) -> bool:
    """Check if this request has already been processed.
    
    Returns True if duplicate, False otherwise.
    Raises exception on S3 errors (other than 404).
    """
    s3_key = f"idempotency/{request_id}"
    
    try:
        s3.head_object(Bucket=s3_bucket, Key=s3_key)
        return True  # Object exists = duplicate
    except s3.exceptions.ClientError as e:
        if e.response['Error']['Code'] == '404':
            return False  # Object doesn't exist = not duplicate
        raise  # Other S3 errors are real errors


def _write_idempotency_marker(s3_bucket: str, request_id: str):
    """Write an idempotency marker to S3."""
    s3_key = f"idempotency/{request_id}"
    s3.put_object(Bucket=s3_bucket, Key=s3_key, Body=b'')


def _delete_sqs_message(queue_url: str, receipt_handle: str):
    """Delete message from SQS queue after processing."""
    if receipt_handle:
        try:
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)
            logger.info("Message deleted from SQS")
        except Exception as e:
            logger.error(f"Error deleting SQS message: {e}")
