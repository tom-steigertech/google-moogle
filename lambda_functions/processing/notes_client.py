"""User-contributed notes storage in S3.

Notes are global — every user benefits from every saved note. They live as a
single JSON document at `notes/notes.json` in the idempotency bucket. The
S3 lifecycle rule on that bucket is scoped to the `idempotency/` prefix so
notes are not auto-expired (see terraform/main.tf).

The single-document layout is fine for the bot's traffic profile (handful of
concurrent users); the race window on read-modify-write is tiny. Switch to
one-file-per-note if that ever changes.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError


NOTES_KEY = "notes/notes.json"
MAX_NOTE_LENGTH = 500
MAX_NOTES_RETAINED = 200
MAX_NOTES_FOR_CONTEXT = 50

logger = logging.getLogger(__name__)
_s3_client = None


def _client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def load_notes(bucket: str) -> list[dict]:
    """Return all stored notes (oldest first). Empty list if none exist yet."""
    try:
        resp = _client().get_object(Bucket=bucket, Key=NOTES_KEY)
        body = resp["Body"].read()
        data = json.loads(body)
        if isinstance(data, list):
            return data
        logger.warning(f"Notes document is not a list; got {type(data).__name__}")
        return []
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return []
        logger.error(f"Failed to load notes: {e}")
        return []
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"Notes document is not valid JSON: {e}")
        return []


def recent_notes(bucket: str, limit: int = MAX_NOTES_FOR_CONTEXT) -> list[dict]:
    """Return the most recent `limit` notes, newest first."""
    notes = load_notes(bucket)
    return list(reversed(notes[-limit:]))


def save_note(bucket: str, text: str, author_id: str, channel_id: str) -> dict:
    """Append a note to the document and return the stored entry.

    Truncates over-long text rather than rejecting it — better to keep
    something than lose the user's contribution.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("note text is empty")
    if len(text) > MAX_NOTE_LENGTH:
        text = text[:MAX_NOTE_LENGTH].rstrip() + "…"

    note = {
        "id": uuid.uuid4().hex[:12],
        "text": text,
        "author_id": author_id or "",
        "channel_id": channel_id or "",
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
    }

    notes = load_notes(bucket)
    notes.append(note)
    if len(notes) > MAX_NOTES_RETAINED:
        dropped = len(notes) - MAX_NOTES_RETAINED
        notes = notes[-MAX_NOTES_RETAINED:]
        logger.info(f"Dropped {dropped} oldest notes to stay within retention cap")

    _client().put_object(
        Bucket=bucket,
        Key=NOTES_KEY,
        Body=json.dumps(notes).encode("utf-8"),
        ContentType="application/json",
    )
    logger.info(f"Saved note {note['id']} (total now: {len(notes)})")
    return note


def search_notes(bucket: str, query: str, limit: int = 20) -> list[dict]:
    """Return notes whose text contains any of the query terms (case-insensitive).

    Multiple terms are OR-matched for broader recall. Results are newest-first
    and capped at `limit`. Caller controls what to do when the result is empty.
    """
    query = (query or "").strip().lower()
    if not query:
        return []

    notes = load_notes(bucket)
    if not notes:
        return []

    terms = [t for t in query.split() if t]
    if not terms:
        return []

    matches = []
    for n in notes:
        text_lower = (n.get("text") or "").lower()
        if any(term in text_lower for term in terms):
            matches.append({
                "id": n.get("id"),
                "text": n.get("text"),
                "author_id": n.get("author_id"),
                "created_at": n.get("created_at"),
            })

    matches.sort(key=lambda n: n.get("created_at") or "", reverse=True)
    return matches[:limit]


def format_notes_for_prompt(notes: list[dict]) -> str:
    """Render notes as a system-prompt section. Returns '' if no notes."""
    if not notes:
        return ""
    lines = [
        "User-contributed FFXI notes from other players. Treat as community knowledge "
        "— useful, but stay skeptical and prefer the wiki tools for verifiable facts. "
        "Reference these only when they are directly relevant to the question:",
    ]
    for n in notes:
        text = (n.get("text") or "").strip()
        if text:
            lines.append(f"- {text}")
    return "\n".join(lines)
