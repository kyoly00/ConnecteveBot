"""
ConnBot — 회의실 예약 서비스.

Microsoft Graph API(services/outlook_room/ms_graph_room.py)를 async 래퍼로 감싸
Turn2 LLM에 전달할 텍스트 컨텍스트를 반환한다.

지원 액션:
  check      — 특정 날짜 회의실 예약 현황 조회
  check_all  — 전체 회의실 occupied·슬롯 가용 조회
  list      — 회의 일정 조회 (person_name 생략=본인, booking_id 포함)
  book       — 회의실 예약 (요청자 email 주최 + resource 초대)
  cancel     — 본인 예약 취소
  modify     — 본인 예약 변경 (Graph PATCH)
  replace    — 본인 예약 취소 후 재예약
  set_reminder — 예약 N분 전 Slack 리마인더 설정
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any
import uuid
from zoneinfo import ZoneInfo

from app.services.date_range import apply_date_range_to_tool_args
from app.services.outlook_room import ms_graph_room as graph
from app.services.outlook_room.attendee_resolver import lookup_user_by_name, resolve_attendees
from app.services.outlook_room.managed_room_events import (
    DEFAULT_LIST_RANGE_DAYS,
    delete_event_by_id,
    event_lookup_hint as booking_lookup_hint,
    event_to_graph_dict,
    find_room_outlook_event_id,
    format_event_line as format_booking_line,
    is_valid_booking_uuid,
    list_attended_events,
    list_events_for_room_day,
    list_owned_events,
    list_schedule_events,
    resolve_organizer_event_id as resolve_outlook_event_id,
    resolve_owned_event as resolve_owned_booking,
    set_event_reminder as set_booking_reminder,
    slot_conflicts_with_rows,
    update_event_after_modify as update_room_booking_after_modify,
    upsert_after_bot_book,
    normalize_date_filter,
)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 공개 상수
# ─────────────────────────────────────────────────────────────

ROOM_NAMES: list[str] = list(graph.ROOM_EMAIL_MAP.keys())

# 질문에서 회의실 예약 의도를 감지할 키워드
ROOM_KEYWORDS: tuple[str, ...] = (
    "회의",
    "미팅",
    "spine",
    "스파인",
    "스핀",
    "femur",
    "피머",
    "펌머",
    "퍼머",    
    "atlas",
    "아틀라스",
    "connechall",
    "코넥홀",
    "예약",
    "룸",
    "방",
    "meeting",
    "리마인더",
    "알림",
)

_LIST_MINE_SELF_MARKERS: tuple[str, ...] = (
    "내 회의",
    "나 회의",
    "제 회의",
    "본인 회의",
    "내 일정",
    "나 일정",
    "내 예약",
)

_NO_EMAIL_MSG = (
    "Slack 프로필에 회사 이메일이 없어 예약할 수 없습니다. "
    "Slack 프로필에 이메일을 등록한 뒤 다시 시도해 주세요."
)

KST = ZoneInfo("Asia/Seoul")

# booking_id로 대상 예약을 특정해야 하는 manage_room_schedule 액션
BOOKING_TARGET_ACTIONS: tuple[str, ...] = (
    "cancel",
    "modify",
    "replace",
    "set_reminder",
)

# Outlook·DB에 쓰기가 발생하는 액션 — 턴당 1회만 허용
ROOM_WRITE_ACTIONS: tuple[str, ...] = (
    "book",
    "cancel",
    "modify",
    "replace",
)

_ROOM_WRITE_ONCE_MSG = (
    "한 번의 요청에서는 회의실 예약·변경·취소 중 하나만 처리할 수 있습니다. "
    "나머지 작업은 별도로 요청해 주세요."
)

_BOOKING_TARGET_ACTIONS_LABEL = "취소·변경·재예약·리마인더"

def is_room_write_action(action: str) -> bool:
    return str(action or "").strip() in ROOM_WRITE_ACTIONS


def room_write_once_message() -> str:
    return _ROOM_WRITE_ONCE_MSG


def default_end_time_one_hour(start_time: str) -> str:
    """시작 시각 기준 기본 1시간 종료 시각."""
    start_dt = datetime.fromisoformat(start_time[:19])
    return (start_dt + timedelta(hours=1)).isoformat()[:19]


def resolve_booking_room(
    room_name: str | None,
    query: str = "",
    conversation_history: list[dict[str, str]] | None = None,
) -> str | None:
    """
    예약 대상 회의실 — Turn1 LLM이 tool room_name으로 넘긴 값만 사용.

    질문 텍스트에서 회의실명을 추출하지 않는다. follow-up은 직전 tool 맥락만 참고.
    """
    _ = query
    explicit = (room_name or "").strip()
    if explicit:
        return explicit
    if conversation_history:
        from_tool = extract_room_name_from_tool_history(conversation_history)
        if from_tool:
            return from_tool
    return None


def room_name_maps_to_managed(room_name: str) -> bool:
    """resolve_room 결과가 관리 대상 회의실인지."""
    _, email = graph.resolve_room(room_name)
    return graph.is_managed_room_email(email)


def enrich_book_tool_args(
    args: dict[str, Any],
    query: str,
    conversation_history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """book 인자 — LLM room_name·시각(종료 없으면 +1h) 보완."""
    out = dict(args)
    room = resolve_booking_room(
        str(out.get("room_name") or "").strip() or None,
        query,
        conversation_history,
    )
    if room:
        out["room_name"] = room

    start = str(out.get("start_time") or "").strip()
    end = str(out.get("end_time") or "").strip()
    if not start:
        parsed_start, parsed_end, _ = parse_time_range_from_query(query)
        if parsed_start:
            start = parsed_start
        if parsed_end and not end:
            end = parsed_end
    if start and not end:
        end = default_end_time_one_hour(start)
    if start:
        out["start_time"] = start
    if end:
        out["end_time"] = end

    subj = str(out.get("subject") or "").strip()
    if not subj:
        out["subject"] = default_book_subject(query, None)
    return out


def peek_manage_room_action(tool_args: dict[str, Any]) -> str:
    """manage_room_schedule tool 인자에서 action만 추출."""
    return str(tool_args.get("action") or "check").strip()

_REMINDER_HINT_GENERIC = (
    "\n💡 챗봇 리마인더가 필요하면 「15분 전 리마인더 설정해」처럼 말씀해 주세요."
)


def _reminder_hint(booking_id: uuid.UUID | str | None = None) -> str:
    if not booking_id:
        return _REMINDER_HINT_GENERIC
    return (
        f"\n💡 챗봇 리마인더가 필요하면 "
        f"「{booking_id} 15분 전 리마인더 설정해」처럼 말씀해 주세요."
    )


# ─────────────────────────────────────────────────────────────
# 내부 헬퍼
# ─────────────────────────────────────────────────────────────

def _get_headers() -> dict:
    """Graph API Authorization 헤더 (blocking — executor에서 호출)."""
    return graph.build_api_headers(graph.get_valid_app_token())


def _parse_date(date_str: str | None) -> date:
    """'2026-06-18', '오늘', None → date 객체."""
    if not date_str or date_str in ("오늘", "today"):
        return date.today()
    if date_str in ("내일", "tomorrow"):
        return date.today() + timedelta(days=1)
    if date_str in ("어제", "yesterday"):
        return date.today() - timedelta(days=1)
    try:
        return date.fromisoformat(date_str[:10])
    except ValueError:
        return date.today()


def _parse_query_range(
    date_str: str | None,
    end_date_str: str | None = None,
) -> tuple[date, date, bool]:
    """list_mine용 범위.

  - date만: 특정 하루(어제·오늘·내일·ISO 날짜)
  - date+end_date: 기간
  - 둘 다 없음: 오늘부터 7일(기간)
    """
    start_s = str(date_str or "").strip()
    end_s = str(end_date_str or "").strip()

    if not start_s and not end_s:
        start = date.today()
        return start, start + timedelta(days=DEFAULT_LIST_RANGE_DAYS), False

    if start_s and end_s:
        start = _parse_date(start_s)
        end = _parse_date(end_s)
        if end < start:
            start, end = end, start
        return start, end + timedelta(days=1), False

    if start_s:
        start = _parse_date(start_s)
        return start, start + timedelta(days=1), True

    start = _parse_date(end_s)
    return start, start + timedelta(days=1), True


def _fmt_slot(start: str, end: str) -> str:
    """ISO 시각 → 'YYYY-MM-DD HH:mm ~ HH:mm' 표시."""
    return f"{start[:16].replace('T', ' ')} ~ {end[11:16]}"


def _fmt_event(ev: dict) -> str:
    """Graph calendarView 이벤트 한 줄 요약."""
    s = ev.get("start", {}).get("dateTime", "")[:16].replace("T", " ")
    e = ev.get("end", {}).get("dateTime", "")[:16].replace("T", " ")
    subj = ev.get("subject", "(제목 없음)")
    return f"• {s} ~ {e}  {subj}"


def _email_address(node: dict | None) -> str:
    return str(((node or {}).get("emailAddress") or {}).get("address") or "").strip()


def _email_name(node: dict | None) -> str:
    info = (node or {}).get("emailAddress") or {}
    return str(info.get("name") or info.get("address") or "").strip()


def _organizer_email(event: dict) -> str:
    return _email_address(event.get("organizer")).lower()


def _event_involves_email(event: dict, email: str) -> bool:
    target = email.strip().lower()
    if not target:
        return False
    if _organizer_email(event) == target:
        return True
    for attendee in event.get("attendees") or []:
        if _email_address(attendee).lower() == target:
            return True
    return False


def _room_info_from_event(event: dict) -> tuple[str, str] | None:
    """상세 event에서 회의실 resource를 찾아 (room_name, room_display) 반환."""
    cached_room_email = str(event.get("room_email") or "").lower()
    if cached_room_email:
        for display, email in graph.ROOM_EMAIL_MAP.items():
            if email.lower() == cached_room_email:
                return display.split()[0].lower(), display

    attendees = event.get("attendees") or []
    attendee_emails = {
        _email_address(a).lower()
        for a in attendees
        if _email_address(a)
    }
    location = str((event.get("location") or {}).get("displayName") or "").lower()

    for display, email in graph.ROOM_EMAIL_MAP.items():
        room_key = display.split()[0].lower()
        if email.lower() in attendee_emails or room_key in location:
            return room_key, display
    return None


def _clean_event_subject(subject: str) -> str:
    cleaned = re.sub(r"^\[[^\]]+\]\s*", "", (subject or "").strip())
    return cleaned or (subject or "회의").strip() or "회의"


def _attendee_status_lines(event: dict, *, include_resources: bool = False) -> list[str]:
    """Graph attendees 응답 상태를 요약. accepted/none 등 raw 상태를 유지한다."""
    room_emails = {email.lower() for email in graph.ROOM_EMAIL_MAP.values()}
    lines: list[str] = []
    for attendee in event.get("attendees") or []:
        email = _email_address(attendee)
        if not email:
            continue
        attendee_type = str(attendee.get("type") or "").strip()
        if not include_resources and (
            attendee_type == "resource" or email.lower() in room_emails
        ):
            continue
        name = _email_name(attendee) or email
        response = str((attendee.get("status") or {}).get("response") or "none").strip()
        lines.append(f"{name} <{email}>: {response}")
    return lines


def _event_summary_line(event: dict, *, prefix: str = "•") -> str:
    start = str((event.get("start") or {}).get("dateTime") or "")[:16].replace("T", " ")
    end = str((event.get("end") or {}).get("dateTime") or "")[:16].replace("T", " ")
    subject = _clean_event_subject(str(event.get("subject") or "회의"))
    room_info = _room_info_from_event(event)
    room_display = room_info[1] if room_info else "회의실"
    organizer = _email_name(event.get("organizer")) or _organizer_email(event)
    return f"{prefix} {room_display} | {subject} | {start} ~ {end} | 주최: {organizer}"


async def _fetch_event_detail_async(headers: dict, mailbox_email: str, event_id: str) -> dict | None:
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None,
            lambda: graph.fetch_event_detail(headers, mailbox_email, event_id),
        )
    except Exception as e:
        logger.warning("[RoomSchedule] event detail 조회 실패: %s", e)
        return None


async def _format_attended_events_text(
    organizer_email: str,
    *,
    date_str: str | None = None,
    start_date: date | None = None,
    end_date_exclusive: date | None = None,
    exclude_outlook_ids: set[str] | None = None,
) -> str | None:
    """list_mine — 참석(비주최) 일정 텍스트 (managed_room_events SQL)."""
    org = organizer_email.strip().lower()
    if not org:
        return None

    rows = await list_attended_events(
        org,
        date_str=date_str,
        start_date=start_date,
        end_date_exclusive=end_date_exclusive,
    )
    exclude = exclude_outlook_ids or set()
    rows = [r for r in rows if r.outlook_event_id not in exclude]
    if not rows:
        return None

    if start_date and end_date_exclusive:
        if (end_date_exclusive - start_date).days <= 1:
            range_label = start_date.isoformat()
        else:
            range_label = (
                f"{start_date.isoformat()}~"
                f"{(end_date_exclusive - timedelta(days=1)).isoformat()}"
            )
    else:
        range_label = normalize_date_filter(date_str) or date.today().isoformat()

    lines = [f"내 참석 회의실 일정 ({range_label}, {len(rows)}건):"]
    for row in rows:
        start = row.start_time[:16].replace("T", " ")
        end = row.end_time[11:16]
        lines.append(
            f"• {start} ~ {end}  {row.event_subject} ({row.room_display})"
        )
    lines.append("(참석 일정은 조회만 가능합니다.)")
    return "\n".join(lines)


def _room_schedule_text(room_name: str, events: list[dict], target_date: date) -> str:
    """check 결과 — occupied 목록 또는 전 시간 가능."""
    date_str = target_date.isoformat()
    if not events:
        return f"[{room_name}] {date_str} — 예약 없음 (전 시간 사용 가능)"
    lines = [f"[{room_name}] {date_str} 예약 현황 ({len(events)}건):"]
    lines.extend(_fmt_event(ev) for ev in events)
    return "\n".join(lines)


def _require_organizer(organizer_email: str | None) -> str | None:
    """요청자 Slack/DB email 필수 — 없으면 안내 메시지."""
    email = (organizer_email or "").strip()
    if not email or "@" not in email:
        return _NO_EMAIL_MSG
    return None


def _query_range_is_past(start_date: date, end_date_exclusive: date) -> bool:
    """조회 구간 종료일이 오늘 이전이면 과거 조회(읽기 전용)."""
    return (end_date_exclusive - timedelta(days=1)) < date.today()


def _booking_end_datetime(row) -> datetime | None:
    """room_bookings.end_time(ISO str) → KST naive datetime."""
    raw = getattr(row, "end_time", None)
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=None) if raw.tzinfo else raw
    try:
        return datetime.fromisoformat(str(raw)[:19])
    except ValueError:
        return None


def _bookings_allow_actions(rows: list) -> bool:
    """미래에 끝나는 본인 주최 예약이 있으면 취소·변경·리마인더 안내 가능."""
    now = _now_kst()
    for row in rows:
        end = _booking_end_datetime(row)
        if end is not None and end > now:
            return True
    return False


def _now_kst() -> datetime:
    """KST naive datetime (Graph 슬롯·검증 공통)."""
    return datetime.now(KST).replace(tzinfo=None, second=0, microsecond=0)


def _validate_time_range(start_time: str, end_time: str) -> str | None:
    """예약용 — 과거·역순·형식 오류 검증. 통과 시 None."""
    if not start_time or not end_time:
        return "시작·종료 시각이 필요합니다."
    if start_time >= end_time:
        return "종료 시각은 시작 시각보다 뒤여야 합니다."
    try:
        start_dt = datetime.fromisoformat(start_time[:19])
    except ValueError:
        return "시작 시각 형식이 올바르지 않습니다 (예: 2026-06-18T14:00:00)."
    if start_dt < _now_kst() - timedelta(minutes=1):
        return "과거 시간은 예약할 수 없습니다."
    return None


def _normalize_check_slot(
    start_time: str,
    end_time: str,
) -> tuple[str, str] | str:
    """조회용 슬롯 — 과거 시작이면 현재 시각으로 보정."""
    if not start_time or not end_time:
        return "시작·종료 시각이 필요합니다."
    try:
        start_dt = datetime.fromisoformat(start_time[:19])
        end_dt = datetime.fromisoformat(end_time[:19])
    except ValueError:
        return "시간 형식이 올바르지 않습니다 (예: 2026-06-18T14:00:00)."
    now = _now_kst()
    if end_dt <= now:
        return "조회 종료 시각이 현재보다 이전입니다."
    if start_dt < now:
        start_dt = now
    if end_dt <= start_dt:
        end_dt = start_dt + timedelta(hours=1)
    return start_dt.isoformat()[:19], end_dt.isoformat()[:19]


# ─────────────────────────────────────────────────────────────
# 공개 API (모두 async — blocking I/O는 executor 처리)
# ─────────────────────────────────────────────────────────────

def _append_focus_time_hint(text: str, focus_time: str | None) -> str:
    if not focus_time:
        return text
    return (
        f"{text}\n\n"
        f"[참고] 사용자 관심 시각: {focus_time} — "
        "위 occupied 목록에서 해당 시각과 겹치는 예약을 확인하세요."
    )


async def check_room_schedule(
    room_name: str,
    date_str: str | None = None,
    *,
    start_time: str | None = None,
    end_time: str | None = None,
    focus_time: str | None = None,
) -> str:
    """
    회의실 예약 현황 조회. start_time·end_time이 있으면 해당 슬롯 가용 여부.

    Returns
    -------
    LLM 컨텍스트용 텍스트
    """
    target = _parse_date(date_str)
    display, room_email = graph.resolve_room(room_name)

    try:
        db_rows = await list_events_for_room_day(room_email, target)
        events = [event_to_graph_dict(r) for r in db_rows]

        if start_time and end_time:
            try:
                end_dt = datetime.fromisoformat(end_time[:19])
            except ValueError:
                return "시간 형식이 올바르지 않습니다 (예: 2026-06-18T14:00:00)."
            if end_dt <= _now_kst():
                return _append_focus_time_hint(
                    _room_schedule_text(display, events, target),
                    focus_time,
                )
            normalized = _normalize_check_slot(start_time, end_time)
            if isinstance(normalized, str):
                return normalized
            start_time, end_time = normalized
            conflict = slot_conflicts_with_rows(db_rows, start_time, end_time)
            if conflict:
                c_start = conflict.start_time[:16].replace("T", " ")
                c_end = conflict.end_time[11:16]
                ok, msg = False, (
                    f"{c_start}~{c_end} '{conflict.event_subject}' 와 겹칩니다."
                )
            else:
                ok, msg = True, ""
            slot = _fmt_slot(start_time, end_time)
            status = "✅ 가용" if ok else f"❌ 불가 ({msg})"
            return f"[{display}] {target.isoformat()} {slot} — {status}"

        logger.info("[RoomSchedule] check %s %s — %d events", display, target, len(events))
        return _append_focus_time_hint(
            _room_schedule_text(display, events, target),
            focus_time,
        )
    except Exception as e:
        logger.exception("[RoomSchedule] check 실패: %s", e)
        return f"[{display}] 일정 조회 중 오류가 발생했습니다: {e}"


def _format_my_event_line(index: int, row: Any, role: str) -> str:
    start = row.start_time[:16].replace("T", " ")
    end = row.end_time[11:16]
    tag = "[주최]" if role == "owned" else "[참석]"
    if role == "owned":
        return (
            f"{index}. {tag} booking_id={row.id} | {row.room_display} | "
            f"{row.subject} | {start}~{end}"
        )
    organizer = (row.organizer_name or row.organizer_email or "").strip()
    return (
        f"{index}. {tag} {row.room_display} | {row.subject} | "
        f"{start}~{end} | 주최: {organizer}"
    )


async def resolve_person_for_schedule(
    person_name: str,
) -> tuple[list[str], list[str], str]:
    """이름 → (조회용 이메일, organizer_name 부분일치, 표시용 이름)."""
    from app.services.flex_hr.flex_hr import match_employees_in_query

    needle = (person_name or "").strip()
    if not needle:
        return [], [], ""

    roster_names = match_employees_in_query(needle)
    display = roster_names[0] if roster_names else needle

    emails: list[str] = []
    for name in roster_names or [needle]:
        hit = await lookup_user_by_name(name)
        if hit and hit.get("email"):
            emails.append(hit["email"].strip().lower())
        if len(name) >= 2:
            short = name[-2:]
            hit_short = await lookup_user_by_name(short)
            if hit_short and hit_short.get("email"):
                emails.append(hit_short["email"].strip().lower())

    name_needles: list[str] = []
    for name in roster_names or [needle]:
        name_needles.append(name)
        if len(name) >= 2:
            name_needles.append(name[-2:])
    name_needles.append(needle)

    return (
        list(dict.fromkeys(emails)),
        list(dict.fromkeys(n for n in name_needles if n)),
        display,
    )


def extract_list_person_name(query: str, args: dict[str, Any]) -> str | None:
    """list — 질문·인자에서 조회 대상 직원 이름 (생략·본인 표현이면 None)."""
    explicit = str(args.get("person_name") or "").strip()
    if explicit:
        return explicit

    q = (query or "").strip()
    if not q:
        return None
    if any(marker in q for marker in _LIST_MINE_SELF_MARKERS):
        return None
    if re.search(r"\b(내|나|제|본인)\b", q) and match_room_in_query(q):
        return None

    from app.services.flex_hr.flex_hr import match_employees_in_query

    names = match_employees_in_query(q)
    return names[0] if names else None


async def list_bookings(
    *,
    user_id: uuid.UUID | None = None,
    slack_user_id: str | None = None,
    date_str: str | None = None,
    end_date_str: str | None = None,
    organizer_email: str | None = None,
    slack_channel_id: str | None = None,
    person_name: str | None = None,
    query: str = "",
) -> str:
    """회의 일정 조회 (person_name 생략=요청자 본인)."""
    target_person = (person_name or "").strip()
    if not target_person:
        target_person = (extract_list_person_name(query, {}) or "").strip()

    start_date, end_date_exclusive, explicit_day = _parse_query_range(
        date_str, end_date_str
    )
    range_label = (
        start_date.isoformat()
        if explicit_day
        else f"{start_date.isoformat()}~{(end_date_exclusive - timedelta(days=1)).isoformat()}"
    )

    person_emails: list[str] = []
    name_needles: list[str] = []
    display_name = "내"
    query_self = not target_person

    if query_self:
        if not organizer_email:
            return "예약 목록 조회에 요청자 이메일이 필요합니다."
        person_emails = [organizer_email.strip().lower()]
    else:
        person_emails, name_needles, display_name = await resolve_person_for_schedule(
            target_person,
        )
        if not person_emails and not name_needles:
            return f"'{target_person}' 직원을 회의 일정 조회 대상으로 찾지 못했습니다."
        req = (organizer_email or "").strip().lower()
        if req and req in person_emails:
            query_self = True
            display_name = "내"
            person_emails = [req]
            name_needles = []

    events = await list_schedule_events(
        person_emails=person_emails or None,
        organizer_name_needles=name_needles or None,
        date_str=date_str if explicit_day else None,
        start_date=start_date if not explicit_day else None,
        end_date_exclusive=end_date_exclusive if not explicit_day else None,
    )

    header_label = "내" if query_self else f"{display_name}님"

    if not events:
        return f"해당 기간({range_label}) {header_label} 등록된 회의실 일정이 없습니다."

    owned_rows = [row for row, role in events if role == "owned"]
    fetch_attendee_detail = query_self
    loop = asyncio.get_running_loop()
    headers = await loop.run_in_executor(None, _get_headers) if fetch_attendee_detail else None

    lines = [
        f"{header_label} 회의 일정 ({range_label}, {len(events)}건, 주최·참석 포함):",
    ]
    for i, (row, role) in enumerate(events, start=1):
        lines.append(_format_my_event_line(i, row, role))
        if not fetch_attendee_detail or role != "owned" or not headers:
            continue
        org_eid = (row.organizer_event_id or "").strip()
        if org_eid and not org_eid.startswith("pending:"):
            detail = await _fetch_event_detail_async(
                headers,
                row.organizer_email,
                org_eid,
            )
            attendee_lines = _attendee_status_lines(detail or {})
            if attendee_lines:
                lines.append("   참석자 응답:")
                lines.extend(f"   - {line}" for line in attendee_lines)

    if query_self:
        if _query_range_is_past(start_date, end_date_exclusive) or not _bookings_allow_actions(owned_rows):
            lines.append("(과거·종료된 일정·참석 일정은 조회만 가능합니다.)")
        elif owned_rows:
            lines.append(
                f"([주최] 일정만 {_BOOKING_TARGET_ACTIONS_LABEL} 가능. "
                "대화 맥락에서 booking_id를 특정하거나 회의실·시간을 지정하세요.)"
            )
            lines.append(_reminder_hint().strip())
    else:
        lines.append("(타인 회의 일정은 조회만 가능합니다.)")
    return "\n".join(lines)


# 하위 호환 alias
list_my_bookings = list_bookings


async def book_room(
    room_name: str,
    subject: str,
    start_time: str,
    end_time: str,
    organizer_email: str | None = None,
    *,
    required_attendees: list[dict[str, str]] | None = None,
    organizer_name: str = "",
    user_id: uuid.UUID | None = None,
    slack_user_id: str | None = None,
    slack_channel_id: str | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """
    회의실 예약.

    Parameters
    ----------
    room_name     : "spine" / "Spine 회의실" / …
    subject       : 회의 제목
    start_time    : "2026-06-18T14:00:00" (KST 로컬)
    end_time      : "2026-06-18T15:00:00"
    organizer_email : 요청자 Slack/DB email (주최자 캘린더)
    required_attendees : 추가 참석자 [{email|name|slack_user_id}, …]
    organizer_name : 주최자 표시명 (Graph payload용)

    Returns
    -------
    (LLM 컨텍스트용 텍스트, booking_meta) — meta에 booking_id
    """
    err = _require_organizer(organizer_email)
    if err:
        return err, None

    organizer = organizer_email.strip()
    time_err = _validate_time_range(start_time, end_time)
    if time_err:
        return time_err, None

    display, _ = graph.resolve_room(room_name)
    loop = asyncio.get_running_loop()

    try:
        headers = await loop.run_in_executor(None, _get_headers)
        resolved_atts, unresolved = await resolve_attendees(
            required_attendees,
            organizer_email=organizer,
            headers=headers,
        )
        if unresolved:
            return (
                f"참석자 이메일을 찾지 못했습니다: {', '.join(unresolved)}. "
                "이름·이메일을 확인해 주세요.",
                None,
            )

        result = await loop.run_in_executor(
            None,
            lambda: graph.create_room_reservation(
                headers,
                organizer,
                room_name,
                subject,
                start_time,
                end_time,
                required_attendees=resolved_atts or None,
            ),
        )
        logger.info("[RoomSchedule] book %s → status=%s", display, result.get("status"))

        if result.get("status") == "error":
            return f"[{display}] 예약 실패: {result.get('message', '알 수 없는 오류')}", None

        eid = result.get("organizer_event_id", "")
        booking_meta: dict[str, Any] | None = None
        saved_row = None
        if eid:
            try:
                _, room_email = graph.resolve_room(
                    str(result.get("room_name") or room_name),
                )
                event_subj = str(result.get("event_subject") or subject)
                st = str(result.get("start_time") or start_time)
                room_outlook_id = await loop.run_in_executor(
                    None,
                    lambda: find_room_outlook_event_id(
                        headers,
                        room_email,
                        event_subject=event_subj,
                        start_time=st,
                    ),
                )
                saved_row = await upsert_after_bot_book(
                    organizer_event_id=eid,
                    room_name=str(result.get("room_name") or room_name),
                    room_display=str(result.get("room_display") or display),
                    room_email=room_email,
                    subject=str(result.get("subject") or subject),
                    event_subject=event_subj,
                    start_time=st,
                    end_time=str(result.get("end_time") or end_time),
                    organizer_email=str(result.get("organizer_email") or organizer),
                    bot_user_id=user_id,
                    bot_slack_user_id=slack_user_id,
                    slack_channel_id=slack_channel_id,
                    room_outlook_event_id=room_outlook_id,
                )
                if saved_row:
                    booking_meta = {"booking_id": str(saved_row.id)}
            except Exception as db_err:
                logger.warning(
                    "[RoomSchedule] DB 저장 실패(예약은 Graph 완료): user_id=%s slack_user_id=%s err=%s",
                    user_id,
                    slack_user_id,
                    db_err,
                    exc_info=True,
                )

        confirmed = "✅ 예약 완료" if result.get("room_confirmed") else "⚠️ 예약 등록됨 (회의실 자동 수락 미확인)"
        bid = str(saved_row.id) if saved_row else "(DB 미저장)"
        db_warn = (
            "\n⚠️ DB에 예약이 저장되지 않아 list_mine·cancel·modify가 불가할 수 있습니다."
            if not saved_row and eid
            else ""
        )
        text = (
            f"[{display}] {confirmed}\n"
            f"booking_id: {bid}\n"
            f"제목: {subject}\n"
            f"시간: {_fmt_slot(start_time, end_time)}\n"
            f"주최: {organizer}\n"
            f"상세: {result.get('message', '')}"
            f"{db_warn}"
            f"{_reminder_hint(saved_row.id if saved_row else None)}"
        )
        return text, booking_meta
    except Exception as e:
        logger.exception("[RoomSchedule] book 실패: %s", e)
        return f"[{display}] 예약 중 오류가 발생했습니다: {e}", None


async def cancel_room(
    *,
    organizer_email: str | None = None,
    user_id: uuid.UUID | None = None,
    slack_user_id: str | None = None,
    booking_id: str | None = None,
    event_id: str | None = None,
    room_name: str | None = None,
    subject: str | None = None,
    start_time: str | None = None,
    date_str: str | None = None,
    prefer_recent: bool = False,
) -> str:
    """
    회의실 예약 취소 (본인 예약만 — organizer 캘린더 일정 삭제 → 회의실 자동 취소).

    Returns
    -------
    LLM 컨텍스트용 텍스트
    """
    resolved_id, matched, err = await resolve_outlook_event_id(
        organizer_email=organizer_email,
        booking_id=booking_id,
        event_id=event_id,
        room_name=room_name,
        subject=subject,
        start_time=start_time,
        date_str=date_str,
        prefer_recent=prefer_recent,
    )
    if err:
        return err
    if not resolved_id:
        return (
            "취소할 예약을 찾지 못했습니다. "
            "list_mine으로 booking_id를 확인하거나 회의실·날짜·시간을 알려주세요."
        )

    organizer = (matched.organizer_email if matched else organizer_email or "").strip()
    if not organizer:
        err_org = _require_organizer(organizer_email)
        return err_org or "취소 실패: 주최자 이메일을 확인할 수 없습니다."

    loop = asyncio.get_running_loop()
    try:
        headers = await loop.run_in_executor(None, _get_headers)
        result = await loop.run_in_executor(
            None,
            lambda: graph.cancel_room_reservation(headers, organizer, resolved_id),
        )
        logger.info(
            "[RoomSchedule] cancel event_id=%s → %s %s",
            resolved_id[:24],
            result.get("status"),
            booking_lookup_hint(matched),
        )

        if result.get("status") == "error":
            return f"예약 취소 실패: {result.get('message', '알 수 없는 오류')}"

        if matched:
            try:
                await delete_event_by_id(matched.id)
            except Exception as db_err:
                logger.warning("[RoomSchedule] DB projection 삭제 실패: %s", db_err)

        hint = booking_lookup_hint(matched)
        suffix = f"\n{hint}" if hint else ""
        return f"✅ 예약 취소 완료: {result.get('message', '')}{suffix}"
    except Exception as e:
        logger.exception("[RoomSchedule] cancel 실패: %s", e)
        return f"예약 취소 중 오류가 발생했습니다: {e}"


async def modify_room(
    *,
    organizer_email: str | None,
    organizer_name: str = "",
    user_id: uuid.UUID | None = None,
    slack_user_id: str | None = None,
    booking_id: str | None = None,
    room_name: str | None = None,
    subject: str | None = None,
    old_start_time: str | None = None,
    old_end_time: str | None = None,
    new_start_time: str = "",
    new_end_time: str = "",
    new_subject: str | None = None,
    date_str: str | None = None,
    required_attendees: list[dict[str, str]] | None = None,
) -> str:
    """
    본인 예약 변경 — Graph PATCH(update) + DB 갱신.

    Turn2 출력용 [modify 실행 로그] 블록을 반환한다.

    Returns
    -------
    LLM 컨텍스트용 텍스트
    """
    err = _require_organizer(organizer_email)
    if err:
        return err

    booking, lookup_err = await resolve_owned_booking(
        organizer_email=organizer_email,
        booking_id=booking_id,
        room_name=room_name,
        subject=subject,
        old_start_time=old_start_time,
        date_str=date_str,
    )
    if lookup_err:
        return lookup_err
    if not booking:
        return "변경할 본인 예약을 찾지 못했습니다. list_mine으로 booking_id를 확인해 주세요."

    org_event_id = (booking.organizer_event_id or "").strip()
    if not org_event_id or org_event_id.startswith("pending:"):
        return "주최자 일정 ID가 동기화되지 않아 변경할 수 없습니다. 잠시 후 다시 시도해 주세요."

    changing_time = bool(new_start_time or new_end_time)
    changing_subject = bool(new_subject)
    changing_attendees = required_attendees is not None
    if not changing_time and not changing_subject and not changing_attendees:
        return "변경할 내용이 필요합니다. 변경할 시간, 제목 또는 참석자를 알려주세요."
    if changing_time and not (new_start_time and new_end_time):
        return "시간 변경에는 변경 시작 시각과 변경 종료 시각이 모두 필요합니다."

    target_start_time = new_start_time or booking.start_time
    target_end_time = new_end_time or booking.end_time
    if changing_time:
        time_err = _validate_time_range(target_start_time, target_end_time)
        if time_err:
            return time_err

    old_slot = _fmt_slot(booking.start_time, booking.end_time)
    new_slot = _fmt_slot(target_start_time, target_end_time)
    time_changed = (
        booking.start_time[:16] != target_start_time[:16]
        or booking.end_time[:16] != target_end_time[:16]
    )
    subject_final = (new_subject or booking.subject).strip()
    short = booking.room_name.strip().upper()
    event_subject_final = f"[{short}] {subject_final}"

    lines = [
        "[modify 실행 로그]",
        f"조회: {booking.room_display} | {booking.subject} | "
        f"{booking.start_time[:16].replace('T', ' ')}~{booking.end_time[11:16]}",
    ]
    if time_changed:
        lines.append(f"시간: {old_slot} → {new_slot}")
    if new_subject:
        lines.append(f"제목: {booking.subject} → {subject_final}")
    if required_attendees is not None:
        lines.append("참석자: 요청한 참석자 목록으로 교체")

    if old_start_time and not booking.start_time.startswith(old_start_time[:16]):
        lines.append(
            f"⚠️ 기존 시간 불일치 (DB: {booking.start_time[:16]}, 요청: {old_start_time[:16]})"
        )

    loop = asyncio.get_running_loop()
    try:
        headers = await loop.run_in_executor(None, _get_headers)
        _, room_email = graph.resolve_room(booking.room_name)
        booking_day = date.fromisoformat(target_start_time[:10])

        if time_changed:
            db_rows = await list_events_for_room_day(room_email, booking_day)
            others = [
                r for r in db_rows
                if r.outlook_event_id != booking.outlook_event_id
            ]
            conflict = slot_conflicts_with_rows(
                others, target_start_time, target_end_time,
            )
            if conflict:
                c_start = conflict.start_time[:16].replace("T", " ")
                c_end = conflict.end_time[11:16]
                lines.append(
                    f"변경: ❌ 실패 — 신규 시간 unavailable "
                    f"({c_start}~{c_end} '{conflict.event_subject}' 와 겹칩니다.)"
                )
                return "\n".join(lines)

        patch_attendees = required_attendees is not None
        resolved_atts: list[dict[str, str]] | None = None
        if patch_attendees:
            resolved_atts, unresolved = await resolve_attendees(
                required_attendees,
                organizer_email=booking.organizer_email,
                headers=headers,
            )
            if unresolved:
                lines.append(
                    f"변경: ❌ 실패 — 참석자 미해결: {', '.join(unresolved)}"
                )
                return "\n".join(lines)
            attendee_names = ", ".join(
                a.get("name") or a.get("email") or ""
                for a in (resolved_atts or [])
                if a.get("name") or a.get("email")
            )
            if attendee_names:
                lines.append(f"참석자 반영: {attendee_names}")

        update_result = await loop.run_in_executor(
            None,
            lambda: graph.update_room_reservation(
                headers,
                booking.organizer_email,
                org_event_id,
                booking.room_name,
                target_start_time,
                target_end_time,
                new_subject=subject_final if new_subject else None,
                required_attendees=resolved_atts,
                patch_attendees=patch_attendees,
            ),
        )
        if update_result.get("status") == "error":
            lines.append(f"변경: ❌ 실패 — {update_result.get('message', '')}")
            return "\n".join(lines)

        if new_subject:
            event_subject_final = str(
                update_result.get("event_subject") or event_subject_final
            )

        await update_room_booking_after_modify(
            booking.id,
            start_time=target_start_time,
            end_time=target_end_time,
            subject=subject_final,
            event_subject=event_subject_final,
            time_changed=time_changed,
        )

        lines.append("변경: ✅ PATCH 완료")
        if time_changed and booking.reminder_minutes_before:
            lines.append(
                f"(시간 변경으로 리마인더 재발송 대기 — "
                f"{booking.reminder_minutes_before}분 전)"
            )
        return "\n".join(lines)

    except Exception as e:
        logger.exception("[RoomSchedule] modify 실패: %s", e)
        lines.append(f"오류: {e}")
        return "\n".join(lines)


async def set_room_reminder(
    *,
    booking_id: str,
    reminder_minutes: int,
    organizer_email: str | None = None,
    user_id: uuid.UUID | None = None,
    slack_user_id: str | None = None,
    slack_channel_id: str | None = None,
) -> str:
    """
    본인 예약에 챗봇 Slack 리마인더(N분 전) 설정.

    Returns
    -------
    LLM 컨텍스트용 텍스트
    """
    if reminder_minutes <= 0:
        return "리마인더는 1분 이상 전으로 설정해 주세요."

    booking, lookup_err = await resolve_owned_booking(
        organizer_email=organizer_email,
        booking_id=booking_id,
    )
    if lookup_err:
        return lookup_err
    if not booking:
        return "리마인더를 설정할 본인 예약을 찾지 못했습니다. list_mine으로 booking_id를 확인해 주세요."

    try:
        start_dt = datetime.fromisoformat(booking.start_time[:19]).replace(tzinfo=KST)
    except ValueError:
        return "예약 시작 시각 형식이 올바르지 않습니다."

    now = datetime.now(KST)
    if start_dt <= now:
        return "이미 시작했거나 지난 예약에는 리마인더를 설정할 수 없습니다."

    remind_at = start_dt - timedelta(minutes=reminder_minutes)
    if remind_at <= now:
        return (
            f"리마인더 시각이 이미 지났습니다. "
            f"회의 시작 {reminder_minutes}분 전보다 늦지 않게 설정해 주세요."
        )

    updated = await set_booking_reminder(
        booking.id,
        reminder_minutes_before=reminder_minutes,
        slack_channel_id=slack_channel_id,
        bot_slack_user_id=slack_user_id,
    )
    if not updated:
        return "리마인더 설정에 실패했습니다."

    start = booking.start_time[:16].replace("T", " ")
    end = booking.end_time[11:16]
    return (
        f"✅ 챗봇 리마인더 설정 완료\n"
        f"(처리 대상: {booking.room_display} | {booking.subject} | {start}~{end})\n"
        f"알림: 시작 {reminder_minutes}분 전 Slack DM"
    )


def parse_reminder_minutes(text: str) -> int | None:
    """자연어에서 N분 전 리마인더 분 수 추출 (보조)."""
    m = re.search(r"(\d+)\s*분", text or "")
    if m:
        return int(m.group(1))
    return None


_BOOKING_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.I,
)

_OPERATIONAL_ROOM_MARKERS: tuple[str, ...] = (
    "booking_id",
    "예약 id",
    "예약 완료",
    "예약이 완료",
    "예약 실패",
    "취소 완료",
    "취소가 완료",
    "patch 완료",
    "list",
    "list_mine",
    "manage_room_schedule",
    "occupied",
    "가용성",
    "✅ 가용",
    "❌ 불가",
)

_DEFAULT_BOOK_SUBJECT = "회의"

_ROOM_STATUS_KEYWORDS: tuple[str, ...] = (
    "예약현황",
    "예약 현황",
    "일정 조회",
    "스케줄",
)

_TIME_RANGE_RE = re.compile(
    r"(\d{1,2})\s*시\s*(?:부터|~|～|-)\s*(\d{1,2})\s*시",
)
_TIME_SINGLE_RE = re.compile(r"(\d{1,2})\s*시")
_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_KO_DATE_RE = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_LIST_MINE_BOOKING_RE = re.compile(
    r"(?:(\d+)\.\s*)?(?:booking_id=|예약\s*ID[=:]?\s*)"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*"
    r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})",
    re.I,
)
_ATTENDEE_EVENT_RE = re.compile(
    r"^[•\-]\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*"
    r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s*~\s*"
    r"(?:\d{4}-\d{2}-\d{2}\s+)?(\d{2}:\d{2})"
    r"(?:\s*\|.*)?$",
    re.M,
)
_TURN2_BOOKING_ID_RE = re.compile(
    r"예약\s*ID[=:]\s*"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.I,
)
_TURN2_ROOM_RE = re.compile(r"회의실\s*[:：]\s*\**([^*\n|]+?)\**", re.I)
_TURN2_SUBJECT_RE = re.compile(r"제목\s*[:：]\s*\**([^*\n|]+?)\**", re.I)
_TURN2_TIME_RE = re.compile(
    r"시간\s*[:：]\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s*~\s*(\d{2}:\d{2})",
    re.I,
)
_PLACEHOLDER_BOOKING_ID_MARKERS = (
    "auto_detect",
    "from_context",
    "placeholder",
    "unknown",
    "todo",
)
_ISO_DATETIME_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?",
)
_TIME_COLON_RE = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])")


def _iso_slot(day: date, start_h: int, end_h: int) -> tuple[str, str]:
    start = f"{day.isoformat()}T{start_h:02d}:00:00"
    end = f"{day.isoformat()}T{end_h:02d}:00:00"
    return start, end


def _combine_day_time(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute)


def parse_time_range_from_query(
    query: str,
    *,
    default_day: date | None = None,
) -> tuple[str | None, str | None, date]:
    """질문에서 KST 슬롯(ISO local) 추출. (start, end, day) — 없으면 start/end None."""
    day = default_day or date.today()
    q = query or ""

    m = _ISO_DATETIME_RE.search(q)
    if m:
        day = date.fromisoformat(m.group(1))
        h, mi = int(m.group(2)), int(m.group(3))
        start_dt = _combine_day_time(day, h, mi)
        end_dt = start_dt + timedelta(hours=1)
        return start_dt.isoformat()[:19], end_dt.isoformat()[:19], day

    m = _ISO_DATE_RE.search(q)
    if m:
        day = date.fromisoformat(m.group(1))

    m = _TIME_COLON_RE.search(q)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        start_dt = _combine_day_time(day, h, mi)
        end_dt = start_dt + timedelta(hours=1)
        return start_dt.isoformat()[:19], end_dt.isoformat()[:19], day

    m = _TIME_RANGE_RE.search(q)
    if m:
        h1, h2 = int(m.group(1)), int(m.group(2))
        if h2 <= h1:
            h2 = min(h1 + 1, 23)
        return _iso_slot(day, h1, h2) + (day,)

    m = _TIME_SINGLE_RE.search(q)
    if m:
        h = int(m.group(1))
        return _iso_slot(day, h, min(h + 1, 24 if h < 23 else 23)) + (day,)

    if any(w in q for w in ("지금", "현재", "당장")):
        now = _now_kst()
        end = now + timedelta(hours=1)
        return now.isoformat()[:19], end.isoformat()[:19], now.date()

    return None, None, day




_TOOL_ROOM_NAME_RE = re.compile(
    r'"room_name"\s*:\s*"([^"]+)"',
    re.I,
)


def extract_room_name_from_tool_history(
    history: list[dict[str, str]] | None,
    *,
    max_messages: int = 12,
) -> str | None:
    """직전 manage_room_schedule tool 인자/결과에 있던 room_name."""
    if not history:
        return None
    for msg in reversed(history[-max_messages:]):
        if msg.get("role") not in ("user", "assistant", "tool"):
            continue
        text = str(msg.get("content") or "")
        m = _TOOL_ROOM_NAME_RE.search(text)
        if m:
            name = m.group(1).strip()
            if name:
                return name
    return None


def extract_room_name_from_history(
    history: list[dict[str, str]] | None,
    *,
    max_messages: int = 8,
) -> str | None:
    return extract_room_name_from_tool_history(history, max_messages=max_messages)


def is_room_status_query(query: str) -> bool:
    """특정 시각 예약 현황(occupied) 조회 — 슬롯 가용 여부가 아님."""
    q = (query or "").lower()
    if not any(k in q for k in _ROOM_STATUS_KEYWORDS):
        return False
    return "회의실" in q or match_room_in_query(query)


def is_generic_all_rooms_query(query: str) -> bool:
    """특정 회의실명 없이 전체 회의실을 묻는 질문."""
    q = (query or "").lower()
    markers = ("전체", "모든", "빈 회의실", "예약현황", "예약 현황")
    if any(m in q for m in markers):
        return True
    return "회의실" in q and not match_room_in_query(query)


def should_use_history_room(query: str) -> bool:
    """직전 대화 tool room_name을 이번 조회에 쓸지."""
    if is_generic_all_rooms_query(query):
        return False
    return True


def extract_focus_time_label(query: str) -> str | None:
    """전체일 조회 시 LLM용 관심 시각 라벨."""
    start, _, day = parse_time_range_from_query(query)
    if not start:
        return None
    try:
        dt = datetime.fromisoformat(start[:19])
        return f"{day.isoformat()} {dt.strftime('%H:%M')}"
    except ValueError:
        return None


async def check_all_rooms(
    date_str: str | None = None,
    *,
    start_time: str | None = None,
    end_time: str | None = None,
    focus_time: str | None = None,
) -> str:
    """전체 회의실 occupied 목록 또는 특정 슬롯 가용 여부."""
    target = _parse_date(date_str)
    day_iso = target.isoformat()

    slot_mode = bool(start_time and end_time)
    if slot_mode:
        normalized = _normalize_check_slot(start_time or "", end_time or "")
        if isinstance(normalized, str):
            return normalized
        start_time, end_time = normalized
        lines = [f"[전체 회의실] {day_iso} {_fmt_slot(start_time, end_time)} 가용성:"]
    else:
        lines = [f"[전체 회의실] {day_iso} 예약 현황:"]

    try:
        for display in ROOM_NAMES:
            _, room_email = graph.resolve_room(display.split()[0].lower())
            db_rows = await list_events_for_room_day(room_email, target)
            if slot_mode:
                conflict = slot_conflicts_with_rows(
                    db_rows, start_time or "", end_time or "",
                )
                if conflict:
                    c_start = conflict.start_time[:16].replace("T", " ")
                    c_end = conflict.end_time[11:16]
                    ok, msg = False, (
                        f"{c_start}~{c_end} '{conflict.event_subject}' 와 겹칩니다."
                    )
                else:
                    ok, msg = True, ""
                status = "✅ 가용" if ok else f"❌ 불가 ({msg})"
                lines.append(f"• {display}: {status}")
            else:
                events = [event_to_graph_dict(r) for r in db_rows]
                if not events:
                    lines.append(f"• {display}: 예약 없음 (전 시간 사용 가능)")
                else:
                    lines.append(f"• {display}: {len(events)}건 occupied")
                    for ev in events:
                        lines.append(f"  {_fmt_event(ev)}")
        return _append_focus_time_hint("\n".join(lines), focus_time if not slot_mode else None)
    except Exception as e:
        logger.exception("[RoomSchedule] check_all 실패: %s", e)
        return f"전체 회의실 조회 중 오류: {e}"


async def replace_room(
    *,
    organizer_email: str | None = None,
    organizer_name: str = "",
    user_id: uuid.UUID | None = None,
    slack_user_id: str | None = None,
    slack_channel_id: str | None = None,
    booking_id: str | None = None,
    room_name: str | None = None,
    subject: str | None = None,
    start_time: str | None = None,
    date_str: str | None = None,
    new_room_name: str | None = None,
    new_subject: str | None = None,
    new_start_time: str = "",
    new_end_time: str = "",
    required_attendees: list[dict[str, str]] | None = None,
) -> str:
    """본인 예약 취소 후 동일/다른 회의실에 재예약."""
    cancel_msg = await cancel_room(
        organizer_email=organizer_email,
        user_id=user_id,
        slack_user_id=slack_user_id,
        booking_id=booking_id,
        room_name=room_name,
        subject=subject,
        start_time=start_time,
        date_str=date_str,
    )
    if "✅" not in cancel_msg:
        return f"[replace] 취소 단계 실패 — {cancel_msg}"

    target_room = (new_room_name or room_name or "").strip()
    subj = (new_subject or subject or "회의실 예약").strip()
    if not target_room or not new_start_time or not new_end_time:
        return (
            f"[replace] 취소 완료 후 재예약 정보 부족 — {cancel_msg}\n"
            "new_room_name·new_start_time·new_end_time이 필요합니다."
        )

    book_msg, _meta = await book_room(
        target_room,
        subj,
        new_start_time,
        new_end_time,
        organizer_email=organizer_email,
        required_attendees=required_attendees,
        organizer_name=organizer_name,
        user_id=user_id,
        slack_user_id=slack_user_id,
        slack_channel_id=slack_channel_id,
    )
    return f"[replace 실행]\n{cancel_msg}\n{book_msg}"


def is_valid_booking_uuid(value: str | None) -> bool:
    """list_mine·DB에서 받은 UUID booking_id인지 확인."""
    raw = str(value or "").strip()
    if not raw:
        return False
    lowered = raw.lower()
    if any(m in lowered for m in _PLACEHOLDER_BOOKING_ID_MARKERS):
        return False
    try:
        uuid.UUID(raw)
    except ValueError:
        return False
    return True


def sanitize_booking_id(value: str | None) -> str | None:
    """유효 UUID만 반환. 가짜·placeholder ID는 None."""
    raw = str(value or "").strip()
    return raw if is_valid_booking_uuid(raw) else None


def _extract_explicit_date_from_query(
    query: str,
    *,
    reference: date | None = None,
) -> str | None:
    """질문에 명시된 날짜(ISO). '6월 30일'·'2026-06-30' 등."""
    q = query or ""
    ref = reference or _now_kst().date()
    m = _ISO_DATE_RE.search(q)
    if m:
        return m.group(1)
    m = _KO_DATE_RE.search(q)
    if m:
        month, day_num = int(m.group(1)), int(m.group(2))
        try:
            return date(ref.year, month, day_num).isoformat()
        except ValueError:
            return None
    return None


def _enrich_booking_target_hints(
    *,
    query: str,
    room_name: str | None,
    subject: str | None,
    date_str: str | None,
    start_time: str | None,
    old_start_time: str | None,
) -> dict[str, str | None]:
    """Turn1 인자 보완 — LLM이 UUID 대신 회의실·날짜·제목만 넘긴 경우."""
    room = (room_name or "").strip() or None
    day = (date_str or "").strip() or _extract_explicit_date_from_query(query) or None
    subj = (subject or "").strip() or None
    start = (start_time or "").strip() or None
    old_start = (old_start_time or "").strip() or None
    if not start and not old_start:
        parsed_start, _, _ = parse_time_range_from_query(query)
        if parsed_start:
            start = parsed_start
    return {
        "room_name": room,
        "subject": subj,
        "date_str": day,
        "start_time": start,
        "old_start_time": old_start,
    }


async def prepare_booking_target(
    *,
    organizer_email: str | None,
    user_id: uuid.UUID | None,
    slack_user_id: str | None,
    slack_channel_id: str | None,
    booking_id: str | None = None,
    event_id: str | None = None,
    room_name: str | None = None,
    subject: str | None = None,
    start_time: str | None = None,
    old_start_time: str | None = None,
    date_str: str | None = None,
    end_date_str: str | None = None,
    query: str = "",
    prefer_recent: bool = False,
) -> tuple[Any | None, str | None]:
    """
    cancel/modify/replace/set_reminder — Outlook→DB 동기화 후 본인 예약 1건 조회.

    Turn1은 room/date/subject 등 식별 힌트만 넘겨도 되고,
    여기서 DB booking_id(UUID)를 확정한다.
    """
    hints = _enrich_booking_target_hints(
        query=query,
        room_name=room_name,
        subject=subject,
        date_str=date_str,
        start_time=start_time,
        old_start_time=old_start_time,
    )
    room = hints["room_name"]
    subj = hints["subject"]
    day = hints["date_str"]
    start = hints["start_time"]
    old_start = hints["old_start_time"]

    bid = booking_id if is_valid_booking_uuid(booking_id) else None
    return await resolve_owned_booking(
        organizer_email=organizer_email,
        booking_id=bid,
        event_id=event_id,
        room_name=room,
        subject=subj,
        start_time=start,
        old_start_time=old_start,
        date_str=day,
        prefer_recent=prefer_recent,
    )


def extract_booking_id(text: str) -> str | None:
    """텍스트에서 UUID booking_id 1개 추출."""
    m = _BOOKING_ID_RE.search(text or "")
    bid = m.group(0) if m else None
    return bid if is_valid_booking_uuid(bid) else None


def extract_bookings_from_history(
    history: list[dict[str, str]] | None,
    *,
    max_messages: int = 12,
) -> list[dict[str, str]]:
    """최근 list_mine·Turn2 답변에서 본인 주최 예약 후보 추출."""
    return extract_room_schedule_candidates_from_history(
        history, max_messages=max_messages,
    )["owned"]


def _parse_turn2_owned_blocks(text: str) -> list[dict[str, str]]:
    """Turn2 assistant 포맷(불릿)에서 본인 예약 추출."""
    out: list[dict[str, str]] = []
    for m in _TURN2_BOOKING_ID_RE.finditer(text or ""):
        bid = m.group(1)
        window = text[max(0, m.start() - 200): m.end() + 400]
        room_m = _TURN2_ROOM_RE.search(window)
        subj_m = _TURN2_SUBJECT_RE.search(window)
        time_m = _TURN2_TIME_RE.search(window)
        out.append({
            "index": "",
            "booking_id": bid,
            "room": (room_m.group(1).strip() if room_m else ""),
            "subject": (subj_m.group(1).strip() if subj_m else ""),
            "date": (time_m.group(1) if time_m else ""),
            "start": (time_m.group(2) if time_m else ""),
            "end": (time_m.group(3) if time_m else ""),
            "cancellable": True,
        })
    return out


def extract_room_schedule_candidates_from_history(
    history: list[dict[str, str]] | None,
    *,
    max_messages: int = 12,
) -> dict[str, list[dict[str, str]]]:
    """Turn1 특정용 — 본인 주최(취소 가능) vs 참석(조회만) 후보."""
    owned: list[dict[str, str]] = []
    attendee: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    if not history:
        return {"owned": owned, "attendee": attendee}

    for msg in history[-max_messages:]:
        if msg.get("role") not in ("user", "assistant"):
            continue
        text = str(msg.get("content") or "")

        in_attendee_section = False
        owned_index = len(owned)
        for line in text.splitlines():
            stripped = line.strip()
            if "본인 예약" in stripped or "본인 주최" in stripped:
                in_attendee_section = False
            elif "내 참석" in stripped or "참석 회의" in stripped:
                in_attendee_section = True
            elif "참석 일정" in stripped and "조회만" in stripped:
                in_attendee_section = True

            for m in _LIST_MINE_BOOKING_RE.finditer(stripped):
                bid = m.group(2)
                if bid in seen_ids:
                    continue
                seen_ids.add(bid)
                owned_index += 1
                owned.append({
                    "index": str(m.group(1) or owned_index),
                    "booking_id": bid,
                    "room": m.group(3).strip(),
                    "subject": m.group(4).strip(),
                    "date": m.group(5),
                    "start": m.group(6),
                    "end": "",
                    "cancellable": True,
                })

            if in_attendee_section:
                am = _ATTENDEE_EVENT_RE.match(stripped)
                if am:
                    attendee.append({
                        "room": am.group(1).strip(),
                        "subject": am.group(2).strip(),
                        "date": am.group(3),
                        "start": am.group(4),
                        "end": am.group(5),
                        "cancellable": False,
                    })

        for block in _parse_turn2_owned_blocks(text):
            bid = block["booking_id"]
            if bid in seen_ids:
                continue
            seen_ids.add(bid)
            owned_index += 1
            block["index"] = str(owned_index)
            owned.append(block)

    return {"owned": owned, "attendee": attendee}


def extract_booking_id_from_history(
    history: list[dict[str, str]] | None,
    *,
    max_messages: int = 12,
) -> str | None:
    """최근 대화에서 booking_id 추출."""
    bookings = extract_bookings_from_history(history, max_messages=max_messages)
    if bookings:
        return bookings[-1]["booking_id"]
    if not history:
        return None
    for msg in reversed(history[-max_messages:]):
        if msg.get("role") not in ("user", "assistant"):
            continue
        bid = extract_booking_id(str(msg.get("content") or ""))
        if bid:
            return bid
    return None


def default_book_subject(query: str = "", explicit: str | None = None) -> str:
    """book 제목 — 미지정·모호 표현이면 '회의'."""
    subj = (explicit or "").strip()
    if subj:
        return subj
    q = (query or "").strip()
    if not q:
        return _DEFAULT_BOOK_SUBJECT
    quoted = re.search(r"['\"]([^'\"]+)['\"]", q)
    if quoted and quoted.group(1).strip():
        return quoted.group(1).strip()
    if re.search(r"제목\s*[:=]", q):
        m = re.search(r"제목\s*[:=]\s*['\"]?([^'\"]+)", q)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return _DEFAULT_BOOK_SUBJECT


def has_recent_operational_room_context(
    history: list[dict[str, str]] | None,
    *,
    max_messages: int = 10,
) -> bool:
    """최근 대화에 실제 예약·조회 tool 결과 또는 booking_id가 있는지."""
    if not history:
        return False
    if extract_booking_id_from_history(history, max_messages=max_messages):
        return True
    blob = " ".join(
        str(m.get("content") or "")
        for m in history[-max_messages:]
        if m.get("role") in ("user", "assistant")
    ).lower()
    return any(m in blob for m in _OPERATIONAL_ROOM_MARKERS)


def has_recent_room_context(
    history: list[dict[str, str]] | None,
    *,
    max_messages: int = 10,
) -> bool:
    """회의실 follow-up 라우팅용 — wiki 안내만으로는 True가 되지 않음."""
    return has_recent_operational_room_context(history, max_messages=max_messages)


def normalize_room_tool_args(
    args: dict[str, Any],
    query: str = "",
) -> dict[str, Any]:
    """LLM manage_room_schedule 인자 정규화."""
    out = dict(args)
    action = str(out.get("action") or "check").strip()
    if action == "list_mine":
        action = "list"
        out["action"] = "list"

    if action in BOOKING_TARGET_ACTIONS:
        raw_bid = str(out.get("booking_id") or "").strip()
        if raw_bid and not is_valid_booking_uuid(raw_bid):
            out.pop("booking_id", None)
        elif raw_bid:
            out["booking_id"] = sanitize_booking_id(raw_bid)
        hints = _enrich_booking_target_hints(
            query=query,
            room_name=str(out.get("room_name") or "").strip() or None,
            subject=str(out.get("subject") or "").strip() or None,
            date_str=str(out.get("date") or "").strip() or None,
            start_time=str(out.get("start_time") or "").strip() or None,
            old_start_time=str(out.get("old_start_time") or "").strip() or None,
        )
        if hints["room_name"]:
            out["room_name"] = hints["room_name"]
        if hints["subject"]:
            out["subject"] = hints["subject"]
        if hints["date_str"]:
            out["date"] = hints["date_str"]
        if hints["start_time"] and not out.get("start_time"):
            out["start_time"] = hints["start_time"]
        if hints["old_start_time"] and not out.get("old_start_time"):
            out["old_start_time"] = hints["old_start_time"]
        return out

    if action == "list":
        out = apply_date_range_to_tool_args(out, query)
        person = extract_list_person_name(query, out)
        if person:
            out["person_name"] = person
        return out

    if action == "book":
        subj = str(out.get("subject") or "").strip()
        if not subj:
            out["subject"] = default_book_subject(query, None)
        return out

    if action not in ("check", "check_all"):
        return out

    q = (query or "").lower()
    use_status_mode = is_room_status_query(query) or "단일 시각" in q
    focus = extract_focus_time_label(query)

    if use_status_mode:
        _, _, day = parse_time_range_from_query(query)
        out["date"] = out.get("date") or day.isoformat()
        out.pop("start_time", None)
        out.pop("end_time", None)
        if focus:
            out["focus_time"] = focus
        tool_room = str(out.get("room_name") or "").strip()
        if tool_room:
            out["action"] = "check"
            out["room_name"] = tool_room
        elif is_generic_all_rooms_query(query) or not out.get("room_name"):
            out["action"] = "check_all"
            out.pop("room_name", None)
        return out

    start = str(out.get("start_time") or "").strip()
    end = str(out.get("end_time") or "").strip()
    if start and end and start >= end:
        try:
            st = datetime.fromisoformat(start[:19])
            out["end_time"] = (st + timedelta(hours=1)).isoformat()[:19]
        except ValueError:
            out.pop("start_time", None)
            out.pop("end_time", None)

    if action == "check" and not str(out.get("room_name") or "").strip():
        if is_generic_all_rooms_query(query):
            out["action"] = "check_all"

    return out


def build_active_room_context_block(
    history: list[dict[str, str]] | None,
) -> str:
    """Turn1 참고 — 대화에 등장한 회의실 예약·UUID (LLM 특정용)."""
    candidates = extract_room_schedule_candidates_from_history(history)
    owned = candidates["owned"]
    attendee = candidates["attendee"]
    if not owned and not attendee and not has_recent_operational_room_context(history):
        return ""

    parts = ["<active_room_context>"]
    parts.append(
        f"대화 맥락에서 확인된 회의실 일정 참고. "
        f"{_BOOKING_TARGET_ACTIONS_LABEL} 시 Turn1은 room_name·date·subject·start_time 등 "
        "식별 힌트를 tool 인자로 전달 (booking_id는 알면 함께). "
        "실행 단계에서 Outlook→DB 동기화 후 booking_id를 확정한다."
    )
    if owned:
        parts.append(f"[본인 주최 - {_BOOKING_TARGET_ACTIONS_LABEL} 가능]")
        for b in owned[-8:]:
            idx = b.get("index") or "?"
            end = f"~{b['end']}" if b.get("end") else ""
            parts.append(
                f"{idx}. booking_id={b['booking_id']} | {b['room']} | {b['subject']} | "
                f"{b['date']} {b['start']}{end}"
            )
    if attendee:
        parts.append(f"[참석 - 조회만, {_BOOKING_TARGET_ACTIONS_LABEL} 불가]")
        for b in attendee[-5:]:
            end = f"~{b['end']}" if b.get("end") else ""
            parts.append(
                f"- {b['room']} | {b['subject']} | {b['date']} {b['start']}{end}"
            )
    parts.append("</active_room_context>")
    return "\n".join(parts)


def match_room_in_query(query: str) -> bool:
    """질문에 회의실 예약 관련 키워드가 포함됐는지 확인."""
    q = (query or "").lower()
    return any(kw in q for kw in ROOM_KEYWORDS)


def list_rooms() -> str:
    """등록된 회의실 힌트 (LLM이 room_name으로 매핑)."""
    return "; ".join(graph.room_hint_list())
