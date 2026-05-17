"""LLM Client for the Moogle bot - Bedrock Converse API with tool use.

Uses the model-agnostic Bedrock Converse API so that swapping between
Amazon Nova models and Anthropic Claude requires only a model ID change.
The tool-use loop runs until the model returns a final text response
(stopReason != "tool_use").
"""

import logging
import os
import re

import boto3

from .ffxi_item_lookup import lookup_as_tool_result


DEFAULT_MODEL_ID = "amazon.nova-lite-v1:0"
MAX_TOOL_ITERATIONS = 4

# Tool definition in Converse API toolSpec format.
FFXI_ITEM_LOOKUP_TOOL = {
    "toolSpec": {
        "name": "ffxi_item_lookup",
        "description": (
            "Look up a specific Final Fantasy XI item on FFXIclopedia and return "
            "structured data: Rare/Ex/Aux flags, item type, stack size, NPC sell "
            "price, vendors (NPC, zone, price), and drop sources (monster, zone). "
            "Call this whenever the user asks about a concrete FFXI item by name "
            "- for example its price, where to buy it, which monsters drop it, "
            "or its flags. Do not call it for general lore questions, character "
            "questions, or items from other Final Fantasy titles."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "item_name": {
                        "type": "string",
                        "description": (
                            "The exact in-game name of the FFXI item, e.g. "
                            "'Bone Chip', 'Excalibur', 'Imperial Bronze Piece'."
                        ),
                    }
                },
                "required": ["item_name"],
            }
        },
    }
}


