"""
Microsoft Graph API — ConnBot 회의실 예약 전용 클라이언트.

Application permission (client_credentials) 사용.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date
from difflib import get_close_matches
from typing import Any
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────

CLIENT_ID = os.getenv("MS_GRAPH_CLIENT_ID")
TENANT_ID = os.getenv("MS_GRAPH_TENANT_ID")
CLIENT_SECRET = os.getenv("MS_GRAPH_CLIENT_SECRET")
SCOPES = "https://graph.microsoft.com/.default"

# 타깃 회의실 계정의 이메일 매핑 목록
ROOM_EMAIL_MAP: dict[str, str] = {
    "Spine 회의실": "spine@connecteve.com",
    "Femur 회의실": "femur@connecteve.com",
    "Atlas 회의실": "atlas@connecteve.com",
    "코넥홀": "connechall@connecteve.com",
}

# 토큰은 디스크에 저장하지 않고 프로세스 메모리에만 캐싱한다.
_TOKEN_CACHE: dict[str, object] = {"access_token": None, "expires_at": 0.0}


# ─────────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────────

def get_valid_app_token() -> str:
    """client_credentials — 애플리케이션 권한 토큰 (메모리 캐시, 디스크 미저장)."""
    cached = _TOKEN_CACHE.get("access_token")
    if cached and float(_TOKEN_CACHE.get("expires_at", 0)) > time.time() + 60:
        return str(cached)

    if not (CLIENT_ID and TENANT_ID and CLIENT_SECRET):
        raise RuntimeError(
            "MS_GRAPH_CLIENT_ID / MS_GRAPH_TENANT_ID / MS_GRAPH_CLIENT_SECRET "
            "환경변수가 필요합니다."
        )

    url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    res = requests.post(
        url,
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "scope": SCOPES,
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    res.raise_for_status()
    token_data = res.json()
    access_token = token_data["access_token"]
    expires_in = int(token_data.get("expires_in", 3600))
    _TOKEN_CACHE["access_token"] = access_token
    _TOKEN_CACHE["expires_at"] = time.time() + expires_in
    return access_token


def build_api_headers(token: str) -> dict:
    """Graph REST 공통 헤더 — KST Prefer 포함."""
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Prefer": 'outlook.timezone="Korea Standard Time"',
    }


def build_event_detail_headers(headers: dict) -> dict:
    """Get event 상세 조회용 Prefer 헤더."""
    detail_headers = dict(headers)
    detail_headers["Prefer"] = (
        'outlook.timezone="Asia/Seoul", '
        'outlook.body-content-type="text"'
    )
    return detail_headers


# ─────────────────────────────────────────────────────────────
# Room / Calendar helpers
# ─────────────────────────────────────────────────────────────

def room_hint_list() -> list[str]:
    """Turn1 tool schema·프롬프트용 회의실 힌트 (판별은 LLM이 room_name으로 수행)."""
    hints: list[str] = []
    for display, email in ROOM_EMAIL_MAP.items():
        local = email.split("@")[0]
        hints.append(f"{display} ({local})")
    return hints


def is_managed_room_email(email: str) -> bool:
    return (email or "").strip().lower() in {
        e.lower() for e in ROOM_EMAIL_MAP.values()
    }


def _room_lookup_table() -> dict[str, tuple[str, str]]:
    """표시명·메일 로컬파트 → (display, email)."""
    table: dict[str, tuple[str, str]] = {}
    for display, email in ROOM_EMAIL_MAP.items():
        pair = (display, email)
        local = email.split("@")[0].lower()
        table[display.lower()] = pair
        table[display.lower().replace(" ", "")] = pair
        table[local] = pair
    return table


def resolve_room(room_name: str) -> tuple[str, str]:
    """
    LLM이 넘긴 room_name → (표시명, 이메일).

    ROOM_EMAIL_MAP 표시명·메일 로컬파트 기준 exact/fuzzy 매칭만 수행한다.
    오타·별칭 해석은 Turn1 LLM이 room_name에 반영한다.
    """
    raw = (room_name or "").strip()
    if not raw:
        return "", ""
    norm = raw.lower().replace(" ", "")
    table = _room_lookup_table()
    if norm in table:
        return table[norm]
    if raw.lower() in table:
        return table[raw.lower()]

    close = get_close_matches(norm, list(table.keys()), n=1, cutoff=0.72)
    if close:
        return table[close[0]]

    return f"{raw.upper()} 회의실", f"{norm}@connecteve.com"


def _day_view_range(day: date | None = None) -> tuple[str, str]:
    """KST 하루 구간 (Prefer: Korea Standard Time 와 함께 사용)."""
    d = day or date.today()
    day_str = d.isoformat()
    return f"{day_str}T00:00:00", f"{day_str}T23:59:59"


def _date_view_range(start_date: date, end_date_exclusive: date) -> tuple[str, str]:
    """KST 날짜 범위. end_date_exclusive는 포함하지 않는다."""
    return (
        f"{start_date.isoformat()}T00:00:00",
        f"{end_date_exclusive.isoformat()}T00:00:00",
    )


def fetch_calendar_view(
    headers: dict,
    mailbox_email: str,
    *,
    day: date | None = None,
) -> list[dict]:
    """calendarView 조회 (출력 없음)."""
    start, end = _day_view_range(day)
    url = f"https://graph.microsoft.com/v1.0/users/{mailbox_email}/calendar/calendarView"
    params = {"startDateTime": start, "endDateTime": end}
    res = requests.get(url, headers=headers, params=params, timeout=30)
    res.raise_for_status()
    return res.json().get("value", [])


def fetch_calendar_view_between(
    headers: dict,
    mailbox_email: str,
    *,
    start_date: date,
    end_date_exclusive: date,
) -> list[dict]:
    """calendarView 범위 조회. end_date_exclusive는 포함하지 않는다."""
    start, end = _date_view_range(start_date, end_date_exclusive)
    url = f"https://graph.microsoft.com/v1.0/users/{mailbox_email}/calendar/calendarView"
    params = {"startDateTime": start, "endDateTime": end}
    res = requests.get(url, headers=headers, params=params, timeout=30)
    res.raise_for_status()
    return res.json().get("value", [])


EVENT_DETAIL_SELECT = (
    "subject,organizer,attendees,bodyPreview,body,start,end,location,"
    "hideAttendees,sensitivity"
)


def event_is_private(event: dict | None) -> bool:
    """Graph event — 비공개 여부는 sensitivity=='private' (isPrivate는 scheduleItem 속성)."""
    if not event:
        return False
    return str(event.get("sensitivity") or "").lower() == "private"


def fetch_event_detail(
    headers: dict,
    mailbox_email: str,
    event_id: str,
) -> dict | None:
    """Get event — event.id로 organizer/attendees/body/start/end/location 상세 조회."""
    eid = (event_id or "").strip()
    if not eid:
        return None

    encoded_event_id = quote(eid, safe="")
    url = f"https://graph.microsoft.com/v1.0/users/{mailbox_email}/events/{encoded_event_id}"
    res = requests.get(
        url,
        headers=build_event_detail_headers(headers),
        params={"$select": EVENT_DETAIL_SELECT},
        timeout=30,
    )
    if res.status_code == 404:
        return None
    res.raise_for_status()
    return res.json()


def fetch_calendar_events_with_details(
    headers: dict,
    mailbox_email: str,
    *,
    day: date | None = None,
) -> list[dict]:
    """calendarView 목록을 가져온 뒤 각 event.id로 상세 이벤트를 보강."""
    events = fetch_calendar_view(headers, mailbox_email, day=day)
    detailed_events: list[dict] = []
    for event in events:
        event_id = str(event.get("id") or "").strip()
        if not event_id:
            detailed_events.append(event)
            continue
        try:
            detailed_events.append(fetch_event_detail(headers, mailbox_email, event_id) or event)
        except requests.exceptions.HTTPError:
            detailed_events.append(event)
    return detailed_events


def fetch_calendar_events_with_details_between(
    headers: dict,
    mailbox_email: str,
    *,
    start_date: date,
    end_date_exclusive: date,
) -> list[dict]:
    """calendarView 범위 목록 + 각 event.id 상세 보강."""
    events = fetch_calendar_view_between(
        headers,
        mailbox_email,
        start_date=start_date,
        end_date_exclusive=end_date_exclusive,
    )
    detailed_events: list[dict] = []
    for event in events:
        event_id = str(event.get("id") or "").strip()
        if not event_id:
            detailed_events.append(event)
            continue
        try:
            detailed_events.append(fetch_event_detail(headers, mailbox_email, event_id) or event)
        except requests.exceptions.HTTPError:
            detailed_events.append(event)
    return detailed_events


def _event_bounds(ev: dict) -> tuple[str, str]:
    """Graph event start/end dateTime (로컬, [:19])."""
    start = str((ev.get("start") or {}).get("dateTime") or "")[:19]
    end = str((ev.get("end") or {}).get("dateTime") or "")[:19]
    return start, end


def times_overlap(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    """ISO 로컬 시각 문자열 구간 겹침 (KST)."""
    sa, ea = start_a[:19], end_a[:19]
    sb, eb = start_b[:19], end_b[:19]
    if not all([sa, ea, sb, eb]):
        return False
    return sa < eb and ea > sb


def slot_conflicts_with_events(
    events: list[dict],
    start_time: str,
    end_time: str,
    *,
    exclude_event_subjects: list[str] | None = None,
    exclude_time_ranges: list[tuple[str, str]] | None = None,
) -> dict | None:
    """겹치는 첫 이벤트 반환, 없으면 None."""
    excludes = [s.strip() for s in (exclude_event_subjects or []) if s and s.strip()]
    skip_ranges = [
        (s[:19], e[:19])
        for s, e in (exclude_time_ranges or [])
        if s and e
    ]
    for ev in events:
        ev_start, ev_end = _event_bounds(ev)
        if skip_ranges and any(ev_start == rs and ev_end == re for rs, re in skip_ranges):
            continue
        if excludes:
            subj = str(ev.get("subject") or "")
            if any(ex in subj for ex in excludes):
                continue
        if times_overlap(start_time, end_time, ev_start, ev_end):
            return ev
    return None


def is_room_slot_available(
    headers: dict,
    room_email: str,
    start_time: str,
    end_time: str,
    *,
    day: date | None = None,
    exclude_event_subjects: list[str] | None = None,
    exclude_time_ranges: list[tuple[str, str]] | None = None,
) -> tuple[bool, str]:
    """회의실 캘린더 기준 슬롯 가용 여부 — (True, '') 또는 (False, 사유)."""
    booking_day = day or date.fromisoformat(start_time[:10])
    events = fetch_calendar_view(headers, room_email, day=booking_day)
    conflict = slot_conflicts_with_events(
        events,
        start_time,
        end_time,
        exclude_event_subjects=exclude_event_subjects,
        exclude_time_ranges=exclude_time_ranges,
    )
    if not conflict:
        return True, ""
    ev_start, ev_end = _event_bounds(conflict)
    subj = conflict.get("subject", "(제목 없음)")
    return (
        False,
        f"{ev_start[:16].replace('T', ' ')}~{ev_end[11:16].replace('T', ' ')} "
        f"'{subj}' 와 겹칩니다.",
    )


# ─────────────────────────────────────────────────────────────
# User lookup (참석자 resolve)
# ─────────────────────────────────────────────────────────────

def search_graph_user(
    headers: dict,
    *,
    email: str = "",
    name: str = "",
) -> dict | None:
    """Graph /users 로 mail·displayName 조회 → {email, name} 또는 None."""
    addr = email.strip()
    if addr and "@" in addr:
        url = f"https://graph.microsoft.com/v1.0/users/{addr}"
        try:
            res = requests.get(
                url,
                headers=headers,
                params={"$select": "displayName,mail,userPrincipalName"},
                timeout=30,
            )
            if res.status_code != 404:
                res.raise_for_status()
                data = res.json()
                mail = str(data.get("mail") or data.get("userPrincipalName") or addr)
                return {
                    "email": mail,
                    "name": str(data.get("displayName") or mail.split("@")[0]),
                }
        except requests.exceptions.HTTPError:
            pass

    query_name = name.strip()
    if not query_name:
        return None

    safe = query_name.replace("'", "''")
    filter_expr = (
        f"startswith(displayName,'{safe}') or "
        f"startswith(givenName,'{safe}') or "
        f"startswith(surname,'{safe}')"
    )
    try:
        res = requests.get(
            "https://graph.microsoft.com/v1.0/users",
            headers=headers,
            params={
                "$filter": filter_expr,
                "$select": "displayName,mail,userPrincipalName",
                "$top": 5,
            },
            timeout=30,
        )
        res.raise_for_status()
        values = res.json().get("value") or []
        if not values:
            return None
        if len(values) > 1:
            exact = [
                u for u in values
                if str(u.get("displayName") or "").strip() == query_name
            ]
            pick = exact[0] if len(exact) == 1 else values[0]
        else:
            pick = values[0]
        mail = str(pick.get("mail") or pick.get("userPrincipalName") or "").strip()
        if not mail:
            return None
        return {
            "email": mail,
            "name": str(pick.get("displayName") or mail.split("@")[0]),
        }
    except requests.exceptions.HTTPError:
        return None


# ─────────────────────────────────────────────────────────────
# Event CRUD
# ─────────────────────────────────────────────────────────────

def _create_calendar_event(headers: dict, mailbox_email: str, payload: dict) -> dict:
    """지정 사서함 캘린더에 일정 1건 생성."""
    url = f"https://graph.microsoft.com/v1.0/users/{mailbox_email}/calendar/events"
    res = requests.post(url, headers=headers, json=payload, timeout=30)
    res.raise_for_status()
    return res.json()


def delete_calendar_event(headers: dict, mailbox_email: str, event_id: str) -> None:
    """지정 사서함 캘린더 일정 1건 삭제."""
    url = f"https://graph.microsoft.com/v1.0/users/{mailbox_email}/events/{event_id}"
    res = requests.delete(url, headers=headers, timeout=30)
    res.raise_for_status()


def update_calendar_event(
    headers: dict,
    mailbox_email: str,
    event_id: str,
    patch_body: dict,
) -> dict:
    """지정 사서함 캘린더 일정 1건 PATCH 수정."""
    url = f"https://graph.microsoft.com/v1.0/users/{mailbox_email}/events/{event_id}"
    res = requests.patch(url, headers=headers, json=patch_body, timeout=30)
    res.raise_for_status()
    return res.json()


def _event_covers_slot(event: dict, *, subject: str, start_time: str) -> bool:
    """제목·시작 시각이 예약과 일치하는지 확인."""
    ev_subject = str(event.get("subject") or "")
    ev_start = str((event.get("start") or {}).get("dateTime") or "")[:16]
    return subject in ev_subject and ev_start == start_time[:16]


def verify_room_booking(
    headers: dict,
    room_email: str,
    *,
    subject: str,
    start_time: str,
    day: date | None = None,
    retries: int = 5,
    delay_sec: float = 2.0,
) -> bool:
    """회의실 자동 수락 후 calendarView에 일정이 잡혔는지 확인."""
    for attempt in range(1, retries + 1):
        events = fetch_calendar_view(headers, room_email, day=day)
        if any(_event_covers_slot(ev, subject=subject, start_time=start_time) for ev in events):
            logger.info("[GraphRoom] room calendar confirmed (attempt %s)", attempt)
            return True
        if attempt < retries:
            time.sleep(delay_sec)
    logger.warning("[GraphRoom] room calendar not confirmed after %s attempts", retries)
    return False


def _build_event_attendees(
    room_display: str,
    room_email: str,
    *,
    required_attendees: list[dict[str, str]] | None = None,
    organizer_email: str = "",
) -> list[dict]:
    """Graph API attendees — required 참석자 + resource 회의실 (주최자는 제외)."""
    seen: set[str] = set()
    payload: list[dict] = []
    organizer_key = organizer_email.strip().lower()

    for item in required_attendees or []:
        email = str(item.get("email") or item.get("address") or "").strip()
        name = str(item.get("name") or "").strip()
        key = email.lower()
        if not email or key == organizer_key or key in seen:
            continue
        seen.add(key)
        payload.append({
            "emailAddress": {
                "address": email,
                "name": (name or email.split("@")[0]).strip(),
            },
            "type": "required",
        })

    payload.append({
        "emailAddress": {"address": room_email, "name": room_display},
        "type": "resource",
    })
    return payload


def create_room_reservation(
    headers: dict,
    organizer_email: str,
    room_name: str,
    subject: str,
    start_time: str,
    end_time: str,
    *,
    required_attendees: list[dict[str, str]] | None = None,
) -> dict:
    """
    주최자 캘린더 1건 생성 + required 참석자 + resource 회의실 초대.

    book/modify 전 is_room_slot_available 로 겹침 검사.
    """
    room_display, room_email = resolve_room(room_name)
    short = room_name.strip().upper()
    event_subject = f"[{short}] {subject}"
    booking_day = date.fromisoformat(start_time[:10])

    avail_ok, avail_msg = is_room_slot_available(
        headers, room_email, start_time, end_time, day=booking_day,
    )
    if not avail_ok:
        return {
            "status": "error",
            "message": f"해당 시간은 이미 예약되어 있습니다. {avail_msg}",
        }

    attendees = _build_event_attendees(
        room_display,
        room_email,
        required_attendees=required_attendees,
        organizer_email=organizer_email,
    )

    payload = {
        "subject": event_subject,
        "start": {"dateTime": start_time, "timeZone": "Korea Standard Time"},
        "end": {"dateTime": end_time, "timeZone": "Korea Standard Time"},
        "attendees": attendees,
        "location": {"displayName": room_display},
    }

    try:
        org_data = _create_calendar_event(headers, organizer_email, payload)
        organizer_event_id = org_data.get("id")
        logger.info("[GraphRoom] event created organizer=%s", organizer_email)
    except requests.exceptions.HTTPError as err:
        return {"status": "error", "message": err.response.text}
    except Exception as e:
        return {"status": "error", "message": str(e)}

    room_confirmed = verify_room_booking(
        headers,
        room_email,
        subject=event_subject,
        start_time=start_time,
        day=booking_day,
    )

    status = "success" if room_confirmed else "partial"
    message = (
        f"{room_display} — 예약 완료, 회의실 캘린더 반영 확인."
        if room_confirmed
        else f"{room_display} — 예약 등록됨, 회의실 미반영(자동 수락 확인 필요)."
    )
    return {
        "status": status,
        "message": message,
        "organizer_event_id": organizer_event_id,
        "room_confirmed": room_confirmed,
        "room_display": room_display,
        "room_name": room_name.strip(),
        "subject": subject,
        "event_subject": event_subject,
        "start_time": start_time,
        "end_time": end_time,
        "organizer_email": organizer_email,
    }


def update_room_reservation(
    headers: dict,
    organizer_email: str,
    event_id: str,
    room_name: str,
    start_time: str,
    end_time: str,
    *,
    new_subject: str | None = None,
    required_attendees: list[dict[str, str]] | None = None,
    patch_attendees: bool = False,
) -> dict:
    """
    주최자 캘린더 일정 PATCH — 시간·제목·참석자 변경.

    patch_attendees=True이면 required_attendees로 attendees 전체 교체.
    """
    if not event_id:
        return {"status": "error", "message": "event_id 없음"}

    room_display, room_email = resolve_room(room_name)
    patch_body: dict[str, Any] = {
        "start": {"dateTime": start_time, "timeZone": "Korea Standard Time"},
        "end": {"dateTime": end_time, "timeZone": "Korea Standard Time"},
    }
    event_subject: str | None = None
    if new_subject:
        short = room_name.strip().upper()
        event_subject = f"[{short}] {new_subject.strip()}"
        patch_body["subject"] = event_subject

    if patch_attendees:
        patch_body["attendees"] = _build_event_attendees(
            room_display,
            room_email,
            required_attendees=required_attendees,
            organizer_email=organizer_email,
        )

    try:
        data = update_calendar_event(headers, organizer_email, event_id, patch_body)
        return {
            "status": "success",
            "message": "일정 변경 완료 (회의실 캘린더에 반영).",
            "organizer_event_id": data.get("id") or event_id,
            "room_display": room_display,
            "room_name": room_name.strip(),
            "subject": (new_subject or "").strip() or None,
            "event_subject": event_subject,
            "start_time": start_time,
            "end_time": end_time,
        }
    except requests.exceptions.HTTPError as err:
        return {"status": "error", "message": err.response.text}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def cancel_room_reservation(
    headers: dict,
    organizer_email: str,
    event_id: str,
) -> dict:
    """주최자 캘린더 일정 삭제 — resource 회의실에 취소 전파."""
    if not event_id:
        return {"status": "error", "message": "event_id 없음"}
    try:
        delete_calendar_event(headers, organizer_email, event_id)
        return {
            "status": "success",
            "message": "일정 삭제 완료 (회의실 자동 취소).",
        }
    except requests.exceptions.HTTPError as err:
        return {"status": "error", "message": err.response.text}
    except Exception as e:
        return {"status": "error", "message": str(e)}
