"""
회의실 예약 챗봇 Slack 리마인더 — DB 폴링 후 chat_postMessage.
"""

from __future__ import annotations

import logging
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.services.outlook_room import ms_graph_room as graph
from app.services.outlook_room.managed_room_events import (
    backfill_slack_channel,
    fetch_pending_reminder_events,
    mark_reminder_sent,
)

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")


def _attendee_status_text(detail: dict | None) -> str:
    if not detail:
        return ""
    room_emails = {email.lower() for email in graph.ROOM_EMAIL_MAP.values()}
    lines: list[str] = []
    for attendee in detail.get("attendees") or []:
        info = attendee.get("emailAddress") or {}
        email = str(info.get("address") or "").strip()
        if not email:
            continue
        attendee_type = str(attendee.get("type") or "").strip()
        if attendee_type == "resource" or email.lower() in room_emails:
            continue
        name = str(info.get("name") or email).strip()
        response = str((attendee.get("status") or {}).get("response") or "none").strip()
        lines.append(f"  - {name} <{email}>: {response}")
    if not lines:
        return ""
    return "\n• 참석자 응답\n" + "\n".join(lines)


async def _fetch_booking_event_detail(booking) -> dict | None:
    organizer = (booking.organizer_email or "").strip()
    event_id = (booking.organizer_event_id or "").strip()
    if not organizer or not event_id or event_id.startswith("pending:"):
        return None
    loop = asyncio.get_running_loop()
    try:
        headers = await loop.run_in_executor(
            None,
            lambda: graph.build_api_headers(graph.get_valid_app_token()),
        )
        return await loop.run_in_executor(
            None,
            lambda: graph.fetch_event_detail(headers, organizer, event_id),
        )
    except Exception as e:
        logger.warning("[RoomReminder] event detail 조회 실패 booking_id=%s: %s", booking.id, e)
        return None


def _parse_kst(iso_local: str) -> datetime:
    return datetime.fromisoformat(iso_local[:19]).replace(tzinfo=KST)


def _reminder_due(booking) -> bool:
    minutes = booking.reminder_minutes_before
    if not minutes or minutes <= 0:
        return False
    try:
        start_dt = _parse_kst(booking.start_time)
    except ValueError:
        return False
    now = datetime.now(KST)
    if start_dt <= now:
        return False
    remind_at = start_dt - timedelta(minutes=minutes)
    return now >= remind_at


async def _resolve_channel(slack_client, booking) -> str | None:
    ch = (booking.slack_channel_id or "").strip()
    if ch:
        return ch
    uid = (booking.bot_slack_user_id or booking.slack_user_id or "").strip()
    if not uid:
        return None
    try:
        resp = slack_client.conversations_open(users=uid)
        ch = str((resp.get("channel") or {}).get("id") or "").strip()
        if ch:
            await backfill_slack_channel(booking.id, ch)
        return ch or None
    except Exception as e:
        logger.warning("[RoomReminder] DM 채널 open 실패 user=%s: %s", uid, e)
        return None


async def process_due_room_reminders(slack_client) -> int:
    """발송 시각이 된 예약 리마인더를 Slack DM으로 전송."""
    rows = await fetch_pending_reminder_events()
    sent = 0
    for booking in rows:
        if not _reminder_due(booking):
            continue
        channel = await _resolve_channel(slack_client, booking)
        if not channel:
            logger.warning(
                "[RoomReminder] 채널 없음 booking_id=%s slack_user=%s",
                booking.id,
                booking.slack_user_id,
            )
            continue
        start = booking.start_time[:16].replace("T", " ")
        end = booking.end_time[11:16]
        minutes = booking.reminder_minutes_before
        detail = await _fetch_booking_event_detail(booking)
        attendee_text = _attendee_status_text(detail)
        text = (
            f"⏰ 회의 리마인더 ({minutes}분 전)\n"
            f"• {booking.room_display} | {booking.subject}\n"
            f"• {start} ~ {end}\n"
            f"• booking_id: {booking.id}"
            f"{attendee_text}"
        )
        try:
            slack_client.chat_postMessage(channel=channel, text=text)
            await mark_reminder_sent(booking.id)
            sent += 1
            logger.info("[RoomReminder] sent booking_id=%s channel=%s", booking.id, channel)
        except Exception as e:
            logger.error("[RoomReminder] 발송 실패 booking_id=%s: %s", booking.id, e)
    return sent
