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
from .ffxi_reddit_search import search_as_tool_result as reddit_search
from .ffxi_wiki_search import search_as_tool_result as wiki_search
from .ffxi_zone_map import fetch_zone_maps
from .ffxiah_lookup import lookup_as_tool_result as ffxiah_lookup
from .ffxidb_lookup import lookup_as_tool_result as ffxidb_lookup
from .notes_client import (
    save_note as save_note_to_s3,
    search_notes as search_notes_in_s3,
    format_notes_for_prompt as _format_notes_for_prompt,
)


DEFAULT_MODEL_ID = "amazon.nova-lite-v1:0"
MAX_TOOL_ITERATIONS = 8

# Cheap model for the YES/NO sentiment classification that gates deep-research
# escalation — no need to spend the front-line model on a one-word answer.
# Nova Micro has the lowest input price on Bedrock and is built for fast
# classification. Override with SENTIMENT_MODEL_ID (e.g. amazon.nova-lite-v1:0
# if Micro mis-triggers on nuance/sarcasm).
DEFAULT_SENTIMENT_MODEL_ID = "amazon.nova-micro-v1:0"

# Deep-planning tier. Complex, multi-step questions (cross-referencing several
# facts, arithmetic over looked-up values, dependent lookup chains) are escalated
# from the front-line model to this more capable model, which gets a planning
# system prompt and a larger tool-iteration budget.
DEFAULT_PLANNER_MODEL_ID = "us.anthropic.claude-sonnet-4-6"
# 15 tool-using passes + 1 final synthesis pass (the last iteration is nudged to
# answer without tools), so the planner can run up to 15 tool calls for a more
# comprehensive set of searches and synthesis.
MAX_PLANNER_ITERATIONS = 16

# Returned when the model never stops calling tools (extremely rare) so the user
# gets a friendly reply instead of a hard error.
TOOL_LOOP_FALLBACK = (
    "Kupo... I dug through my references but couldn't pull that together into a "
    "clear answer this time. Try asking again or rephrasing it for me, kupo!"
)

