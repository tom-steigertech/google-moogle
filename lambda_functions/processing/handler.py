"""Handler for the Processing Lambda - thin orchestration layer.

This module contains the Lambda handler entry point and orchestrates:
- SQS message consumption
- Idempotency checking (S3)
- LLM response generation (via MoogleLLMClient)
- Slack response posting (via MoogleSlackClient)
"""

import json
import boto3

from .llm_client import MoogleLLMClient
from .slack_client import MoogleSlackClient
from .utils import setup_logging, extract_question, truncate_text, get_env_var

# Initialize AWS clients
s3 = boto3.client('s3')
sqs = boto3.client('sqs')

# Initialize module-level logger (will be configured on first use)
logger = None
llm_client = None
slack_client = None


def _initialize_clients():
    """Initialize singleton clients if not already done.
    
    This is called lazily on first request to ensure environment is ready.
    """
    global logger, llm_client, slack_client
    
    if logger is None:
        logger = setup_logging()
    
    if llm_client is None:
        llm_client = MoogleLLMClient(
            api_key=get_env_var('OPENAI_API_KEY'),
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
            
            # Generate response via LLM
            logger.info("Calling OpenAI API")
            answer = llm_client.generate_response(question)
            logger.info(f"OpenAI response received, length: {len(answer)}")
            
            # Send to Slack
            logger.info(f"Sending response to Slack")
            slack_client.send_response(
                channel_id=channel_id,
                text=answer,
                thread_ts=thread_ts,
                is_mention=is_mention
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
