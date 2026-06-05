"""Reddit search for FFXI questions — LAST-RESORT fallback.

Reddit blocks AWS/datacenter IPs and its public ``.json`` API outright, so we
cannot query Reddit directly from Lambda. Instead this module scrapes
``old.reddit.com``'s server-rendered search HTML through the ScrapingAnt proxy
API. old.reddit clears ScrapingAnt's *datacenter* proxy with **no JS rendering**
— the cheapest mode (~9 credits/request) — and its markup is stable and easy to
parse, unlike new Reddit's React DOM.

Gated on the ``SCRAPINGANT_API_KEY`` env var: with no key set the tool reports
itself disabled and returns no results, so it stays dormant until a key is
dropped in. Each answer costs at most two proxy fetches (one search page + one
comment page).
"""

import logging
import os
import re
import urllib.parse

import requests
from bs4 import BeautifulSoup

SUBREDDIT = "ffxi"
OLD_REDDIT_SEARCH = "https://old.reddit.com/r/{sr}/search"
SCRAPINGANT_ENDPOINT = "https://api.scrapingant.com/v2/general"

# old.reddit clears the cheap datacenter proxy; residential is the fallback if
# datacenter ever gets flagged. Both run with JS rendering OFF (server-rendered).
PROXY_MODES = ("datacenter", "residential")

MAX_RESULTS = 5
MAX_SNIPPET_CHARS = 300
MAX_SELFTEXT_CHARS = 800
MAX_COMMENT_CHARS = 400
MAX_COMMENTS = 3
MAX_CONTENT_CHARS = 2500
FETCH_TIMEOUT = 45

logger = logging.getLogger(__name__)

_WS_RE = re.compile(r"\s+")


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def _parse_int(el) -> int | None:
    """Pull the first integer out of an element's text (e.g. '60 points' → 60)."""
    if el is None:
        return None
    m = re.search(r"-?\d+", el.get_text())
    return int(m.group()) if m else None


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "MoogleBot/1.0 (FFXI Slack Bot; r/ffxi last-resort search)"
    return s


def _fetch(url: str, sess: requests.Session, api_key: str) -> str:
    """Fetch ``url`` through ScrapingAnt, escalating proxy modes if blocked.

    Tries the cheap datacenter proxy first (one retry on a transient 5xx), then
    residential. Returns the raw HTML, or raises if every attempt fails.
    """
    last_err: Exception | None = None
    for proxy_type in PROXY_MODES:
        params = {
            "url": url,
            "x-api-key": api_key,
            "browser": "false",
            "proxy_type": proxy_type,
        }
        if proxy_type == "residential":
            params["proxy_country"] = "US"

        for attempt in range(2):
            try:
                resp = sess.get(SCRAPINGANT_ENDPOINT, params=params, timeout=FETCH_TIMEOUT)
            except requests.RequestException as e:
                last_err = e
                logger.warning(f"ScrapingAnt request error via {proxy_type}: {e}")
                break  # network-level failure — escalate proxy mode

            if resp.status_code == 200:
                return resp.text

            last_err = RuntimeError(
                f"ScrapingAnt HTTP {resp.status_code} via {proxy_type} for {url}"
            )
            logger.warning(str(last_err))
            # Retry the same mode once on a transient 5xx; otherwise (e.g. 423
            # "browser detected") escalate to the next proxy mode.
            if 500 <= resp.status_code < 600 and attempt == 0:
                continue
            break

    raise last_err or RuntimeError(f"Failed to fetch {url}")


def _parse_search(html: str) -> list[dict]:
    """Extract search-result rows from an old.reddit search page."""
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for div in soup.select("div.search-result-link")[:MAX_RESULTS]:
        a = div.select_one("a.search-title")
        if not a:
            continue
        body = div.select_one(".search-result-body")
        results.append({
            "title": _clean(a.get_text()),
            "url": a.get("href", ""),
            "score": _parse_int(div.select_one(".search-score")),
            "num_comments": _parse_int(div.select_one("a.search-comments")),
            "snippet": _clean(body.get_text())[:MAX_SNIPPET_CHARS] if body else "",
        })
    return results


