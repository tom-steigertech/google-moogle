"""AgentCore Memory client for the Moogle bot.

Handles reading prior conversation turns and writing new ones using the
Bedrock AgentCore data-plane API (bedrock-agentcore). The memory resource
itself is provisioned by Terraform; this module only reads/writes events.

Session model
-------------
actorId  = "slack:{slack_user_id}"
sessionId = "{channel_id}:{thread_ts}"   # in-thread @mention
sessionId = "{channel_id}:{user_id}"     # top-level @mention or slash cmd

Both paths share the same load/save interface — the caller computes the IDs.

Idle-gap filtering
------------------
AgentCore TTL is day-granularity. We enforce a finer-grained idle boundary
by inspecting event timestamps: if the gap between the last event and the
next-older event exceeds idle_minutes, we discard everything before the gap
and keep only the contiguous recent tail. This makes 30-min conversations
feel separate even within the 7-day storage window.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import boto3


_agentcore_client: Optional[object] = None
logger = logging.getLogger(__name__)

DELETE_CAP = 200  # max events to delete in clear_session (safety limit)


def _client():
    global _agentcore_client
    if _agentcore_client is None:
        region = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION", "us-east-1")
        _agentcore_client = boto3.client("bedrock-agentcore", region_name=region)
    return _agentcore_client


def _parse_ts(event: dict) -> datetime:
    """Parse the event timestamp to a timezone-aware datetime."""
    raw = event.get("eventTimestamp") or event.get("timestamp") or event.get("createdAt") or ""
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return datetime.fromtimestamp(0, tz=timezone.utc)


def _event_to_message(event: dict) -> Optional[dict]:
    """Convert a raw AgentCore event to a Claude messages-array entry."""
    payload = event.get("payload") or []
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return None

    for item in (payload if isinstance(payload, list) else []):
        conv = item.get("conversational", {})
        role_raw = conv.get("role", "").lower()
        if role_raw not in ("user", "assistant"):
            continue
        content = conv.get("content", {})
        text = (content.get("text", "") if isinstance(content, dict) else "").strip()
        if text:
            return {"role": role_raw, "content": text}
    return None


def load_recent_turns(memory_id: str, actor_id: str, session_id: str,
                      idle_minutes: int = 30) -> list:
    """Return Claude-shaped messages for the active part of the session.

    Fetches all events for the session, orders them oldest-first, then trims
    everything before the most recent idle gap (> idle_minutes between events).
    Returns an empty list if no events exist or if the last event itself is
    older than idle_minutes (a fully expired session).
    """
    try:
        events = _list_all_events(memory_id, actor_id, session_id)
    except Exception as e:
        logger.error(f"Failed to load session events: {e}")
        return []

    if not events:
        return []

    events.sort(key=_parse_ts)

    now = datetime.now(tz=timezone.utc)
    idle_seconds = idle_minutes * 60

    last_ts = _parse_ts(events[-1])
    if (now - last_ts).total_seconds() > idle_seconds:
        logger.info("Last event older than idle window — treating as new session")
        return []

    # Find the most recent idle gap and keep only what's after it.
    cutoff_index = 0
    for i in range(len(events) - 1, 0, -1):
        gap = (_parse_ts(events[i]) - _parse_ts(events[i - 1])).total_seconds()
        if gap > idle_seconds:
            cutoff_index = i
            break

    recent = events[cutoff_index:]
    messages = []
    for ev in recent:
        msg = _event_to_message(ev)
        if msg:
            messages.append(msg)

    logger.info(f"Loaded {len(messages)} turns from session {session_id!r} (of {len(events)} total events)")
    return messages


def save_turn(memory_id: str, actor_id: str, session_id: str,
              role: str, content: str) -> None:
    """Persist a single conversation turn (user or assistant) to AgentCore Memory."""
    role_api = "USER" if role == "user" else "ASSISTANT"
    try:
        _client().create_event(
            memoryId=memory_id,
            actorId=actor_id,
            sessionId=session_id,
            eventTimestamp=datetime.now(tz=timezone.utc),
            payload=[{
                "conversational": {
                    "role": role_api,
                    "content": {"text": content}
                }
            }]
        )
        logger.debug(f"Saved {role} turn to session {session_id!r}")
    except Exception as e:
        logger.error(f"Failed to save {role} turn: {e}")
        raise


def clear_session(memory_id: str, actor_id: str, session_id: str) -> int:
    """Delete all events for a session (for /moogle reset).

    Returns the number of events deleted. Capped at DELETE_CAP for safety —
    logs a warning if the session had more events than the cap.
    """
    try:
        events = _list_all_events(memory_id, actor_id, session_id)
    except Exception as e:
        logger.error(f"Failed to list events for clear_session: {e}")
        raise

    if len(events) > DELETE_CAP:
        logger.warning(
            f"Session {session_id!r} has {len(events)} events; deleting only the first {DELETE_CAP}"
        )
        events = events[:DELETE_CAP]

    deleted = 0
    for event in events:
        event_id = event.get("eventId") or event.get("id")
        if not event_id:
            continue
        try:
            _client().delete_event(
                memoryId=memory_id,
                actorId=actor_id,
                sessionId=session_id,
                eventId=event_id,
            )
            deleted += 1
        except Exception as e:
            logger.error(f"Failed to delete event {event_id}: {e}")

    logger.info(f"Cleared {deleted} events from session {session_id!r}")
    return deleted


def _list_all_events(memory_id: str, actor_id: str, session_id: str) -> list:
    """Paginate through all events for a session."""
    events = []
    paginator_kwargs = dict(
        memoryId=memory_id,
        actorId=actor_id,
        sessionId=session_id,
    )
    next_token = None
    while True:
        if next_token:
            paginator_kwargs["nextToken"] = next_token
        resp = _client().list_events(**paginator_kwargs)
        events.extend(resp.get("events") or resp.get("memoryEvents") or [])
        next_token = resp.get("nextToken")
        if not next_token:
            break
    return events
