"""
managed_room_sync — Graph webhook·일일 전체 동기화·subscription 관리.

JSON 이벤트 캐시 없음. managed_room_events DB만 갱신.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import requests

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.core.config import OUTLOOK_ROOM_DIR, OUTLOOK_ROOM_SUBSCRIPTIONS_PATH
from app.services.outlook_room import ms_graph_room as graph
from app.services.outlook_room.managed_room_events import (
    delete_by_room_event,
    event_in_retention_window,
    purge_outside_retention,
    retention_window,
    upsert_from_graph_event,
)

logger = logging.getLogger(__name__)

GRAPH_SUBSCRIPTION_URL = "https://graph.microsoft.com/v1.0/subscriptions"
_MANAGED_ROOM_EMAILS = set(e.lower() for e in graph.ROOM_EMAIL_MAP.values())


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _is_managed_room_mailbox(mailbox: str) -> bool:
    return mailbox.strip().lower() in _MANAGED_ROOM_EMAILS


def parse_notification_resource(resource: str) -> tuple[str, str] | None:
    """users/{mailbox}/events/{event-id} → (mailbox, event_id)."""
    parts = [p for p in (resource or "").split("/") if p]
    lowered = [p.lower() for p in parts]
    if "users" not in lowered or "events" not in lowered:
        return None
    try:
        user_idx = lowered.index("users")
        event_idx = lowered.index("events")
        mailbox = unquote(parts[user_idx + 1])
        event_id = unquote(parts[event_idx + 1])
    except (IndexError, ValueError):
        return None
    return mailbox, event_id


async def handle_change_notification(notification: dict[str, Any]) -> None:
    """Graph webhook 1건 — DB projection 갱신."""
    resource = str(notification.get("resource") or "")
    parsed = parse_notification_resource(resource)
    if not parsed:
        logger.warning("[ManagedRoomSync] unsupported resource=%s", resource)
        return

    mailbox, event_id = parsed
    if not _is_managed_room_mailbox(mailbox):
        logger.debug("[ManagedRoomSync] skip non-room mailbox=%s", mailbox)
        return

    change_type = str(notification.get("changeType") or "").lower()
    headers = graph.build_api_headers(graph.get_valid_app_token())

    if "deleted" in change_type:
        await delete_by_room_event(mailbox, event_id)
        logger.info(
            "[ManagedRoomSync] webhook deleted mailbox=%s event=%s",
            mailbox,
            event_id[:24],
        )
        return

    try:
        detail = graph.fetch_event_detail(headers, mailbox, event_id)
    except requests.exceptions.HTTPError as err:
        if err.response is not None and err.response.status_code == 404:
            await delete_by_room_event(mailbox, event_id)
            return
        raise

    if not detail:
        await delete_by_room_event(mailbox, event_id)
        return

    start = str((detail.get("start") or {}).get("dateTime") or "")
    if not event_in_retention_window(start):
        await delete_by_room_event(mailbox, event_id)
        return

    await upsert_from_graph_event(mailbox, detail)
    logger.info(
        "[ManagedRoomSync] webhook upsert mailbox=%s event=%s",
        mailbox,
        event_id[:24],
    )


def collect_room_events_for_window(
    headers: dict,
    *,
    start_date: date,
    end_date_exclusive: date,
) -> list[tuple[str, dict[str, Any]]]:
    """3개 회의실 Graph 조회."""
    out: list[tuple[str, dict[str, Any]]] = []
    for room_email in graph.ROOM_EMAIL_MAP.values():
        events = graph.fetch_calendar_events_with_details_between(
            headers,
            room_email,
            start_date=start_date,
            end_date_exclusive=end_date_exclusive,
        )
        for event in events:
            out.append((room_email, event))
    return out


async def sync_all_managed_rooms(*, reference: date | None = None) -> dict[str, int]:
    """
    전체 동기화 — retention 윈도우 내 이벤트 upsert, 누락분 삭제, 기간 밖 purge.

    Returns stats dict.
    """
    start, end_exclusive = retention_window(reference)
    headers = graph.build_api_headers(graph.get_valid_app_token())
    fetched = collect_room_events_for_window(
        headers,
        start_date=start,
        end_date_exclusive=end_exclusive,
    )

    seen_keys: set[tuple[str, str]] = set()
    upserted = 0
    for room_email, event in fetched:
        eid = str(event.get("id") or "").strip()
        if not eid:
            continue
        seen_keys.add((room_email.lower(), eid))
        row = await upsert_from_graph_event(room_email, event)
        if row:
            upserted += 1

    removed_stale = 0
    from sqlalchemy import and_, delete, select

    from app.db.connection import get_db_session
    from app.db.models import ManagedRoomEvent

    async with get_db_session() as session:
        result = await session.execute(
            select(
                ManagedRoomEvent.room_email,
                ManagedRoomEvent.outlook_event_id,
            ).where(
                and_(
                    ManagedRoomEvent.start_time >= start.isoformat(),
                    ManagedRoomEvent.start_time < end_exclusive.isoformat(),
                )
            )
        )
        for room_email, outlook_eid in result.all():
            if outlook_eid.startswith("pending:"):
                continue
            if (room_email.lower(), outlook_eid) not in seen_keys:
                await session.execute(
                    delete(ManagedRoomEvent).where(
                        and_(
                            ManagedRoomEvent.room_email == room_email,
                            ManagedRoomEvent.outlook_event_id == outlook_eid,
                        )
                    )
                )
                removed_stale += 1

    purged = await purge_outside_retention(reference=reference)
    logger.info(
        "[ManagedRoomSync] full sync %s~%s upserted=%d stale_removed=%d purged=%d",
        start,
        end_exclusive,
        upserted,
        removed_stale,
        purged,
    )
    return {
        "upserted": upserted,
        "stale_removed": removed_stale,
        "purged": purged,
        "window_start": start.isoformat(),
        "window_end_exclusive": end_exclusive.isoformat(),
    }


def load_subscriptions() -> dict[str, Any]:
    if not OUTLOOK_ROOM_SUBSCRIPTIONS_PATH.is_file():
        return {"subscriptions": []}
    try:
        with open(OUTLOOK_ROOM_SUBSCRIPTIONS_PATH, encoding="utf-8") as f:
            payload = json.load(f)
        return payload if isinstance(payload, dict) else {"subscriptions": []}
    except (OSError, json.JSONDecodeError):
        return {"subscriptions": []}


def save_subscriptions(payload: dict[str, Any]) -> None:
    OUTLOOK_ROOM_DIR.mkdir(parents=True, exist_ok=True)
    OUTLOOK_ROOM_SUBSCRIPTIONS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def create_or_refresh_subscriptions(
    *,
    notification_url: str,
    client_state: str,
    expiration_hours: int = 48,
) -> dict[str, Any]:
    """회의실 mailbox events 구독 생성."""
    headers = graph.build_api_headers(graph.get_valid_app_token())
    for sub in load_subscriptions().get("subscriptions") or []:
        sub_id = str((sub or {}).get("id") or "").strip()
        if not sub_id:
            continue
        try:
            requests.delete(
                f"{GRAPH_SUBSCRIPTION_URL}/{sub_id}",
                headers=headers,
                timeout=30,
            )
        except Exception:
            logger.debug(
                "[ManagedRoomSync] old subscription delete skipped id=%s",
                sub_id,
            )

    expires_at = (datetime.utcnow() + timedelta(hours=expiration_hours)).replace(
        microsecond=0,
    )
    expiration = f"{expires_at.isoformat()}Z"
    created: list[dict[str, Any]] = []

    for room_email in graph.ROOM_EMAIL_MAP.values():
        body = {
            "changeType": "created,updated,deleted",
            "notificationUrl": notification_url,
            "resource": f"users/{room_email}/events",
            "expirationDateTime": expiration,
            "clientState": client_state,
        }
        res = requests.post(
            GRAPH_SUBSCRIPTION_URL,
            headers=headers,
            json=body,
            timeout=30,
        )
        res.raise_for_status()
        data = res.json()
        data["room_email"] = room_email
        created.append(data)

    payload = {
        "updated_at": _now_iso(),
        "notification_url": notification_url,
        "subscriptions": created,
    }
    save_subscriptions(payload)
    logger.info("[ManagedRoomSync] subscriptions created=%d", len(created))
    return payload