# Tool definition in Converse API toolSpec format.
FFXI_ITEM_LOOKUP_TOOL = {
    "toolSpec": {
        "name": "ffxi_item_lookup",
        "description": (
            "Look up a specific Final Fantasy XI item on FFXIclopedia and return "
            "structured data: Rare/Ex/Aux flags, item type, stack size, NPC sell "
            "price, vendors (NPC, zone, price), drop sources (monster, zone), AND "
            "how the item is obtained — crafting/synthesis recipes (craft skill + "
            "level), auction-house availability and category, and a 'how to obtain' "
            "summary. Call this whenever the user asks about a concrete FFXI item by "
            "name — its price, where to buy/get/farm it, how to obtain it, which "
            "monsters drop it, how it's crafted, or its flags. This is the right "
            "tool for crafting ingredients too (e.g. 'where can I get Cornstarch'): "
            "it returns the synthesis recipe and AH info even when the item has no "
            "vendors or drops. Do not call it for general lore questions, character "
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

FFXIAH_LOOKUP_TOOL = {
    "toolSpec": {
        "name": "ffxiah_lookup",
        "description": (
            "Look up a Final Fantasy XI item's AUCTION HOUSE market data on "
            "ffxiah.com for the SYLPH server: the current player-market value "
            "(median single price and stack price), how fast it sells (sale rate "
            "in units sold per day), recent price range (min/max/average), and "
            "current stock. All figures are for the Sylph server specifically. "
            "Call this when the user asks what an item is WORTH on the AH, how much "
            "it sells for, its market/resale value, or how quickly/easily it sells. "
            "This is DIFFERENT from ffxi_item_lookup (which gives the fixed NPC "
            "vendor/sell price, drop sources, and crafting recipe) — use ffxiah_lookup "
            "specifically for live player-driven Auction House pricing and selling "
            "speed. Items with no Sylph AH sales (Rare/Ex, or simply unsold on Sylph) "
            "return no_ah_data=true. Unlike item cards, this data is NOT auto-posted — "
            "relay the relevant numbers in your answer."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "item_name": {
                        "type": "string",
                        "description": (
                            "The exact in-game name of the FFXI item, e.g. "
                            "'Fire Crystal', 'Hi-Potion', 'Scintillating Rhomb'."
                        ),
                    }
                },
                "required": ["item_name"],
            }
        },
    }
}

FFXIDB_LOOKUP_TOOL = {
    "toolSpec": {
        "name": "ffxidb_lookup",
        "description": (
            "Look up a Final Fantasy XI item on ffxidb.com — an alternate item "
            "database — and return its description, type, equippable jobs and level, "
            "stats, and known drop sources (monster, zone, drop chance). Use this as "
            "a SECONDARY information source for general item facts: when "
            "ffxi_item_lookup did not return what the user asked, to cross-reference "
            "another database, or for an item's description/stats/level/job "
            "requirements. For Auction House prices and selling speed use "
            "ffxiah_lookup instead; for the primary item card (vendors, drops, "
            "crafting) use ffxi_item_lookup. Relay the relevant facts in your answer."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "item_name": {
                        "type": "string",
                        "description": (
                            "The exact in-game name of the FFXI item, e.g. "
                            "'Excalibur', 'Bone Chip', 'Hi-Potion'."
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

FFXI_REDDIT_SEARCH_TOOL = {
    "toolSpec": {
        "name": "ffxi_reddit_search",
        "description": (
            "LAST RESORT — search the r/ffxi subreddit for community discussion. "
            "Use this ONLY when ffxi_wiki_search AND search_notes have already failed "
            "to provide an answer, or for questions that are inherently about player "
            "opinion, experience, or current community consensus (e.g. 'what do players "
            "think is the best solo job', 'is X content still active', subjective "
            "recommendations) that a wiki would not cover. Do NOT call this before the "
            "wiki — it returns unverified forum posts and opinions, not authoritative "
            "facts. Returns post titles, snippets, and top comments from the most "
            "relevant thread; treat the content as community opinion and say so when "
            "you use it."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Search terms for r/ffxi, e.g. "
                            "'best solo trust setup', "
                            "'is Dynamis still worth doing', "
                            "'returning player gil making 2024'."
                        ),
                    }
                },
                "required": ["query"],
            }
        },
    }
}

ESCALATE_TO_PLANNER_TOOL = {
    "toolSpec": {
        "name": "escalate_to_planner",
        "description": (
            "Hand a COMPLEX, multi-step question off to the deep-planning Moogle. "
            "Call this INSTEAD of answering when the question cannot be resolved by a "
            "single lookup and instead requires planning: cross-referencing several "
            "separate facts, doing arithmetic over looked-up values, or a chain of "
            "dependent lookups whose later steps depend on earlier results. Classic "
            "examples: 'how many <X> fights do I need for enough <Y> to finish the "
            "<Z> upgrade', 'what's the cheapest way to skill up <craft> from 40 to 60', "
            "'compare the gil-per-hour of farming A vs B'. Do NOT call this for a single "
            "item price/drop/recipe lookup or a single quest/mechanic question — answer "
            "those yourself with the other tools. When you call this, the user is told "
            "it will take a moment and a more capable Moogle takes over the whole "
            "question, so do not also try to answer it yourself."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": (
                            "A clear one- or two-sentence restatement of what the user "
                            "ultimately wants computed or planned, phrased so the planner "
                            "can act on it without seeing the rest of the chat. e.g. "
                            "'Compute how many Glavoid fights are needed to gather enough "
                            "Glavoid Shells to complete the Magian Trial path for the "
                            "knife upgrade.'"
                        ),
                    }
                },
                "required": ["goal"],
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

You have the tools below — always prefer them over answering from memory:

1. ffxi_item_lookup: Use when the user asks about a specific item's price, vendors, drop sources, flags, OR how to obtain/craft it (including crafting ingredients like Cornstarch — the tool returns the synthesis recipe and auction-house info even when an item has no vendors or drops). When asked "where can I get those ingredients?", call this once per ingredient. A formatted card is posted to Slack automatically — do NOT repeat the stats. Reply with 1-2 sentences of Moogle flavor only. If the lookup comes back with no vendors, drops, or crafting info, no card is posted — in that case answer the question yourself in Moogle voice (use ffxi_wiki_search if needed). If the item is NOT found, the result may include "suggestions" (close item names) — offer those to the user in Moogle voice (e.g. "Did you mean Royal Grape or San d'Orian Grape, kupo?"). If "suggestions" is empty, say you couldn't find it.

2. ffxiah_lookup: Use when the user asks what an item is WORTH on the Auction House — its market/resale value, current price, how much it sells for, or how fast or easily it sells. Returns the median single price, stack price, sale rate (units sold per day), price range, and stock from ffxiah.com. All figures are for the SYLPH server — say so when you give prices (e.g. "on Sylph, kupo"). This is the live player-market price, distinct from ffxi_item_lookup's fixed NPC sell price. No card is posted for this — state the relevant numbers in your answer (in Moogle voice). If the result has no_ah_data, tell the user it has no Sylph AH sales (it may be Rare/Ex and unsellable, or just hasn't sold on Sylph).

3. ffxidb_lookup: A SECONDARY item database (ffxidb.com) returning an item's description, type, equippable jobs/level, stats, and drop sources. Use it to cross-reference or when ffxi_item_lookup didn't return what the user needs (e.g. an item's description, level, or job requirements). Prefer ffxi_item_lookup for the main item card and ffxiah_lookup for prices; reach for ffxidb_lookup as a backup or extra-detail source. Relay the relevant facts in your answer.

4. ffxi_wiki_search: Use for any FFXI question you are not fully certain about — quests, missions, job abilities, spells, monsters, zones, NPCs, game mechanics, lore. This searches BG-Wiki and falls back to FFXIclopedia automatically. Search first, then answer using the content returned.

5. search_notes: Search the shared pool of user-contributed FFXI knowledge. Call this for FFXI questions where community wisdom might apply, and ALWAYS consider calling it after ffxi_wiki_search to find player notes that expand on or contradict the wiki. If matches come back, weave them into your answer and credit "another adventurer's notes." If no matches, just use the wiki/your knowledge — don't apologise for an empty search.

6. save_note: Call ONLY when the user is explicitly contributing a fact for you to remember ("remember that…", "take note…", "save this…"). Do NOT call for ordinary questions. After saving, acknowledge in 1-2 sentences of Moogle voice.

7. ffxi_zone_map: Fetch the zone map image(s) and BG-Wiki page text when the user asks about zone layout, navigation within a zone, or how to move between areas or sub-maps. The tool returns both the page text and the map images — read both carefully. Answer ONLY from what the page text and maps show; never blend in knowledge about surrounding zones. For multi-map zones, the sub-maps are all part of the same zone — transitions between them appear as passages or exits on the map images, not as zone lines to other zones.

8. ffxi_reddit_search: LAST RESORT ONLY. Search the r/ffxi subreddit for community discussion. Use this ONLY after ffxi_wiki_search and search_notes have failed to answer, OR for inherently opinion/experience-based questions a wiki cannot cover (e.g. "what's the best solo job", "is this content still worth doing", subjective recommendations). The results are unverified player posts and opinions, NOT authoritative facts — never prefer Reddit over the wiki, and when you do use it, make clear you're relaying community opinion from r/ffxi rather than confirmed fact.

9. escalate_to_planner: Use for COMPLEX, multi-step questions that a single lookup can't answer — ones that need cross-referencing several facts, arithmetic over looked-up values, or a chain of dependent lookups (e.g. "how many X fights for enough Y to finish the Z upgrade", "cheapest way to skill up a craft from 40 to 60", "compare gil/hour of farming A vs B"). When a question is like that, call escalate_to_planner with a one-line restatement of the goal INSTEAD of trying to answer it yourself — a more capable Moogle will take over the whole question. Do NOT escalate ordinary single-item or single-topic questions; handle those with the tools above.

Some recent notes may already appear below; use search_notes to dig deeper into the full pool by keyword.

Do not guess when you can look it up, kupo!

Remember: Stay in character as a Moogle!"""

    # Appended to the personality when a question is escalated to the planner
    # tier. The planner keeps Moogle voice but works the problem methodically and
    # does NOT have the escalate_to_planner tool (it is the escalation target).
    PLANNER_ADDENDUM = """

You have been handed a COMPLEX question that needs multi-step planning. Work it methodically, kupo:

1. PLAN: Break the goal into the concrete sub-facts you need (e.g. how many of an item a trial consumes, how many drop per fight, the drop rate, the number of trial stages).
2. GATHER: Use your tools to look up EACH sub-fact. Prefer ffxi_wiki_search for trial/quest/mechanic details and ffxi_item_lookup for item drop/source data. Do NOT guess numbers you can look up.
3. COMPUTE: Do the arithmetic explicitly. State the numbers you found and show the calculation so the user can follow it.
4. ANSWER: Lead with a clear, direct answer to the original question, then a short breakdown of how you got there. Call out any assumptions or ranges (e.g. drop-rate variance) plainly.

If a needed number genuinely cannot be found after searching, say so and give your best estimate clearly labeled as an estimate. Accuracy of the numbers matters most here — but stay in Moogle character.

Follow the Slack formatting rules from above EXACTLY in your final answer: no Markdown headers (##, ###), no horizontal rules (---). Use **Bold Label:** lines for sections and "- " for bullets. Keep the whole answer under ~2000 characters."""

    def __init__(self, model_id: str = None, region_name: str = None,
                 log_level: str = "ERROR", system_prompt: str = None,
                 planner_model_id: str = None, sentiment_model_id: str = None):
        self.model_id = model_id or os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)
        self.planner_model_id = (planner_model_id
                                 or os.environ.get("PLANNER_MODEL_ID")
                                 or DEFAULT_PLANNER_MODEL_ID)
        self.sentiment_model_id = (sentiment_model_id
                                   or os.environ.get("SENTIMENT_MODEL_ID")
                                   or DEFAULT_SENTIMENT_MODEL_ID)
        # Kill-switch for the more expensive planner tier. When disabled, the
        # escalate_to_planner tool is never offered, so every question is answered
        # by the front-line model only. Toggled via the ENABLE_PLANNER_ESCALATION
        # env var (default on) without a code change.
        self.escalation_enabled = _env_truthy(
            os.environ.get("ENABLE_PLANNER_ESCALATION"), default=True
        )
        self.region_name = (region_name
                            or os.environ.get("BEDROCK_REGION")
                            or os.environ.get("AWS_REGION")
                            or "us-east-1")
        self.client = boto3.client("bedrock-runtime", region_name=self.region_name)
        self.system_prompt = system_prompt or self.DEFAULT_PERSONALITY

        # Per-request audit trail of tool calls, as [{"name", "input"}, ...].
        # Reset at the start of each generate_response and read afterwards by the
        # handler for operational logging. Spans both the front-line and planner
        # tiers in the order the tools were invoked.
        self.last_tool_calls = []

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
                          note_context: dict = None,
                          escalation_notifier=None,
                          force_planner_goal: str = None) -> tuple:
        """Generate a Moogle response via the Bedrock Converse API.

        Args:
            messages: Conversation history as simple dicts
                      [{"role": "user"|"assistant", "content": str}, ...].
                      The caller appends the new user turn before calling.
            notes: User-contributed FFXI notes to inject into the system
                   prompt for this call only.
            note_context: Metadata used by the `save_note` tool dispatcher —
                          {"bucket": str, "author_id": str, "channel_id": str}.
            escalation_notifier: Optional callable(goal: str) invoked when the
                          front-line model escalates a complex question to the
                          planner tier. The caller uses it to post an interim
                          "this will take a moment" message to the user.
            force_planner_goal: When set, skip the front-line tier entirely and
                          send this goal straight to the planner. Used when the
                          caller has already decided a deep-research escalation is
                          warranted (e.g. repeated user dissatisfaction). The
                          escalation_notifier is NOT called in this case — the
                          caller is expected to post its own notice first.

        Returns:
            (text, item_lookups, escalated) where item_lookups is a list of dicts
            from any ffxi_item_lookup tool calls made during this turn, and
            escalated is True if the deep-planning tier produced the answer.

        Side effect:
            Populates self.last_tool_calls with every tool invoked this turn
            (name + input summary), in call order, for operational logging.
        """
        # Stash context for the in-call tool dispatcher. Safe because each
        # Lambda invocation gets its own client instance and runs serially.
        self._note_context = note_context or {}
        self.last_tool_calls = []

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

        system_text = self.system_prompt
        notes_section = _format_notes_for_prompt(notes) if notes else ""
        if notes_section:
            system_text = f"{system_text}\n\n{notes_section}"

        # Caller-forced deep-research escalation: skip the front-line tier and
        # hand the goal straight to the planner.
        if force_planner_goal:
            self.logger.info(f"Forced planner escalation: {force_planner_goal!r}")
            self.last_tool_calls.append({
                "name": "escalate_to_planner",
                "input": _summarize_tool_input({"goal": force_planner_goal}),
            })
            text, items = self._run_planner_tier(
                messages, force_planner_goal, system_text, max_tokens, temperature
            )
            return text, items, True

        # Force a tool call on the first iteration when the question looks like an
        # item query.  Smaller models tend to skip the tool with long history.
        force_tool_first = _looks_like_item_query(str(last_user))
        self.logger.info(f"force_tool_first={force_tool_first}")

        # --- Front-line tier: the everyday model. The escalate tool is only
        # offered when the planner tier is enabled. ---
        self.logger.info(f"planner escalation enabled: {self.escalation_enabled}")
        converse_messages = _to_converse_messages(messages)
        text, item_lookups, escalate_goal = self._run_tool_loop(
            converse_messages,
            system_text=system_text,
            model_id=self.model_id,
            max_iters=MAX_TOOL_ITERATIONS,
            max_tokens=max_tokens,
            temperature=temperature,
            include_escalate=self.escalation_enabled,
            force_first=force_tool_first,
        )

        if escalate_goal is None:
            return text, item_lookups, False

        # --- Planner tier: a more capable model with a planning prompt. ---
        self.last_tool_calls.append(
            {"name": "escalate_to_planner", "input": _summarize_tool_input({"goal": escalate_goal})}
        )
        if escalation_notifier:
            try:
                escalation_notifier(escalate_goal)
            except Exception as e:
                self.logger.error(f"escalation_notifier failed: {e}", exc_info=True)

        planner_text, planner_items = self._run_planner_tier(
            messages, escalate_goal, system_text, max_tokens, temperature
        )
        return planner_text, planner_items, True

    def _run_planner_tier(self, messages: list, goal: str, system_text: str,
                          max_tokens: int, temperature: float) -> tuple:
        """Run the deep-planning tier on the full conversation for ``goal``.

        A more capable model with the planning addendum and a larger tool budget
        (MAX_PLANNER_ITERATIONS). Returns (text, item_lookups). The planner does
        not get the escalate_to_planner tool — it is the escalation target.
        """
        self.logger.info(f"Escalating to planner ({self.planner_model_id}): {goal!r}")
        planner_system = f"{system_text}{self.PLANNER_ADDENDUM}"
        # Run the planner on the full conversation plus an explicit planning brief
        # so it has both context and a crisp restatement of the goal.
        planner_messages = _to_converse_messages(messages)
        planner_messages.append({
            "role": "assistant",
            "content": [{"text": (
                "This is a complex question, kupo — let me work it out step by step."
            )}],
        })
        planner_messages.append({
            "role": "user",
            "content": [{"text": (
                f"Please work out this goal carefully: {goal}"
            )}],
        })
        planner_text, planner_items, _ = self._run_tool_loop(
            planner_messages,
            system_text=planner_system,
            model_id=self.planner_model_id,
            max_iters=MAX_PLANNER_ITERATIONS,
            max_tokens=max(max_tokens, 1500),
            temperature=temperature,
            include_escalate=False,
            force_first=False,
        )
        return planner_text, planner_items

    def detect_repeated_negativity(self, messages: list) -> bool:
        """True if the user has voiced dissatisfaction with the bot's answers
        on two or more separate messages this conversation.

        One cheap classification call on a low-cost model (self.sentiment_model_id,
        default Nova-Lite; no tools, tiny token budget). Used to decide whether to
        force a deep-research escalation. Best-effort: any error returns False so a
        classifier hiccup never changes the answer path.
        """
        user_turns = [m for m in messages if m.get("role") == "user"]
        if len(user_turns) < 2:
            return False  # can't be "twice" with fewer than two user messages

        transcript = _format_transcript(messages)
        prompt = (
            "Here is a conversation between a user and an assistant:\n\n"
            f"{transcript}\n\n"
            "Has the USER expressed clear negative sentiment or dissatisfaction "
            "with the assistant's answers on TWO OR MORE separate user messages? "
            "Consider replies like 'that's wrong', 'still not helpful', 'no', "
            "'that's not what I asked'. Answer with exactly one word: YES or NO."
        )
        try:
            resp = self.client.converse(
                modelId=self.sentiment_model_id,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": 5, "temperature": 0},
            )
            verdict = _extract_text(
                resp["output"]["message"].get("content", [])
            ).strip().upper()
            self.logger.info(f"Repeated-negativity verdict: {verdict!r}")
            return verdict.startswith("YES")
        except Exception as e:
            self.logger.error(f"Sentiment detection failed: {e}", exc_info=True)
            return False

    def _run_tool_loop(self, converse_messages: list, system_text: str,
                       model_id: str, max_iters: int, max_tokens: int,
                       temperature: float, include_escalate: bool,
                       force_first: bool = False) -> tuple:
        """Run the Converse tool-use loop until a final answer or escalation.

        Returns (text, item_lookups, escalate_goal). When the model calls
        escalate_to_planner (only possible if include_escalate is True), the loop
        stops immediately and returns (None, item_lookups, goal); the caller is
        responsible for running the planner tier. Otherwise escalate_goal is None.
        """
        all_item_lookups = []

        for iteration in range(max_iters):
            force = force_first and iteration == 0
            final_iteration = iteration == max_iters - 1

            # On the last allowed pass, nudge the model to stop calling tools and
            # answer with what it has. The nudge goes in the system prompt rather
            # than a user turn so we don't break Converse's strict user/assistant
            # role alternation (the previous turn is already a tool-result user turn).
            call_system = system_text
            if final_iteration:
                call_system = (
                    f"{system_text}\n\nIMPORTANT: You have gathered enough "
                    "information. Provide your final answer NOW using what you "
                    "already have. Do NOT call any more tools."
                )

            response = self._invoke(converse_messages, max_tokens, temperature,
                                    system_text=call_system, force_tool=force,
                                    model_id=model_id, include_escalate=include_escalate)
            stop_reason = response.get("stopReason")
            output_message = response["output"]["message"]
            content_blocks = output_message.get("content", [])

            self.logger.info(
                f"[{model_id}] Iteration {iteration}: stopReason={stop_reason}, "
                f"blocks={len(content_blocks)}"
            )

            if stop_reason != "tool_use":
                text = _extract_text(content_blocks)
                self.logger.info(f"Final response length: {len(text)}")
                self.logger.debug(f"Response preview: {text[:100]}...")
                return text, all_item_lookups, None

            # If the model chose to escalate, stop here and hand the goal back to
            # the caller. We abandon this transcript (the dangling tool_use never
            # needs a tool_result because the planner runs a fresh conversation).
            if include_escalate:
                goal = _find_escalation_goal(content_blocks)
                if goal is not None:
                    self.logger.info(f"escalate_to_planner requested: {goal!r}")
                    return None, all_item_lookups, goal

            # Model still wants a tool even on its final chance. Degrade
            # gracefully: return any text it produced, else a friendly fallback —
            # never raise, so the user always gets a reply.
            if final_iteration:
                text = _extract_text(content_blocks)
                if text:
                    self.logger.warning(
                        "Tool-use loop hit cap; returning partial text answer"
                    )
                    return text, all_item_lookups, None
                self.logger.error(
                    "Tool-use loop hit cap with no usable text; returning fallback"
                )
                return TOOL_LOOP_FALLBACK, all_item_lookups, None

            # Append assistant turn and user turn with tool results.
            converse_messages.append(output_message)
            tool_results, captured_items = self._run_tool_calls(content_blocks)
            all_item_lookups.extend(captured_items)
            converse_messages.append({"role": "user", "content": tool_results})

        # Unreachable: the final iteration always returns above.
        return TOOL_LOOP_FALLBACK, all_item_lookups, None

    def _invoke(self, converse_messages: list, max_tokens: int, temperature: float,
                system_text: str = None, force_tool: bool = False,
                model_id: str = None, include_escalate: bool = True) -> dict:
        tools = [
            FFXI_ITEM_LOOKUP_TOOL,
            FFXIAH_LOOKUP_TOOL,
            FFXIDB_LOOKUP_TOOL,
            FFXI_WIKI_SEARCH_TOOL,
            SEARCH_NOTES_TOOL,
            SAVE_NOTE_TOOL,
            FFXI_ZONE_MAP_TOOL,
            FFXI_REDDIT_SEARCH_TOOL,
        ]
        if include_escalate:
            tools.append(ESCALATE_TO_PLANNER_TOOL)
        tool_config: dict = {"tools": tools}
        if force_tool:
            tool_config["toolChoice"] = {"any": {}}
        try:
            return self.client.converse(
                modelId=model_id or self.model_id,
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
            self.last_tool_calls.append(
                {"name": tool_name, "input": _summarize_tool_input(tool_input)}
            )
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
        if name == "ffxiah_lookup":
            item_name = (tool_input or {}).get("item_name", "").strip()
            if not item_name:
                return {"found": False, "error": "item_name is required"}
            return ffxiah_lookup(item_name)
        if name == "ffxidb_lookup":
            item_name = (tool_input or {}).get("item_name", "").strip()
            if not item_name:
                return {"found": False, "error": "item_name is required"}
            return ffxidb_lookup(item_name)
        if name == "ffxi_wiki_search":
            query = (tool_input or {}).get("query", "").strip()
            if not query:
                return {"found": False, "error": "query is required"}
            return wiki_search(query)
        if name == "ffxi_reddit_search":
            query = (tool_input or {}).get("query", "").strip()
            if not query:
                return {"found": False, "error": "query is required"}
            return reddit_search(query)
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


def _format_transcript(messages: list, limit: int = 12) -> str:
    """Render the most recent turns as 'Role: text' lines for a classifier prompt."""
    lines = []
    for m in messages[-limit:]:
        role = "User" if m.get("role") == "user" else "Assistant"
        content = m.get("content", "")
        if isinstance(content, list):
            content = _extract_text(content)
        lines.append(f"{role}: {str(content).strip()}")
    return "\n".join(lines)


def _summarize_tool_input(tool_input: dict) -> str:
    """One-line summary of a tool's input for the operation audit log.

    Returns the first non-empty string argument (item_name/query/zone_name/goal/
    note_text), truncated, so the ops log reads e.g. ffxi_item_lookup(Bone Chip).
    """
    if not tool_input:
        return ""
    for value in tool_input.values():
        if isinstance(value, str) and value.strip():
            summary = value.strip()
            return summary[:77] + "..." if len(summary) > 80 else summary
    return ""


def _env_truthy(value, default: bool = False) -> bool:
    """Parse a boolean-ish env var. None/unset returns the given default."""
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _find_escalation_goal(content_blocks: list):
    """Return the goal string if the model called escalate_to_planner, else None."""
    for block in content_blocks:
        tool_use = block.get("toolUse")
        if tool_use and tool_use.get("name") == "escalate_to_planner":
            goal = (tool_use.get("input") or {}).get("goal", "")
            return goal.strip() or "the user's question"
    return None


_THINKING_RE = re.compile(r"<thinking>.*?</thinking>", re.DOTALL | re.IGNORECASE)

def _extract_text(content_blocks: list) -> str:
    """Pull all text blocks out of a Converse response content list."""
    parts = []
    for block in content_blocks:
        if "text" in block and block["text"]:
            parts.append(block["text"])
    text = "\n".join(parts).strip()
    return _THINKING_RE.sub("", text).strip()
