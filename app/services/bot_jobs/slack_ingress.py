"""Slack 웹훅 → bot_jobs enqueue."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs

from app.services.bot_jobs.constants import JobSource
from app.services.bot_jobs.queue import enqueue_job

logger = logging.getLogger(__name__)


def should_enqueue_slack_message(event: dict[str, Any]) -> bool:
    """기존 handle_message_events 필터와 동일."""
    subtype = event.get("subtype")
    if subtype and subtype not in ("file_share",):
        return False
    if "bot_id" in event:
        return False
    text = (event.get("text") or "").strip()
    files = event.get("files") or []
    if not text and not files:
        return False
    return True


def slack_conversation_key(
    *,
    team_id: str | None,
    channel_id: str | None,
    thread_ts: str | None,
) -> str:
    return f"{team_id or '-'}:{channel_id or '-'}:{thread_ts or '-'}"


async def enqueue_slack_event_callback(payload: dict[str, Any]) -> bool:
    event_id = str(payload.get("event_id") or "").strip()
    if not event_id:
        logger.warning("[SlackIngress] event_callback without event_id")
        return False

    event = payload.get("event") or {}
    event_type = str(event.get("type") or "").strip()
    if not event_type:
        return False

    if event_type == "message":
        if not should_enqueue_slack_message(event):
            return False
    elif event_type == "app_home_opened":
        if event.get("tab") != "messages":
            return False
    else:
        logger.debug("[SlackIngress] unsupported event type: %s", event_type)
        return False

    team_id = str(payload.get("team_id") or event.get("team") or "").strip() or None
    channel_id = str(event.get("channel") or "").strip() or None
    user_id = str(event.get("user") or "").strip() or None
    thread_ts = str(event.get("thread_ts") or event.get("ts") or "").strip() or None
    event_ts = str(event.get("ts") or "").strip() or None

    created, _ = await enqueue_job(
        source=JobSource.SLACK,
        source_event_id=event_id,
        event_type=event_type,
        team_id=team_id,
        channel_id=channel_id,
        user_id=user_id,
        thread_ts=thread_ts,
        event_ts=event_ts,
        conversation_key=slack_conversation_key(
            team_id=team_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
        ),
        payload=payload,
    )
    return created


def _flatten_form(body: bytes) -> dict[str, str]:
    parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    return {k: (v[0] if v else "") for k, v in parsed.items()}


async def enqueue_slack_slash_command(body: bytes) -> bool:
    form = _flatten_form(body)
    trigger_id = str(form.get("trigger_id") or "").strip()
    if not trigger_id:
        logger.warning("[SlackIngress] slash command without trigger_id")
        return False

    team_id = str(form.get("team_id") or "").strip() or None
    channel_id = str(form.get("channel_id") or "").strip() or None
    user_id = str(form.get("user_id") or "").strip() or None
    thread_ts = str(form.get("thread_ts") or form.get("ts") or "").strip() or None

    created, _ = await enqueue_job(
        source=JobSource.SLACK,
        source_event_id=f"slash:{trigger_id}",
        event_type="slash_command",
        team_id=team_id,
        channel_id=channel_id,
        user_id=user_id,
        thread_ts=thread_ts,
        event_ts=str(form.get("ts") or "").strip() or None,
        conversation_key=slack_conversation_key(
            team_id=team_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
        ),
        payload=form,
    )
    return created
