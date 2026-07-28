"""본인 Outlook 일정 — Graph 직접 조회·생성·변경·취소 (DB 동기화 없음)."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.services.date_range import apply_date_range_to_tool_args
from app.services.outlook_room import ms_graph_room as graph
from app.services.outlook_room.schedule_reserve import (
    default_end_time_one_hour,
    parse_time_range_from_query,
)

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

_NO_EMAIL_MSG = (
    "Slack 프로필에 회사 이메일이 없어 Outlook 일정을 처리할 수 없습니다. "
    "Slack 프로필에 이메일을 등록한 뒤 다시 시도해 주세요."
)

PERSONAL_WRITE_ACTIONS: tuple[str, ...] = ("create", "modify", "cancel")
DEFAULT_LIST_RANGE_DAYS = 7
DEFAULT_SUBJECT = "회의"


def _now_kst() -> datetime:
    return datetime.now(KST)


def _today() -> date:
    return _now_kst().date()


def _get_headers() -> dict:
    return graph.build_api_headers(graph.get_valid_app_token())


def _parse_query_range(
    date_str: str | None,
    end_date_str: str | None,
) -> tuple[date, date, bool]:
    """(start, end_exclusive, explicit_single_day)."""
    day = None
    if date_str:
        try:
            day = date.fromisoformat(str(date_str).strip()[:10])
        except ValueError:
            day = None
    end_day = None
    if end_date_str:
        try:
            end_day = date.fromisoformat(str(end_date_str).strip()[:10])
        except ValueError:
            end_day = None

    if day and end_day and end_day >= day:
        return day, end_day + timedelta(days=1), day == end_day
    if day:
        return day, day + timedelta(days=1), True

    start = _today()
    return start, start + timedelta(days=DEFAULT_LIST_RANGE_DAYS), False


def _fmt_event_line(index: int, event: dict[str, Any]) -> str:
    if graph.event_is_private(event):
        start, end = graph._event_bounds(event)
        start_l = start[:16].replace("T", " ") if start else "?"
        end_l = end[11:16] if end else "?"
        return f"{index}. (비공개) | {start_l}~{end_l}"

    eid = str(event.get("id") or "").strip()
    subject = str(event.get("subject") or "(제목 없음)").strip()
    start, end = graph._event_bounds(event)
    start_l = start[:16].replace("T", " ") if start else "?"
    end_l = end[11:16] if end else "?"
    loc = str((event.get("location") or {}).get("displayName") or "").strip()
    loc_part = f" | 장소: {loc}" if loc else ""
    return f"{index}. event_id={eid} | {subject} | {start_l}~{end_l}{loc_part}"


def _attendee_emails_from_args(attendees: list[Any] | None) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in attendees or []:
        if isinstance(item, str):
            email = item.strip()
            name = email.split("@")[0] if "@" in email else email
        elif isinstance(item, dict):
            email = str(item.get("email") or item.get("address") or "").strip()
            name = str(item.get("name") or "").strip() or (
                email.split("@")[0] if email else ""
            )
        else:
            continue
        key = email.lower()
        if not email or "@" not in email or key in seen:
            continue
        seen.add(key)
        out.append({"email": email, "name": name})
    return out


def _build_personal_attendees(
    required: list[dict[str, str]],
    *,
    organizer_email: str,
) -> list[dict]:
    org = organizer_email.strip().lower()
    payload: list[dict] = []
    seen: set[str] = set()
    for item in required:
        email = str(item.get("email") or "").strip()
        name = str(item.get("name") or "").strip()
        key = email.lower()
        if not email or key == org or key in seen:
            continue
        seen.add(key)
        payload.append({
            "emailAddress": {
                "address": email,
                "name": name or email.split("@")[0],
            },
            "type": "required",
        })
    return payload


def normalize_personal_tool_args(
    args: dict[str, Any],
    query: str = "",
) -> dict[str, Any]:
    """manage_personal_schedule 인자 정규화."""
    out = dict(args)
    action = str(out.get("action") or "list").strip()
    if action == "book":
        action = "create"
        out["action"] = "create"

    if action == "list":
        out = apply_date_range_to_tool_args(out, query)
        return out

    if action == "create":
        subj = str(out.get("subject") or "").strip()
        if not subj:
            out["subject"] = DEFAULT_SUBJECT
        start = str(out.get("start_time") or "").strip()
        end = str(out.get("end_time") or "").strip()
        if not start:
            parsed_start, parsed_end, _ = parse_time_range_from_query(query)
            if parsed_start:
                out["start_time"] = parsed_start
                start = parsed_start
            if parsed_end and not end:
                out["end_time"] = parsed_end
                end = parsed_end
        if start and not end:
            out["end_time"] = default_end_time_one_hour(start)
        return out

    return out


def is_personal_write_action(action: str) -> bool:
    return str(action or "").strip() in PERSONAL_WRITE_ACTIONS


def personal_write_once_message() -> str:
    return (
        "이번 요청에서는 Outlook 일정 쓰기(생성·변경·취소)를 이미 한 번 처리했습니다. "
        "추가 변경이 필요하면 이어서 말씀해 주세요."
    )


async def list_personal_schedule(
    *,
    organizer_email: str,
    date_str: str | None = None,
    end_date_str: str | None = None,
) -> str:
    """본인 캘린더 calendarView 조회 (Graph 직접, DB 없음)."""
    email = (organizer_email or "").strip()
    if not email or "@" not in email:
        return _NO_EMAIL_MSG

    start_date, end_exclusive, explicit_day = _parse_query_range(date_str, end_date_str)
    range_label = (
        start_date.isoformat()
        if explicit_day
        else f"{start_date.isoformat()}~{(end_exclusive - timedelta(days=1)).isoformat()}"
    )

    loop = asyncio.get_running_loop()
    try:
        headers = await loop.run_in_executor(None, _get_headers)
        events = await loop.run_in_executor(
            None,
            lambda: graph.fetch_calendar_view_between(
                headers,
                email,
                start_date=start_date,
                end_date_exclusive=end_exclusive,
            ),
        )
    except Exception as exc:
        logger.exception("[PersonalSchedule] list failed email=%s", email)
        return f"Outlook 일정 조회 중 오류가 발생했습니다: {exc}"

    events = sorted(
        events,
        key=lambda e: str((e.get("start") or {}).get("dateTime") or ""),
    )
    if not events:
        return f"해당 기간({range_label}) 내 Outlook 일정이 없습니다."

    lines = [f"내 Outlook 일정 ({range_label}, {len(events)}건):"]
    for i, event in enumerate(events, start=1):
        lines.append(_fmt_event_line(i, event))
    return "\n".join(lines)


def _match_event(
    events: list[dict],
    *,
    event_id: str | None = None,
    subject: str | None = None,
    start_time: str | None = None,
    date_str: str | None = None,
) -> tuple[dict | None, str | None]:
    eid = (event_id or "").strip()
    if eid:
        for event in events:
            if str(event.get("id") or "").strip() == eid:
                return event, None
        return None, "지정한 event_id에 해당하는 일정을 찾지 못했습니다."

    subj = (subject or "").strip().lower()
    start = (start_time or "").strip()[:16]
    day = (date_str or "").strip()[:10]
    if start and len(start) == 10:
        day = start
        start = ""

    candidates: list[dict] = []
    for event in events:
        if graph.event_is_private(event):
            continue
        ev_start, _ = graph._event_bounds(event)
        ev_subj = str(event.get("subject") or "").strip().lower()
        if day and not ev_start.startswith(day):
            continue
        if start and ev_start[:16] != start[:16]:
            continue
        if subj and subj not in ev_subj:
            continue
        if not subj and not start and not day:
            continue
        candidates.append(event)

    if not candidates:
        return None, (
            "일정을 특정하지 못했습니다. "
            "제목·날짜·시작 시각을 알려 주시거나 목록에서 대상을 지정해 주세요."
        )
    if len(candidates) > 1:
        preview = "; ".join(
            f"{str(e.get('subject') or '')[:40]} "
            f"{str((e.get('start') or {}).get('dateTime') or '')[:16]}"
            for e in candidates[:5]
        )
        return None, f"일정이 여러 건 일치합니다. 더 구체적으로 지정해 주세요: {preview}"
    return candidates[0], None


async def _load_events_for_hints(
    email: str,
    *,
    date_str: str | None,
    start_time: str | None,
    end_date_str: str | None = None,
) -> list[dict]:
    day_hint = (date_str or "").strip()[:10]
    if not day_hint and start_time:
        day_hint = str(start_time).strip()[:10]
    start_d, end_ex, _ = _parse_query_range(day_hint or None, end_date_str)
    if not day_hint:
        start_d = _today() - timedelta(days=3)
        end_ex = _today() + timedelta(days=DEFAULT_LIST_RANGE_DAYS)

    loop = asyncio.get_running_loop()
    headers = await loop.run_in_executor(None, _get_headers)
    return await loop.run_in_executor(
        None,
        lambda: graph.fetch_calendar_view_between(
            headers,
            email,
            start_date=start_d,
            end_date_exclusive=end_ex,
        ),
    )


async def create_personal_event(
    *,
    organizer_email: str,
    subject: str,
    start_time: str,
    end_time: str,
    attendees: list[Any] | None = None,
    location: str | None = None,
) -> str:
    email = (organizer_email or "").strip()
    if not email or "@" not in email:
        return _NO_EMAIL_MSG

    subj = (subject or "").strip() or DEFAULT_SUBJECT
    start = (start_time or "").strip()
    end = (end_time or "").strip() or default_end_time_one_hour(start)
    if not start:
        return "시작 시각을 알려 주세요. (예: 2026-07-28T15:00:00)"

    try:
        end_dt = datetime.fromisoformat(end[:19])
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=KST)
        if end_dt <= _now_kst():
            return "과거 시간으로는 일정을 만들 수 없습니다."
    except ValueError:
        return "시간 형식이 올바르지 않습니다 (예: 2026-07-28T15:00:00)."

    attendee_payload = _build_personal_attendees(
        _attendee_emails_from_args(attendees),
        organizer_email=email,
    )
    payload: dict[str, Any] = {
        "subject": subj,
        "start": {"dateTime": start[:19], "timeZone": "Korea Standard Time"},
        "end": {"dateTime": end[:19], "timeZone": "Korea Standard Time"},
    }
    if attendee_payload:
        payload["attendees"] = attendee_payload
    loc = (location or "").strip()
    if loc:
        payload["location"] = {"displayName": loc}

    loop = asyncio.get_running_loop()
    try:
        headers = await loop.run_in_executor(None, _get_headers)
        created = await loop.run_in_executor(
            None,
            lambda: graph._create_calendar_event(headers, email, payload),
        )
    except Exception as exc:
        logger.exception("[PersonalSchedule] create failed")
        return f"Outlook 일정 생성 중 오류: {exc}"

    eid = str(created.get("id") or "").strip()
    start_l = start[:16].replace("T", " ")
    end_l = end[11:16]
    return (
        f"Outlook 일정을 등록했습니다.\n"
        f"제목: {subj}\n"
        f"일시: {start_l} ~ {end_l}\n"
        f"event_id={eid}"
    )


async def modify_personal_event(
    *,
    organizer_email: str,
    event_id: str | None = None,
    subject: str | None = None,
    date_str: str | None = None,
    start_time: str | None = None,
    new_subject: str | None = None,
    new_start_time: str | None = None,
    new_end_time: str | None = None,
    attendees: list[Any] | None = None,
) -> str:
    email = (organizer_email or "").strip()
    if not email or "@" not in email:
        return _NO_EMAIL_MSG

    try:
        events = await _load_events_for_hints(
            email,
            date_str=date_str,
            start_time=start_time or new_start_time,
        )
    except Exception as exc:
        return f"Outlook 일정 조회 중 오류: {exc}"

    matched, err = _match_event(
        events,
        event_id=event_id,
        subject=subject,
        start_time=start_time,
        date_str=date_str,
    )
    if err:
        return err
    assert matched is not None

    eid = str(matched.get("id") or "").strip()
    old_start, old_end = graph._event_bounds(matched)
    old_subj = str(matched.get("subject") or "").strip()

    patch: dict[str, Any] = {}
    ns = (new_subject or "").strip()
    nstart = (new_start_time or "").strip()
    nend = (new_end_time or "").strip()
    if ns:
        patch["subject"] = ns
    if nstart:
        patch["start"] = {
            "dateTime": nstart[:19],
            "timeZone": "Korea Standard Time",
        }
        if not nend:
            nend = default_end_time_one_hour(nstart)
    if nend:
        patch["end"] = {
            "dateTime": nend[:19],
            "timeZone": "Korea Standard Time",
        }
    attendee_list = _attendee_emails_from_args(attendees)
    if attendees is not None:
        patch["attendees"] = _build_personal_attendees(
            attendee_list,
            organizer_email=email,
        )

    if not patch:
        return "변경할 항목(제목·시간·참석자)을 알려 주세요."

    loop = asyncio.get_running_loop()
    try:
        headers = await loop.run_in_executor(None, _get_headers)
        await loop.run_in_executor(
            None,
            lambda: graph.update_calendar_event(headers, email, eid, patch),
        )
    except Exception as exc:
        logger.exception("[PersonalSchedule] modify failed")
        return f"Outlook 일정 변경 중 오류: {exc}"

    lines = [
        "Outlook 일정을 변경했습니다.",
        f"대상: {old_subj} | {old_start[:16].replace('T', ' ')}~{old_end[11:16]}",
    ]
    if ns:
        lines.append(f"제목: {old_subj} → {ns}")
    if nstart or nend:
        lines.append(
            f"시간: {old_start[:16].replace('T', ' ')}~{old_end[11:16]} → "
            f"{(nstart or old_start)[:16].replace('T', ' ')}~"
            f"{(nend or old_end)[11:16]}"
        )
    if attendees is not None:
        names = [a.get("email") or "" for a in attendee_list]
        lines.append(f"참석자: {', '.join(names) if names else '(없음)'}")
    return "\n".join(lines)


async def cancel_personal_event(
    *,
    organizer_email: str,
    event_id: str | None = None,
    subject: str | None = None,
    date_str: str | None = None,
    start_time: str | None = None,
) -> str:
    email = (organizer_email or "").strip()
    if not email or "@" not in email:
        return _NO_EMAIL_MSG

    try:
        events = await _load_events_for_hints(
            email,
            date_str=date_str,
            start_time=start_time,
        )
    except Exception as exc:
        return f"Outlook 일정 조회 중 오류: {exc}"

    matched, err = _match_event(
        events,
        event_id=event_id,
        subject=subject,
        start_time=start_time,
        date_str=date_str,
    )
    if err:
        return err
    assert matched is not None

    eid = str(matched.get("id") or "").strip()
    old_start, old_end = graph._event_bounds(matched)
    old_subj = str(matched.get("subject") or "").strip()

    loop = asyncio.get_running_loop()
    try:
        headers = await loop.run_in_executor(None, _get_headers)
        await loop.run_in_executor(
            None,
            lambda: graph.delete_calendar_event(headers, email, eid),
        )
    except Exception as exc:
        logger.exception("[PersonalSchedule] cancel failed")
        return f"Outlook 일정 취소 중 오류: {exc}"

    return (
        "Outlook 일정을 취소했습니다.\n"
        f"대상: {old_subj} | "
        f"{old_start[:16].replace('T', ' ')}~{old_end[11:16]}"
    )
