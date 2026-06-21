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
    
    level = getattr(logging, level_str)
    logger = logging.getLogger()
    logger.setLevel(level)

    # Lambda's runtime pre-installs a handler at WARNING level; update it so our
    # chosen level actually takes effect.  Add our own handler only if none exist.
    if logger.handlers:
        for h in logger.handlers:
            h.setLevel(level)
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        ))
        logger.addHandler(handler)
    
    return logger


_ops_logger = None


def get_ops_logger() -> logging.Logger:
    """Return the dedicated operational-audit logger.

    Always emits at INFO regardless of the global LOG_LEVEL (which stays ERROR in
    production) so per-request operation summaries reliably reach CloudWatch
    without enabling verbose application logging. propagate=False keeps these
    summaries off the root logger so they are neither duplicated nor suppressed
    by the root level.
    """
    global _ops_logger
    if _ops_logger is None:
        lg = logging.getLogger("moogle.ops")
        lg.setLevel(logging.INFO)
        lg.propagate = False
        if not lg.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(message)s"))
            lg.addHandler(handler)
        _ops_logger = lg
    return _ops_logger


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
