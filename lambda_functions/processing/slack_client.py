"""Slack Client for the Moogle bot - handles Slack API interactions."""

import os
import logging
import requests


class MoogleSlackClient:
    """Encapsulates Slack Web API interactions for the Moogle bot.
    
    Handles sending messages to Slack channels and threads.
    """
    
    # Error message template for when things go wrong
    ERROR_MESSAGE = """Kupo... I ran into an issue with the Moogle Magic! 

The crystal ball is a bit cloudy right now. Please try asking your question again in a moment, kupo!"""
    
    def __init__(self, bot_token: str = None, log_level: str = "ERROR"):
        """Initialize the Slack client.
        
        Args:
            bot_token: Slack Bot Token (defaults to SLACK_BOT_TOKEN env var)
            log_level: Logging level for this client
        """
        self.bot_token = bot_token or os.environ.get('SLACK_BOT_TOKEN')
        if not self.bot_token:
            raise ValueError("Slack bot token is required. Provide it as argument or set SLACK_BOT_TOKEN env var.")
        
        self.api_url = 'https://slack.com/api/chat.postMessage'
        self.headers = {
            'Authorization': f'Bearer {self.bot_token}',
            'Content-Type': 'application/json'
        }
        
        # Setup logger
        self.logger = logging.getLogger(__name__)
        self._configure_logging(log_level)
    
    def _configure_logging(self, log_level: str):
        """Configure logging for this client."""
        valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
        level = log_level.upper() if log_level.upper() in valid_levels else 'ERROR'
        self.logger.setLevel(getattr(logging, level))
        
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            ))
            self.logger.addHandler(handler)
    
    def send_response(self, channel_id: str, text: str, thread_ts: str = None,
                     is_mention: bool = False, timeout: int = 30) -> dict:
        """Send a message to Slack.
        
        Args:
            channel_id: The Slack channel ID
            text: The message text to send
            thread_ts: Thread timestamp (for replies in threads)
            is_mention: Whether this is a mention response (to determine threading)
            timeout: Request timeout in seconds
            
        Returns:
            The Slack API response as a dict
            
        Raises:
            Exception: If the Slack API call fails
        """
        if not channel_id:
            raise ValueError("channel_id is required")
        
        if not text:
            self.logger.warning("Empty text provided, using error message")
            text = self.ERROR_MESSAGE
        
        data = {
            'channel': channel_id,
            'text': text
        }
        
        # Only add thread_ts for mention responses in threads
        if is_mention and thread_ts:
            data['thread_ts'] = thread_ts
        
        self.logger.debug(f"Sending message to channel {channel_id}")
        if thread_ts:
            self.logger.debug(f"In thread: {thread_ts}")
        
        try:
            resp = requests.post(
                self.api_url,
                headers=self.headers,
                json=data,
                timeout=timeout
            )
            resp.raise_for_status()
            response_data = resp.json()
            
            if response_data.get('ok'):
                self.logger.info("Successfully sent message to Slack")
                return response_data
            else:
                error = response_data.get('error', 'Unknown error')
                self.logger.error(f"Slack API error: {error}")
                raise Exception(f"Slack API error: {error}")
                
        except requests.exceptions.Timeout:
            self.logger.error(f"Timeout sending message to Slack after {timeout}s")
            raise Exception(f"Slack API timeout after {timeout}s")
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error sending message to Slack: {e}")
            raise
    
    def send_error_message(self, channel_id: str, thread_ts: str = None,
                         is_mention: bool = False) -> dict:
        """Send the standard error message to Slack.
        
        Args:
            channel_id: The Slack channel ID
            thread_ts: Thread timestamp (for threading)
            is_mention: Whether this is a mention response
            
        Returns:
            The Slack API response as a dict
        """
        return self.send_response(
            channel_id=channel_id,
            text=self.ERROR_MESSAGE,
            thread_ts=thread_ts,
            is_mention=is_mention
        )
    
    def validate_configuration(self) -> bool:
        """Validate that the client is properly configured.
        
        Returns True if ready to make API calls, False otherwise.
        """
        if not self.bot_token:
            self.logger.error("No bot token configured")
            return False
        return True
    
    def test_connection(self) -> bool:
        """Test the Slack connection by calling auth.test.
        
        Returns True if connection is working, False otherwise.
        """
        try:
            resp = requests.get(
                'https://slack.com/api/auth.test',
                headers=self.headers,
                timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            
            if data.get('ok'):
                self.logger.info(f"Connected to Slack as {data.get('user', 'unknown')}")
                return True
            else:
                self.logger.error(f"Slack auth test failed: {data.get('error')}")
                return False
        except Exception as e:
            self.logger.error(f"Failed to test Slack connection: {e}")
            return False
