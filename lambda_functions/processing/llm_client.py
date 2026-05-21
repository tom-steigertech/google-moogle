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
from .ffxi_wiki_search import search_as_tool_result as wiki_search
from .ffxi_zone_map import fetch_zone_maps
from .notes_client import (
    save_note as save_note_to_s3,
    search_notes as search_notes_in_s3,
    format_notes_for_prompt as _format_notes_for_prompt,
)


DEFAULT_MODEL_ID = "amazon.nova-lite-v1:0"
MAX_TOOL_ITERATIONS = 6

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

FFXI_WIKI_SEARCH_TOOL = {
    "toolSpec": {
        "name": "ffxi_wiki_search",
        "description": (
            "Search for Final Fantasy XI information about quests, missions, "
            "jobs, abilities, spells, monsters, zones, NPCs, game mechanics, or any "
            "general FFXI topic. Searches BG-Wiki first, then falls back to FFXIclopedia "
            "automatically. Call this whenever the user asks about something "
            "you are not fully certain about — quest steps, mission walkthroughs, job "
            "ability details, spell requirements, NPC locations, etc. Prefer looking "
            "it up over answering from memory. Do NOT use for item price/vendor/drop "
            "lookups — use ffxi_item_lookup for those instead."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Search terms for the FFXI topic, e.g. "
                            "'Red Mage artifact armor quest', "
                            "'Monk Formless Strikes ability', "
                            "'Chains of Promathia mission 3-5'."
                        ),
                    }
                },
                "required": ["query"],
            }
        },
    }
}

SEARCH_NOTES_TOOL = {
    "toolSpec": {
        "name": "search_notes",
        "description": (
            "Search the shared pool of user-contributed FFXI notes for entries "
            "relevant to a topic. Use this when answering FFXI questions where "
            "community knowledge could help — strategy tips, recent observations, "
            "lesser-known facts, or anything a wiki might not cover. Especially "
            "useful AFTER ffxi_wiki_search to find players' notes that expand on "
            "or contradict wiki content. Returns matching notes (text, author, "
            "date); if it returns no matches, fall back to the wiki and your own "
            "knowledge."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Keywords to search for in note text. Multiple words "
                            "are OR-matched for broader recall. Use the key "
                            "nouns from the user's question — e.g. 'Despot "
                            "Astral Ring', 'Caedarva NM spawn', 'Red Mage "
                            "artifact'."
                        ),
                    }
                },
                "required": ["query"],
            }
        },
    }
}

FFXI_ZONE_MAP_TOOL = {
    "toolSpec": {
        "name": "ffxi_zone_map",
        "description": (
            "Fetch the map image(s) for a Final Fantasy XI zone from BG-Wiki. "
            "Use this when the user asks about zone layout, how to navigate within "
            "a zone, where zone lines are located, how to move between different "
            "areas or sub-maps of the same zone, or any question that requires "
            "understanding the physical layout of a zone. Returns the actual map "
            "image(s) which you can interpret visually to answer the question. "
            "For multi-level or multi-area zones, multiple maps may be returned."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "zone_name": {
                        "type": "string",
                        "description": (
                            "The exact name of the FFXI zone as it appears on "
                            "BG-Wiki, e.g. 'Jugner Forest', 'Qufim Island', "
                            "'Windurst Waters', 'Promyvion - Holla'."
                        ),
                    }
                },
                "required": ["zone_name"],
            }
        },
    }
}

