"""Processing Lambda package for the Moogle bot.

This package contains the SQS processing handler and supporting modules:
- handler: Lambda entry point and orchestration
- llm_client: OpenAI LLM interactions (testable in isolation)
- slack_client: Slack Web API interactions
- utils: Shared utility functions
"""

from .handler import handler

__all__ = ['handler']