def _parse_post(html: str) -> tuple[str, list[dict]]:
    """Extract the OP self-text and top-level comments from a post page."""
    soup = BeautifulSoup(html, "html.parser")

    op = soup.select_one("#siteTable .usertext-body .md")
    selftext = _clean(op.get_text(" ")) if op else ""
    if selftext in ("[removed]", "[deleted]"):
        selftext = ""

    comments = []
    for c in soup.select(".commentarea > .sitetable > .comment"):
        md = c.select_one(".entry .usertext-body .md")
        if not md:
            continue
        body = _clean(md.get_text(" "))
        if not body or body in ("[removed]", "[deleted]") or len(body) < 20:
            continue
        score_el = c.select_one(".score.unvoted")
        comments.append({
            "score": _clean(score_el.get("title", "")) if score_el else "",
            "body": body[:MAX_COMMENT_CHARS],
        })
        if len(comments) >= MAX_COMMENTS:
            break

    return selftext[:MAX_SELFTEXT_CHARS], comments


def _build_content(query: str, results: list[dict], best: dict,
                   selftext: str, comments: list[dict]) -> str:
    """Render the parsed results into a readable blob for the model."""
    parts = [f"r/{SUBREDDIT} discussion search for '{query}':", ""]
    for r in results:
        meta = []
        if r["score"] is not None:
            meta.append(f"{r['score']} pts")
        if r["num_comments"] is not None:
            meta.append(f"{r['num_comments']} comments")
        suffix = f" ({', '.join(meta)})" if meta else ""
        parts.append(f"- {r['title']}{suffix}")
        if r["snippet"]:
            parts.append(f"  {r['snippet']}")

    if selftext:
        parts += ["", f"Most relevant post '{best['title']}' — original post:", selftext]

    if comments:
        parts += ["", f"Top comments on '{best['title']}':"]
        for c in comments:
            prefix = f"[{c['score']}] " if c.get("score") else ""
            parts.append(f"• {prefix}{c['body']}")

    content = "\n".join(parts)
    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS] + "..."
    return content


def search_reddit(query: str, api_key: str) -> dict:
    """Search r/ffxi and return relevant post content + top comments."""
    sess = _session()

    search_url = OLD_REDDIT_SEARCH.format(sr=SUBREDDIT) + "?" + urllib.parse.urlencode({
        "q": query,
        "restrict_sr": "on",
        "sort": "relevance",
        "limit": MAX_RESULTS,
    })
    results = _parse_search(_fetch(search_url, sess, api_key))
    if not results:
        return {"found": False, "query": query, "subreddit": SUBREDDIT}

    # Enrich the most-relevant result (results are already relevance-sorted) with
    # its OP text + top comments — one extra fetch. Everything else stays at
    # title+snippet to keep credit cost low. Relevance beats raw upvotes here: a
    # popular-but-tangential thread won't answer the user's actual question.
    best = results[0]
    selftext, comments = "", []
    try:
        post_url = best["url"].split("?")[0] + "?sort=top&limit=20"
        selftext, comments = _parse_post(_fetch(post_url, sess, api_key))
    except Exception as e:
        logger.warning(f"Reddit comment fetch failed for {best['url']}: {e}")

    return {
        "found": True,
        "query": query,
        "subreddit": SUBREDDIT,
        "top_post": best["title"],
        "top_post_url": best["url"],
        "result_count": len(results),
        "content": _build_content(query, results, best, selftext, comments),
    }


def search_as_tool_result(query: str) -> dict:
    """Tool entrypoint. No-op (disabled) when SCRAPINGANT_API_KEY is unset."""
    api_key = os.environ.get("SCRAPINGANT_API_KEY", "").strip()
    if not api_key:
        logger.info("Reddit search skipped — SCRAPINGANT_API_KEY not configured")
        return {
            "found": False,
            "query": query,
            "disabled": True,
            "note": "Reddit search is not configured.",
        }
    try:
        return search_reddit(query, api_key)
    except Exception as e:
        logger.error(f"Reddit search error for {query!r}: {e}", exc_info=True)
        return {"found": False, "query": query, "error": str(e)}
