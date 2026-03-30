import json
import os
import openai
from slack_sdk import WebClient

# Log function startup
print("GoogleMoogle Lambda Function v1.0 - Starting up")
print(f"Environment: {os.environ.get('AWS_LAMBDA_FUNCTION_VERSION', 'unknown')}")
print(f"Region: {os.environ.get('AWS_REGION', 'unknown')}")

# Initialize Slack client
slack_token = os.environ.get("SLACK_BOT_TOKEN")
slack_client = WebClient(token=slack_token)

# Initialize OpenAI API
openai.api_key = os.environ.get("OPENAI_API_KEY")

MOOGLE_SYSTEM_PROMPT = """You are GoogleMoogle, a cheerful and helpful Moogle from Final Fantasy XI. 
You ONLY respond to questions about Final Fantasy XI. You are an expert on all things FF11.

Key guidelines:
- ALL questions should be interpreted as relating to Final Fantasy XI
- For questions about where to obtain items, reference the FFXIclopedia at https://ffxiclopedia.fandom.com/ 
- When discussing item locations, recommend checking FFXIclopedia for the most current drop rates and farming locations
- If a question seems non-FF11 related, redirect it back to FF11 topics politely
- Always end responses with "kupo!" or include it naturally in your speech

Personality traits:
- Be enthusiastic and positive about helping adventurers
- Use phrases like "I'll help you kupo!", "Let me search that for you kupo!", "Moogle mail delivery!" when appropriate
- Reference Moogles, adventurers, jobs, and FF11 themes naturally
- Be friendly and supportive, like a true Moogle companion
- Keep responses concise but engaging
- Occasionally mention carrying items in your pouch or being a mail carrier

Core mission: Help adventurers with FF11 knowledge - from job advice to quest help to item farming locations!"""


def lambda_handler(event, context):
    """
    AWS Lambda handler for Slack events.
    
    Handles:
    - url_verification: Slack endpoint verification
    - event_callback with app_mention: Bot mentions in channels
    
    Args:
        event: AWS Lambda event (from API Gateway)
        context: AWS Lambda context
        
    Returns:
        dict: Response in API Gateway format {statusCode, body, headers}
    """
    print(f"Request received - RequestId: {context.request_id}")
    
    try:
        # Parse the event body
        if 'body' in event:
            body = json.loads(event['body']) if isinstance(event['body'], str) else event['body']
        else:
            body = event
        
        event_type = body.get('type')
        print(f"Event type: {event_type}")
        
        # Handle Slack's url_verification challenge
        if event_type == 'url_verification':
            challenge = body.get('challenge')
            print("Handling url_verification challenge")
            return {
                "statusCode": 200,
                "body": json.dumps({"challenge": challenge}),
                "headers": {"Content-Type": "application/json"}
            }
        
        # Handle regular events
        if event_type == 'event_callback':
            event_data = body.get('event', {})
            event_subtype = event_data.get('type')
            
            # Handle app_mention events
            if event_subtype == 'app_mention':
                user = event_data.get('user')
                channel = event_data.get('channel')
                text = event_data.get('text')
                
                print(f"App mention from {user} in {channel}")
                
                # Extract the question (remove the @mention)
                # The mention format is <@USER_ID>
                mention_pattern = f"<@{os.environ.get('SLACK_BOT_ID', '')}>"
                question = text.replace(mention_pattern, "").strip()
                
                if not question:
                    question = "Hello there, kupo!"
                
                print(f"Processing question: {question[:100]}...")
                
                # Get response from OpenAI
                try:
                    response = openai.ChatCompletion.create(
                        model="gpt-3.5-turbo",
                        messages=[
                            {"role": "system", "content": MOOGLE_SYSTEM_PROMPT},
                            {"role": "user", "content": question}
                        ],
                        max_tokens=2000,
                        temperature=0.8
                    )
                    
                    gpt_response = response["choices"][0]["message"]["content"]
                    print(f"OpenAI response generated: {len(gpt_response)} characters")
                    
                    # Post response to Slack
                    slack_client.chat_postMessage(
                        channel=channel,
                        text=gpt_response
                    )
                    print("Response posted to Slack successfully")
                    
                except openai.error.OpenAIError as e:
                    print(f"OpenAI API error: {str(e)}")
                    slack_client.chat_postMessage(
                        channel=channel,
                        text=f"Oh no! GoogleMoogle encountered an error with the crystal ball, kupo! {str(e)}"
                    )
                except Exception as e:
                    print(f"Error getting OpenAI response: {str(e)}")
                    import traceback
                    print(traceback.format_exc())
                    slack_client.chat_postMessage(
                        channel=channel,
                        text=f"Oh no! GoogleMoogle encountered an error, kupo! {str(e)}"
                    )
        
        # Return 200 OK for all valid requests
        return {
            "statusCode": 200,
            "body": json.dumps({"ok": True}),
            "headers": {"Content-Type": "application/json"}
        }
        
    except Exception as e:
        print(f"Unhandled error: {str(e)}")
        import traceback
        print(traceback.format_exc())
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)}),
            "headers": {"Content-Type": "application/json"}
        }
