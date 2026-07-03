"""
managed_room_events — 회의실 Outlook 캘린더 로컬 projection (read model).

원본: Microsoft Graph. DB는 check/list_mine/cancel 대상 조회용.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, delete, desc, or_, select, update

from app.db.connection import get_db_session
from app.db.models import ManagedRoomEvent
from app.services.outlook_room import ms_graph_room as graph

logger = logging.getLogger(__name__)

_ACTIVE = "active"
_CANCELLED = "cancelled"

RETENTION_PAST_DAYS = 7
RETENTION_FUTURE_DAYS = 30
DEFAULT_LIST_RANGE_DAYS = 7

_PLACEHOLDER_BOOKING_ID_MARKERS = (
    "auto_detect",
    "from_context",
    "placeholder",
    "unknown",
    "todo",
)

_SUBJECT_TAG_RE = re.compile(r"^\[[A-Z]+\]\s*")


def retention_window(
    reference: date | None = None,
) -> tuple[date, date]:
    """저장·동기화 윈도우: 오늘 -7일 ~ +30일 (end exclusive = +31일 00:00 기준)."""
    today = reference or date.today()
    start = today - timedelta(days=RETENTION_PAST_DAYS)
    end_exclusive = today + timedelta(days=RETENTION_FUTURE_DAYS + 1)
    return start, end_exclusive


def is_valid_booking_uuid(value: str | None) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    if any(m in raw.lower() for m in _PLACEHOLDER_BOOKING_ID_MARKERS):
        return False
    try:
        uuid.UUID(raw)
    except ValueError:
        return False
    return True


def is_usable_event_id(event_id: str | None) -> bool:
    eid = _normalize_event_id(event_id)
    if not eid or "…" in (event_id or ""):
        return False
    return len(eid) >= 80


def _normalize_event_id(event_id: str | None) -> str:
    return (event_id or "").strip().replace("…", "").replace("...", "")


def normalize_date_filter(date_str: str | None) -> str | None:
    if not date_str or not str(date_str).strip():
        return None
    raw = str(date_str).strip()
    if raw in ("오늘", "today"):
        return date.today().isoformat()
    if raw in ("내일", "tomorrow"):
        return (date.today() + timedelta(days=1)).isoformat()
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        return None


def _email_address(node: dict | None) -> str:
    return str(((node or {}).get("emailAddress") or {}).get("address") or "").strip()


def _clean_subject(subject: str) -> str:
    return _SUBJECT_TAG_RE.sub("", (subject or "").strip()).strip() or "(제목 없음)"


def _room_display_from_email(room_email: str) -> str:
    target = room_email.lower()
    for display, email in graph.ROOM_EMAIL_MAP.items():
        if email.lower() == target:
            return display
    return room_email


def _room_name_from_email(room_email: str) -> str:
    return _room_display_from_email(room_email).split()[0].lower()


def _extract_attendee_emails(event: dict[str, Any]) -> list[str]:
    room_emails = {e.lower() for e in graph.ROOM_EMAIL_MAP.values()}
    out: list[str] = []
    seen: set[str] = set()
    for attendee in event.get("attendees") or []:
        email = _email_address(attendee).lower()
        if not email or email in room_emails or email in seen:
            continue
        seen.add(email)
        out.append(email)
    org = _email_address(event.get("organizer")).lower()
    if org and org not in seen and org not in room_emails:
        out.insert(0, org)
    return out


def event_in_retention_window(
    start_time: str,
    *,
    reference: date | None = None,
) -> bool:
    """이벤트 시작일이 retention 윈도우 안인지."""
    try:
        day = date.fromisoformat(str(start_time)[:10])
    except ValueError:
        return False
    win_start, win_end = retention_window(reference)
    return win_start <= day < win_end


def parse_graph_event(room_email: str, event: dict[str, Any]) -> dict[str, Any] | None:
    """Graph event → DB 필드 dict."""
    eid = str(event.get("id") or "").strip()
    start = str((event.get("start") or {}).get("dateTime") or "").strip()
    end = str((event.get("end") or {}).get("dateTime") or "").strip()
    if not eid or not start or not end:
        return None
    if not event_in_retention_window(start):
        return None
    if graph.event_is_private(event):
        return None

    org_email = _email_address(event.get("organizer")).lower()
    org_name = str(
        ((event.get("organizer") or {}).get("emailAddress") or {}).get("name") or ""
    ).strip()
    raw_subject = str(event.get("subject") or "").strip()
    room = room_email.strip().lower()

    return {
        "room_email": room,
        "room_name": _room_name_from_email(room),
        "room_display": _room_display_from_email(room),
        "subject": _clean_subject(raw_subject),
        "event_subject": raw_subject or "(제목 없음)",
        "start_time": start,
        "end_time": end,
        "organizer_email": org_email,
        "organizer_name": org_name or None,
        "outlook_event_id": eid,
        "attendee_emails": _extract_attendee_emails(event),
        "status": _ACTIVE,
    }


def event_to_graph_dict(row: ManagedRoomEvent) -> dict[str, Any]:
    """check/check_all 포맷 호환용 dict."""
    return {
        "id": row.outlook_event_id,
        "subject": row.event_subject,
        "start": {"dateTime": row.start_time},
        "end": {"dateTime": row.end_time},
        "organizer": {
            "emailAddress": {
                "address": row.organizer_email,
                "name": row.organizer_name or row.organizer_email,
            },
        },
        "attendees": [
            {"emailAddress": {"address": e}, "type": "required"}
            for e in (row.attendee_emails or [])
        ],
        "room_email": row.room_email,
        "room_display": row.room_display,
    }


async def upsert_from_graph_event(
    room_email: str,
    event: dict[str, Any],
    *,
    preserve_bot_fields: bool = True,
) -> ManagedRoomEvent | None:
    """Graph 이벤트 1건 upsert (webhook·periodic sync)."""
    fields = parse_graph_event(room_email, event)
    if not fields:
        eid = str(event.get("id") or "").strip()
        if eid:
            await delete_by_room_event(room_email, eid)
        return None

    now = datetime.now(timezone.utc)
    room = fields["room_email"]
    eid = fields["outlook_event_id"]

    async with get_db_session() as session:
        existing = await session.execute(
            select(ManagedRoomEvent).where(
                and_(
                    ManagedRoomEvent.room_email == room,
                    ManagedRoomEvent.outlook_event_id == eid,
                )
            )
        )
        row = existing.scalar_one_or_none()
        if not row:
            pending = await session.execute(
                select(ManagedRoomEvent).where(
                    and_(
                        ManagedRoomEvent.organizer_email == fields["organizer_email"],
                        ManagedRoomEvent.start_time == fields["start_time"],
                        ManagedRoomEvent.outlook_event_id.like("pending:%"),
                    )
                )
            )
            row = pending.scalar_one_or_none()
            if row:
                old_pending = row.outlook_event_id
                row.outlook_event_id = eid
                if not row.organizer_event_id:
                    row.organizer_event_id = None
                logger.info(
                    "[ManagedRoomEvent] pending→real outlook id %s… → %s…",
                    old_pending[:20],
                    eid[:24],
                )

        if row:
            row.room_name = fields["room_name"]
            row.room_display = fields["room_display"]
            row.subject = fields["subject"]
            row.event_subject = fields["event_subject"]
            row.start_time = fields["start_time"]
            row.end_time = fields["end_time"]
            row.organizer_email = fields["organizer_email"]
            row.organizer_name = fields["organizer_name"]
            row.attendee_emails = fields["attendee_emails"]
            row.status = _ACTIVE
            row.synced_at = now
            await session.flush()
            return row

        row = ManagedRoomEvent(
            **fields,
            synced_at=now,
        )
        session.add(row)
        await session.flush()
        logger.info(
            "[ManagedRoomEvent] inserted room=%s outlook_event_id=%s…",
            room,
            eid[:24],
        )
        return row


async def upsert_after_bot_book(
    *,
    organizer_event_id: str,
    room_name: str,
    room_display: str,
    room_email: str,
    subject: str,
    event_subject: str,
    start_time: str,
    end_time: str,
    organizer_email: str,
    bot_user_id: uuid.UUID | None = None,
    bot_slack_user_id: str | None = None,
    slack_channel_id: str | None = None,
    room_outlook_event_id: str | None = None,
) -> ManagedRoomEvent | None:
    """봇 book 성공 직후 — organizer_event_id·봇 메타 저장. room outlook id는 webhook으로 보강."""
    org_eid = organizer_event_id.strip()
    if not org_eid:
        return None

    now = datetime.now(timezone.utc)
    room_mail = room_email.strip().lower()
    outlook_eid = (room_outlook_event_id or "").strip()

    async with get_db_session() as session:
        if outlook_eid:
            existing = await session.execute(
                select(ManagedRoomEvent).where(
                    and_(
                        ManagedRoomEvent.room_email == room_mail,
                        ManagedRoomEvent.outlook_event_id == outlook_eid,
                    )
                )
            )
            row = existing.scalar_one_or_none()
        else:
            existing = await session.execute(
                select(ManagedRoomEvent).where(
                    ManagedRoomEvent.organizer_event_id == org_eid,
                )
            )
            row = existing.scalar_one_or_none()

        if row:
            row.organizer_event_id = org_eid
            row.room_name = room_name.strip()
            row.room_display = room_display.strip()
            row.subject = subject.strip()
            row.event_subject = event_subject.strip()
            row.start_time = start_time.strip()
            row.end_time = end_time.strip()
            row.organizer_email = organizer_email.strip().lower()
            row.status = _ACTIVE
            row.bot_user_id = bot_user_id or row.bot_user_id
            row.bot_slack_user_id = (bot_slack_user_id or "").strip() or row.bot_slack_user_id
            if slack_channel_id:
                row.slack_channel_id = slack_channel_id.strip()
            if outlook_eid:
                row.outlook_event_id = outlook_eid
            row.synced_at = now
            await session.flush()
            return row

        if not outlook_eid:
            outlook_eid = f"pending:{org_eid[:48]}"

        row = ManagedRoomEvent(
            room_email=room_mail,
            room_name=room_name.strip(),
            room_display=room_display.strip(),
            subject=subject.strip(),
            event_subject=event_subject.strip(),
            start_time=start_time.strip(),
            end_time=end_time.strip(),
            organizer_email=organizer_email.strip().lower(),
            outlook_event_id=outlook_eid,
            organizer_event_id=org_eid,
            attendee_emails=[],
            status=_ACTIVE,
            bot_user_id=bot_user_id,
            bot_slack_user_id=(bot_slack_user_id or "").strip() or None,
            slack_channel_id=(slack_channel_id or "").strip() or None,
            synced_at=now,
        )
        session.add(row)
        await session.flush()
        return row


async def delete_by_room_event(room_email: str, outlook_event_id: str) -> None:
    """webhook deleted — projection에서 제거."""
    room = room_email.strip().lower()
    eid = outlook_event_id.strip()
    if not room or not eid:
        return
    async with get_db_session() as session:
        await session.execute(
            delete(ManagedRoomEvent).where(
                and_(
                    ManagedRoomEvent.room_email == room,
                    ManagedRoomEvent.outlook_event_id == eid,
                )
            )
        )


async def purge_outside_retention(*, reference: date | None = None) -> int:
    """기간 밖 행 삭제."""
    win_start, win_end = retention_window(reference)
    async with get_db_session() as session:
        result = await session.execute(
            delete(ManagedRoomEvent).where(
                or_(
                    ManagedRoomEvent.start_time < win_start.isoformat(),
                    ManagedRoomEvent.start_time >= win_end.isoformat(),
                )
            )
        )
        return int(result.rowcount or 0)


async def list_events_for_room_day(
    room_email: str,
    target: date,
    *,
    active_only: bool = True,
) -> list[ManagedRoomEvent]:
    day = target.isoformat()
    conditions = [
        ManagedRoomEvent.room_email == room_email.strip().lower(),
        ManagedRoomEvent.start_time < f"{day}T23:59:59",
        ManagedRoomEvent.end_time > f"{day}T00:00:00",
    ]
    if active_only:
        conditions.append(ManagedRoomEvent.status == _ACTIVE)

    async with get_db_session() as session:
        stmt = (
            select(ManagedRoomEvent)
            .where(and_(*conditions))
            .order_by(ManagedRoomEvent.start_time.asc())
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def list_events_between(
    start_date: date,
    end_date_exclusive: date,
    *,
    room_email: str | None = None,
    active_only: bool = True,
) -> list[ManagedRoomEvent]:
    conditions = [
        ManagedRoomEvent.start_time >= start_date.isoformat(),
        ManagedRoomEvent.start_time < end_date_exclusive.isoformat(),
    ]
    if room_email:
        conditions.append(
            ManagedRoomEvent.room_email == room_email.strip().lower(),
        )
    if active_only:
        conditions.append(ManagedRoomEvent.status == _ACTIVE)

    async with get_db_session() as session:
        stmt = (
            select(ManagedRoomEvent)
            .where(and_(*conditions))
            .order_by(ManagedRoomEvent.start_time.asc())
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


def slot_conflicts_with_rows(
    rows: list[ManagedRoomEvent],
    start_time: str,
    end_time: str,
) -> ManagedRoomEvent | None:
    events = [event_to_graph_dict(r) for r in rows]
    conflict = graph.slot_conflicts_with_events(events, start_time, end_time)
    if not conflict:
        return None
    cid = str(conflict.get("id") or "")
    for row in rows:
        if row.outlook_event_id == cid:
            return row
    return rows[0] if rows else None


async def list_owned_events(
    organizer_email: str,
    *,
    date_str: str | None = None,
    start_date: date | None = None,
    end_date_exclusive: date | None = None,
    limit: int = 30,
) -> list[ManagedRoomEvent]:
    """list_mine — 본인 주최 일정."""
    org = organizer_email.strip().lower()
    if not org:
        return []

    conditions = [
        ManagedRoomEvent.status == _ACTIVE,
        ManagedRoomEvent.organizer_email == org,
    ]
    day = normalize_date_filter(date_str)
    if day:
        conditions.append(ManagedRoomEvent.start_time.startswith(day))
    elif start_date and end_date_exclusive:
        conditions.append(ManagedRoomEvent.start_time >= start_date.isoformat())
        conditions.append(ManagedRoomEvent.start_time < end_date_exclusive.isoformat())

    async with get_db_session() as session:
        stmt = (
            select(ManagedRoomEvent)
            .where(and_(*conditions))
            .order_by(ManagedRoomEvent.start_time.asc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def list_attended_events(
    attendee_email: str,
    *,
    date_str: str | None = None,
    start_date: date | None = None,
    end_date_exclusive: date | None = None,
    exclude_organizer: str | None = None,
    limit: int = 20,
) -> list[ManagedRoomEvent]:
    """list_mine — 참석(비주최) 일정."""
    email = attendee_email.strip().lower()
    if not email:
        return []

    conditions = [
        ManagedRoomEvent.status == _ACTIVE,
        ManagedRoomEvent.organizer_email != email,
    ]
    if exclude_organizer:
        pass
    day = normalize_date_filter(date_str)
    if day:
        conditions.append(ManagedRoomEvent.start_time.startswith(day))
    elif start_date and end_date_exclusive:
        conditions.append(ManagedRoomEvent.start_time >= start_date.isoformat())
        conditions.append(ManagedRoomEvent.start_time < end_date_exclusive.isoformat())

    async with get_db_session() as session:
        stmt = (
            select(ManagedRoomEvent)
            .where(and_(*conditions))
            .order_by(ManagedRoomEvent.start_time.asc())
            .limit(limit * 3)
        )
        result = await session.execute(stmt)
        rows = list(result.scalars().all())

    filtered: list[ManagedRoomEvent] = []
    for row in rows:
        attendees = [str(e).lower() for e in (row.attendee_emails or [])]
        if email in attendees:
            filtered.append(row)
        if len(filtered) >= limit:
            break
    return filtered


def _event_date_conditions(
    *,
    date_str: str | None = None,
    start_date: date | None = None,
    end_date_exclusive: date | None = None,
) -> list[Any]:
    conditions: list[Any] = [ManagedRoomEvent.status == _ACTIVE]
    day = normalize_date_filter(date_str)
    if day:
        conditions.append(ManagedRoomEvent.start_time.startswith(day))
    elif start_date and end_date_exclusive:
        conditions.append(ManagedRoomEvent.start_time >= start_date.isoformat())
        conditions.append(ManagedRoomEvent.start_time < end_date_exclusive.isoformat())
    return conditions


async def list_schedule_events(
    *,
    person_emails: list[str] | None = None,
    organizer_name_needles: list[str] | None = None,
    date_str: str | None = None,
    start_date: date | None = None,
    end_date_exclusive: date | None = None,
    limit: int = 40,
) -> list[tuple[ManagedRoomEvent, str]]:
    """주최·참석 회의실 일정 (본인·타인 공통, managed_room_events projection)."""
    emails = [e.strip().lower() for e in (person_emails or []) if (e or "").strip()]
    needles = [n.strip() for n in (organizer_name_needles or []) if (n or "").strip()]
    if not emails and not needles:
        return []

    person_filters: list[Any] = []
    for email in emails:
        person_filters.append(ManagedRoomEvent.organizer_email == email)
        person_filters.append(ManagedRoomEvent.attendee_emails.contains([email]))
    for needle in needles:
        person_filters.append(ManagedRoomEvent.organizer_name.ilike(f"%{needle}%"))

    conditions = [
        *_event_date_conditions(
            date_str=date_str,
            start_date=start_date,
            end_date_exclusive=end_date_exclusive,
        ),
        or_(*person_filters),
    ]

    async with get_db_session() as session:
        stmt = (
            select(ManagedRoomEvent)
            .where(and_(*conditions))
            .order_by(ManagedRoomEvent.start_time.asc())
            .limit(limit * 2)
        )
        result = await session.execute(stmt)
        rows = list(result.scalars().all())

    email_set = set(emails)
    out: list[tuple[ManagedRoomEvent, str]] = []
    seen: set[uuid.UUID] = set()
    for row in rows:
        if row.id in seen:
            continue
        seen.add(row.id)
        org_email = (row.organizer_email or "").strip().lower()
        attendees = [str(e).lower() for e in (row.attendee_emails or [])]
        if email_set and org_email in email_set:
            role = "owned"
        elif email_set and any(e in email_set for e in attendees):
            role = "attended"
        else:
            role = "owned"
        out.append((row, role))
        if len(out) >= limit:
            break
    return out


async def get_event_by_id(
    booking_id: str,
    *,
    organizer_email: str | None = None,
    active_only: bool = True,
) -> ManagedRoomEvent | None:
    if not is_valid_booking_uuid(booking_id):
        return None
    try:
        bid = uuid.UUID(booking_id.strip())
    except ValueError:
        return None

    conditions = [ManagedRoomEvent.id == bid]
    if active_only:
        conditions.append(ManagedRoomEvent.status == _ACTIVE)
    if organizer_email:
        conditions.append(
            ManagedRoomEvent.organizer_email == organizer_email.strip().lower(),
        )

    async with get_db_session() as session:
        result = await session.execute(
            select(ManagedRoomEvent).where(and_(*conditions))
        )
        return result.scalar_one_or_none()


async def resolve_owned_event(
    *,
    organizer_email: str | None = None,
    booking_id: str | None = None,
    event_id: str | None = None,
    room_name: str | None = None,
    subject: str | None = None,
    start_time: str | None = None,
    old_start_time: str | None = None,
    date_str: str | None = None,
    prefer_recent: bool = False,
) -> tuple[ManagedRoomEvent | None, str | None]:
    """cancel/modify — 본인 주최 이벤트 1건."""
    org = (organizer_email or "").strip().lower()
    if not org:
        return None, "예약 조회에 요청자 이메일이 필요합니다."

    if booking_id and is_valid_booking_uuid(booking_id):
        row = await get_event_by_id(booking_id, organizer_email=org)
        if row:
            return row, None

    org_eid = _normalize_event_id(event_id)
    if is_usable_event_id(org_eid):
        async with get_db_session() as session:
            result = await session.execute(
                select(ManagedRoomEvent).where(
                    and_(
                        ManagedRoomEvent.status == _ACTIVE,
                        ManagedRoomEvent.organizer_email == org,
                        or_(
                            ManagedRoomEvent.organizer_event_id == org_eid,
                            ManagedRoomEvent.outlook_event_id == org_eid,
                        ),
                    )
                )
            )
            row = result.scalar_one_or_none()
        if row:
            return row, None
        return None, "본인이 주최한 일정만 취소·변경할 수 있습니다."

    conditions = [
        ManagedRoomEvent.status == _ACTIVE,
        ManagedRoomEvent.organizer_email == org,
    ]
    room = (room_name or "").strip().lower()
    subj = (subject or "").strip()
    start = (old_start_time or start_time or "").strip()
    day = normalize_date_filter(date_str) or ""

    if room:
        conditions.append(
            or_(
                ManagedRoomEvent.room_name.ilike(f"%{room}%"),
                ManagedRoomEvent.room_display.ilike(f"%{room}%"),
            )
        )
    if subj:
        conditions.append(
            or_(
                ManagedRoomEvent.subject.ilike(f"%{subj}%"),
                ManagedRoomEvent.event_subject.ilike(f"%{subj}%"),
            )
        )
    if start:
        conditions.append(ManagedRoomEvent.start_time.startswith(start[:16]))
    elif day:
        conditions.append(ManagedRoomEvent.start_time.startswith(day))

    async with get_db_session() as session:
        stmt = (
            select(ManagedRoomEvent)
            .where(and_(*conditions))
            .order_by(desc(ManagedRoomEvent.synced_at))
            .limit(5)
        )
        result = await session.execute(stmt)
        rows = list(result.scalars().all())

    if not rows and prefer_recent:
        async with get_db_session() as session:
            result = await session.execute(
                select(ManagedRoomEvent)
                .where(
                    and_(
                        ManagedRoomEvent.status == _ACTIVE,
                        ManagedRoomEvent.organizer_email == org,
                    )
                )
                .order_by(desc(ManagedRoomEvent.synced_at))
                .limit(1)
            )
            row = result.scalar_one_or_none()
            if row:
                return row, None

    if rows:
        return rows[0], None

    return None, (
        "취소·변경할 예약을 찾지 못했습니다. "
        "list_mine으로 booking_id를 확인하거나 회의실·날짜·시간을 알려주세요."
    )


async def resolve_organizer_event_id(
    *,
    organizer_email: str | None = None,
    booking_id: str | None = None,
    event_id: str | None = None,
    room_name: str | None = None,
    subject: str | None = None,
    start_time: str | None = None,
    old_start_time: str | None = None,
    date_str: str | None = None,
    prefer_recent: bool = False,
) -> tuple[str | None, ManagedRoomEvent | None, str | None]:
    row, err = await resolve_owned_event(
        organizer_email=organizer_email,
        booking_id=booking_id,
        event_id=event_id,
        room_name=room_name,
        subject=subject,
        start_time=start_time,
        old_start_time=old_start_time,
        date_str=date_str,
        prefer_recent=prefer_recent,
    )
    if err:
        return None, None, err
    if not row:
        return None, None, "취소할 예약을 찾지 못했습니다."
    org_eid = (row.organizer_event_id or "").strip()
    if not org_eid or org_eid.startswith("pending:"):
        return None, row, (
            "주최자 일정 ID가 아직 동기화되지 않았습니다. "
            "잠시 후 다시 시도해 주세요."
        )
    return org_eid, row, None


def format_event_line(index: int, event: ManagedRoomEvent) -> str:
    start = event.start_time[:16].replace("T", " ")
    end = event.end_time[11:16]
    return (
        f"{index}. booking_id={event.id} | {event.room_display} | "
        f"{event.subject} | {start}~{end}"
    )


def event_lookup_hint(event: ManagedRoomEvent | None) -> str:
    if not event:
        return ""
    start = event.start_time[:16].replace("T", " ")
    end = event.end_time[11:16]
    return (
        f"(처리 대상: {event.room_display} | {event.subject} | "
        f"{start}~{end})"
    )


async def delete_event_by_id(event_id: uuid.UUID) -> None:
    """Graph 취소 직후 projection 즉시 제거 (webhook 보조)."""
    async with get_db_session() as session:
        await session.execute(
            delete(ManagedRoomEvent).where(ManagedRoomEvent.id == event_id)
        )


async def update_event_after_modify(
    event_id: uuid.UUID,
    *,
    start_time: str,
    end_time: str,
    subject: str,
    event_subject: str,
    time_changed: bool,
) -> None:
    values: dict[str, Any] = {
        "start_time": start_time.strip(),
        "end_time": end_time.strip(),
        "subject": subject.strip(),
        "event_subject": event_subject.strip(),
        "synced_at": datetime.now(timezone.utc),
    }
    if time_changed:
        values["reminder_sent_at"] = None

    async with get_db_session() as session:
        await session.execute(
            update(ManagedRoomEvent)
            .where(ManagedRoomEvent.id == event_id)
            .values(**values)
        )


async def set_event_reminder(
    event_id: uuid.UUID,
    *,
    reminder_minutes_before: int,
    slack_channel_id: str | None = None,
    bot_slack_user_id: str | None = None,
) -> ManagedRoomEvent | None:
    values: dict[str, Any] = {
        "reminder_minutes_before": reminder_minutes_before,
        "reminder_sent_at": None,
    }
    ch = (slack_channel_id or "").strip()
    if ch:
        values["slack_channel_id"] = ch
    uid = (bot_slack_user_id or "").strip()
    if uid:
        values["bot_slack_user_id"] = uid

    async with get_db_session() as session:
        await session.execute(
            update(ManagedRoomEvent)
            .where(
                and_(
                    ManagedRoomEvent.id == event_id,
                    ManagedRoomEvent.status == _ACTIVE,
                )
            )
            .values(**values)
        )
        result = await session.execute(
            select(ManagedRoomEvent).where(ManagedRoomEvent.id == event_id)
        )
        return result.scalar_one_or_none()


async def fetch_pending_reminder_events() -> list[ManagedRoomEvent]:
    async with get_db_session() as session:
        stmt = (
            select(ManagedRoomEvent)
            .where(
                ManagedRoomEvent.status == _ACTIVE,
                ManagedRoomEvent.reminder_minutes_before.isnot(None),
                ManagedRoomEvent.reminder_sent_at.is_(None),
            )
            .order_by(ManagedRoomEvent.start_time.asc())
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def mark_reminder_sent(event_id: uuid.UUID) -> None:
    now = datetime.now(timezone.utc)
    async with get_db_session() as session:
        await session.execute(
            update(ManagedRoomEvent)
            .where(ManagedRoomEvent.id == event_id)
            .values(reminder_sent_at=now)
        )


async def backfill_slack_channel(event_id: uuid.UUID, slack_channel_id: str) -> None:
    ch = (slack_channel_id or "").strip()
    if not ch:
        return
    async with get_db_session() as session:
        await session.execute(
            update(ManagedRoomEvent)
            .where(
                and_(
                    ManagedRoomEvent.id == event_id,
                    or_(
                        ManagedRoomEvent.slack_channel_id.is_(None),
                        ManagedRoomEvent.slack_channel_id == "",
                    ),
                )
            )
            .values(slack_channel_id=ch)
        )


def find_room_outlook_event_id(
    headers: dict,
    room_email: str,
    *,
    event_subject: str,
    start_time: str,
    day: date | None = None,
) -> str | None:
    """book 직후 room 캘린더에서 outlook event id 매칭."""
    target_day = day or date.fromisoformat(start_time[:10])
    events = graph.fetch_calendar_view(headers, room_email, day=target_day)
    start_prefix = start_time[:16]
    for ev in events:
        ev_start = str((ev.get("start") or {}).get("dateTime") or "")[:16]
        ev_subj = str(ev.get("subject") or "").strip()
        if ev_start == start_prefix and ev_subj == event_subject.strip():
            return str(ev.get("id") or "").strip() or None
    for ev in events:
        ev_start = str((ev.get("start") or {}).get("dateTime") or "")[:16]
        if ev_start == start_prefix:
            return str(ev.get("id") or "").strip() or None
    return None
