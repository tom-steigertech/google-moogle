"""FFXI zone map fetcher — retrieves map images from BG-Wiki via MediaWiki API."""

import logging

import requests

logger = logging.getLogger(__name__)

BG_WIKI_API = "https://www.bg-wiki.com/api.php"
MAX_MAPS = 3  # max map images to return (covers multi-floor / multi-area zones)


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


def fetch_zone_maps(zone_name: str) -> dict:
    """Fetch zone map image(s) from BG-Wiki for the given zone name.

    Returns:
        {
            "found": bool,
            "zone_name": str,
            "maps": [{"bytes": bytes, "format": str, "label": str}],
            "error": str,   # only when found is False
        }
    """
    sess = _session()
    try:
        titles = _map_image_titles(zone_name, sess)
        if not titles:
            return {
                "found": False,
                "zone_name": zone_name,
                "maps": [],
                "error": f"No map images found for '{zone_name}' on BG-Wiki.",
            }

        maps = []
        for title in titles[:MAX_MAPS]:
            url = _image_url(title, sess)
            if not url:
                continue
            img_resp = sess.get(url, timeout=15)
            img_resp.raise_for_status()
            maps.append({
                "bytes": img_resp.content,
                "format": _fmt(url),
                "label": title.replace("File:", "").rsplit(".", 1)[0],
            })

        if not maps:
            return {
                "found": False,
                "zone_name": zone_name,
                "maps": [],
                "error": f"Could not download map images for '{zone_name}'.",
            }

        return {"found": True, "zone_name": zone_name, "maps": maps}

    except Exception as exc:
        logger.error(f"fetch_zone_maps failed for '{zone_name}': {exc}", exc_info=True)
        return {
            "found": False,
            "zone_name": zone_name,
            "maps": [],
            "error": f"Failed to fetch maps: {exc}",
        }
