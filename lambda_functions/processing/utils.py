"""Utility functions for the processing Lambda."""

import os
import logging


def setup_logging(log_level: str = None) -> logging.Logger:
    """Setup and configure logging.
    
    Args:
        log_level: Log level string (DEBUG, INFO, WARNING, ERROR, CRITICAL)
                Defaults to LOG_LEVEL env var or ERROR
                
    Returns:
        Configured logger instance
    """
    level_str = (log_level or os.environ.get('LOG_LEVEL', 'ERROR')).upper()
    valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
    
    if level_str not in valid_levels:
        level_str = 'ERROR'
    
    logger = logging.getLogger()
    logger.setLevel(getattr(logging, level_str))
    
    # Add handler if not present (for Lambda, this may already be set up)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        ))
        logger.addHandler(handler)
    
    return logger


def extract_question(payload: dict) -> str:
    """Extract the user's question from various Slack payload formats.
    
    Handles:
    - Slash command payloads (text field)
    - Events API payloads (event.text field with bot mention stripping)
    - Interactive component payloads (actions)
    
    Args:
        payload: The Slack payload dict
        
    Returns:
        The extracted question text, or a default question if extraction fails
    """
    # Handle slash command format
    if payload.get('text'):
        return payload['text']
    
    # Handle Events API format (for @mentions)
    event = payload.get('event', {})
    if event.get('text'):
        text = event['text']
        # Strip bot mention if present (e.g., "<@U12345> question" -> "question")
        if text.startswith('<@'):
            # Find the end of the mention and get text after it
            parts = text.split(' ', 1)
            if len(parts) > 1:
                return parts[1]
            else:
                return ''
        return text
    
    # Handle interactive component format
    if payload.get('actions'):
        # Try to get text from the message that triggered the action
        return payload.get('message', {}).get('text', 
            'What would you like to know about Final Fantasy?')
    
    # Default fallback
    return "Tell me about Final Fantasy!"


def truncate_text(text: str, max_length: int = 100) -> str:
    """Truncate text for logging/display purposes.
    
    Args:
        text: The text to truncate
        max_length: Maximum length before truncation
        
    Returns:
        Truncated text with '...' if needed
    """
    if len(text) <= max_length:
        return text
    return text[:max_length - 3] + '...'


def get_env_var(name: str, required: bool = True, default: str = None) -> str:
    """Get an environment variable with optional validation.
    
    Args:
        name: The environment variable name
        required: Whether the variable is required (raises if missing)
        default: Default value if not required and not set
        
    Returns:
        The environment variable value
        
    Raises:
        ValueError: If required=True and variable is not set
    """
    value = os.environ.get(name, default)
    if required and not value:
        raise ValueError(f"Required environment variable {name} is not set")
    return value
