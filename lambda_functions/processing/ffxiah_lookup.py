"""FFXIAH (ffxiah.com) Auction House lookup — importable module for the bot.

ffxiah.com tracks FFXI Auction House prices and sale velocity. Item pages
(``/item/<id>``) are fully server-rendered — the headline stats (Median, Stack
Price, Rate, Stock, Max/Min/Average) live in a plain two-column table. No bot
protection, no JS execution needed.

All results are scoped to the **Sylph** server. ffxiah selects the active
server via a ``sid`` cookie (Sylph = 8) and recomputes the stats table for that
server, so we set that cookie on every item fetch. (The site's default with no
cookie is the Asura server, not an all-server aggregate.)

ffxiah's ``/search`` results page is rendered client-side, but its autocomplete
endpoint ``/scripts/autoc_item.php?q=<name>`` returns plain ``Name|ID`` lines
server-side. That's the canonical name -> item ID resolver, exposed here as
:func:`resolve_item` and reused by the ffxidb tool (FFXI item IDs are game-wide
canonical, shared across both sites). Resolution is server-independent.

Exposes:
    resolve_item(name, session=None) -> Optional[dict]   # {id, name}
    lookup(name) -> Optional[dict]
    lookup_as_tool_result(name) -> dict

Items with no Sylph AH sales (Rare/Ex items like Excalibur, or items that
simply haven't sold on Sylph) report ``no_ah_data: True``.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

AUTOCOMPLETE_URL = "https://www.ffxiah.com/scripts/autoc_item.php"
ITEM_URL = "https://www.ffxiah.com/item/{id}"
USER_AGENT = "MoogleBot/1.0 (FFXI Slack Bot; ffxiah AH prices)"
FETCH_TIMEOUT = 15
RESOLVE_LIMIT = 10

# All AH stats are reported for the Sylph server. ffxiah scopes an item page to
# a server via the `sid` cookie; Sylph's id is 8 (from the page's server <select>).
SERVER_NAME = "Sylph"
SERVER_SID = 8

# Headline stat rows we surface from the item page's two-column stats table.
_STAT_LABELS = {"stock", "stack price", "rate", "median", "max", "min", "average"}

_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

logger = logging.getLogger(__name__)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    # Scope every ffxiah item page to the Sylph server.
    s.cookies.set("sid", str(SERVER_SID), domain="www.ffxiah.com")
    return s


def _resolve_candidates(name: str, sess: requests.Session) -> List[dict]:
    """Hit ffxiah's autocomplete endpoint -> [{id, name}, ...] (best first)."""
    resp = sess.get(
        AUTOCOMPLETE_URL,
        params={"q": name, "limit": RESOLVE_LIMIT},
        headers={"X-Requested-With": "XMLHttpRequest"},
        timeout=FETCH_TIMEOUT,
    )
    resp.raise_for_status()
    out: List[dict] = []
    for line in resp.text.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        label, _, raw_id = line.rpartition("|")
        label = label.strip()
        if label and raw_id.strip().isdigit():
            out.append({"id": int(raw_id.strip()), "name": label})
    return out


def resolve_item(name: str, session: Optional[requests.Session] = None) -> Optional[dict]:
    """Resolve an item name to ``{id, name}`` via ffxiah's autocomplete endpoint.

    Prefers an exact case-insensitive name match, otherwise the first result.
    Returns None when nothing matches. Shared by the ffxidb tool.
    """
    sess = session or _session()
    candidates = _resolve_candidates(name, sess)
    if not candidates:
        return None
    target = name.strip().lower()
    for c in candidates:
        if c["name"].lower() == target:
            return c
    return candidates[0]


def _first_number(text: str) -> Optional[float]:
    """Pull the first number out of a stat string, e.g. '10000 ( 833.33 per)' -> 10000."""
    m = _NUM_RE.search(text or "")
    if not m:
        return None
    try:
        val = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    return int(val) if val.is_integer() else round(val, 2)


def _parse_stats(soup: BeautifulSoup) -> dict:
    """Collect label -> value from the item page's two-column stats rows."""
    stats: dict = {}
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) != 2:
            continue
        label = re.sub(r"\s+", " ", tds[0].get_text(" ", strip=True)).strip()
        value = re.sub(r"\s+", " ", tds[1].get_text(" ", strip=True)).strip()
        if label.lower() in _STAT_LABELS and value:
            stats[label.lower()] = value
    return stats


def lookup(name: str) -> Optional[dict]:
    """Resolve ``name`` to an item ID, then scrape its Sylph-server ffxiah page.

    Returns None when the name can't be resolved at all. When the item has no
    Sylph AH sales (Rare/Ex, or simply unsold on Sylph), returns a dict with
    ``no_ah_data: True``.
    """
    sess = _session()
    candidates = _resolve_candidates(name, sess)
    if not candidates:
        return None
    target = name.strip().lower()
    resolved = next((c for c in candidates if c["name"].lower() == target), candidates[0])

    item_id = resolved["id"]
    # The `sid` cookie set on the session scopes this page to the Sylph server.
    resp = sess.get(ITEM_URL.format(id=item_id), timeout=FETCH_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    title_el = soup.find("h1")
    title = title_el.get_text(" ", strip=True) if title_el else resolved["name"]
    other = [c["name"] for c in candidates if c["id"] != item_id][:5]

    stats = _parse_stats(soup)

    result = {
        "id": item_id,
        "name": title,
        "url": resp.url,  # canonical slug URL after the redirect
        "server": SERVER_NAME,
        "other_results": other,
    }

    # No Median row means no Sylph AH sales — either a Rare/Ex item that can't be
    # auctioned at all, or one that simply hasn't sold on Sylph.
    if "median" not in stats:
        result["no_ah_data"] = True
        result["note"] = (
            f"No Auction House sales data for this item on the {SERVER_NAME} "
            "server — it may be Rare/Ex (cannot be sold on the AH) or simply "
            f"hasn't sold on {SERVER_NAME} recently."
        )
        return result

    result["no_ah_data"] = False
    result["median_price"] = _first_number(stats.get("median", ""))
    result["stack_price"] = _first_number(stats.get("stack price", ""))
    result["sale_rate_per_day"] = _first_number(stats.get("rate", ""))
    result["average_price"] = _first_number(stats.get("average", ""))
    result["max_price"] = _first_number(stats.get("max", ""))
    result["min_price"] = _first_number(stats.get("min", ""))
    result["stock"] = _first_number(stats.get("stock", ""))
    return result


def lookup_as_tool_result(name: str) -> dict:
    """Tool entrypoint. Returns a dict with a ``found`` flag; never raises."""
    try:
        info = lookup(name)
    except Exception as e:
        logger.error(f"ffxiah lookup error for {name!r}: {e}", exc_info=True)
        return {"found": False, "name": name, "error": str(e)}
    if info is None:
        return {"found": False, "name": name}
    info["found"] = True
    info["query"] = name
    return info


__all__ = ["resolve_item", "lookup", "lookup_as_tool_result"]
