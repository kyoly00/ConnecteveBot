"""bot_jobs 디스패치 — 소스별 실제 처리."""

from __future__ import annotations

import logging
from typing import Any

from app.db.models import BotJob
from app.services.bot_jobs.constants import JobSource
from app.services.bot_jobs.post_response import POST_RESPONSE_EVENT

logger = logging.getLogger(__name__)


async def dispatch_bot_job(job: BotJob) -> None:
    if job.source == JobSource.SLACK:
        await _dispatch_slack_job(job)
    elif job.source == JobSource.INTERNAL:
        await _dispatch_internal_job(job)
    elif job.source == JobSource.GRAPH:
        await _dispatch_graph_job(job)
    elif job.source == JobSource.CONFLUENCE:
        await _dispatch_confluence_job(job)
    else:
        raise ValueError(f"unsupported job source: {job.source}")


async def _dispatch_slack_job(job: BotJob) -> None:
    # main 순환 import 방지
    import app.main as main_module

    payload: dict[str, Any] = job.payload or {}

    if job.event_type == "message":
        event = payload.get("event") or {}
        text = (event.get("text") or "").strip()
        files = event.get("files") or []
        channel_id = event.get("channel") or job.channel_id
        user_id = event.get("user") or job.user_id
        thread_ts = event.get("thread_ts") or event.get("ts") or job.thread_ts
        channel_type = event.get("channel_type", "")
        await main_module.process_and_respond(
            channel_id,
            user_id,
            text,
            thread_ts,
            channel_type=channel_type,
            files=files,
        )
        return

    if job.event_type == "app_home_opened":
        user_id = job.user_id or (payload.get("event") or {}).get("user")
        if not user_id:
            raise ValueError("app_home_opened without user_id")
        await main_module.send_app_welcome_message(main_module.slack_app.client, user_id)
        return

    if job.event_type == "slash_command":
        channel_id = payload.get("channel_id") or job.channel_id
        user_id = payload.get("user_id") or job.user_id
        text = (payload.get("text") or "").strip()
        thread_ts = payload.get("thread_ts") or payload.get("ts") or job.thread_ts
        await main_module.process_and_respond(
            channel_id,
            user_id,
            text,
            thread_ts,
        )
        return

    raise ValueError(f"unsupported slack event_type: {job.event_type}")


async def _dispatch_internal_job(job: BotJob) -> None:
    if job.event_type == POST_RESPONSE_EVENT:
        import app.main as main_module

        await main_module.run_post_response_tasks(job.payload or {})
        return
    raise ValueError(f"unsupported internal event_type: {job.event_type}")


async def _dispatch_graph_job(job: BotJob) -> None:
    from app.services.outlook_room.managed_room_sync import handle_change_notification

    notification = job.payload
    if not isinstance(notification, dict):
        raise ValueError("graph job payload must be dict")
    await handle_change_notification(notification)


async def _dispatch_confluence_job(job: BotJob) -> None:
    from app.integrations.confluence_webhook_sync import schedule_webhook_sync

    payload = job.payload or {}
    schedule_webhook_sync(
        payload.get("event") or job.event_type,
        str(payload.get("page_id") or ""),
        payload.get("title"),
        webhook_meta=payload.get("webhook_meta"),
    )
