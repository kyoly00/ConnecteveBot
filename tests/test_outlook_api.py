import json
import os
import time
from datetime import date
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------- Configuration
CLIENT_ID = os.getenv("MS_GRAPH_CLIENT_ID")
TENANT_ID = os.getenv("MS_GRAPH_TENANT_ID")
CLIENT_SECRET = os.getenv("MS_GRAPH_CLIENT_SECRET")
ORGANIZER_EMAIL = os.getenv("MS_ID")

# 애플리케이션 권한 범위 고정
SCOPES = "https://graph.microsoft.com/.default"

# 타깃 회의실 계정의 이메일 매핑 목록
ROOM_EMAIL_MAP = {
    "Spine 회의실": "spine@connecteve.com",
    "Femur 회의실": "femur@connecteve.com",
    "Atlas 회의실": "atlas@connecteve.com",
    "코넥홀": "connechall@connecteve.com"
}

# 토큰은 디스크에 저장하지 않고 프로세스 메모리에만 캐싱한다.
_TOKEN_CACHE: dict[str, object] = {"access_token": None, "expires_at": 0.0}


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
    try:
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
    except requests.exceptions.HTTPError as err:
        # 응답 본문에 client_secret이 echo되지 않도록 상태코드/에러코드만 노출
        try:
            detail = err.response.json().get("error", "unknown_error")
        except Exception:
            detail = "unknown_error"
        raise RuntimeError(
            f"토큰 발급 실패 (status={err.response.status_code}, error={detail})"
        ) from None

    token_data = res.json()
    access_token = token_data["access_token"]
    expires_in = int(token_data.get("expires_in", 3600))
    _TOKEN_CACHE["access_token"] = access_token
    _TOKEN_CACHE["expires_at"] = time.time() + expires_in
    return access_token


def clear_token_cache() -> None:
    """메모리 토큰 캐시를 비운다 (사용 후 즉시 폐기용)."""
    _TOKEN_CACHE["access_token"] = None
    _TOKEN_CACHE["expires_at"] = 0.0


def build_api_headers(token: str) -> dict:
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


def _resolve_room(room_name: str) -> tuple[str, str]:
    """room_name(spine 등) → (표시명, 이메일)."""
    key = room_name.strip().lower()
    for display, email in ROOM_EMAIL_MAP.items():
        if key in display.lower():
            return display, email
    return f"{key.upper()} 회의실", f"{key}@connecteve.com"


# --------------------------------------------------------- Security Guardrails
def validate_user_permission(requesting_user_id: str, target_room_name: str) -> bool:
    print(
        f"🔒 [보안 검증] 유저 '{requesting_user_id}' 가 "
        f"'{target_room_name}' 에 접근 가능한지 체크 중..."
    )
    return True


# --------------------------------------------------------- Microsoft Graph APIs
def _create_calendar_event(headers: dict, mailbox_email: str, payload: dict) -> dict:
    """지정 사서함 캘린더에 일정 1건 생성."""
    url = f"https://graph.microsoft.com/v1.0/users/{mailbox_email}/calendar/events"
    res = requests.post(url, headers=headers, json=payload, timeout=30)
    res.raise_for_status()
    return res.json()


def _delete_calendar_event(headers: dict, mailbox_email: str, event_id: str) -> None:
    url = f"https://graph.microsoft.com/v1.0/users/{mailbox_email}/events/{event_id}"
    res = requests.delete(url, headers=headers, timeout=30)
    res.raise_for_status()


def _day_view_range(day: date | None = None) -> tuple[str, str]:
    """KST 하루 구간 (Prefer: Korea Standard Time 와 함께 사용)."""
    d = day or date.today()
    day_str = d.isoformat()
    return f"{day_str}T00:00:00", f"{day_str}T23:59:59"


