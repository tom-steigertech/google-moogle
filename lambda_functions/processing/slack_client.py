"""Slack Client for the Moogle bot - handles Slack API interactions."""

import os
import re
import logging
import requests


class MoogleSlackClient:
    """Encapsulates Slack Web API interactions for the Moogle bot.

    Handles sending messages to Slack channels and threads.
    """

    DROPS_INLINE = 5  # max drop sources shown in the card; remainder goes in a thread

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
        self.update_url = 'https://slack.com/api/chat.update'
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
    
    @staticmethod
    def _md_to_mrkdwn(text: str) -> str:
        """Convert common Markdown syntax to Slack mrkdwn syntax."""
        # Bold: **text** → *text*
        text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text, flags=re.DOTALL)
        # Markdown headers: ## Header → *Header*
        text = re.sub(r'^#{1,3}\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)
        # Unordered list items at line start: "- item" or "* item" → "• item"
        text = re.sub(r'^[*-] ', '• ', text, flags=re.MULTILINE)
        return text

    @staticmethod
    def _md_to_blocks(text: str) -> list:
        """Convert a Markdown-formatted LLM response into Slack Block Kit blocks.

        Splits on blank lines, detects section headers (a single bold line),
        and inserts dividers between sections for a clean visual layout.
        """
        text = MoogleSlackClient._md_to_mrkdwn(text.strip())
        paragraphs = re.split(r'\n{2,}', text)
        blocks = []
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            first_line = para.split('\n')[0]
            # A section header is a line that is entirely bold (starts and ends with *)
            is_header = bool(re.match(r'^\*[^*\n]+\*:?\s*$', first_line))
            if is_header and blocks:
                blocks.append({"type": "divider"})
            # Slack section blocks cap at 3000 chars
            if len(para) > 2900:
                para = para[:2897] + "…"
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": para}
            })
        return blocks or [{
            "type": "section",
            "text": {"type": "mrkdwn", "text": text or "(no response)"}
        }]

    def send_response(self, channel_id: str, text: str, thread_ts: str = None,
                     is_mention: bool = False, timeout: int = 30,
                     update_ts: str = None) -> dict:
        """Send a formatted message to Slack as Block Kit blocks.

        Converts Markdown in `text` to Slack mrkdwn and splits it into
        section blocks with dividers between named sections.

        When ``update_ts`` is given, the message at that timestamp is edited in
        place (chat.update) instead of posting a new message — used to replace
        the "thinking" placeholder with the real answer.
        """
        if not channel_id:
            raise ValueError("channel_id is required")

        if not text:
            self.logger.warning("Empty text provided, using error message")
            text = self.ERROR_MESSAGE

        blocks = self._md_to_blocks(text)
        # Fallback text for notifications / accessibility (first 150 chars, plain)
        fallback = re.sub(r'[*_~`]', '', text)[:150]

        return self.send_blocks(
            channel_id=channel_id,
            blocks=blocks,
            text=fallback,
            thread_ts=thread_ts,
            is_mention=is_mention,
            timeout=timeout,
            update_ts=update_ts,
        )

    def send_blocks(self, channel_id: str, blocks: list, text: str = "",
                    thread_ts: str = None, is_mention: bool = False,
                    timeout: int = 30, update_ts: str = None) -> dict:
        """Send a Block Kit message to Slack.

        Args:
            blocks: Slack Block Kit blocks list
            text: Fallback text for notifications / accessibility
            update_ts: If set, edit the message at this ts (chat.update) instead
                       of posting a new one. thread_ts is ignored when updating.
        """
        if not channel_id:
            raise ValueError("channel_id is required")

        data = {
            'channel': channel_id,
            'blocks': blocks,
            'text': text,
        }
        if update_ts:
            data['ts'] = update_ts
        elif thread_ts:
            data['thread_ts'] = thread_ts

        url = self.update_url if update_ts else self.api_url
        self.logger.debug(
            f"{'Updating' if update_ts else 'Sending'} blocks to channel "
            f"{channel_id} ({len(blocks)} blocks)"
        )

        try:
            resp = requests.post(
                url,
                headers=self.headers,
                json=data,
                timeout=timeout
            )
            resp.raise_for_status()
            response_data = resp.json()

            if response_data.get('ok'):
                self.logger.info("Successfully sent blocks to Slack")
                return response_data
            else:
                error = response_data.get('error', 'Unknown error')
                self.logger.error(f"Slack API error (blocks): {error}")
                raise Exception(f"Slack API error: {error}")

        except requests.exceptions.Timeout:
            raise Exception(f"Slack API timeout after {timeout}s")
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error sending blocks to Slack: {e}")
            raise

    @staticmethod
    def format_item_card(item_data: dict) -> list:
        """Build Slack Block Kit blocks for an FFXI item lookup result.

        Returns an empty list if the item was not found (let the LLM handle it).
        """
        if not item_data.get("found"):
            return []

        blocks = []
        name = item_data.get("name", "Unknown Item")

        # Header
        blocks.append({
            "type": "header",
            "text": {"type": "plain_text", "text": name, "emoji": True}
        })

        # Note when the lookup resolved a fuzzy match (e.g. "Grape" -> "Royal Grape").
        query = item_data.get("query")
        matched = item_data.get("matched_title")
        if matched and query and matched.strip().lower() != query.strip().lower():
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn",
                              "text": f"_Closest match for “{query}”_"}]
            })

        # Metadata row
        meta_parts = []
        if item_data.get("item_type"):
            meta_parts.append(f"*Type:* {item_data['item_type']}")
        stack = item_data.get("stack_size")
        if stack and stack > 1:
            meta_parts.append(f"*Stack:* ×{stack}")
        sell = item_data.get("npc_sell_price")
        if sell:
            meta_parts.append(f"*Sell:* {sell:,} gil")
        flags = item_data.get("flags", [])
        if flags:
            meta_parts.append(f"*Flags:* {', '.join(flags)}")

        meta_text = "  |  ".join(meta_parts) if meta_parts else ""
        if meta_text:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": meta_text}
            })

        # Vendors
        vendors = item_data.get("vendors", [])
        if vendors:
            blocks.append({"type": "divider"})
            vendors_total = item_data.get("vendors_total", len(vendors))
            truncated = item_data.get("vendors_truncated", False)
            count_label = f"{vendors_total}" + (" shown" if truncated else "")
            lines = [f"*Vendors* ({count_label}):"]
            for v in vendors:
                npc = v.get("npc") or ""
                zone = v.get("zone") or ""
                price = v.get("price")
                line = f"• {npc}"
                if zone:
                    line += f"  {zone}"
                if price:
                    line += f"  _{price:,} gil_"
                lines.append(line)
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(lines)}
            })

        # Drops — show up to DROPS_INLINE inline; full list goes in a thread
        drops = item_data.get("drops", [])
        if drops:
            blocks.append({"type": "divider"})
            drops_total = item_data.get("drops_total", len(drops))
            drops_truncated = item_data.get("drops_truncated", False)
            plural = "source" if drops_total == 1 else "sources"
            needs_thread = len(drops) > MoogleSlackClient.DROPS_INLINE or drops_truncated

            lines = [f"*Dropped by* ({drops_total} {plural}):"]
            for d in drops[:MoogleSlackClient.DROPS_INLINE]:
                monster = d.get("monster") or "?"
                zone = d.get("zone") or ""
                lines.append(f"• {monster}" + (f"  _{zone}_" if zone else ""))
            if needs_thread:
                lines.append("_See thread for full list ↓_")
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(lines)}
            })

        # How to Obtain — crafting / auction-house / acquisition info.
        # Essential for craft-only or AH-only items (e.g. Cornstarch) that have
        # no vendors and no drops and would otherwise show a near-empty card.
        crafts = item_data.get("synthesis_crafts") or []
        crystal = item_data.get("synthesis_crystal")
        ingredients = item_data.get("synthesis_ingredients") or []
        synthesis = item_data.get("synthesis")
        how = item_data.get("how_to_obtain")
        ah = item_data.get("auction_house")
        ah_cat = item_data.get("ah_category")

        obtain_lines = []
        if crafts:
            craft_str = ", ".join(f"{c['skill']} ({c['level']})" for c in crafts)
            obtain_lines.append(f"*Crafted via:* {craft_str}")
            # Prefer a tidy crystal + ingredients recipe; fall back to raw text.
            recipe_parts = []
            if crystal:
                recipe_parts.append(crystal)
            for ing in ingredients:
                qty = ing.get("qty") or 1
                recipe_parts.append(f"{qty}× {ing['name']}" if qty > 1 else ing["name"])
            if recipe_parts:
                obtain_lines.append("*Ingredients:* " + " + ".join(recipe_parts))
            elif synthesis:
                obtain_lines.append(f"_{MoogleSlackClient._trim(synthesis, 240)}_")
        elif synthesis:
            obtain_lines.append(f"_{MoogleSlackClient._trim(synthesis, 240)}_")
        if ah:
            ah_line = "*Auction House:* available"
            if ah_cat:
                ah_line += f"  _{ah_cat}_"
            obtain_lines.append(ah_line)
        if how:
            # Strip the leading "Auction House Category : <cat>" we already surfaced.
            extra = how
            if ah_cat:
                extra = re.sub(
                    rf"^Auction House Category\s*:?\s*{re.escape(ah_cat)}\s*",
                    "", extra,
                ).strip()
            if extra:
                obtain_lines.append(MoogleSlackClient._trim(extra, 280))

        if obtain_lines:
            blocks.append({"type": "divider"})
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn",
                         "text": "*How to Obtain*\n" + "\n".join(obtain_lines)}
            })

        # Used in recipes — compact snippet (full list lives on the wiki).
        used_in = item_data.get("used_in")
        if used_in:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn",
                         "text": f"*Used in recipes:* {MoogleSlackClient._trim(used_in, 240)}"}
            })

        return blocks

    @staticmethod
    def _trim(text: str, limit: int) -> str:
        """Collapse whitespace and truncate to ``limit`` chars with an ellipsis."""
        text = re.sub(r"\s+", " ", text or "").strip()
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "…"

    @staticmethod
    def format_drops_thread(item_data: dict) -> list:
        """Build blocks for the full drop list, for posting as a thread reply on the item card."""
        drops = item_data.get("drops", [])
        if not drops:
            return []

        drops_total = item_data.get("drops_total", len(drops))
        drops_truncated = item_data.get("drops_truncated", False)
        plural = "source" if drops_total == 1 else "sources"
        name = item_data.get("name", "this item")

        drop_lines = []
        for d in drops:
            monster = d.get("monster") or "?"
            zone = d.get("zone") or ""
            drop_lines.append(f"• {monster}" + (f"  _{zone}_" if zone else ""))
        if drops_truncated:
            extra = drops_total - len(drops)
            if extra > 0:
                drop_lines.append(f"_( +{extra} additional sources not shown)_")

        header = f"*All drop sources for {name}* ({drops_total} {plural}):"
        blocks = []
        current_lines = [header]
        for line in drop_lines:
            if len("\n".join(current_lines + [line])) > 2800:
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "\n".join(current_lines)}
                })
                current_lines = [line]
            else:
                current_lines.append(line)
        if current_lines:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(current_lines)}
            })
        return blocks

    def send_error_message(self, channel_id: str, thread_ts: str = None,
                         is_mention: bool = False, update_ts: str = None) -> dict:
        """Send the standard error message to Slack.

        Args:
            channel_id: The Slack channel ID
            thread_ts: Thread timestamp (for threading)
            is_mention: Whether this is a mention response
            update_ts: If set, replace the placeholder at this ts with the error

        Returns:
            The Slack API response as a dict
        """
        return self.send_response(
            channel_id=channel_id,
            text=self.ERROR_MESSAGE,
            thread_ts=thread_ts,
            is_mention=is_mention,
            update_ts=update_ts,
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
