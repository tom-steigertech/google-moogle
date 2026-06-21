"""FFXIDB (ffxidb.com) item lookup — importable module for the Moogle bot.

ffxidb.com is a server-rendered FFXI item/game database (description, type,
jobs, level, drop sources) — the "general item info" alternate source alongside
the FFXIclopedia ffxi_item_lookup tool. It is NOT a price tracker; prices and
sale velocity come from the ffxiah tool instead.

Name -> item ID is resolved primarily through ffxiah's autocomplete endpoint
(see :func:`ffxiah_lookup.resolve_item`), which is far more complete than
ffxidb's own search (ffxidb's search misses common items such as the elemental
crystals). FFXI item IDs are game-wide canonical, so ``/items/4096`` is Fire
Crystal on both sites. ffxidb's own ``/search?q=`` page is kept as a fallback
resolver for the rare items ffxiah's index doesn't carry.

Exposes:
    lookup(name, session=None) -> Optional[dict]
    lookup_as_tool_result(name) -> dict
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

from .ffxiah_lookup import resolve_item as _resolve_via_ffxiah

BASE = "https://www.ffxidb.com"
SEARCH_URL = BASE + "/search"
ITEM_URL = BASE + "/items/{id}"
USER_AGENT = "MoogleBot/1.0 (FFXI Slack Bot; ffxidb item info)"
FETCH_TIMEOUT = 15

MAX_DESC_CHARS = 400
MAX_INFO_CHARS = 400
MAX_DROPS = 8

# ffxidb's nav/sidebar also emit /items/... links (e.g. "drops" round-ups); the
# real result rows carry a plain item name. We keep all candidates and let the
# matcher prefer an exact (case-insensitive) name hit.
_ITEM_HREF_RE = re.compile(r"^/items/(\d+)\b")

logger = logging.getLogger(__name__)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def _unwrap_bytes(text: str) -> str:
    """Strip the literal ``b'...'`` / ``b"..."`` wrapper ffxidb renders around
    item descriptions (the site prints a Python bytes repr verbatim)."""
    s = (text or "").strip()
    m = re.fullmatch(r"b(['\"])(.*)\1", s, re.DOTALL)
    return m.group(2) if m else s


def _search_candidates(name: str, sess: requests.Session) -> List[dict]:
    """Return [{id, name, url}, ...] from ffxidb's server-rendered search page."""
    resp = sess.get(SEARCH_URL, params={"q": name}, timeout=FETCH_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    out: List[dict] = []
    seen: set = set()
    for a in soup.find_all("a", href=True):
        m = _ITEM_HREF_RE.match(a["href"])
        if not m:
            continue
        item_id = int(m.group(1))
        label = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        if not label or item_id in seen:
            continue
        seen.add(item_id)
        out.append({"id": item_id, "name": label, "url": BASE + f"/items/{item_id}"})
    return out


def _resolve(name: str, sess: requests.Session) -> Optional[dict]:
    """Resolve an item name to ``{id, name, url}``.

    Tries ffxiah's autocomplete endpoint first (the most complete index), then
    falls back to ffxidb's own search page for items ffxiah doesn't carry.
    Returns None when nothing matches.
    """
    try:
        hit = _resolve_via_ffxiah(name, sess)
    except Exception as e:
        logger.warning(f"ffxiah resolver failed for {name!r}: {e}; using ffxidb search")
        hit = None
    if hit:
        return {"id": hit["id"], "name": hit["name"],
                "url": BASE + f"/items/{hit['id']}"}

    candidates = _search_candidates(name, sess)
    if not candidates:
        return None
    target = name.strip().lower()
    for c in candidates:
        if c["name"].lower() == target:
            return c
    return candidates[0]


def _drop_sources(soup: BeautifulSoup) -> List[dict]:
    """Pull a few NPC/Zone drop rows from the 'Dropped By' table when present."""
    drops: List[dict] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header = [c.get_text(" ", strip=True).lower() for c in rows[0].find_all(["th", "td"])]
        if "npc" not in header or "zone" not in header:
            continue
        npc_i, zone_i = header.index("npc"), header.index("zone")
        chance_i = header.index("chance") if "chance" in header else None
        for tr in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if not cells or all(not c for c in cells):
                continue
            joined = " ".join(cells)
            if "no drop data" in joined.lower() or "no voidwatch" in joined.lower():
                continue
            npc = cells[npc_i] if npc_i < len(cells) else ""
            zone = cells[zone_i] if zone_i < len(cells) else ""
            if not npc and not zone:
                continue
            entry = {"npc": npc, "zone": zone}
            if chance_i is not None and chance_i < len(cells) and cells[chance_i]:
                entry["chance"] = cells[chance_i]
            drops.append(entry)
            if len(drops) >= MAX_DROPS:
                return drops
    return drops


def lookup(name: str, session: Optional[requests.Session] = None) -> Optional[dict]:
    """Resolve ``name`` and scrape its ffxidb item page into a dict, or None."""
    sess = session or _session()
    resolved = _resolve(name, sess)
    if not resolved:
        return None

    resp = sess.get(resolved["url"], timeout=FETCH_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    def _cls_text(cls: str) -> str:
        el = soup.find(class_=cls)
        return el.get_text(" ", strip=True) if el else ""

    title = _cls_text("itemtitle") or resolved["name"]
    desc_raw = _cls_text("itemdesc")
    desc = _unwrap_bytes(desc_raw)
    tags = _cls_text("itemtags")

    # .iteminfo is the richest field — type, races, stats, level, jobs — but it
    # embeds the same bytes-wrapped description. Swap the raw wrapper for the
    # cleaned text so the model sees readable info regardless of apostrophes.
    info = _cls_text("iteminfo")
    if desc_raw and desc_raw in info:
        info = info.replace(desc_raw, desc)
    info = re.sub(r"\s+", " ", info).strip()

    return {
        "id": resolved["id"],
        "name": title,
        "url": resolved["url"],
        "description": desc[:MAX_DESC_CHARS] or None,
        "info": info[:MAX_INFO_CHARS] or None,
        "tags": tags or None,
        "drops": _drop_sources(soup),
    }


def lookup_as_tool_result(name: str) -> dict:
    """Tool entrypoint. Returns a dict with a ``found`` flag; never raises."""
    try:
        info = lookup(name)
    except Exception as e:
        logger.error(f"ffxidb lookup error for {name!r}: {e}", exc_info=True)
        return {"found": False, "name": name, "error": str(e)}
    if info is None:
        return {"found": False, "name": name}
    info["found"] = True
    info["query"] = name
    return info


__all__ = ["lookup", "lookup_as_tool_result"]
