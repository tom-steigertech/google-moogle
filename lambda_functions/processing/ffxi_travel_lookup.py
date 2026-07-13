"""FFXI travel/warp lookup — Home Points, Survival Guides, and travel NPCs.

Unlike the other lookup modules, this one serves from three curated CSVs bundled
with the package (``data/*.csv``) rather than scraping a live site. The web
search gives inconsistent answers about where these fixed-location resources are,
so this tool provides authoritative, coordinate-level data for:

- **Home Points**: teleport-restore crystals in most zones (``ffxi_home_points.csv``).
- **Survival Guides**: the free warp/teleport books placed around the world
  (``ffxi_survival_guides.csv``).
- **Travel NPCs**: chocobo renters, airship attendants, Outpost Warp overseers,
  Runic Portals, ferries, Manaclippers, Cavernous Maws, Nomad Moogles, warp
  taxis, waypoints, etc. — anything that moves a player between areas
  (``ffxi_teleport_npcs.csv``).

The data loads once at import time. Query by ``zone`` (what travel resources are
IN a zone) and/or by ``destination`` (which travel NPC gets you TO a place).

Exposes:
    lookup(zone=None, resource_type="all", destination=None) -> dict
    lookup_as_tool_result(zone=None, resource_type="all", destination=None) -> dict
"""

from __future__ import annotations

import csv
import difflib
import logging
import os
import re
from typing import List, Optional

from .ffxi_zones import resolve_zone

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# Cap how many travel-NPC rows we return for a destination search so the tool
# result stays compact for the model.
MAX_DESTINATION_MATCHES = 25

# Valid resource_type filter values.
RESOURCE_TYPES = ("all", "home_point", "survival_guide", "travel_npc")


def _norm(text: str) -> str:
    """Normalize a zone/name for fuzzy comparison.

    Lowercases and collapses everything that isn't a letter or digit to single
    spaces, so "Southern San d'Oria [S]" -> "southern san d oria s" and
    apostrophes/brackets/hyphens stop mattering for matching.
    """
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _load_csv(filename: str) -> List[dict]:
    """Load a bundled data CSV into a list of row dicts (empty on any failure).

    The last column of each file is free-text description that occasionally
    contains an unquoted comma (e.g. "on Dvucca Isle, near zone to..."). A plain
    DictReader would spill that overflow into a stray field, so we read rows
    positionally and merge any trailing extra columns back into the final one.
    """
    path = os.path.join(_DATA_DIR, filename)
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            header = [h.strip() for h in next(reader)]
            last = len(header) - 1
            rows = []
            for values in reader:
                if not values:
                    continue
                if len(values) > len(header):
                    # Rejoin the unquoted-comma overflow into the last column.
                    values = values[:last] + [",".join(values[last:])]
                cells = [v.strip() for v in values]
                cells += [""] * (len(header) - len(cells))  # pad short rows
                rows.append(dict(zip(header, cells)))
        logger.info(f"Loaded {len(rows)} rows from {filename}")
        return rows
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(f"Failed to load travel data {filename}: {exc}", exc_info=True)
        return []


# --- Load the three datasets once at import ---------------------------------
_HOME_POINTS = _load_csv("ffxi_home_points.csv")
_SURVIVAL_GUIDES = _load_csv("ffxi_survival_guides.csv")
_TRAVEL_NPCS = _load_csv("ffxi_teleport_npcs.csv")

# Every distinct zone name across the three datasets — the pool we fuzzy-match a
# queried zone against.
_ALL_ZONES = sorted({
    row["Zone"]
    for rows in (_HOME_POINTS, _SURVIVAL_GUIDES, _TRAVEL_NPCS)
    for row in rows
    if row.get("Zone")
})
_NORM_TO_ZONE: dict = {}
for _z in _ALL_ZONES:
    _NORM_TO_ZONE.setdefault(_norm(_z), _z)


def _match_zones(query: str) -> List[str]:
    """Resolve a free-text zone query to the actual zone name(s) in the data.

    Ranking: an exact normalized match wins outright (returns just that zone);
    otherwise substring matches (either direction) are returned; otherwise a
    fuzzy close-match fallback. Returns [] when nothing is close.
    """
    q = _norm(query)
    if not q:
        return []

    exact = [z for z in _ALL_ZONES if _norm(z) == q]
    if exact:
        return exact

    subs = [z for z in _ALL_ZONES if q in _norm(z) or _norm(z) in q]
    if subs:
        # Sort shortest-name first so the closest ("Bastok Mines") leads over
        # longer superset names.
        return sorted(subs, key=lambda z: len(z))

    close = difflib.get_close_matches(q, list(_NORM_TO_ZONE), n=5, cutoff=0.6)
    return [_NORM_TO_ZONE[c] for c in close]


def _home_points_for(zones: List[str]) -> List[dict]:
    zoneset = set(zones)
    return [
        {
            "zone": r["Zone"],
            "home_point": r.get("HomePoint", ""),
            "coordinates": r.get("Coordinates", ""),
            "description": r.get("Description", ""),
        }
        for r in _HOME_POINTS
        if r.get("Zone") in zoneset
    ]