class MoogleLLMClient:
    """Encapsulates Bedrock Converse API interactions for the Moogle bot."""

    DEFAULT_PERSONALITY = """You are a helpful and knowledgeable Moogle (モーグリ) from the Final Fantasy XI Game!

Your personality traits:
- You end many sentences with "kupo!" or "kupo kupo!"
- You are cheerful, friendly, and eager to help
- You have extensive knowledge of all Final Fantasy games, characters, lore, mechanics, and history
- You speak with a slightly whimsical but informative tone
- You sometimes reference Moogles' roles in various Final Fantasy games (like delivering mail, saving games, running shops, or being playable characters)
- You're particularly fond of mentioning that you have a pom-pom on your head

Answer questions about the Final Fantasy XI game, characters, storylines, gameplay mechanics, and lore. Be thorough but keep responses concise (under 2000 characters for Slack).

When the user asks about a specific FFXI item (its price, vendors, drops, or flags), call the ffxi_item_lookup tool instead of answering from memory - it returns authoritative wiki data.

IMPORTANT: When the ffxi_item_lookup tool is called and the item IS found, a formatted data card with all the stats (flags, type, vendors, drop sources) will be posted automatically to Slack. Do NOT list, describe, or repeat any of those stats in your response. Instead, reply with 1-2 sentences of Moogle flavor only - enthusiasm, a fun fact about the item, or whimsical commentary - without referencing specific numbers, prices, or locations from the tool result.

If the item is NOT found, respond naturally in Moogle voice that you couldn't locate it, kupo!

Remember: Stay in character as a Moogle!"""

    def __init__(self, model_id: str = None, region_name: str = None,
                 log_level: str = "ERROR", system_prompt: str = None):
        self.model_id = model_id or os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)
        self.region_name = (region_name
                            or os.environ.get("BEDROCK_REGION")
                            or os.environ.get("AWS_REGION")
                            or "us-east-1")
        self.client = boto3.client("bedrock-runtime", region_name=self.region_name)
        self.system_prompt = system_prompt or self.DEFAULT_PERSONALITY

        self.logger = logging.getLogger(__name__)
        self._configure_logging(log_level)

    def _configure_logging(self, log_level: str):
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        level = log_level.upper() if log_level.upper() in valid_levels else "ERROR"
        self.logger.setLevel(getattr(logging, level))
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            ))
            self.logger.addHandler(handler)

    def generate_response(self, messages: list, max_tokens: int = 1000,
                          temperature: float = 0.7) -> tuple:
        """Generate a Moogle response via the Bedrock Converse API.

        Args:
            messages: Conversation history as simple dicts
                      [{"role": "user"|"assistant", "content": str}, ...].
                      The caller appends the new user turn before calling.

        Returns:
            (text, item_lookups) where item_lookups is a list of dicts from
            any ffxi_item_lookup tool calls made during this turn.
        """
        if not messages:
            messages = [{"role": "user", "content": "Tell me about Final Fantasy!"}]

        last_user = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"), ""
        )
        if not last_user or not str(last_user).strip():
            self.logger.warning("Empty user message; substituting default")
            messages[-1]["content"] = "Tell me about Final Fantasy!"

        self.logger.info(f"Invoking {self.model_id} via Converse API with {len(messages)} message(s)")
        self.logger.debug(f"Last user message: {str(last_user)[:100]}...")

        # Convert simple {role, content} dicts to Converse content-block format.
        converse_messages = _to_converse_messages(messages)
        all_item_lookups = []

        # Force a tool call on the first iteration when the question looks like an
        # item query.  Nova Lite tends to skip the tool with long conversation history.
        force_tool_first = _looks_like_item_query(str(last_user))
        self.logger.info(f"force_tool_first={force_tool_first}")

        for iteration in range(MAX_TOOL_ITERATIONS):
            force = force_tool_first and iteration == 0
            response = self._invoke(converse_messages, max_tokens, temperature, force_tool=force)
            stop_reason = response.get("stopReason")
            output_message = response["output"]["message"]
            content_blocks = output_message.get("content", [])

            self.logger.info(
                f"Iteration {iteration}: stopReason={stop_reason}, "
                f"blocks={len(content_blocks)}"
            )

            if stop_reason != "tool_use":
                text = _extract_text(content_blocks)
                self.logger.info(f"Final response length: {len(text)}")
                self.logger.debug(f"Response preview: {text[:100]}...")
                return text, all_item_lookups

            # Append assistant turn and user turn with tool results.
            converse_messages.append(output_message)
            tool_results, captured_items = self._run_tool_calls(content_blocks)
            all_item_lookups.extend(captured_items)
            converse_messages.append({"role": "user", "content": tool_results})

        self.logger.error(f"Tool-use loop exceeded {MAX_TOOL_ITERATIONS} iterations")
        raise RuntimeError(
            f"Converse tool-use loop did not converge after {MAX_TOOL_ITERATIONS} iterations"
        )

    def _invoke(self, converse_messages: list, max_tokens: int, temperature: float,
                force_tool: bool = False) -> dict:
        tool_config: dict = {"tools": [FFXI_ITEM_LOOKUP_TOOL]}
        if force_tool:
            tool_config["toolChoice"] = {"any": {}}
        try:
            return self.client.converse(
                modelId=self.model_id,
                system=[{"text": self.system_prompt}],
                messages=converse_messages,
                toolConfig=tool_config,
                inferenceConfig={
                    "maxTokens": max_tokens,
                    "temperature": temperature,
                },
            )
        except Exception as e:
            self.logger.error(f"Bedrock converse error: {e}", exc_info=True)
            raise

    def _run_tool_calls(self, content_blocks: list) -> tuple:
        """Execute toolUse blocks and return (tool_result_blocks, captured_item_lookups)."""
        results = []
        captured_items = []
        for block in content_blocks:
            if "toolUse" not in block:
                continue
            tool_use = block["toolUse"]
            tool_name = tool_use["name"]
            tool_input = tool_use.get("input") or {}
            tool_use_id = tool_use["toolUseId"]

            self.logger.info(f"Tool call: {tool_name} input={tool_input}")
            try:
                output = self._dispatch_tool(tool_name, tool_input)
                if tool_name == "ffxi_item_lookup":
                    captured_items.append(output)
                results.append({
                    "toolResult": {
                        "toolUseId": tool_use_id,
                        "content": [{"json": output}],
                    }
                })
            except Exception as e:
                self.logger.error(f"Tool {tool_name} raised: {e}", exc_info=True)
                results.append({
                    "toolResult": {
                        "toolUseId": tool_use_id,
                        "status": "error",
                        "content": [{"text": f"Tool error: {e}"}],
                    }
                })
        return results, captured_items

    def _dispatch_tool(self, name: str, tool_input: dict):
        if name == "ffxi_item_lookup":
            item_name = (tool_input or {}).get("item_name", "").strip()
            if not item_name:
                return {"found": False, "error": "item_name is required"}
            return lookup_as_tool_result(item_name)
        raise ValueError(f"Unknown tool: {name}")

    def set_personality(self, prompt: str):
        self.system_prompt = prompt
        self.logger.info("System prompt updated")

    def set_model(self, model_id: str):
        self.model_id = model_id
        self.logger.info(f"Model changed to: {model_id}")

    def get_personality(self) -> str:
        return self.system_prompt

    def validate_configuration(self) -> bool:
        if not self.model_id:
            self.logger.error("No model_id configured")
            return False
        return True


def _to_converse_messages(messages: list) -> list:
    """Convert simple {role, content: str} dicts to Converse content-block format."""
    result = []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            converted = [{"text": content}]
        elif isinstance(content, list):
            converted = content  # already in Converse block format
        else:
            converted = [{"text": str(content)}]
        result.append({"role": m["role"], "content": converted})
    return result


_ITEM_QUERY_PATTERNS = re.compile(
    r"\b(where (can|do) i (get|find|buy|farm)|drop(s|ped)? (from|by)|sold by|"
    r"vendor|price|cost|how (much|many)|stack(able)?|rare|ex |exclusive|"
    r"npc sell|resale|obtain|craft|synth|synthesis|recipe)\b",
    re.IGNORECASE,
)

def _looks_like_item_query(text: str) -> bool:
    """Return True if the text looks like a question about a specific FFXI item."""
    return bool(_ITEM_QUERY_PATTERNS.search(text))


_THINKING_RE = re.compile(r"<thinking>.*?</thinking>", re.DOTALL | re.IGNORECASE)

def _extract_text(content_blocks: list) -> str:
    """Pull all text blocks out of a Converse response content list."""
    parts = []
    for block in content_blocks:
        if "text" in block and block["text"]:
            parts.append(block["text"])
    text = "\n".join(parts).strip()
    return _THINKING_RE.sub("", text).strip()
