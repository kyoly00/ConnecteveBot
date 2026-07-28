"""
managed_room_sync — Graph API ↔ managed_room_events DB 동기화.

- 4개 회의실 × 1주일 calendarView → DB upsert
- 주기 폴링(기본 10분)으로 변경 추적
- 조회 시 DB·API 교차 검증 (불일치 시 API 기준으로 DB 보정)
"""

from __future__ import annotations

import asyncio
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
    event_to_graph_dict,
    list_events_for_room_day,
    purge_outside_retention,
    sync_window,
    upsert_from_graph_event,
)

logger = logging.getLogger(__name__)

GRAPH_SUBSCRIPTION_URL = "https://graph.microsoft.com/v1.0/subscriptions"
_MANAGED_ROOM_EMAILS = set(e.lower() for e in graph.ROOM_EMAIL_MAP.values())
SYNC_POLL_INTERVAL_SEC = int(os.getenv("MANAGED_ROOM_SYNC_POLL_INTERVAL_SEC", "600"))


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
    """4개 회의실 Graph calendarView 조회."""
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
    전체 동기화 — 1주일 윈도우 내 이벤트 upsert, 누락분 삭제, 기간 밖 purge.

    Returns stats dict.
    """
    start, end_exclusive = sync_window(reference)
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


def _event_snapshot(event_id: str, start: str, end: str, subject: str) -> tuple[str, str, str, str]:
    return (
        event_id.strip(),
        start[:16],
        end[:16],
        (subject or "").strip().lower()[:120],
    )


def _api_event_snapshots(api_events: list[dict]) -> dict[str, tuple[str, str, str, str]]:
    out: dict[str, tuple[str, str, str, str]] = {}
    for event in api_events:
        eid = str(event.get("id") or "").strip()
        if not eid:
            continue
        start, end = graph._event_bounds(event)
        subj = str(event.get("subject") or "")
        out[eid] = _event_snapshot(eid, start, end, subj)
    return out


def _db_event_snapshots(db_rows: list[Any]) -> dict[str, tuple[str, str, str, str]]:
    out: dict[str, tuple[str, str, str, str]] = {}
    for row in db_rows:
        eid = str(row.outlook_event_id or "").strip()
        if not eid or eid.startswith("pending:"):
            continue
        out[eid] = _event_snapshot(
            eid,
            str(row.start_time or ""),
            str(row.end_time or ""),
            str(row.event_subject or row.subject or ""),
        )
    return out


def room_day_events_match(db_rows: list[Any], api_events: list[dict]) -> bool:
    """해당 날짜 DB·API 이벤트 스냅샷이 일치하는지."""
    return _db_event_snapshots(db_rows) == _api_event_snapshots(api_events)


async def reconcile_room_day_from_api(
    room_email: str,
    target: date,
    api_events: list[dict],
) -> int:
    """API 스냅샷 기준으로 해당 날짜 DB projection을 맞춘다."""
    room = room_email.strip().lower()
    api_by_id = {
        str(e.get("id") or "").strip(): e
        for e in api_events
        if str(e.get("id") or "").strip()
    }
    db_rows = await list_events_for_room_day(room, target)

    upserted = 0
    for event in api_by_id.values():
        if graph.event_is_private(event):
            continue
        row = await upsert_from_graph_event(room, event)
        if row:
            upserted += 1

    for row in db_rows:
        eid = str(row.outlook_event_id or "").strip()
        if not eid or eid.startswith("pending:"):
            continue
        if eid not in api_by_id:
            await delete_by_room_event(room, eid)

    return upserted


def _fetch_live_room_events_for_day_sync(
    room_email: str,
    target: date,
) -> list[dict]:
    headers = graph.build_api_headers(graph.get_valid_app_token())
    events = graph.fetch_calendar_events_with_details(
        headers,
        room_email,
        day=target,
    )
    return [e for e in events if not graph.event_is_private(e)]


async def fetch_verified_room_events_for_day(
    room_email: str,
    target: date,
    *,
    room_display: str = "",
) -> tuple[list[dict], list[dict], str]:
    """
    DB·Graph API를 모두 조회해 검증한다.

    Returns
    -------
    (graph_event_dicts, api_events, verification_note)
    불일치 시 API 기준으로 DB를 보정한 뒤 최신 DB를 반환한다.
    """
    db_rows = await list_events_for_room_day(room_email, target)
    loop = asyncio.get_running_loop()
    try:
        api_events = await loop.run_in_executor(
            None,
            _fetch_live_room_events_for_day_sync,
            room_email,
            target,
        )
    except Exception as exc:
        logger.exception(
            "[ManagedRoomSync] live API fetch failed room=%s day=%s",
            room_email,
            target,
        )
        events = [event_to_graph_dict(r) for r in db_rows]
        return events, [], f"⚠️ Outlook API 조회 실패 — DB 캐시만 사용 ({exc})"

    label = room_display or room_email
    if room_day_events_match(db_rows, api_events):
        logger.info(
            "[ManagedRoomSync] verify OK %s %s db=%d api=%d",
            label,
            target,
            len(db_rows),
            len(api_events),
        )
        return [event_to_graph_dict(r) for r in db_rows], api_events, "✅ DB↔Outlook 일치"

    db_snap = _db_event_snapshots(db_rows)
    api_snap = _api_event_snapshots(api_events)
    only_db = sorted(set(db_snap) - set(api_snap))
    only_api = sorted(set(api_snap) - set(db_snap))
    changed = sorted(
        eid for eid in db_snap.keys() & api_snap.keys()
        if db_snap[eid] != api_snap[eid]
    )
    logger.warning(
        "[ManagedRoomSync] verify MISMATCH %s %s only_db=%s only_api=%s changed=%s",
        label,
        target,
        only_db,
        only_api,
        changed,
    )
    await reconcile_room_day_from_api(room_email, target, api_events)
    db_rows = await list_events_for_room_day(room_email, target)
    note = (
        "⚠️ DB↔Outlook 불일치 → API 기준으로 재동기화 "
        f"(db_only={len(only_db)}, api_only={len(only_api)}, changed={len(changed)})"
    )
    return [event_to_graph_dict(r) for r in db_rows], api_events, note


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
