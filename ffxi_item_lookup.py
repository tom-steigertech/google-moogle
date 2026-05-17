#!/usr/bin/env python3
"""
ffxi_item_lookup.py
====================

Look up a Final Fantasy XI item on FFXIclopedia (https://ffxiclopedia.fandom.com)
and print:

  * Rare / Ex / Aux flags (and Stack size / Type when present)
  * NPC sell price (a.k.a. resale / discard value)
  * Vendor NPCs that sell the item, with their zone and price
  * Drop sources -- monsters that drop it, with the zone they live in

Usage
-----
    python3 ffxi_item_lookup.py "Imperial Bronze Piece"
    python3 ffxi_item_lookup.py "Excalibur" --json
    python3 ffxi_item_lookup.py "Bone Chip" --raw          # dump the cleaned text

Requirements
------------
    pip install requests beautifulsoup4

Notes
-----
FFXIclopedia is a MediaWiki-based wiki hosted by Fandom. This script uses the
MediaWiki ``action=parse`` API to fetch the rendered HTML of an item page,
then parses it with BeautifulSoup. Item pages on FFXIclopedia don't follow a
perfectly consistent schema, so the parser tries a few strategies (infobox
fields, "Resale Price" lines, "Sold by"/"Dropped by"/"How to Obtain" section
headings, vendor and drop tables) and falls back to printing the raw text of
any section it couldn't structure. If a field comes back empty for an item you
expected to find, run with ``--raw`` to see what the page actually looks like.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Iterable, List, Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup, NavigableString, Tag


WIKI_API = "https://ffxiclopedia.fandom.com/api.php"
WIKI_BASE = "https://ffxiclopedia.fandom.com/wiki/"
USER_AGENT = "ffxi-item-lookup/1.0 (+https://ffxiclopedia.fandom.com)"

# Exact heading names (case-insensitive) used by FFXIclopedia item pages.
# We use exact matches -- substring matching produced false positives like
# "Obtained from Desynthesis" being treated as a drop section.
VENDOR_HEADINGS = (
    "guild merchants",
    "merchants",
    "sold by vendors",
    "sold by npcs",
    "vendors",
    "vendor",
    "purchased from vendors",
    "purchased from",
    "where to buy",
    "dispensed from",
)
DROP_HEADINGS = (
    "dropped from",
    "dropped by",
    "stolen from",
    "despoiled from",
    "found on",
    "treasure casket",
    "spawned treasure",
    "salvage",
)


@dataclass
class Vendor:
    npc: Optional[str] = None
    zone: Optional[str] = None
    price: Optional[int] = None
    notes: Optional[str] = None
    raw: str = ""


@dataclass
class Drop:
    monster: Optional[str] = None
    zone: Optional[str] = None
    notes: Optional[str] = None
    raw: str = ""


@dataclass
class ItemInfo:
    name: str
    url: str
    flags: List[str] = field(default_factory=list)
    item_type: Optional[str] = None
    stack_size: Optional[int] = None
    npc_sell_price: Optional[int] = None
    vendors: List[Vendor] = field(default_factory=list)
    drops: List[Drop] = field(default_factory=list)
    raw_text_by_section: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_item_html(title: str, session: requests.Session) -> Optional[str]:
    """Return the rendered HTML for an FFXIclopedia page, or None if missing."""
    params = {
        "action": "parse",
        "page": title,
        "format": "json",
        "prop": "text",
        "redirects": 1,
    }
    resp = session.get(WIKI_API, params=params, timeout=30,
                       headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        return None
    return data.get("parse", {}).get("text", {}).get("*")


def candidate_titles(name: str) -> Iterable[str]:
    """Yield URL-friendly title variants to try in order."""
    name = name.strip()
    yield name.replace(" ", "_")
    # Mediawiki capitalizes the first letter automatically, but try the
    # explicit title-case variant for items like "vile elixir".
    yield name.title().replace(" ", "_")
    # Bone_chip -> Bone_chip is the same as the first, but uppercase first.
    if name and not name[0].isupper():
        yield (name[0].upper() + name[1:]).replace(" ", "_")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

GIL_RE = re.compile(r"([\d,]+)\s*gil", re.IGNORECASE)
PRICE_RANGE_RE = re.compile(r"([\d,]+)\s*(?:~|-|to|–|—)\s*([\d,]+)\s*gil", re.IGNORECASE)
STACK_RE = re.compile(r"Stackable?[:\s]*\(?([\d]+)\)?", re.IGNORECASE)
FLAG_WORDS = ("Rare", "Ex", "Exclusive", "Aux", "Augmented")


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_flags(soup: BeautifulSoup) -> List[str]:
    """Look for Rare/Ex/Aux markers, usually shown in the infobox or first lines."""
    flags: List[str] = []
    seen = set()
    # Many item infoboxes render flags as small inline images with alt text,
    # e.g. <img alt="Rare"> / <img alt="Ex">.
    for img in soup.find_all("img"):
        alt = (img.get("alt") or "").strip()
        if alt in FLAG_WORDS and alt not in seen:
            flags.append("Ex" if alt == "Exclusive" else alt)
            seen.add(alt)
    # Fallback: look at the first ~1500 chars of text for the literal words.
    if not flags:
        head = clean(soup.get_text(" ", strip=True))[:1500]
        for w in FLAG_WORDS:
            if re.search(rf"\b{w}\b", head) and w not in seen:
                flags.append("Ex" if w == "Exclusive" else w)
                seen.add(w)
    return flags


def extract_stack_size(soup: BeautifulSoup) -> Optional[int]:
    text = soup.get_text(" ", strip=True)
    m = STACK_RE.search(text)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def extract_item_type(soup: BeautifulSoup) -> Optional[str]:
    """Try to read the 'Type' row from a standard FFXIclopedia item infobox."""
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            if len(cells) >= 2:
                label = clean(cells[0].get_text(" ", strip=True)).lower().rstrip(":")
                value = clean(cells[1].get_text(" ", strip=True))
                if label in {"type", "category"} and value:
                    return value
    return None


def extract_sell_price(soup: BeautifulSoup) -> Optional[int]:
    """Find NPC sell / resale / discard price."""
    text = clean(soup.get_text(" ", strip=True))
    patterns = [
        r"Resale Price[:\s]*([\d,]+)\s*gil",
        r"Sells (?:to NPC )?for[:\s]*([\d,]+)\s*gil",
        r"NPC Sell Price[:\s]*([\d,]+)\s*gil?",
        r"Discard Value[:\s]*([\d,]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return None


def _heading_text(tag: Tag) -> str:
    # MediaWiki wraps headings in <h2><span class="mw-headline">Title</span></h2>.
    # Some skins also append a "[ ]" edit/show-hide marker; strip that.
    span = tag.find("span", class_="mw-headline")
    text = clean((span or tag).get_text(" ", strip=True))
    text = re.sub(r"\s*\[\s*\]\s*$", "", text)
    return text


def sections_by_keywords(soup: BeautifulSoup, keywords: Iterable[str]) -> List[Tag]:
    """Return heading tags whose text exactly matches one of the keywords (case-insensitive)."""
    targets = {k.lower() for k in keywords}
    out = []
    for h in soup.find_all(["h2", "h3", "h4"]):
        title = _heading_text(h).lower()
        if title in targets:
            out.append(h)
    return out


def expand_rowspan(rows: List[Tag]) -> List[List[str]]:
    """
    Expand HTML table ``rowspan`` so every output row has one entry per logical column.
    Returns a list of rows, each a list of cleaned cell strings.
    """
    grid: List[List[str]] = []
    pending: dict = {}  # column index -> (text, rows_remaining)
    for tr in rows:
        cells = tr.find_all(["th", "td"], recursive=False) or tr.find_all(["th", "td"])
        out_row: List[str] = []
        col = 0
        cell_iter = iter(cells)
        # Determine total number of columns needed: max of (current cells + already-pending)
        cell_list = list(cell_iter)
        idx = 0
        while idx < len(cell_list) or any(v[1] > 0 for v in pending.values()):
            if col in pending and pending[col][1] > 0:
                text, remaining = pending[col]
                out_row.append(text)
                pending[col] = (text, remaining - 1)
                col += 1
                continue
            if idx >= len(cell_list):
                break
            cell = cell_list[idx]
            idx += 1
            text = clean(cell.get_text(" ", strip=True))
            try:
                span = int(cell.get("rowspan") or 1)
            except ValueError:
                span = 1
            try:
                colspan = int(cell.get("colspan") or 1)
            except ValueError:
                colspan = 1
            for _ in range(colspan):
                if span > 1:
                    pending[col] = (text, span - 1)
                out_row.append(text)
                col += 1
        grid.append(out_row)
    return grid


def section_siblings(heading: Tag) -> Iterable[Tag]:
    """Yield element siblings until we hit the next heading of equal/higher level."""
    level = int(heading.name[1])
    for el in heading.find_all_next():
        if isinstance(el, Tag) and el.name and el.name.startswith("h"):
            try:
                if int(el.name[1]) <= level:
                    return
            except ValueError:
                pass
        yield el


def section_text(heading: Tag) -> str:
    chunks = []
    for el in section_siblings(heading):
        if isinstance(el, Tag) and el.name in ("ul", "ol", "table", "p", "dl"):
            chunks.append(el.get_text(" ", strip=True))
    return clean("\n".join(chunks))


def _header_columns(grid: List[List[str]]):
    """
    Identify the header row in a small table. Some FFXIclopedia tables have a
    one-cell preamble row (e.g., "Price: 150 gil") above the real header.
    Returns (header_row_idx, {column_name_lower: column_index}).
    """
    for i, row in enumerate(grid[:3]):
        lowered = [c.lower() for c in row]
        if "name" in lowered or "monster" in lowered or "npc" in lowered or "vendor" in lowered:
            return i, {c: idx for idx, c in enumerate(lowered)}
    # Fall back: assume the first row is the header.
    if grid:
        lowered = [c.lower() for c in grid[0]]
        return 0, {c: idx for idx, c in enumerate(lowered)}
    return 0, {}


def _table_preamble_price(grid: List[List[str]], header_idx: int) -> Optional[int]:
    """
    FFXIclopedia 'Guild Merchants' tables put a single-cell row like
    'Price: 150 gil' above the column header. Pull a price out of that row.
    """
    for row in grid[:header_idx]:
        for cell in row:
            price = _parse_price(cell)
            if price is not None:
                return price
    return None


def parse_vendors(soup: BeautifulSoup) -> List[Vendor]:
    vendors: List[Vendor] = []
    seen = set()
    for heading in sections_by_keywords(soup, VENDOR_HEADINGS):
        for el in section_siblings(heading):
            if not isinstance(el, Tag):
                continue
            if el.name == "table":
                rows = el.find_all("tr")
                if not rows:
                    continue
                grid = expand_rowspan(rows)
                header_idx, col = _header_columns(grid)
                table_price = _table_preamble_price(grid, header_idx)

                npc_idx = next((col[k] for k in ("name", "npc", "vendor", "merchant")
                                if k in col), 0)
                zone_idx = next((col[k] for k in ("location", "zone", "area")
                                 if k in col), None)
                price_idx = next((col[k] for k in ("price", "cost", "gil")
                                  if k in col), None)
                note_idx = next((col[k] for k in ("notes", "note", "guild",
                                                  "fame", "condition")
                                 if k in col), None)

                for row in grid[header_idx + 1:]:
                    if not row or all(not c for c in row):
                        continue
                    raw = " | ".join(row)
                    npc = row[npc_idx] if npc_idx is not None and npc_idx < len(row) else None
                    zone = row[zone_idx] if zone_idx is not None and zone_idx < len(row) else None
                    notes = row[note_idx] if note_idx is not None and note_idx < len(row) else None
                    price = None
                    if price_idx is not None and price_idx < len(row):
                        price = _parse_price(row[price_idx])
                    if price is None:
                        price = table_price
                    key = (npc, zone, price, raw)
                    if key in seen:
                        continue
                    seen.add(key)
                    vendors.append(Vendor(npc=npc, zone=zone, price=price,
                                          notes=notes, raw=raw))
            elif el.name in ("ul", "ol"):
                for li in el.find_all("li", recursive=False):
                    raw = clean(li.get_text(" ", strip=True))
                    if not raw:
                        continue
                    npc, zone, price = _split_list_entry(raw)
                    key = (npc, zone, price, raw)
                    if key in seen:
                        continue
                    seen.add(key)
                    vendors.append(Vendor(npc=npc, zone=zone, price=price, raw=raw))
    return vendors


def parse_drops(soup: BeautifulSoup) -> List[Drop]:
    drops: List[Drop] = []
    seen = set()
    for heading in sections_by_keywords(soup, DROP_HEADINGS):
        source_kind = _heading_text(heading)  # e.g., "Stolen From"
        for el in section_siblings(heading):
            if not isinstance(el, Tag):
                continue
            if el.name == "table":
                rows = el.find_all("tr")
                if not rows:
                    continue
                grid = expand_rowspan(rows)
                header_idx, col = _header_columns(grid)
                mon_idx = next((col[k] for k in ("name", "monster", "mob", "enemy")
                                if k in col), 0)
                zone_idx = next((col[k] for k in ("zone", "area", "location")
                                 if k in col), None)
                level_idx = next((col[k] for k in ("level", "lvl", "lv.")
                                  if k in col), None)
                note_idx = next((col[k] for k in ("notes", "note")
                                 if k in col), None)
                for row in grid[header_idx + 1:]:
                    if not row or all(not c for c in row):
                        continue
                    raw = " | ".join(row)
                    monster = row[mon_idx] if mon_idx is not None and mon_idx < len(row) else None
                    zone = row[zone_idx] if zone_idx is not None and zone_idx < len(row) else None
                    notes_parts = []
                    if level_idx is not None and level_idx < len(row) and row[level_idx]:
                        notes_parts.append(f"Lv {row[level_idx]}")
                    if note_idx is not None and note_idx < len(row) and row[note_idx]:
                        notes_parts.append(row[note_idx])
                    notes_parts.append(source_kind)
                    notes = "; ".join(notes_parts) if notes_parts else None
                    key = (monster, zone, notes, raw)
                    if key in seen:
                        continue
                    seen.add(key)
                    drops.append(Drop(monster=monster, zone=zone,
                                      notes=notes, raw=raw))
            elif el.name in ("ul", "ol"):
                for li in el.find_all("li", recursive=False):
                    raw = clean(li.get_text(" ", strip=True))
                    if not raw:
                        continue
                    monster, zone, _ = _split_list_entry(raw)
                    key = (monster, zone, source_kind, raw)
                    if key in seen:
                        continue
                    seen.add(key)
                    drops.append(Drop(monster=monster, zone=zone,
                                      notes=source_kind, raw=raw))
    return drops


def _parse_price(text: str) -> Optional[int]:
    if not text:
        return None
    m = GIL_RE.search(text)
    if m:
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            return None
    # Allow a bare number ("150" or "1,234") when the cell is clearly numeric.
    stripped = text.strip().rstrip(".")
    if re.fullmatch(r"[\d,]+", stripped):
        try:
            return int(stripped.replace(",", ""))
        except ValueError:
            return None
    return None


def _split_list_entry(text: str):
    """Best-effort split of `NPC (Zone) - 1,234 gil` style list items."""
    price = _parse_price(text)
    # NPC (Zone)
    m = re.match(r"^(?P<npc>[^()]+?)\s*\((?P<zone>[^)]+)\)", text)
    if m:
        return clean(m.group("npc")), clean(m.group("zone")), price
    # NPC - Zone
    m = re.match(r"^(?P<npc>[^-,]+?)\s*[-,]\s*(?P<zone>[^-,]+)", text)
    if m:
        return clean(m.group("npc")), clean(m.group("zone")), price
    return clean(text), None, price


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def lookup(name: str) -> Optional[ItemInfo]:
    session = requests.Session()
    html = None
    used_title = None
    for title in candidate_titles(name):
        html = fetch_item_html(title, session)
        if html:
            used_title = title
            break
    if html is None:
        return None

    soup = BeautifulSoup(html, "html.parser")
    info = ItemInfo(
        name=name,
        url=WIKI_BASE + quote(used_title, safe="_()"),
        flags=extract_flags(soup),
        item_type=extract_item_type(soup),
        stack_size=extract_stack_size(soup),
        npc_sell_price=extract_sell_price(soup),
        vendors=parse_vendors(soup),
        drops=parse_drops(soup),
    )

    # Stash raw section text for sections we recognized but didn't structure.
    seen_headings = set()
    for h in (sections_by_keywords(soup, VENDOR_HEADINGS) +
              sections_by_keywords(soup, DROP_HEADINGS)):
        ht = _heading_text(h)
        if ht in seen_headings:
            continue
        seen_headings.add(ht)
        info.raw_text_by_section[ht] = section_text(h)

    return info


def render_text(info: ItemInfo, include_raw: bool = False) -> str:
    out = []
    out.append(f"\n=== {info.name} ===")
    out.append(f"URL: {info.url}")
    if info.flags:
        out.append(f"Flags: {', '.join(info.flags)}")
    if info.item_type:
        out.append(f"Type: {info.item_type}")
    if info.stack_size:
        out.append(f"Stack size: {info.stack_size}")
    if info.npc_sell_price is not None:
        out.append(f"NPC sell price: {info.npc_sell_price:,} gil")
    else:
        out.append("NPC sell price: (not listed / cannot be sold to NPC)")

    out.append("")
    if info.vendors:
        out.append(f"Vendors ({len(info.vendors)}):")
        for v in info.vendors:
            line = "  - "
            if v.npc:
                line += v.npc
            if v.zone:
                line += f" ({v.zone})"
            if v.price is not None:
                line += f" -- {v.price:,} gil"
            if v.notes:
                line += f"  [{v.notes}]"
            if not (v.npc or v.zone or v.price):
                line += v.raw
            out.append(line)
    else:
        out.append("Vendors: none listed")

    out.append("")
    if info.drops:
        out.append(f"Drop sources ({len(info.drops)}):")
        for d in info.drops:
            line = "  - "
            if d.monster:
                line += d.monster
            if d.zone:
                line += f" ({d.zone})"
            if d.notes:
                line += f"  [{d.notes}]"
            if not (d.monster or d.zone):
                line += d.raw
            out.append(line)
    else:
        out.append("Drop sources: none listed")

    if include_raw and info.raw_text_by_section:
        out.append("")
        out.append("--- Raw section text ---")
        for title, text in info.raw_text_by_section.items():
            out.append(f"\n[{title}]")
            out.append(text or "(empty)")
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Look up an FFXI item on FFXIclopedia.",
    )
    parser.add_argument("item", help="Item name, e.g. 'Bone Chip'")
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON instead of human-readable output")
    parser.add_argument("--raw", action="store_true",
                        help="Also print the raw text of recognized sections")
    args = parser.parse_args(argv)

    try:
        info = lookup(args.item)
    except requests.RequestException as e:
        print(f"Network error: {e}", file=sys.stderr)
        return 2

    if info is None:
        print(f"Item not found on FFXIclopedia: {args.item!r}", file=sys.stderr)
        return 1

    if args.json:
        # dataclass -> dict (vendor/drop become plain dicts already via asdict)
        print(json.dumps(asdict(info), indent=2, ensure_ascii=False))
    else:
        print(render_text(info, include_raw=args.raw))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
