"""Reddit search fallback for FFXI questions not covered by BG-Wiki.

Uses Reddit's public JSON API — no OAuth or API key required for reading
public subreddits. Searches r/ffxi and returns titles + post bodies from
the most relevant results, with top comments from the best match.
"""

import logging
import re

import requests

REDDIT_SEARCH = "https://www.reddit.com/r/ffxi/search.json"
REDDIT_POST = "https://www.reddit.com/r/ffxi/comments/{post_id}.json"
MAX_CONTENT_CHARS = 2500
MAX_SELFTEXT_CHARS = 800
MAX_COMMENT_CHARS = 400

logger = logging.getLogger(__name__)

_WHITESPACE_RE = re.compile(r"\n{3,}")


def _session() -> requests.Session:
    s = requests.Session()
    # Reddit requires a descriptive User-Agent or it returns 429/403
    s.headers["User-Agent"] = "MoogleBot/1.0 (FFXI Slack Bot; r/ffxi question answering)"
    return s


def _clean(text: str) -> str:
    return _WHITESPACE_RE.sub("\n\n", text.strip())


def search_reddit(query: str) -> dict:
    """Search r/ffxi and return relevant post content + top comments."""
    sess = _session()

    resp = sess.get(
        REDDIT_SEARCH,
        params={
            "q": query,
            "restrict_sr": 1,
            "sort": "relevance",
            "t": "all",
            "limit": 5,
            "type": "link",
        },
        timeout=10,
    )
    resp.raise_for_status()

    posts = resp.json().get("data", {}).get("children", [])
    if not posts:
        return {"found": False, "query": query}

    results = []
    for post in posts:
        d = post["data"]
        selftext = _clean(d.get("selftext", ""))
        if selftext and selftext != "[removed]" and selftext != "[deleted]":
            selftext = selftext[:MAX_SELFTEXT_CHARS]
            if len(d.get("selftext", "")) > MAX_SELFTEXT_CHARS:
                selftext += "..."
        else:
            selftext = ""

        results.append({
            "title": d.get("title", ""),
            "score": d.get("score", 0),
            "url": f"https://www.reddit.com{d.get('permalink', '')}",
            "post_id": d.get("id", ""),
            "body": selftext,
        })

    # Fetch top comments from the highest-scored post
    best = max(results, key=lambda r: r["score"])
    comments = _fetch_top_comments(best["post_id"], sess)

    content_parts = []
    for r in results:
        content_parts.append(f"Post: {r['title']}")
        if r["body"]:
            content_parts.append(r["body"])

    if comments:
        content_parts.append(f"\nTop comments on '{best['title']}':")
        content_parts.extend(comments)

    content = "\n".join(content_parts)
    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS] + "..."

    return {
        "found": True,
        "query": query,
        "top_post": best["title"],
        "top_post_url": best["url"],
        "content": content,
        "result_count": len(results),
    }


def _fetch_top_comments(post_id: str, sess: requests.Session) -> list[str]:
    """Return the top 3 non-empty comment bodies from a post."""
    if not post_id:
        return []
    try:
        resp = sess.get(
            REDDIT_POST.format(post_id=post_id),
            params={"limit": 10, "depth": 1, "sort": "top"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        comments_listing = data[1]["data"]["children"] if len(data) > 1 else []
        out = []
        for c in comments_listing:
            body = _clean(c.get("data", {}).get("body", ""))
            if body and body not in ("[removed]", "[deleted]") and len(body) > 20:
                body = body[:MAX_COMMENT_CHARS]
                out.append(f"• {body}")
            if len(out) >= 3:
                break
        return out
    except Exception as e:
        logger.warning(f"Failed to fetch comments for post {post_id}: {e}")
        return []


def search_as_tool_result(query: str) -> dict:
    try:
        return search_reddit(query)
    except Exception as e:
        logger.error(f"Reddit search error for {query!r}: {e}", exc_info=True)
        return {"found": False, "query": query, "error": str(e)}
