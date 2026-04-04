import json
import os
import boto3
import requests
import logging
from openai import OpenAI

# Configure logging based on environment variable
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'ERROR').upper()
valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
if LOG_LEVEL not in valid_levels:
    LOG_LEVEL = 'ERROR'

logger = logging.getLogger()
logger.setLevel(getattr(logging, LOG_LEVEL))

s3 = boto3.client('s3')
sqs = boto3.client('sqs')

OPENAI_API_KEY = os.environ['OPENAI_API_KEY']
SLACK_BOT_TOKEN = os.environ['SLACK_BOT_TOKEN']
S3_BUCKET = os.environ['S3_BUCKET_IDEMPOTENCY']
SQS_QUEUE_URL = os.environ['SQS_QUEUE_URL']

client = OpenAI(api_key=OPENAI_API_KEY)

# Error message template
MOOGLE_ERROR_MESSAGE = """Kupo... I ran into an issue with the Moogle Magic! Please try asking your question again in a moment, kupo!"""

def handler(event, context):
    """
    Processing Lambda - consumes SQS messages, checks idempotency, calls OpenAI, sends response.
    """
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
            message = json.loads(record['body'])
            payload = message['payload']
            request_id = message.get('request_id')

            logger.info(f"Processing request: {request_id}")

            channel_id = message.get('channel_id')
            thread_ts = message.get('thread_ts')
            is_mention = message.get('is_mention', False)
            is_slash_command = message.get('is_slash_command', False)

            if LOG_LEVEL == 'DEBUG':
                logger.debug(f"Message info - channel_id: {channel_id}, is_mention: {is_mention}, is_slash_command: {is_slash_command}")

            if not channel_id:
                logger.error(f"No channel_id for request {request_id} - deleting malformed message")
                delete_sqs_message(receipt_handle)
                continue

            # Check idempotency
            s3_key = f"idempotency/{request_id}"
            try:
                s3.head_object(Bucket=S3_BUCKET, Key=s3_key)
                logger.info(f"Duplicate request detected: {request_id}")
                delete_sqs_message(receipt_handle)
                continue
            except s3.exceptions.ClientError as e:
                if e.response['Error']['Code'] != '404':
                    raise
                logger.info(f"Idempotency check passed for {request_id}")

            # Write idempotency marker
            s3.put_object(Bucket=S3_BUCKET, Key=s3_key, Body=b'')
            logger.info(f"Idempotency marker written for {request_id}")

            # Extract question
            question = extract_question(payload)
            logger.info(f"Extracted question (truncated): {question[:50]}...")

            # Call OpenAI
            logger.info("Calling OpenAI API")
            answer = call_openai(question)
            logger.info(f"OpenAI response received, length: {len(answer)}")

            # Send to Slack
            message_info = {
                'channel_id': channel_id,
                'thread_ts': thread_ts,
                'is_mention': is_mention,
                'is_slash_command': is_slash_command
            }
            logger.info(f"Sending response to Slack")
            send_slack_response(answer, message_info)

            logger.info(f"Successfully processed request {request_id}")
            delete_sqs_message(receipt_handle)

        except Exception as e:
            logger.error(f"Error processing request {request_id}: {str(e)}", exc_info=(LOG_LEVEL == 'DEBUG'))

            # Post error message
            if channel_id:
                try:
                    message_info = {
                        'channel_id': channel_id,
                        'thread_ts': thread_ts,
                        'is_mention': is_mention,
                        'is_slash_command': is_slash_command
                    }
                    logger.info(f"Posting error message to Slack for request {request_id}")
                    send_slack_response(MOOGLE_ERROR_MESSAGE, message_info)
                except Exception as slack_error:
                    logger.error(f"Failed to post error message: {slack_error}")

            delete_sqs_message(receipt_handle)

    return {'statusCode': 200}

def extract_question(payload):
    """Extract the user's question from various Slack payload formats."""
    if payload.get('text'):
        return payload['text']

    event = payload.get('event', {})
    if event.get('text'):
        text = event['text']
        if text.startswith('<@'):
            text = text.split(' ', 1)[1] if ' ' in text else ''
        return text

    if payload.get('actions'):
        return payload.get('message', {}).get('text', 'What would you like to know about Final Fantasy?')

    return "Tell me about Final Fantasy!"

def call_openai(question):
    """Call OpenAI API with Moogle personality."""
    system_prompt = """You are a helpful and knowledgeable Moogle (モーグリ) from the Final Fantasy series! 

Your personality traits:
- You end many sentences with "kupo!" or "kupo kupo!"
- You are cheerful, friendly, and eager to help
- You have extensive knowledge of all Final Fantasy games, characters, lore, mechanics, and history
- You speak with a slightly whimsical but informative tone
- You sometimes reference Moogles' roles in various Final Fantasy games (like delivering mail, saving games, running shops, or being playable characters)
- You're particularly fond of mentioning that you have a pom-pom on your head

Answer questions about Final Fantasy games, characters, storylines, gameplay mechanics, and lore. Be thorough but keep responses concise (under 2000 characters for Slack).

Remember: Stay in character as a Moogle!"""

    try:
        if LOG_LEVEL == 'DEBUG':
            logger.debug("Calling OpenAI with model gpt-4o-mini")
        
        response = client.chat.completions.create(
            model="gpt-5-nano",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question}
            ],
            max_tokens=1000,
            temperature=0.7
        )

        result = response.choices[0].message.content
        if LOG_LEVEL == 'DEBUG':
            logger.debug(f"OpenAI response: {result[:100]}...")
        return result
    except Exception as e:
        logger.error(f"OpenAI API error: {e}", exc_info=(LOG_LEVEL == 'DEBUG'))
        raise

def send_slack_response(answer, message_info):
    """Send the response back to Slack via Web API."""
    channel_id = message_info.get('channel_id')
    thread_ts = message_info.get('thread_ts')
    is_mention = message_info.get('is_mention', False)

    url = 'https://slack.com/api/chat.postMessage'
    headers = {
        'Authorization': f'Bearer {SLACK_BOT_TOKEN}',
        'Content-Type': 'application/json'
    }

    data = {'channel': channel_id, 'text': answer}
    if is_mention and thread_ts:
        data['thread_ts'] = thread_ts

    try:
        if LOG_LEVEL == 'DEBUG':
            logger.debug(f"Sending POST to Slack chat.postMessage for channel {channel_id}")
        
        resp = requests.post(url, headers=headers, json=data, timeout=30)
        resp.raise_for_status()
        response_data = resp.json()

        if response_data.get('ok'):
            logger.info("Successfully sent response to Slack")
        else:
            logger.error(f"Slack API error: {response_data.get('error')}")
            raise Exception(f"Slack API error: {response_data.get('error')}")
    except Exception as e:
        logger.error(f"Error sending Slack response: {e}", exc_info=(LOG_LEVEL == 'DEBUG'))
        raise

def delete_sqs_message(receipt_handle):
    """Delete message from SQS queue after processing."""
    if receipt_handle:
        try:
            sqs.delete_message(QueueUrl=SQS_QUEUE_URL, ReceiptHandle=receipt_handle)
            logger.info("Message deleted from SQS")
        except Exception as e:
            logger.error(f"Error deleting SQS message: {e}")