SAVE_NOTE_TOOL = {
    "toolSpec": {
        "name": "save_note",
        "description": (
            "Save a user-contributed piece of FFXI knowledge to a shared notes "
            "pool so all users can benefit from it in future answers. Call this "
            "ONLY when the user is explicitly contributing a fact for you to "
            "remember — phrases like 'remember that…', 'take note…', 'save this…', "
            "'note that…', 'keep in mind…', 'for future reference…'. Do NOT call "
            "this for ordinary questions, opinions, or conversational chatter. "
            "Phrase the saved text as a concise standalone statement that will "
            "still make sense to a future reader who lacks this conversation's "
            "context."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "note_text": {
                        "type": "string",
                        "description": (
                            "The fact to save, rewritten as a self-contained "
                            "statement. Example: user says 'remember the spawn "
                            "is on Earthsday' → note_text: 'NMs in Caedarva Mire "
                            "spawn on Earthsday.'"
                        ),
                    }
                },
                "required": ["note_text"],
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

Formatting rules (your responses are rendered in Slack):
- Use **Section Title:** on its own line to introduce a named section, followed by a blank line and its content.
- Use "- item" for bullet lists (one item per line).
- Use **bold text** for emphasis on key terms within a sentence.
- Separate each section with a blank line.
- Do NOT use Markdown headers (##, ###) — they do not render in Slack.
- Do NOT use horizontal rules (---).

You have four tools — always prefer them over answering from memory:

1. ffxi_item_lookup: Use when the user asks about a specific item's price, vendors, drop sources, or flags. A formatted card is posted to Slack automatically — do NOT repeat the stats. Reply with 1-2 sentences of Moogle flavor only. If the item is NOT found, say so in Moogle voice.

2. ffxi_wiki_search: Use for any FFXI question you are not fully certain about — quests, missions, job abilities, spells, monsters, zones, NPCs, game mechanics, lore. This searches BG-Wiki and falls back to FFXIclopedia automatically. Search first, then answer using the content returned.

3. search_notes: Search the shared pool of user-contributed FFXI knowledge. Call this for FFXI questions where community wisdom might apply, and ALWAYS consider calling it after ffxi_wiki_search to find player notes that expand on or contradict the wiki. If matches come back, weave them into your answer and credit "another adventurer's notes." If no matches, just use the wiki/your knowledge — don't apologise for an empty search.

4. save_note: Call ONLY when the user is explicitly contributing a fact for you to remember ("remember that…", "take note…", "save this…"). Do NOT call for ordinary questions. After saving, acknowledge in 1-2 sentences of Moogle voice.

5. ffxi_zone_map: Fetch the zone map image(s) and BG-Wiki page text when the user asks about zone layout, navigation within a zone, or how to move between areas or sub-maps. The tool returns both the page text and the map images — read both carefully. Answer ONLY from what the page text and maps show; never blend in knowledge about surrounding zones. For multi-map zones, the sub-maps are all part of the same zone — transitions between them appear as passages or exits on the map images, not as zone lines to other zones.

Some recent notes may already appear below; use search_notes to dig deeper into the full pool by keyword.

Do not guess when you can look it up, kupo!

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
                          temperature: float = 0.7, notes: list = None,
                          note_context: dict = None) -> tuple:
        """Generate a Moogle response via the Bedrock Converse API.

        Args:
            messages: Conversation history as simple dicts
                      [{"role": "user"|"assistant", "content": str}, ...].
                      The caller appends the new user turn before calling.
            notes: User-contributed FFXI notes to inject into the system
                   prompt for this call only.
            note_context: Metadata used by the `save_note` tool dispatcher —
                          {"bucket": str, "author_id": str, "channel_id": str}.

        Returns:
            (text, item_lookups) where item_lookups is a list of dicts from
            any ffxi_item_lookup tool calls made during this turn.
        """
        # Stash context for the in-call tool dispatcher. Safe because each
        # Lambda invocation gets its own client instance and runs serially.
        self._note_context = note_context or {}

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

        system_text = self.system_prompt
        notes_section = _format_notes_for_prompt(notes) if notes else ""
        if notes_section:
            system_text = f"{system_text}\n\n{notes_section}"

        for iteration in range(MAX_TOOL_ITERATIONS):
            force = force_tool_first and iteration == 0
            response = self._invoke(converse_messages, max_tokens, temperature,
                                    system_text=system_text, force_tool=force)
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
                system_text: str = None, force_tool: bool = False) -> dict:
        tool_config: dict = {
            "tools": [
                FFXI_ITEM_LOOKUP_TOOL,
                FFXI_WIKI_SEARCH_TOOL,
                SEARCH_NOTES_TOOL,
                SAVE_NOTE_TOOL,
                FFXI_ZONE_MAP_TOOL,
            ]
        }
        if force_tool:
            tool_config["toolChoice"] = {"any": {}}
        try:
            return self.client.converse(
                modelId=self.model_id,
                system=[{"text": system_text or self.system_prompt}],
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
                # Image-returning tools (e.g. ffxi_zone_map) return a pre-built
                # list of content blocks; all other tools return a plain dict.
                if isinstance(output, list):
                    content_blocks = output
                else:
                    if tool_name == "ffxi_item_lookup":
                        captured_items.append(output)
                    content_blocks = [{"json": output}]
                results.append({
                    "toolResult": {
                        "toolUseId": tool_use_id,
                        "content": content_blocks,
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
        if name == "ffxi_wiki_search":
            query = (tool_input or {}).get("query", "").strip()
            if not query:
                return {"found": False, "error": "query is required"}
            return wiki_search(query)
        if name == "search_notes":
            ctx = getattr(self, "_note_context", {}) or {}
            bucket = ctx.get("bucket")
            if not bucket:
                self.logger.error("search_notes called without a configured bucket")
                return {"matches": [], "count": 0, "error": "notes storage is not configured"}
            query = (tool_input or {}).get("query", "").strip()
            if not query:
                return {"matches": [], "count": 0, "error": "query is required"}
            try:
                matches = search_notes_in_s3(bucket, query)
                return {"matches": matches, "count": len(matches), "query": query}
            except Exception as e:
                self.logger.error(f"search_notes failed: {e}", exc_info=True)
                return {"matches": [], "count": 0, "error": str(e)}
        if name == "save_note":
            ctx = getattr(self, "_note_context", {}) or {}
            bucket = ctx.get("bucket")
            if not bucket:
                self.logger.error("save_note called without a configured bucket")
                return {"saved": False, "error": "notes storage is not configured"}
            text = (tool_input or {}).get("note_text", "").strip()
            if not text:
                return {"saved": False, "error": "note_text is required"}
            try:
                note = save_note_to_s3(
                    bucket, text,
                    ctx.get("author_id", ""), ctx.get("channel_id", ""),
                )
                return {"saved": True, "id": note["id"], "text": note["text"]}
            except Exception as e:
                self.logger.error(f"save_note failed: {e}", exc_info=True)
                return {"saved": False, "error": str(e)}
        if name == "ffxi_zone_map":
            zone_name = (tool_input or {}).get("zone_name", "").strip()
            if not zone_name:
                return [{"text": "zone_name is required"}]
            result = fetch_zone_maps(zone_name)
            if not result["found"]:
                return [{"text": result.get("error", f"No maps found for '{zone_name}'")}]
            maps = result["maps"]
            total = len(maps)
            blocks = []

            # Lead with the zone page text — this grounds the model in what the
            # zone actually contains and prevents hallucination from training data.
            zone_text = result.get("zone_text", "")
            if zone_text:
                blocks.append({"text": (
                    f"=== BG-Wiki page text for {result['zone_name']} ===\n{zone_text}\n"
                    "=== End of page text ==="
                )})

            # Preamble for multi-map zones
            if total > 1:
                map_labels = ", ".join(
                    f"Map {m['map_number']}" if m["map_number"] else m["label"]
                    for m in maps
                )
                blocks.append({"text": (
                    f"{result['zone_name']} has {total} sub-maps ({map_labels}). "
                    "These are all sections of the SAME zone, not separate zones. "
                    "Transitions between sub-maps happen at specific passages, "
                    "doorways, staircases, or labeled exit points visible on the maps. "
                    "Only use what you can actually see on the maps and in the page "
                    "text above — do NOT use prior knowledge about surrounding zones."
                )})

            # Interleave a text label before each image
            for m in maps:
                map_label = f"Map {m['map_number']}" if m["map_number"] else m["label"]
                blocks.append({"text": f"--- {result['zone_name']}: {map_label} ---"})
                blocks.append({
                    "image": {
                        "format": m["format"],
                        "source": {"bytes": m["bytes"]},
                    }
                })

            blocks.append({"text": (
                "Answer the navigation question using ONLY the map images and page "
                "text provided above. Do not reference zones or areas not mentioned "
                "in the page text or visible in the maps."
            )})
            return blocks
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