def _survival_guides_for(zones: List[str]) -> List[dict]:
    zoneset = set(zones)
    return [
        {
            "zone": r["Zone"],
            "coordinates": r.get("Coordinates", ""),
            "description": r.get("Description", ""),
        }
        for r in _SURVIVAL_GUIDES
        if r.get("Zone") in zoneset
    ]


def _travel_npc_row(r: dict) -> dict:
    return {
        "category": r.get("Category", ""),
        "npc_name": r.get("NPC_Name", ""),
        "zone": r.get("Zone", ""),
        "coordinates": r.get("Coordinates", ""),
        "description": r.get("Description", ""),
        "destinations": r.get("Destinations", ""),
    }


def _travel_npcs_in(zones: List[str]) -> List[dict]:
    zoneset = set(zones)
    return [_travel_npc_row(r) for r in _TRAVEL_NPCS if r.get("Zone") in zoneset]


def _travel_npcs_to(destination: str) -> List[dict]:
    """Travel NPCs whose destination/zone/description mentions ``destination``."""
    q = _norm(destination)
    if not q:
        return []
    matches = []
    for r in _TRAVEL_NPCS:
        hay = _norm(" ".join((
            r.get("Destinations", ""),
            r.get("Zone", ""),
            r.get("Description", ""),
            r.get("Category", ""),
        )))
        if q in hay:
            matches.append(_travel_npc_row(r))
    return matches[:MAX_DESTINATION_MATCHES]


def lookup(zone: Optional[str] = None, resource_type: str = "all",
           destination: Optional[str] = None) -> dict:
    """Look up FFXI travel resources.

    Args:
        zone: A zone name; returns the home points, survival guides, and travel
            NPCs located IN that zone. Fuzzy-matched against the known zones.
        resource_type: Filter — one of "all" (default), "home_point",
            "survival_guide", or "travel_npc". Applies to the ``zone`` results.
        destination: A place you want to travel TO; returns the travel NPCs
            (chocobos, airships, portals, ferries, maws, etc.) that go there.

    Returns a dict with the matched data and always includes "found".
    """
    resource_type = (resource_type or "all").strip().lower()
    if resource_type not in RESOURCE_TYPES:
        resource_type = "all"

    result: dict = {
        "found": False,
        "query": {
            "zone": zone or "",
            "resource_type": resource_type,
            "destination": destination or "",
        },
    }

    if not (zone and zone.strip()) and not (destination and destination.strip()):
        result["error"] = "Provide a zone and/or a destination to look up."
        return result

    # --- Zone-based lookup: what travel resources are IN this zone ---
    if zone and zone.strip():
        # Canonicalize a misspelled/loosely-typed zone name first, so e.g.
        # "Batalia Downs" resolves before we try to match the travel data.
        zres = resolve_zone(zone)
        search_name = zone
        if zres["match"] and zres["high_confidence"]:
            search_name = zres["match"]
            if zres["corrected"]:
                result["zone_corrected_to"] = zres["match"]

        matched = _match_zones(search_name)
        result["matched_zones"] = matched
        if not matched:
            # No travel data. Distinguish "not a real zone" (offer name
            # suggestions) from "real zone, just nothing in this dataset".
            result["zone_suggestions"] = zres["suggestions"]
            if zres["exact"] or (zres["match"] and zres["high_confidence"]):
                result["note"] = (
                    f"'{search_name}' is a valid FFXI zone but has no Home Point, "
                    "Survival Guide, or travel-NPC entry in this dataset."
                )
        else:
            if resource_type in ("all", "home_point"):
                result["home_points"] = _home_points_for(matched)
            if resource_type in ("all", "survival_guide"):
                result["survival_guides"] = _survival_guides_for(matched)
            if resource_type in ("all", "travel_npc"):
                result["travel_npcs"] = _travel_npcs_in(matched)

    # --- Destination-based lookup: which NPC gets me TO this place ---
    if destination and destination.strip():
        npcs = _travel_npcs_to(destination)
        result["travel_options"] = npcs
        if len(npcs) == MAX_DESTINATION_MATCHES:
            result["travel_options_truncated"] = True

    # "found" is true if any populated list has entries.
    result["found"] = any(
        result.get(key)
        for key in ("home_points", "survival_guides", "travel_npcs", "travel_options")
    )
    return result


def lookup_as_tool_result(zone: Optional[str] = None, resource_type: str = "all",
                          destination: Optional[str] = None) -> dict:
    """Tool-facing wrapper. Same as :func:`lookup`; never raises.

    On unexpected failure returns a found=False dict with an error string so the
    tool loop degrades gracefully instead of erroring the whole request.
    """
    try:
        return lookup(zone=zone, resource_type=resource_type, destination=destination)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(f"travel lookup failed: {exc}", exc_info=True)
        return {
            "found": False,
            "error": f"Travel lookup failed: {exc}",
            "query": {"zone": zone or "", "destination": destination or ""},
        }
