"""FFXIclopedia item lookup - importable module for the Moogle bot.

Exposes:
    lookup(name) -> Optional[ItemInfo]
    lookup_as_tool_result(name, max_vendors=10, max_drops=15) -> dict

The module mirrors the standalone CLI tool but trims the result for use as a
Claude tool result (large `raw_text_by_section` payloads are dropped).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Iterable, List, Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup, Tag


WIKI_API = "https://ffxiclopedia.fandom.com/api.php"
WIKI_BASE = "https://ffxiclopedia.fandom.com/wiki/"
USER_AGENT = "ffxi-moogle-bot/1.0 (+https://ffxiclopedia.fandom.com)"

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


def fetch_item_html(title: str, session: requests.Session) -> Optional[str]:
    params = {
        "action": "parse",
        "page": title,
        "format": "json",
        "prop": "text",
        "redirects": 1,
    }
    resp = session.get(WIKI_API, params=params, timeout=20,
                       headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        return None
    return data.get("parse", {}).get("text", {}).get("*")


def candidate_titles(name: str) -> Iterable[str]:
    name = name.strip()
    seen: set = set()

    def _emit(raw: str):
        t = raw.replace(" ", "_")
        if t not in seen:
            seen.add(t)
            return t
        return None

    for raw in [name, name.title()]:
        t = _emit(raw)
        if t:
            yield t
    if name and not name[0].isupper():
        t = _emit(name[0].upper() + name[1:])
        if t:
            yield t

    # Singular fallback: if the LLM passed a plural form (e.g. "Beehive Chips"),
    # also try without the trailing 's'.
    if name.lower().endswith("s") and len(name) > 2:
        singular = name[:-1]
        for raw in [singular, singular.title()]:
            t = _emit(raw)
            if t:
                yield t


GIL_RE = re.compile(r"([\d,]+)\s*gil", re.IGNORECASE)
STACK_RE = re.compile(r"Stackable?[:\s]*\(?([\d]+)\)?", re.IGNORECASE)
FLAG_WORDS = ("Rare", "Ex", "Exclusive", "Aux", "Augmented")


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_flags(soup: BeautifulSoup) -> List[str]:
    flags: List[str] = []
    seen = set()
    for img in soup.find_all("img"):
        alt = (img.get("alt") or "").strip()
        if alt in FLAG_WORDS and alt not in seen:
            flags.append("Ex" if alt == "Exclusive" else alt)
            seen.add(alt)
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
    span = tag.find("span", class_="mw-headline")
    text = clean((span or tag).get_text(" ", strip=True))
    text = re.sub(r"\s*\[\s*\]\s*$", "", text)
    return text


def sections_by_keywords(soup: BeautifulSoup, keywords: Iterable[str]) -> List[Tag]:
    targets = {k.lower() for k in keywords}
    out = []
    for h in soup.find_all(["h2", "h3", "h4"]):
        title = _heading_text(h).lower()
        if title in targets:
            out.append(h)
    return out


def expand_rowspan(rows: List[Tag]) -> List[List[str]]:
    grid: List[List[str]] = []
    pending: dict = {}
    for tr in rows:
        cells = tr.find_all(["th", "td"], recursive=False) or tr.find_all(["th", "td"])
        out_row: List[str] = []
        col = 0
        cell_list = list(cells)
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
    for i, row in enumerate(grid[:3]):
        lowered = [c.lower() for c in row]
        if "name" in lowered or "monster" in lowered or "npc" in lowered or "vendor" in lowered:
            return i, {c: idx for idx, c in enumerate(lowered)}
    if grid:
        lowered = [c.lower() for c in grid[0]]
        return 0, {c: idx for idx, c in enumerate(lowered)}
    return 0, {}


def _table_preamble_price(grid: List[List[str]], header_idx: int) -> Optional[int]:
    for row in grid[:header_idx]:
        for cell in row:
            price = _parse_price(cell)
            if price is not None:
                return price
    return None


def _parse_price(text: str) -> Optional[int]:
    if not text:
        return None
    m = GIL_RE.search(text)
    if m:
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            return None
    stripped = text.strip().rstrip(".")
    if re.fullmatch(r"[\d,]+", stripped):
        try:
            return int(stripped.replace(",", ""))
        except ValueError:
            return None
    return None


def _split_list_entry(text: str):
    price = _parse_price(text)
    m = re.match(r"^(?P<npc>[^()]+?)\s*\((?P<zone>[^)]+)\)", text)
    if m:
        return clean(m.group("npc")), clean(m.group("zone")), price
    m = re.match(r"^(?P<npc>[^-,]+?)\s*[-,]\s*(?P<zone>[^-,]+)", text)
    if m:
        return clean(m.group("npc")), clean(m.group("zone")), price
    return clean(text), None, price


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
        source_kind = _heading_text(heading)
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
    return info


def lookup_as_tool_result(name: str, max_vendors: int = 10,
                          max_drops: int = 15) -> dict:
    """Run lookup() and return a trimmed dict suitable for a Claude tool result.

    Drops raw HTML text and caps long vendor/drop lists to keep the payload
    small.
    """
    info = lookup(name)
    if info is None:
        return {"found": False, "name": name}

    result = {
        "found": True,
        "name": info.name,
        "url": info.url,
        "flags": info.flags,
        "item_type": info.item_type,
        "stack_size": info.stack_size,
        "npc_sell_price": info.npc_sell_price,
        "vendors": [
            {"npc": v.npc, "zone": v.zone, "price": v.price, "notes": v.notes}
            for v in info.vendors[:max_vendors]
        ],
        "vendors_truncated": len(info.vendors) > max_vendors,
        "vendors_total": len(info.vendors),
        "drops": [
            {"monster": d.monster, "zone": d.zone, "notes": d.notes}
            for d in info.drops[:max_drops]
        ],
        "drops_truncated": len(info.drops) > max_drops,
        "drops_total": len(info.drops),
    }
    return result


__all__ = [
    "ItemInfo",
    "Vendor",
    "Drop",
    "lookup",
    "lookup_as_tool_result",
]
