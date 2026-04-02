import json
import os
import boto3
import requests
from openai import OpenAI

s3 = boto3.client('s3')
sqs = boto3.client('sqs')

OPENAI_API_KEY = os.environ['OPENAI_API_KEY']
SLACK_BOT_TOKEN = os.environ['SLACK_BOT_TOKEN']
S3_BUCKET = os.environ['S3_BUCKET_IDEMPOTENCY']
SQS_QUEUE_URL = os.environ['SQS_QUEUE_URL']

client = OpenAI(api_key=OPENAI_API_KEY)

def handler(event, context):
    """
    Processing Lambda - consumes SQS messages, checks idempotency, calls OpenAI, sends response.
    """
    for record in event.get('Records', []):
        receipt_handle = record.get('receiptHandle')
        try:
            message = json.loads(record['body'])
            payload = message['payload']
            response_url = message.get('response_url')
            request_id = message.get('request_id')
            
            if not response_url:
                print(f"No response_url for request {request_id} - deleting malformed message")
                delete_sqs_message(receipt_handle)
                continue
            
            # Check idempotency - has this request already been processed?
            s3_key = f"idempotency/{request_id}"
            try:
                s3.head_object(Bucket=S3_BUCKET, Key=s3_key)
                # Object exists, duplicate request - delete from queue and skip
                print(f"Duplicate request detected and discarded: {request_id}")
                delete_sqs_message(receipt_handle)
                continue
            except s3.exceptions.ClientError as e:
                if e.response['Error']['Code'] != '404':
                    raise
                # Object doesn't exist, proceed with processing
            
            # Write idempotency marker (zero-byte file) before processing
            s3.put_object(Bucket=S3_BUCKET, Key=s3_key, Body=b'')
            print(f"Idempotency marker written for request: {request_id}")
            
            # Extract the user's question
            question = extract_question(payload)
            
            # Call OpenAI with Moogle personality
            answer = call_openai(question)
            
            # Send response to Slack
            send_slack_response(response_url, answer, payload)
            
            print(f"Successfully processed request {request_id}")
            
            # Delete message from SQS after successful processing
            delete_sqs_message(receipt_handle)
            
        except Exception as e:
            print(f"Error processing message: {e} - deleting message to prevent retries")
            delete_sqs_message(receipt_handle)
    
    return {'statusCode': 200}

def extract_question(payload):
    """Extract the user's question from various Slack payload formats."""
    # Slash command
    if payload.get('text'):
        return payload['text']
    
    # Event subscription (message)
    event = payload.get('event', {})
    if event.get('text'):
        # Remove bot mention if present
        text = event['text']
        # Simple mention removal - in production, use proper regex
        if text.startswith('<@'):
            text = text.split(' ', 1)[1] if ' ' in text else ''
        return text
    
    # Interactive component
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
        response = client.chat.completions.create(
            model="gpt-5-nano",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question}
            ],
            max_tokens=1000,
            temperature=0.7
        )
        
        return response.choices[0].message.content
    except Exception as e:
        print(f"OpenAI API error: {e}")
        return "Kupo... I'm having trouble connecting to Moogle Magic right now. Please try again later!"

def send_slack_response(response_url, answer, original_payload):
    """Send the response back to Slack using response_url."""
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {SLACK_BOT_TOKEN}'
    }
    
    # Determine if this is a slash command or regular message
    if original_payload.get('command'):
        data = {
            'text': answer,
            'response_type': 'in_channel'  # Make visible to everyone
        }
    else:
        data = {
            'text': answer,
            'replace_original': False
        }
    
    try:
        resp = requests.post(response_url, headers=headers, json=data, timeout=30)
        resp.raise_for_status()
        print(f"Successfully sent response to Slack: {resp.status_code}")
    except Exception as e:
        print(f"Error sending Slack response: {e}")
        raise

def delete_sqs_message(receipt_handle):
    """Delete message from SQS queue after processing."""
    if receipt_handle:
        try:
            sqs.delete_message(
                QueueUrl=SQS_QUEUE_URL,
                ReceiptHandle=receipt_handle
            )
            print("Message deleted from SQS")
        except Exception as e:
            print(f"Error deleting SQS message: {e}")
            # Don't raise - message will be reprocessed or go to DLQ
