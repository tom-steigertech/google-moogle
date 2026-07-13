"""Canonical FFXI zone names + misspelling-tolerant resolver.

The Moogle frequently receives zone names that are misspelled, differently
punctuated (``[S]`` vs ``(S)``), or loosely typed ("batalia downs", "sandoria").
This module holds the authoritative list of every FFXI zone (compiled from
BG-Wiki's area categories plus its zone master list, ``data/ffxi_zones.txt``) and
exposes :func:`resolve_zone`, which maps a fuzzy input to the correct canonical
spelling so the rest of the bot — the zone-map fetch, the travel lookup, and
free-form answers — can work with a name that actually exists in-game.

Exposes:
    ZONES: tuple[str, ...]                 # canonical zone names
    is_zone(name) -> bool                  # exact (punctuation-insensitive) membership
    resolve_zone(name, limit=5) -> dict    # fuzzy correction + suggestions
"""

from __future__ import annotations

import difflib
import logging
import os
import re
from typing import List, Optional

logger = logging.getLogger(__name__)

_DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "ffxi_zones.txt")

# Similarity (0..1) at/above which a fuzzy match is confident enough to silently
# substitute for the user's input (e.g. "Batalia Downs" -> "Batallia Downs").
AUTOCORRECT_CUTOFF = 0.86
# Lower bar for offering a name as a "did you mean?" suggestion.
SUGGEST_CUTOFF = 0.6


def _norm(text: str) -> str:
    """Lowercase and collapse non-alphanumerics to single spaces.

    Makes matching insensitive to apostrophes, hyphens, and the ``[S]``/``(S)``
    notation split: "Southern San d'Oria [S]" and "...  (S)" both become
    "southern san d oria s".
    """
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _load_zones() -> List[str]:
    try:
        with open(_DATA_PATH, encoding="utf-8") as fh:
            zones = [line.strip() for line in fh if line.strip()]
        logger.info(f"Loaded {len(zones)} canonical zone names")
        return zones
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(f"Failed to load zone list: {exc}", exc_info=True)
        return []


ZONES = tuple(_load_zones())

# Normalized form -> canonical spelling. First spelling wins on collisions
# (e.g. an [S]/(S) pair that normalizes identically).
_NORM_TO_ZONE: dict = {}
for _z in ZONES:
    _NORM_TO_ZONE.setdefault(_norm(_z), _z)
# Space-stripped normalized form -> canonical, a secondary index that catches
# run-together typos like "sandoria" -> "San d'Oria".
_TIGHT_TO_ZONE: dict = {}
for _z in ZONES:
    _TIGHT_TO_ZONE.setdefault(_norm(_z).replace(" ", ""), _z)


def is_zone(name: str) -> bool:
    """True if ``name`` is a real FFXI zone (ignoring case/punctuation)."""
    return _norm(name) in _NORM_TO_ZONE


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def resolve_zone(name: str, limit: int = 5) -> dict:
    """Resolve a possibly-misspelled zone name to its canonical FFXI spelling.

    Returns a dict:
        input:          the original string
        match:          best canonical zone, or None if nothing is close enough
        exact:          True if the input already names a real zone
        corrected:      True if `match` differs from what the user typed
        high_confidence:True if `match` is a confident autocorrect (safe to
                        substitute silently); when False, prefer confirming with
                        the user via `suggestions`
        confidence:     similarity (0..1) of `match` to the input
        suggestions:    up to `limit` close canonical names (best first)
    """
    result = {
        "input": name or "",
        "match": None,
        "exact": False,
        "corrected": False,
        "high_confidence": False,
        "confidence": 0.0,
        "suggestions": [],
    }
    q = _norm(name)
    if not q or not ZONES:
        return result

    # 1) Exact (punctuation-insensitive) hit.
    if q in _NORM_TO_ZONE:
        canonical = _NORM_TO_ZONE[q]
        result.update(
            match=canonical, exact=True, high_confidence=True, confidence=1.0,
            corrected=(canonical != (name or "").strip()),
        )
        return result

    # 2) Run-together typo (spaces/punct dropped): "sandoria", "batalliadowns".
    tight = q.replace(" ", "")
    if tight in _TIGHT_TO_ZONE:
        canonical = _TIGHT_TO_ZONE[tight]
        result.update(
            match=canonical, corrected=True, high_confidence=True,
            confidence=max(0.9, _ratio(q, _norm(canonical))),
            suggestions=[canonical],
        )
        return result

    # 3) Fuzzy: rank all zones by normalized similarity.
    scored = sorted(
        ((_ratio(q, nz), z) for nz, z in _NORM_TO_ZONE.items()),
        key=lambda t: t[0], reverse=True,
    )
    suggestions = [z for score, z in scored if score >= SUGGEST_CUTOFF][:limit]
    result["suggestions"] = suggestions

    best_score, best_zone = scored[0]
    if best_score >= SUGGEST_CUTOFF:
        result.update(
            match=best_zone, corrected=True, confidence=round(best_score, 3),
            high_confidence=(best_score >= AUTOCORRECT_CUTOFF),
        )
    return result
