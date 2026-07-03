"""Graph / Confluence 웹훅 → bot_jobs enqueue."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from app.services.bot_jobs.constants import JobSource
from app.services.bot_jobs.queue import enqueue_job

logger = logging.getLogger(__name__)


def _graph_notification_id(notification: dict[str, Any]) -> str:
    nid = str(notification.get("id") or "").strip()
    if nid:
        return nid
    raw = json.dumps(notification, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


async def enqueue_graph_notification(notification: dict[str, Any]) -> bool:
    source_event_id = _graph_notification_id(notification)
    resource = str(notification.get("resource") or "").strip()
    change_type = str(notification.get("changeType") or "unknown").strip()

    created, _ = await enqueue_job(
        source=JobSource.GRAPH,
        source_event_id=source_event_id,
        event_type=change_type,
        conversation_key=resource or source_event_id,
        payload=notification,
    )
    return created


async def enqueue_confluence_webhook(
    *,
    event: str,
    page_id: str,
    title: str | None,
    webhook_meta: dict[str, Any] | None = None,
) -> bool:
    page_version = ""
    space_key = ""
    if webhook_meta:
        page_version = str(webhook_meta.get("page_version") or "")
        space_key = str(webhook_meta.get("space_key") or "")

    source_event_id = f"{page_id}:{event}:{page_version or '0'}"
    created, _ = await enqueue_job(
        source=JobSource.CONFLUENCE,
        source_event_id=source_event_id,
        event_type=event,
        channel_id=space_key or None,
        conversation_key=space_key or page_id,
        payload={
            "event": event,
            "page_id": page_id,
            "title": title,
            "webhook_meta": webhook_meta,
        },
    )
    return created
