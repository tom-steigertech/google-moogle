"""Processing Lambda package for the Moogle bot.

This package contains the SQS processing handler and supporting modules:
- handler: Lambda entry point and orchestration
- llm_client: Claude-on-Bedrock LLM interactions with tool use
- ffxi_item_lookup: FFXIclopedia scraper exposed as a Claude tool
- slack_client: Slack Web API interactions
- utils: Shared utility functions
"""

from .handler import handler

__all__ = ['handler']