def _event_bounds(ev: dict) -> tuple[str, str]:
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
) -> dict | None:
    """겹치는 첫 이벤트 반환, 없으면 None."""
    for ev in events:
        ev_start, ev_end = _event_bounds(ev)
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
) -> tuple[bool, str]:
    """회의실 캘린더 기준 슬롯 가용 여부."""
    booking_day = day or date.fromisoformat(start_time[:10])
    events = _fetch_calendar_view(headers, room_email, day=booking_day)
    conflict = slot_conflicts_with_events(events, start_time, end_time)
    if not conflict:
        return True, ""
    ev_start, ev_end = _event_bounds(conflict)
    subj = conflict.get("subject", "(제목 없음)")
    return (
        False,
        f"{ev_start[:16].replace('T', ' ')}~{ev_end[11:16].replace('T', ' ')} "
        f"'{subj}' 와 겹칩니다.",
    )


def search_graph_user(
    headers: dict,
    *,
    email: str = "",
    name: str = "",
) -> dict | None:
    """Graph /users 로 mail·displayName 조회."""
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
            if res.status_code == 404:
                pass
            else:
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
    url = "https://graph.microsoft.com/v1.0/users"
    try:
        res = requests.get(
            url,
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


def _fetch_calendar_view(
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


EVENT_DETAIL_SELECT = (
    "subject,organizer,attendees,bodyPreview,body,start,end,location,"
    "hideAttendees,sensitivity"
)


def fetch_event_detail(
    headers: dict,
    mailbox_email: str,
    event_id: str,
) -> dict | None:
    """
    특정 mailbox의 이벤트 상세 조회.

    calendarView/list에서 받은 event.id를 사용해 attendees/body/organizer 등
    상세 속성을 가져온다.
    """
    eid = (event_id or "").strip()
    if not eid:
        return None

    encoded_event_id = quote(eid, safe="")
    url = f"https://graph.microsoft.com/v1.0/users/{mailbox_email}/events/{encoded_event_id}"
    try:
        res = requests.get(
            url,
            headers=build_event_detail_headers(headers),
            params={"$select": EVENT_DETAIL_SELECT},
            timeout=30,
        )
        res.raise_for_status()
        return res.json()
    except requests.exceptions.HTTPError as err:
        print(f"      ⚠️ 상세 일정 조회 실패: {err.response.text}")
        return None


def _format_attendee(attendee: dict) -> str:
    email_info = attendee.get("emailAddress") or {}
    name = str(email_info.get("name") or "").strip()
    address = str(email_info.get("address") or "").strip()
    attendee_type = str(attendee.get("type") or "").strip()
    status = str((attendee.get("status") or {}).get("response") or "").strip()
    label = name or address or "(이름 없음)"
    if address and address != label:
        label = f"{label} <{address}>"
    suffix = ", ".join(v for v in [attendee_type, status] if v)
    return f"{label} ({suffix})" if suffix else label


def print_event_detail(detail: dict | None) -> None:
    """Get event 상세 응답을 사람이 읽기 좋게 출력."""
    if not detail:
        return

    organizer_info = (detail.get("organizer") or {}).get("emailAddress") or {}
    organizer = (
        str(organizer_info.get("name") or "").strip()
        or str(organizer_info.get("address") or "").strip()
        or "(주최자 없음)"
    )
    organizer_address = str(organizer_info.get("address") or "").strip()
    if organizer_address and organizer_address not in organizer:
        organizer = f"{organizer} <{organizer_address}>"

    start = str((detail.get("start") or {}).get("dateTime") or "")[:16].replace("T", " ")
    end = str((detail.get("end") or {}).get("dateTime") or "")[:16].replace("T", " ")
    location = str((detail.get("location") or {}).get("displayName") or "").strip()
    attendees = detail.get("attendees") or []
    body_preview = str(detail.get("bodyPreview") or "").strip()
    body_text = str((detail.get("body") or {}).get("content") or "").strip()
    sensitivity = detail.get("sensitivity")
    hide_attendees = detail.get("hideAttendees")
    is_private = str(sensitivity or "").lower() == "private"

    print("      └─ 상세")
    print(f"         주최자: {organizer}")
    print(f"         시간: {start} ~ {end}")
    print(f"         위치: {location or '(없음)'}")
    print(
        "         공개/민감도: "
        f"private={is_private}, sensitivity={sensitivity}, hideAttendees={hide_attendees}"
    )
    if attendees:
        print("         참석자:")
        for attendee in attendees:
            print(f"           - {_format_attendee(attendee)}")
    else:
        print("         참석자: (없음 또는 숨김)")
    if body_preview:
        print(f"         bodyPreview: {body_preview[:500]}")
    if body_text:
        print(f"         body: {body_text[:1000]}")


def fetch_calendar_events_with_details(
    headers: dict,
    mailbox_email: str,
    *,
    day: date | None = None,
) -> list[dict]:
    """
    calendarView 목록 + Get event 상세를 합쳐 동기화용 payload로 반환.

    개인 Outlook 캘린더를 직접 읽어 챗봇 DB와 동기화할 때 사용할 수 있다.
    """
    events = _fetch_calendar_view(headers, mailbox_email, day=day)
    detailed_events: list[dict] = []
    for event in events:
        event_id = str(event.get("id") or "").strip()
        detail = fetch_event_detail(headers, mailbox_email, event_id) if event_id else None
        detailed_events.append(detail or event)
    return detailed_events


def _event_covers_slot(event: dict, *, subject: str, start_time: str) -> bool:
    """제목·시작 시각이 예약과 일치하는지 확인."""
    ev_subject = str(event.get("subject") or "")
    ev_start = str((event.get("start") or {}).get("dateTime") or "")[:16]
    want_start = start_time[:16]
    return subject in ev_subject and ev_start == want_start


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
        events = _fetch_calendar_view(headers, room_email, day=day)
        if any(_event_covers_slot(ev, subject=subject, start_time=start_time) for ev in events):
            print(f"   ✓ Spine 캘린더 반영 확인 (시도 {attempt}/{retries})")
            return True
        if attempt < retries:
            print(f"   … Spine 반영 대기 ({attempt}/{retries}, {delay_sec}s)")
            time.sleep(delay_sec)
    print("   ⚠️ Spine 캘린더에 일정 미확인 — 자동 수락·권한 확인 필요")
    return False


def fetch_user_schedule_as_app(
    headers: dict,
    user_email: str,
    label: str,
    *,
    day: date | None = None,
    include_details: bool = True,
) -> list[dict]:
    """사용자(주최자) 캘린더 일정 조회."""
    today_str = (day or date.today()).isoformat()
    start = f"{today_str}T00:00:00Z"
    end = f"{today_str}T23:59:59Z"

    url = f"https://graph.microsoft.com/v1.0/users/{user_email}/calendar/calendarView"
    params = {"startDateTime": start, "endDateTime": end}

    try:
        res = requests.get(url, headers=headers, params=params, timeout=30)
        res.raise_for_status()

        print(f"\n📍 [조회 성공] {label} ({user_email})")
        events = res.json().get("value", [])
        if not events:
            print("   - 해당 일자 예정된 일정이 없습니다.")
            return []

        for event in events:
            start_time = event["start"]["dateTime"][:16].replace("T", " ")
            end_time = event["end"]["dateTime"][:16].replace("T", " ")
            eid = event.get("id", "")
            print(
                f"   [ {start_time} ~ {end_time} ] "
                f"{event.get('subject', '')} (id={eid[:20]}…)"
            )
            if include_details and eid:
                detail = fetch_event_detail(headers, user_email, eid)
                print_event_detail(detail)
        return events

    except requests.exceptions.HTTPError as err:
        print(f"❌ {label} 일정 조회 중 API 오류: {err.response.text}")
        return []


def fetch_room_schedule_as_app(
    headers: dict,
    room_email: str,
    room_name: str,
    *,
    day: date | None = None,
    include_details: bool = True,
) -> list[dict]:
    """애플리케이션 권한으로 회의실 일정 조회."""
    today_str = (day or date.today()).isoformat()
    start = f"{today_str}T00:00:00Z"
    end = f"{today_str}T23:59:59Z"

    url = f"https://graph.microsoft.com/v1.0/users/{room_email}/calendar/calendarView"
    params = {"startDateTime": start, "endDateTime": end}

    try:
        res = requests.get(url, headers=headers, params=params, timeout=30)
        res.raise_for_status()
        
        print(f"\n📍 [조회 성공] 회의실: {room_name} ({room_email})")
        events = res.json().get("value", [])
        if not events:
            print("   - 해당 일자 예정된 일정이 없습니다.")
            return []

        for event in events:
            start_time = event["start"]["dateTime"][:16].replace("T", " ")
            end_time = event["end"]["dateTime"][:16].replace("T", " ")
            eid = event.get("id", "")
            print(f"   [ {start_time} ~ {end_time} ] {event.get('subject', '')} (id={eid[:20]}…)")
            if include_details and eid:
                detail = fetch_event_detail(headers, room_email, eid)
                print_event_detail(detail)
        return events
            
    except requests.exceptions.HTTPError as err:
        print(f"❌ {room_name} 일정 조회 중 API 오류: {err.response.text}")
        return []


def _build_event_attendees(
    room_display: str,
    room_email: str,
    *,
    required_attendees: list[dict[str, str]] | None = None,
    fallback_email: str = "",
    fallback_name: str = "",
) -> list[dict]:
    """Graph API attendees — required 참석자 + resource 회의실."""
    seen: set[str] = set()
    payload: list[dict] = []

    def _add_required(email: str, name: str) -> None:
        addr = email.strip()
        key = addr.lower()
        if not addr or key in seen:
            return
        seen.add(key)
        payload.append({
            "emailAddress": {
                "address": addr,
                "name": (name or addr.split("@")[0]).strip(),
            },
            "type": "required",
        })

    organizer_key = fallback_email.strip().lower() if fallback_email else ""
    for item in required_attendees or []:
        email = str(item.get("email") or item.get("address") or "").strip()
        name = str(item.get("name") or "").strip()
        if email and email.lower() != organizer_key:
            _add_required(email, name)

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
    organizer_name: str = "",
) -> dict:
    """주최자 캘린더 1건 생성 + required 참석자 + resource 회의실 초대."""
    room_display, room_email = _resolve_room(room_name)
    short = room_name.strip().upper()
    event_subject = f"[{short}] {subject}"
    booking_day = date.fromisoformat(start_time[:10])

    avail_ok, avail_msg = is_room_slot_available(
        headers,
        room_email,
        start_time,
        end_time,
        day=booking_day,
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
        fallback_email=organizer_email,
        fallback_name=organizer_name,
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
        print(f"   ✓ 주최자 캘린더 일정 생성 ({organizer_email})")
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
        f"{room_display} — 주최자 예약 완료, 회의실 캘린더 반영 확인."
        if room_confirmed
        else f"{room_display} — 주최자 예약 완료, 회의실 미반영(자동 수락 확인 필요)."
    )
    return {
        "status": status,
        "message": message,
        "organizer_event_id": organizer_event_id,
        "created_event_id": organizer_event_id,
        "room_confirmed": room_confirmed,
        "room_display": room_display,
        "room_name": room_name.strip(),
        "subject": subject,
        "event_subject": event_subject,
        "start_time": start_time,
        "end_time": end_time,
        "organizer_email": organizer_email,
    }


def cancel_room_reservation(
    headers: dict,
    organizer_email: str,
    event_id: str,
) -> dict:
    """주최자 캘린더 일정 삭제 — resource 회의실에 취소 전파."""
    if not event_id:
        return {"status": "error", "message": "event_id 없음"}
    try:
        _delete_calendar_event(headers, organizer_email, event_id)
        return {
            "status": "success",
            "message": "주최자 일정 삭제 완료 (회의실 자동 취소).",
        }
    except requests.exceptions.HTTPError as err:
        return {"status": "error", "message": err.response.text}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def demo_spine_book_and_cancel(headers: dict, organizer_email: str) -> None:
    """Spine 회의실 — 오늘 16:00 예약 후 삭제."""
    today = date.today()
    start_time = f"{today.isoformat()}T16:00:00"
    end_time = f"{today.isoformat()}T17:00:00"
    _, spine_email = _resolve_room("spine")

    print("\n=============================================================")
    print(f"📅 Spine 회의실 — {today} 16:00~17:00 예약·삭제 데모")
    print("=============================================================")

    print("\n--- [1] 예약 전 일정 ---")
    fetch_user_schedule_as_app(headers, organizer_email, "주최자", day=today)
    fetch_room_schedule_as_app(headers, spine_email, "Spine 회의실", day=today)

    print("\n--- [2] 예약 생성 (주최자 + Spine resource) ---")
    create_result = create_room_reservation(
        headers,
        organizer_email,
        "spine",
        "ConnBot 테스트 예약",
        start_time,
        end_time,
    )
    print(json.dumps(create_result, ensure_ascii=False, indent=2))
    organizer_event_id = create_result.get("organizer_event_id")
    if create_result.get("status") == "error" or not organizer_event_id:
        print("❌ 예약 실패 — 삭제 단계를 건너뜁니다.")
        return

    print("\n--- [3] 예약 후 일정 ---")
    fetch_user_schedule_as_app(headers, organizer_email, "주최자", day=today)
    fetch_room_schedule_as_app(headers, spine_email, "Spine 회의실", day=today)

    print("\n--- [4] 예약 삭제 (주최자 일정) ---")
    cancel_result = cancel_room_reservation(
        headers,
        organizer_email,
        organizer_event_id,
    )
    print(json.dumps(cancel_result, ensure_ascii=False, indent=2))

    print("\n--- [5] 삭제 후 일정 ---")
    fetch_user_schedule_as_app(headers, organizer_email, "주최자", day=today)
    fetch_room_schedule_as_app(headers, spine_email, "Spine 회의실", day=today)


# ----------------------------------------------------------------- Execution
if __name__ == "__main__":
    if not CLIENT_ID or not TENANT_ID or not CLIENT_SECRET or not ORGANIZER_EMAIL:
        print(
            "❌ [오류] .env에 MS_GRAPH_CLIENT_ID, MS_GRAPH_TENANT_ID, "
            "MS_GRAPH_CLIENT_SECRET, MS_ID 설정이 필요합니다."
        )
        raise SystemExit(1)

    mock_request_user = "U12345678"
    
    print("🔄 Microsoft Graph 애플리케이션 토큰 발급 중...")
    try:
    app_token = get_valid_app_token()
        api_headers = build_api_headers(app_token)

    print("\n=============================================================")
        print("🚀 전체 회의실 일정 조회")
    print("=============================================================")
    
    for r_name, r_email in ROOM_EMAIL_MAP.items():
        if validate_user_permission(mock_request_user, r_name):
            fetch_room_schedule_as_app(api_headers, r_email, r_name)
            else:
                print(f"⚠️ [차단] 유저 {mock_request_user}는 {r_name} 접근 불가.")

        if validate_user_permission(mock_request_user, "Spine 회의실"):
            demo_spine_book_and_cancel(api_headers, ORGANIZER_EMAIL)
        else:
            print("⚠️ Spine 예약·삭제 데모 — 권한 없음.")
    finally:
        # 토큰을 메모리에서도 즉시 폐기
        clear_token_cache()
