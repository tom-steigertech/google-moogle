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

# "How to obtain" / crafting sections. These are prose/recipe sections (not the
# structured vendor/drop tables) that explain how an item is acquired. They
# matter for items that are craft-only or auction-house-only (e.g. Cornstarch),
# where vendors and drops are both empty and the card would otherwise be bare.
OBTAIN_HEADINGS = ("how to obtain",)
SYNTHESIS_HEADINGS = ("synthesis recipes", "synthesis recipe")
USED_IN_HEADINGS = ("used in recipes", "used in recipe")

CRAFT_SKILLS = (
    "Alchemy", "Bonecraft", "Clothcraft", "Cooking", "Goldsmithing",
    "Leathercraft", "Smithing", "Woodworking", "Fishing",
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
    # "How to obtain" / crafting info — populated for craft-only or AH-only items
    # that have no vendors and no drops.
    how_to_obtain: Optional[str] = None
    synthesis: Optional[str] = None
    synthesis_crafts: List[dict] = field(default_factory=list)
    synthesis_crystal: Optional[str] = None
    synthesis_ingredients: List[dict] = field(default_factory=list)
    used_in: Optional[str] = None
    # The original lookup string, and the wiki title it resolved to when an
    # exact-title match failed and we fell back to search (e.g. "Grape" ->
    # "Royal Grape"). matched_title is None for exact hits.
    query: Optional[str] = None
    matched_title: Optional[str] = None
    auction_house: bool = False
    ah_category: Optional[str] = None
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


_HEADING_TAG_RE = re.compile(r"^h[1-6]$")


def _shallow_section_text(heading: Tag, max_chars: int = 600) -> str:
    """Cleaned text of the content directly under a heading.

    Unlike ``section_text``, this stops at the NEXT heading of ANY level, so for
    an ``<h2>How to Obtain</h2>`` whose ``<h3>`` children are vendor/drop tables
    we capture only the intro prose, not the whole structured tree. Only true
    siblings of the heading are collected (same parent) to avoid double-counting
    nested elements.
    """
    parent = heading.parent
    chunks: List[str] = []
    for el in heading.find_all_next():
        if not isinstance(el, Tag):
            continue
        if el.name and _HEADING_TAG_RE.match(el.name):
            break
        if el.parent is parent and el.name in ("p", "ul", "ol", "dl", "table"):
            t = clean(el.get_text(" ", strip=True))
            if t:
                chunks.append(t)
    text = clean("\n".join(chunks))
    return text[:max_chars]


# FFXIclopedia renders empty sections with the literal placeholder "None".
_EMPTY_SECTION_VALUES = {"none", "n/a", "na", "-"}


def _normalize_section(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    if text.strip().lower() in _EMPTY_SECTION_VALUES:
        return None
    return text


def _first_section_text(soup: BeautifulSoup, keywords: Iterable[str],
                        max_chars: int = 600) -> Optional[str]:
    headings = sections_by_keywords(soup, keywords)
    if not headings:
        return None
    text = _shallow_section_text(headings[0], max_chars=max_chars)
    return _normalize_section(text)


_CRYSTAL_RE = re.compile(
    r"(Fire|Ice|Wind|Earth|Lightning|Water|Light|Dark)\s+Crystal", re.IGNORECASE
)
# Ingredients render as "2 x Millioncorn" (qty first). Yields render as
# "Cornstarch x 1" (qty last), so this pattern won't pick them up.
_INGREDIENT_RE = re.compile(r"(\d+)\s*x\s*([A-Z][A-Za-z'’-]*(?:\s+[A-Z][A-Za-z'’-]*)*)")


def parse_synthesis_details(text: str):
    """Pull (crystal, ingredients) out of synthesis recipe text.

    Returns (crystal_str_or_None, [{"qty": int, "name": str}, ...]).
    """
    if not text:
        return None, []
    cm = _CRYSTAL_RE.search(text)
    crystal = clean(cm.group(0)).title() if cm else None
    ingredients = []
    seen = set()
    for m in _INGREDIENT_RE.finditer(text):
        name = clean(m.group(2))
        # Skip the item's own yield lines that slipped through, and crystals.
        if name.lower().endswith("crystal"):
            continue
        if name in seen:
            continue
        seen.add(name)
        ingredients.append({"qty": int(m.group(1)), "name": name})
    return crystal, ingredients


def parse_crafts(text: str) -> List[dict]:
    """Pull ``{skill, level}`` pairs out of synthesis recipe text.

    Matches patterns like ``Alchemy ( 9 /20 )`` → {"skill": "Alchemy", "level": 9}.
    """
    if not text:
        return []
    skills = "|".join(CRAFT_SKILLS)
    out: List[dict] = []
    seen = set()
    for m in re.finditer(rf"({skills})\s*\(\s*(\d+)", text):
        skill, level = m.group(1), int(m.group(2))
        if (skill, level) not in seen:
            seen.add((skill, level))
            out.append({"skill": skill, "level": level})
    return out


# Boilerplate that appears verbatim on nearly every item page — pure noise in a card.
_OBTAIN_BOILERPLATE_RE = re.compile(
    r"Can be obtained as a random reward from the Gobbie Mystery Box[^.]*\.?",
    re.IGNORECASE,
)


def extract_obtain_info(soup: BeautifulSoup) -> dict:
    """Extract craft / auction-house / how-to-obtain info for an item page."""
    how_to_obtain = _first_section_text(soup, OBTAIN_HEADINGS, max_chars=600)
    synthesis = _first_section_text(soup, SYNTHESIS_HEADINGS, max_chars=500)
    used_in = _first_section_text(soup, USED_IN_HEADINGS, max_chars=400)

    auction_house = False
    ah_category = None
    if how_to_obtain:
        if re.search(r"auction house", how_to_obtain, re.IGNORECASE):
            auction_house = True
        # Category is a short Title-Case path like "Food > Ingredients"; stop
        # before the trailing boilerplate sentence that follows it.
        m = re.search(
            r"Auction House Category\s*:?\s*"
            r"([A-Z][A-Za-z]*(?:\s*[>/]\s*[A-Za-z]+)*)",
            how_to_obtain,
        )
        if m:
            ah_category = clean(m.group(1))
        # Drop the generic Gobbie Mystery Box line so the card shows real info.
        how_to_obtain = clean(_OBTAIN_BOILERPLATE_RE.sub("", how_to_obtain)) or None
        how_to_obtain = _normalize_section(how_to_obtain)

    crystal, ingredients = parse_synthesis_details(synthesis or "")

    return {
        "how_to_obtain": how_to_obtain,
        "synthesis": synthesis,
        "synthesis_crafts": parse_crafts(synthesis or ""),
        "synthesis_crystal": crystal,
        "synthesis_ingredients": ingredients,
        "used_in": used_in,
        "auction_house": auction_house,
        "ah_category": ah_category,
    }


def opensearch_titles(name: str, session: requests.Session = None,
                      limit: int = 5) -> List[str]:
    """Return FFXIclopedia page titles matching ``name`` via the opensearch API.

    Used as a fuzzy fallback when an exact title lookup misses — recipe
    ingredient names often don't match page titles exactly (e.g. "Grape" ->
    "Royal Grape", "Rice Flour" -> "Fine Rice Flour").
    """
    session = session or requests.Session()
    try:
        resp = session.get(
            WIKI_API,
            params={"action": "opensearch", "search": name, "limit": limit,
                    "namespace": 0, "format": "json"},
            timeout=15, headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        data = resp.json()
        return data[1] if isinstance(data, list) and len(data) > 1 else []
    except Exception:
        return []


def _best_match(name: str, titles: List[str]) -> Optional[str]:
    """Pick the most likely intended title from opensearch results."""
    if not titles:
        return None
    lowered = name.strip().lower()
    for t in titles:
        if t.lower() == lowered:
            return t
    return titles[0]


def lookup(name: str) -> Optional[ItemInfo]:
    session = requests.Session()
    html = None
    used_title = None
    matched_title = None
    for title in candidate_titles(name):
        html = fetch_item_html(title, session)
        if html:
            used_title = title
            break
    if html is None:
        # Fuzzy fallback: search the wiki and fetch the best-matching result.
        for cand in [c for c in [_best_match(name, opensearch_titles(name, session))]
                     if c]:
            t = cand.replace(" ", "_")
            html = fetch_item_html(t, session)
            if html:
                used_title = t
                matched_title = cand
                break
    if html is None:
        return None

    soup = BeautifulSoup(html, "html.parser")
    info = ItemInfo(
        name=matched_title or name,
        url=WIKI_BASE + quote(used_title, safe="_()"),
        flags=extract_flags(soup),
        item_type=extract_item_type(soup),
        stack_size=extract_stack_size(soup),
        npc_sell_price=extract_sell_price(soup),
        vendors=parse_vendors(soup),
        drops=parse_drops(soup),
    )
    obtain = extract_obtain_info(soup)
    info.how_to_obtain = obtain["how_to_obtain"]
    info.synthesis = obtain["synthesis"]
    info.synthesis_crafts = obtain["synthesis_crafts"]
    info.synthesis_crystal = obtain["synthesis_crystal"]
    info.synthesis_ingredients = obtain["synthesis_ingredients"]
    info.used_in = obtain["used_in"]
    info.auction_house = obtain["auction_house"]
    info.ah_category = obtain["ah_category"]
    info.query = name
    info.matched_title = matched_title
    return info


def lookup_as_tool_result(name: str, max_vendors: int = 10,
                          max_drops: int = 15) -> dict:
    """Run lookup() and return a trimmed dict suitable for a Claude tool result.

    Drops raw HTML text and caps long vendor/drop lists to keep the payload
    small.
    """
    info = lookup(name)
    if info is None:
        # Offer close matches so the model can suggest alternatives or retry.
        suggestions = opensearch_titles(name)
        return {"found": False, "name": name, "suggestions": suggestions}

    result = {
        "found": True,
        "name": info.name,
        "query": info.query,
        "matched_title": info.matched_title,
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
        "how_to_obtain": info.how_to_obtain,
        "synthesis": info.synthesis,
        "synthesis_crafts": info.synthesis_crafts,
        "synthesis_crystal": info.synthesis_crystal,
        "synthesis_ingredients": info.synthesis_ingredients,
        "used_in": info.used_in,
        "auction_house": info.auction_house,
        "ah_category": info.ah_category,
    }
    return result


__all__ = [
    "ItemInfo",
    "Vendor",
    "Drop",
    "lookup",
    "lookup_as_tool_result",
]
