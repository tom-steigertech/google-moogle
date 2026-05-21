"""FFXI zone map fetcher — retrieves map images and page text from BG-Wiki."""

import logging
import re

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BG_WIKI_API = "https://www.bg-wiki.com/api.php"
MAX_MAPS = 4        # max map images to return
MAX_TEXT_CHARS = 2500  # cap on zone page text passed to the model


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "MoogleBot/1.0 (FFXI Slack Bot)"
    return s


def _map_image_titles(zone_name: str, sess: requests.Session) -> list[str]:
    """Return filenames of map images listed on the zone's BG-Wiki page."""
    resp = sess.get(
        BG_WIKI_API,
        params={
            "action": "query",
            "titles": zone_name,
            "prop": "images",
            "imlimit": 50,
            "format": "json",
            "utf8": 1,
        },
        timeout=10,
    )
    resp.raise_for_status()
    pages = resp.json().get("query", {}).get("pages", {})
    titles = []
    for page in pages.values():
        for img in page.get("images", []):
            title = img.get("title", "")
            if "map" in title.lower():
                titles.append(title)
    return titles


def _image_url(file_title: str, sess: requests.Session) -> str | None:
    """Resolve a wiki File: title to a direct download URL."""
    resp = sess.get(
        BG_WIKI_API,
        params={
            "action": "query",
            "titles": file_title,
            "prop": "imageinfo",
            "iiprop": "url",
            "format": "json",
            "utf8": 1,
        },
        timeout=10,
    )
    resp.raise_for_status()
    pages = resp.json().get("query", {}).get("pages", {})
    for page in pages.values():
        info = page.get("imageinfo", [])
        if info:
            return info[0].get("url")
    return None


def _fmt(url: str) -> str:
    lower = url.lower()
    if lower.endswith(".png"):
        return "png"
    if lower.endswith(".gif"):
        return "gif"
    if lower.endswith(".webp"):
        return "webp"
    return "jpeg"


def _fetch_zone_text(zone_name: str, sess: requests.Session) -> str:
    """Fetch and return plain text from the BG-Wiki zone page."""
    try:
        resp = sess.get(
            BG_WIKI_API,
            params={
                "action": "parse",
                "page": zone_name,
                "prop": "text",
                "format": "json",
                "utf8": 1,
            },
            timeout=10,
        )
        resp.raise_for_status()
        html = resp.json().get("parse", {}).get("text", {}).get("*", "")
        if not html:
            return ""
        soup = BeautifulSoup(html, "html.parser")
        content = soup.find("div", class_="mw-parser-output") or soup
        for tag in content.find_all(["script", "style", "sup"]):
            tag.decompose()
        for cls in ["catlinks", "printfooter", "mw-references-wrap", "mw-editsection"]:
            for tag in content.find_all(class_=cls):
                tag.decompose()
        text = re.sub(r"\n{3,}", "\n\n", content.get_text(separator="\n", strip=True)).strip()
        return text[:MAX_TEXT_CHARS] + ("..." if len(text) > MAX_TEXT_CHARS else "")
    except Exception as exc:
        logger.warning(f"Could not fetch zone text for '{zone_name}': {exc}")
        return ""


def _map_number(filename: str) -> int:
    """Extract trailing map number from a filename stem, e.g. 'Map_zone_2' → 2.
    Returns 0 if no number is found (sorts unnumbered maps first)."""
    m = re.search(r'[_-](\d+)$', filename)
    return int(m.group(1)) if m else 0


def fetch_zone_maps(zone_name: str) -> dict:
    """Fetch zone map image(s) from BG-Wiki for the given zone name.

    Returns:
        {
            "found": bool,
            "zone_name": str,
            "maps": [{"bytes": bytes, "format": str, "label": str, "map_number": int}],
            "error": str,   # only when found is False
        }
    """
    sess = _session()
    try:
        # Fetch page text and map images in parallel would be ideal but requests
        # is synchronous; text first (fast) then images.
        zone_text = _fetch_zone_text(zone_name, sess)

        titles = _map_image_titles(zone_name, sess)
        if not titles:
            return {
                "found": False,
                "zone_name": zone_name,
                "maps": [],
                "zone_text": zone_text,
                "error": f"No map images found for '{zone_name}' on BG-Wiki.",
            }

        # Sort by map number so Map 1 always comes before Map 2
        titles_sorted = sorted(
            titles[:MAX_MAPS],
            key=lambda t: _map_number(t.replace("File:", "").rsplit(".", 1)[0])
        )

        maps = []
        for title in titles_sorted:
            url = _image_url(title, sess)
            if not url:
                continue
            img_resp = sess.get(url, timeout=15)
            img_resp.raise_for_status()
            stem = title.replace("File:", "").rsplit(".", 1)[0]
            num = _map_number(stem)
            maps.append({
                "bytes": img_resp.content,
                "format": _fmt(url),
                "label": stem,
                "map_number": num,
            })

        if not maps:
            return {
                "found": False,
                "zone_name": zone_name,
                "maps": [],
                "zone_text": zone_text,
                "error": f"Could not download map images for '{zone_name}'.",
            }

        return {"found": True, "zone_name": zone_name, "maps": maps, "zone_text": zone_text}

    except Exception as exc:
        logger.error(f"fetch_zone_maps failed for '{zone_name}': {exc}", exc_info=True)
        return {
            "found": False,
            "zone_name": zone_name,
            "maps": [],
            "zone_text": "",
            "error": f"Failed to fetch maps: {exc}",
        }
