"""Slack 웹훅 → bot_jobs enqueue."""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import parse_qs

from app.services.bot_jobs.constants import JobSource
from app.services.bot_jobs.queue import enqueue_job

logger = logging.getLogger(__name__)


def _expected_slack_team_id() -> str:
    return (os.getenv("SLACK_TEAM_ID") or "").strip()


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


def slack_session_key(channel_id: str | None, user_id: str | None) -> str:
    """DM/채널 평면 대화용 세션 키 (user × channel)."""
    return f"{(channel_id or '').strip()}:{(user_id or '').strip()}"


def slack_conversation_key(
    *,
    team_id: str | None,
    channel_id: str | None,
    user_id: str | None,
) -> str:
    return f"{team_id or '-'}:{channel_id or '-'}:{user_id or '-'}"


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
    expected_team = _expected_slack_team_id()
    if expected_team and team_id and team_id != expected_team:
        logger.info(
            "[SlackIngress] ignore other workspace team=%s expected=%s event_id=%s",
            team_id,
            expected_team,
            event_id,
        )
        return False

    channel_id = str(event.get("channel") or "").strip() or None
    user_id = str(event.get("user") or "").strip() or None
    event_ts = str(event.get("ts") or "").strip() or None

    created, _ = await enqueue_job(
        source=JobSource.SLACK,
        source_event_id=event_id,
        event_type=event_type,
        team_id=team_id,
        channel_id=channel_id,
        user_id=user_id,
        thread_ts=None,
        event_ts=event_ts,
        conversation_key=slack_conversation_key(
            team_id=team_id,
            channel_id=channel_id,
            user_id=user_id,
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
    expected_team = _expected_slack_team_id()
    if expected_team and team_id and team_id != expected_team:
        logger.info(
            "[SlackIngress] ignore slash other workspace team=%s expected=%s",
            team_id,
            expected_team,
        )
        return False

    channel_id = str(form.get("channel_id") or "").strip() or None
    user_id = str(form.get("user_id") or "").strip() or None

    created, _ = await enqueue_job(
        source=JobSource.SLACK,
        source_event_id=f"slash:{trigger_id}",
        event_type="slash_command",
        team_id=team_id,
        channel_id=channel_id,
        user_id=user_id,
        thread_ts=None,
        event_ts=str(form.get("ts") or "").strip() or None,
        conversation_key=slack_conversation_key(
            team_id=team_id,
            channel_id=channel_id,
            user_id=user_id,
        ),
        payload=form,
    )
    return created
