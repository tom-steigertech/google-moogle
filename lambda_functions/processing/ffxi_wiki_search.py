"""BG-Wiki search tool for general FFXI information.

Uses the BG-Wiki MediaWiki API to search for pages, then scrapes rendered HTML
with BeautifulSoup to extract text. Falls back to progressively simpler queries
when BG-Wiki's full-text search returns no results. If BG-Wiki finds nothing,
falls back to FFXIclopedia (Fandom wiki).
"""

import logging
import re

import requests
from bs4 import BeautifulSoup

BG_WIKI_API = "https://www.bg-wiki.com/api.php"
BG_WIKI_BASE = "https://www.bg-wiki.com/ffxi"
FANDOM_API = "https://ffxiclopedia.fandom.com/api.php"
FANDOM_BASE = "https://ffxiclopedia.fandom.com/wiki"
MAX_CONTENT_CHARS = 3000

logger = logging.getLogger(__name__)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "MoogleBot/1.0 (FFXI Slack Bot)"
    return s


def _search_titles(query: str, sess: requests.Session, api_url: str = BG_WIKI_API) -> list[dict]:
    resp = sess.get(
        api_url,
        params={
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": 5,
            "format": "json",
            "utf8": 1,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("query", {}).get("search", [])


def _fallback_queries(query: str) -> list[str]:
    """Generate progressively simpler queries from the original."""
    queries = [query]
    words = query.split()
    # Drop one word at a time from the end
    for i in range(len(words) - 1, 1, -1):
        queries.append(" ".join(words[:i]))
    # First two words only
    if len(words) > 2:
        queries.append(" ".join(words[:2]))
    return list(dict.fromkeys(queries))  # deduplicate, preserve order


def _fetch_content(title: str, sess: requests.Session, api_url: str = BG_WIKI_API) -> str:
    """Fetch rendered page HTML and extract plain text."""
    resp = sess.get(
        api_url,
        params={
            "action": "parse",
            "page": title,
            "prop": "text",
            "format": "json",
            "utf8": 1,
        },
        timeout=10,
    )
    resp.raise_for_status()
    html = resp.json().get("parse", {}).get("text", {}).get("*", "")
    return _html_to_text(html) if html else ""


def _html_to_text(html: str) -> str:
    """Extract readable plain text from BG-Wiki rendered HTML."""
    soup = BeautifulSoup(html, "html.parser")

    content = soup.find("div", class_="mw-parser-output") or soup

    # Remove noise elements
    for tag in content.find_all(["script", "style", "sup"]):
        tag.decompose()
    for cls in ["catlinks", "printfooter", "mw-references-wrap", "mw-editsection"]:
        for tag in content.find_all(class_=cls):
            tag.decompose()

    # Get all text, collapsing whitespace
    raw = content.get_text(separator="\n", strip=True)

    # Collapse runs of blank lines
    text = re.sub(r"\n{3,}", "\n\n", raw).strip()

    if len(text) > MAX_CONTENT_CHARS:
        text = text[:MAX_CONTENT_CHARS] + "..."
    return text


def _search_wiki(api_url: str, base_url: str, query: str, sess: requests.Session) -> dict:
    """Try one MediaWiki instance; return result dict or {"found": False}."""
    results = []
    for q in _fallback_queries(query):
        results = _search_titles(q, sess, api_url=api_url)
        if results:
            logger.info(f"Wiki {api_url!r} search '{q}' → {[r['title'] for r in results]}")
            break

    if not results:
        return {"found": False, "query": query}

    for result in results:
        title = result["title"]
        content = _fetch_content(title, sess, api_url=api_url)
        if content and len(content) > 50:
            url = f"{base_url}/{title.replace(' ', '_')}"
            return {
                "found": True,
                "query": query,
                "title": title,
                "url": url,
                "content": content,
                "other_results": [r["title"] for r in results if r["title"] != title],
            }

    return {"found": False, "query": query, "titles_checked": [r["title"] for r in results]}


def search_bgwiki(query: str) -> dict:
    """Search BG-Wiki, then fall back to FFXIclopedia if nothing is found."""
    sess = _session()
    result = _search_wiki(BG_WIKI_API, BG_WIKI_BASE, query, sess)
    if result["found"]:
        return result
    logger.info(f"BG-Wiki returned no content for {query!r}; trying FFXIclopedia")
    return _search_wiki(FANDOM_API, FANDOM_BASE, query, sess)


def search_as_tool_result(query: str) -> dict:
    try:
        return search_bgwiki(query)
    except Exception as e:
        logger.error(f"Wiki search error for {query!r}: {e}", exc_info=True)
        return {"found": False, "query": query, "error": str(e)}
