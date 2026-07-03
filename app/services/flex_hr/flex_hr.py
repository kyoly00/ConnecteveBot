"""Flex HR 근태 데이터 — 수집·인덱스·Slack 브리핑·챗봇 tool 조회."""

from __future__ import annotations

import sys
import json
import os
import re
import itertools
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import requests
from dotenv import load_dotenv
from pytz import timezone

KST = timezone("Asia/Seoul")

_CONN_BOT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _CONN_BOT_DIR.parent
for _path in (_CONN_BOT_DIR, _REPO_ROOT):
    _s = str(_path)
    if _s not in sys.path:
        sys.path.insert(0, _s)

from app.core.config import (
    FLEX_HR_DIR,
    FLEX_HR_EMPLOYEE_ROSTER,
    FLEX_HR_LATEST_INDEX,
    FLEX_HR_MONTHLY_CATALOG_INDEX,
    flex_hr_monthly_json_path,
    now_iso,
)
from app.services.business_calendar import (
    next_business_day_run as next_flex_hr_roster_run,
    should_skip_daily_notification as should_skip_flex_hr_daily_roster,
)
from app.services.date_range import apply_date_range_to_tool_args
from app.services.flex_hr.flex_parser import LEAVE_TYPES

load_dotenv()

REMOTE_TYPE = "재택근무"
VACATION_TYPE = "휴가"
OUTING_TYPE = "외근"
OVERSEAS_TYPE = "출장"
DEFAULT_AFTERNOON_LEAVE_END = "오후 6:00"

# 질문 키워드 → roster employees[].team 값
TEAM_QUERY_ALIASES: dict[str, tuple[str, ...]] = {
    "Operation": ("운영", "운영팀", "operation", "오퍼레이션"),
    "RA": ("ra", "로봇", "robot", "로봇팀", "ra팀"),
    "SW": ("sw", "개발", "개발팀", "소프트웨어", "sw팀"),
    "AI": ("ai", "ai팀"),
    "Sales": ("영업", "영업팀", "sales", "세일즈", "마케팅"),
    "경영": ("경영", "경영팀", "경영진", "대표"),
    "QA": ("qa", "qa팀"),
    "사업화": ("사업화", "사업화팀"),
}

# 질문 키워드 → roster employees[].role 부분 일치
ROLE_QUERY_ALIASES: dict[str, tuple[str, ...]] = {
    "CEO": ("ceo", "대표", "대표님", "경영총괄"),
    "CFO": ("cfo", "재무총괄", "재무"),
    "CTO": ("cto", "기술연구개발"),
    "CBO": ("cbo",),
    "CPO": ("cpo", "사업화총괄"),
    "이사": ("이사", "소장"),
}

# --- Playwright 프로필 단일 사용 (일간 우선) ---

FlexPlaywrightJobKind = Literal["daily", "monthly"]

_PLAYWRIGHT_PRIORITY: dict[FlexPlaywrightJobKind, int] = {"daily": 0, "monthly": 1}
_playwright_ticket_counter = itertools.count()
_playwright_mutex = threading.Lock()
_playwright_cond = threading.Condition(_playwright_mutex)
_playwright_waiters: list[tuple[int, int, FlexPlaywrightJobKind]] = []
_playwright_running: FlexPlaywrightJobKind | None = None


def _monthly_playwright_grace_sec() -> float:
    """월간이 먼저 큐에 들어와도 일간 기동을 기다리는 시간(초)."""
    return float(os.getenv("FLEX_MONTHLY_LOCK_GRACE_SEC", "5") or "5")


def _has_daily_playwright_waiters() -> bool:
    return any(item[2] == "daily" for item in _playwright_waiters)


@contextmanager
def flex_playwright_session(kind: FlexPlaywrightJobKind):
    """
    persistent Chromium 프로필을 한 번에 하나의 작업만 사용한다.

    동시 요청 시 daily(일간)가 monthly(월간)보다 항상 먼저 실행된다.
    """
    global _playwright_running

    priority = _PLAYWRIGHT_PRIORITY[kind]
    ticket = (priority, next(_playwright_ticket_counter), kind)

    with _playwright_cond:
        _playwright_waiters.append(ticket)
        _playwright_waiters.sort()

        if kind == "monthly":
            grace_deadline = time.monotonic() + _monthly_playwright_grace_sec()
            while time.monotonic() < grace_deadline:
                if _has_daily_playwright_waiters() or _playwright_running == "daily":
                    print("[Flex Playwright] 월간 수집 대기 — 일간 작업 우선")
                    break
                _playwright_cond.wait(timeout=0.15)

        while _playwright_running is not None or ticket != _playwright_waiters[0]:
            _playwright_cond.wait()

        _playwright_waiters.remove(ticket)
        _playwright_running = kind
        print(f"[Flex Playwright] {kind} 세션 시작")

    try:
        yield
    finally:
        with _playwright_cond:
            _playwright_running = None
            print(f"[Flex Playwright] {kind} 세션 종료")
            _playwright_cond.notify_all()


def _planned_statuses(member: dict[str, Any]) -> list[dict[str, Any]]:
    """확정 일정 블록만 — live·indicator 제외."""
    return [
        s
        for s in (member.get("schedule_status") or [])
        if str(s.get("record_kind") or "block") == "block"
    ]


def _live_statuses(member: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        s
        for s in (member.get("schedule_status") or [])
        if str(s.get("record_kind") or "") == "live"
    ]


def _member_name(member: dict[str, Any]) -> str:
    return str((member.get("user") or {}).get("name") or "").strip()


def write_latest_flex_hr_index(data: dict[str, Any], source_path: Path | str) -> Path:
    """파싱 결과를 latest_flex_hr.json에 저장한다."""
    payload = dict(data)
    payload["updated_at"] = now_iso()
    payload["source_json"] = str(source_path)
    FLEX_HR_LATEST_INDEX.parent.mkdir(parents=True, exist_ok=True)
    FLEX_HR_LATEST_INDEX.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return FLEX_HR_LATEST_INDEX


def _monthly_json_path(year_month: str) -> Path:
    return flex_hr_monthly_json_path(year_month)


def _year_month_from_monthly_json(path: Path) -> str | None:
    match = re.fullmatch(r"flex_hr_(\d{4}-\d{2})_monthly", path.stem)
    return match.group(1) if match else None


def _resolve_monthly_json_path(year_month: str) -> Path | None:
    path = _monthly_json_path(year_month)
    return path if path.is_file() else None


def _iter_monthly_json_files() -> list[Path]:
    return sorted(FLEX_HR_DIR.glob("flex_hr_*_monthly.json"))


def _shift_year_month(year_month: str, delta: int) -> str:
    year, month = map(int, year_month.split("-"))
    month += delta
    while month > 12:
        month -= 12
        year += 1
    while month < 1:
        month += 12
        year -= 1
    return f"{year:04d}-{month:02d}"


def _now_kst() -> datetime:
    return datetime.now(KST)


def current_year_month() -> str:
    return _now_kst().strftime("%Y-%m")


def next_year_month(year_month: str | None = None) -> str:
    base = _normalize_year_month(year_month) or current_year_month()
    return _shift_year_month(base, 1)


def previous_year_month(year_month: str | None = None) -> str:
    base = _normalize_year_month(year_month) or current_year_month()
    return _shift_year_month(base, -1)


def scheduled_monthly_year_months() -> list[str]:
    """매일 정오 갱신 대상 — 이번 달 + 다음 달."""
    current = current_year_month()
    return list(dict.fromkeys([current, next_year_month(current)]))


def bootstrap_monthly_year_months() -> list[str]:
    """초기·부트스트랩 대상 월 (env 또는 전월·당월·익월)."""
    raw = os.getenv("FLEX_HR_MONTHLY_BOOTSTRAP_MONTHS", "").strip()
    if raw:
        months = [_normalize_year_month(part.strip()) for part in raw.split(",")]
        return [m for m in months if m]
    current = current_year_month()
    return [
        previous_year_month(current),
        current,
        next_year_month(current),
    ]


def list_available_monthly_year_months() -> list[str]:
    months = [
        ym
        for path in _iter_monthly_json_files()
        if (ym := _year_month_from_monthly_json(path))
    ]
    return sorted(months)


def write_flex_hr_monthly_catalog(*, preferred_month: str | None = None) -> Path:
    """월별 JSON 메타를 flex_hr_monthly_index.json에 기록한다."""
    month_meta: dict[str, dict[str, Any]] = {}
    for path in _iter_monthly_json_files():
        year_month = _year_month_from_monthly_json(path)
        if not year_month:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or data.get("period") != "monthly":
            continue
        meta = data.get("meta") or {}
        month_meta[year_month] = {
            "source_json": str(path),
            "member_count": meta.get("member_count"),
            "day_count": meta.get("day_count"),
            "updated_at": data.get("updated_at"),
        }

    available = sorted(month_meta.keys())
    payload = {
        "updated_at": now_iso(),
        "current_month": preferred_month or current_year_month(),
        "available_months": available,
        "months": month_meta,
    }
    FLEX_HR_MONTHLY_CATALOG_INDEX.parent.mkdir(parents=True, exist_ok=True)
    FLEX_HR_MONTHLY_CATALOG_INDEX.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return FLEX_HR_MONTHLY_CATALOG_INDEX


def _find_newest_monthly_parsed_json(year_month: str | None = None) -> Path | None:
    if year_month:
        return _resolve_monthly_json_path(year_month)
    paths = _iter_monthly_json_files()
    if not paths:
        return None
    return max(paths, key=lambda p: p.stat().st_mtime)


def load_flex_hr_monthly(year_month: str | None = None) -> dict[str, Any] | None:
    """월간 JSON(flex_hr_YYYY-MM_monthly.json)을 로드한다."""
    if year_month:
        path = _resolve_monthly_json_path(_normalize_year_month(year_month) or year_month)
    else:
        path = _resolve_monthly_json_path(current_year_month()) or _find_newest_monthly_parsed_json()

    if not path or not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and data.get("period") == "monthly" else None
    except (OSError, json.JSONDecodeError):
        return None


def _normalize_year_month(value: str | None) -> str | None:
    text = (value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}", text):
        return text
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text[:7]
    return None


def _today_iso() -> str:
    return datetime.now(timezone("Asia/Seoul")).strftime("%Y-%m-%d")


def _is_today(date: str | None) -> bool:
    return bool(date) and date == _today_iso()


def _find_newest_parsed_json() -> Path | None:
    candidates = sorted(FLEX_HR_DIR.glob("flex_hr_parsed_*.json"), reverse=True)
    return candidates[0] if candidates else None


def load_latest_flex_hr() -> dict[str, Any] | None:
    """latest_flex_hr.json 또는 최신 파싱 JSON을 로드한다."""
    for path in (FLEX_HR_LATEST_INDEX, _find_newest_parsed_json()):
        if not path or not path.is_file():
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            continue
    return None


def write_employee_roster(
    names: list[str],
    *,
    source_date: str = "",
    members: list[dict[str, Any]] | None = None,
) -> Path:
    """직원 명단을 employee_roster.json에 저장한다 (기존 팀·직무는 유지)."""
    existing_by_name = {
        e["name"]: e for e in load_employee_roster_entries() if e.get("name")
    }
    employees: list[dict[str, str]] = []

    if members:
        for member in members:
            name = _member_name(member)
            if not name:
                continue
            flex_role = str((member.get("user") or {}).get("role") or "").strip()
            prev = existing_by_name.get(name) or {}
            team = str(prev.get("team") or "").strip() or infer_team_from_role(flex_role)
            role = flex_role or str(prev.get("role") or "").strip()
            employees.append({"name": name, "team": team, "role": role})
    else:
        for name in dict.fromkeys(n for n in names if n):
            prev = existing_by_name.get(name) or {}
            employees.append({
                "name": name,
                "team": str(prev.get("team") or "").strip(),
                "role": str(prev.get("role") or "").strip(),
            })

    payload = {
        "updated_at": now_iso(),
        "source_date": source_date or datetime.now().strftime("%Y-%m-%d"),
        "employees": employees,
    }
    FLEX_HR_EMPLOYEE_ROSTER.parent.mkdir(parents=True, exist_ok=True)
    FLEX_HR_EMPLOYEE_ROSTER.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return FLEX_HR_EMPLOYEE_ROSTER


def infer_team_from_role(role: str) -> str:
    """Flex role 문자열에서 roster team을 추정한다 (신규 직원 fallback)."""
    r = (role or "").strip()
    lower = r.lower()
    if any(x in lower for x in ("ceo", "cfo", "cbo", "cto", "cpo")) or "이사" in r:
        return "경영"
    if r.startswith("운영") or lower == "operation":
        return "Operation"
    if "robot engineer" in lower or re.match(r"^ra[\(\s]", lower):
        return "RA"
    if "ai engineer" in lower:
        return "AI"
    if "sw" in lower or "devops" in lower or "project manager" in lower:
        return "SW"
    if "qa" in lower:
        return "QA"
    if "영업" in r or "sales" in lower or "마케터" in r:
        return "Sales"
    if "ux" in lower or "ui" in lower or "design" in lower or "디자인" in r:
        return "사업화"
    if "사업화" in r:
        return "사업화"
    return "기타"


def load_employee_roster_entries() -> list[dict[str, str]]:
    """employee_roster.json에서 직원 name·team·role 목록을 로드한다."""
    if FLEX_HR_EMPLOYEE_ROSTER.is_file():
        try:
            with open(FLEX_HR_EMPLOYEE_ROSTER, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                employees = data.get("employees")
                if isinstance(employees, list):
                    out: list[dict[str, str]] = []
                    for item in employees:
                        if not isinstance(item, dict):
                            continue
                        name = str(item.get("name") or "").strip()
                        if not name:
                            continue
                        out.append({
                            "name": name,
                            "team": str(item.get("team") or "").strip(),
                            "role": str(item.get("role") or "").strip(),
                        })
                    if out:
                        return out
                names = data.get("names")
                if isinstance(names, list):
                    return [
                        {"name": str(n).strip(), "team": "", "role": ""}
                        for n in names
                        if str(n).strip()
                    ]
        except (OSError, json.JSONDecodeError):
            pass

    return [
        {
            "name": name,
            "team": infer_team_from_role(
                str((m.get("user") or {}).get("role") or "")
            ),
            "role": str((m.get("user") or {}).get("role") or "").strip(),
        }
        for m in (load_latest_flex_hr() or {}).get("members") or []
        if (name := _member_name(m))
    ]


def load_employee_roster() -> list[str]:
    """employee_roster.json에서 직원 이름 목록만 로드한다."""
    return [e["name"] for e in load_employee_roster_entries() if e.get("name")]


def list_employee_names_from_hr_data(data: dict[str, Any] | None = None) -> list[str]:
    payload = data or load_latest_flex_hr()
    if not payload:
        return []
    names = [_member_name(m) for m in (payload.get("members") or [])]
    return [n for n in names if n]


def list_employee_names_from_monthly_data(data: dict[str, Any] | None = None) -> list[str]:
    payload = data or load_flex_hr_monthly()
    if not payload:
        return []
    names = [_member_name(m) for m in (payload.get("members") or [])]
    return [n for n in names if n]


def list_employee_names(data: dict[str, Any] | None = None) -> list[str]:
    if data is not None:
        if data.get("period") == "monthly":
            return list_employee_names_from_monthly_data(data)
        return list_employee_names_from_hr_data(data)
    roster = load_employee_roster()
    if roster:
        return roster
    names = list_employee_names_from_hr_data()
    if names:
        return names
    return list_employee_names_from_monthly_data()


def match_employees_in_query(query: str) -> list[str]:
    """질문에 포함된 직원 이름(전체 또는 성 제외 뒤 2글자)을 찾는다."""
    q = (query or "").strip()
    if not q:
        return []

    matched: list[str] = []
    for entry in load_employee_roster_entries():
        name = entry.get("name") or ""
        if not name:
            continue
        if name in q:
            matched.append(name)
            continue
        if len(name) >= 2 and name[-2:] in q:
            matched.append(name)
    return list(dict.fromkeys(matched))


def _query_contains_token(query: str, token: str) -> bool:
    t = (token or "").strip()
    if not t:
        return False
    q = query.casefold()
    return t.casefold() in q


def match_teams_in_query(query: str) -> list[str]:
    """질문에 포함된 팀(roster team 값)을 찾는다."""
    q = (query or "").strip()
    if not q:
        return []
    matched: list[str] = []
    for team, aliases in TEAM_QUERY_ALIASES.items():
        if _query_contains_token(q, team):
            matched.append(team)
            continue
        if any(_query_contains_token(q, alias) for alias in aliases):
            matched.append(team)
    return list(dict.fromkeys(matched))


def match_roles_in_query(query: str) -> list[str]:
    """질문에 포함된 직무 키워드(CEO·CFO 등)를 찾는다."""
    q = (query or "").strip()
    if not q:
        return []
    matched: list[str] = []
    for role_key, aliases in ROLE_QUERY_ALIASES.items():
        if any(_query_contains_token(q, alias) for alias in aliases):
            matched.append(role_key)
    return list(dict.fromkeys(matched))


def _role_entry_matches(entry: dict[str, str], role_keys: list[str]) -> bool:
    role_text = (entry.get("role") or "").casefold()
    for key in role_keys:
        if key.casefold() in role_text:
            return True
        for alias in ROLE_QUERY_ALIASES.get(key, ()):
            if alias.casefold() in role_text:
                return True
    return False


def filter_roster_names(
    *,
    teams: list[str] | None = None,
    roles: list[str] | None = None,
) -> list[str]:
    """roster에서 팀·직무 조건에 맞는 직원 이름 목록."""
    team_set = {t.strip() for t in (teams or []) if t.strip()}
    role_keys = [r.strip() for r in (roles or []) if r.strip()]
    names: list[str] = []
    for entry in load_employee_roster_entries():
        name = entry.get("name") or ""
        if not name:
            continue
        if team_set and (entry.get("team") or "") not in team_set:
            continue
        if role_keys and not _role_entry_matches(entry, role_keys):
            continue
        names.append(name)
    return list(dict.fromkeys(names))


def resolve_schedule_worker_names(
    query: str,
    worker_name: str = "",
    *,
    team: str = "",
    role_title: str = "",
) -> list[str]:
    """질문·tool 인자에서 조회할 직원 전체 이름 목록 (다중 인물·팀·직무 지원)."""
    from_query = match_employees_in_query(query)
    if from_query:
        return from_query

    tool_team = (team or "").strip()
    tool_role = (role_title or "").strip()
    if tool_team or tool_role:
        names = filter_roster_names(
            teams=[tool_team] if tool_team else None,
            roles=[tool_role] if tool_role else None,
        )
        if names:
            return names

    roles = match_roles_in_query(query)
    if roles:
        names = filter_roster_names(roles=roles)
        if names:
            return names

    teams = match_teams_in_query(query)
    if teams:
        names = filter_roster_names(teams=teams)
        if names:
            return names

    tool = (worker_name or "").strip()
    if not tool:
        return []

    from_tool = match_employees_in_query(tool)
    return from_tool or [tool]


def _is_meaningful_summary_time(value: str | None) -> bool:
    """누적 근무시간이 실제로 추적된 값일 때만 표시."""
    v = (value or "").strip()
    if not v:
        return False
    if re.fullmatch(r"0+[:：]0+", v):
        return False
    if re.fullmatch(r"0+\s*시간(?:\s*0+\s*분)?", v):
        return False
    if re.fullmatch(r"0+\s*h(?:\s*0+\s*m)?", v, re.IGNORECASE):
        return False
    return True


def _members_with_type(members: list[dict[str, Any]], schedule_type: str) -> list[str]:
    names: list[str] = []
    for member in members:
        if any(str(s.get("type") or "") == schedule_type for s in _planned_statuses(member)):
            name = _member_name(member)
            if name:
                names.append(name)
    return names


def _infer_afternoon_leave_end(start: str, end: str) -> str:
    """오후 시작·종료 미지정 시 퇴근 시각 보완 (parser LEAVE_TYPES 마커와 동일 해석)."""
    if start and not end and "오후" in start:
        return DEFAULT_AFTERNOON_LEAVE_END
    return end


def _minutes_to_clock_label(total_minutes: int) -> str:
    hour, minute = divmod(int(total_minutes), 60)
    return f"{hour}:{minute:02d}"


def _normalize_clock_time(value: str) -> str:
    """'8', '8:30', '오후 4:00' → '8:00', '16:00' 등."""
    from app.services.flex_hr.flex_parser.day_parser import _time_to_minutes

    v = (value or "").strip()
    if not v:
        return ""
    minutes = _time_to_minutes(v)
    if minutes is None:
        return v
    return _minutes_to_clock_label(minutes)


def _format_leave_time_suffix(status: dict[str, Any]) -> str:
    """LEAVE_TYPES 일정 시각 → 명단용 괄호 문자열 (휴가·외근 등)."""
    start = str(status.get("start_time") or "").strip()
    end = _infer_afternoon_leave_end(
        start, str(status.get("end_time") or "").strip()
    )
    if start and end:
        return f"({start} ~ {end})"
    if start:
        return f"({start} ~ )"
    if end:
        return f"( ~ {end})"
    return ""


def _format_leave_roster_name(member: dict[str, Any], schedule_type: str) -> str:
    """휴가·외근 등 LEAVE_TYPES 명단 — 시간부 일정은 이름 옆에 시각을 붙인다."""
    name = _member_name(member)
    if not name:
        return ""

    leave_statuses = [
        s for s in _planned_statuses(member)
        if str(s.get("type") or "") == schedule_type
    ]
    if not leave_statuses:
        return ""

    for status in leave_statuses:
        suffix = _format_leave_time_suffix(status)
        if suffix:
            return f"{name}{suffix}"
    return name


def _format_vacation_roster_name(member: dict[str, Any]) -> str:
    return _format_leave_roster_name(member, VACATION_TYPE)


def _members_leave_roster(
    members: list[dict[str, Any]], schedule_type: str
) -> list[str]:
    return [
        label for m in members
        if (label := _format_leave_roster_name(m, schedule_type))
    ]


def _members_vacation_roster(members: list[dict[str, Any]]) -> list[str]:
    return _members_leave_roster(members, VACATION_TYPE)


def build_daily_roster_text(data: dict[str, Any] | None = None) -> str:
    """금일 재택·휴가자 명단 문자열 (슬랙 최적화 버전)."""
    payload = data or load_latest_flex_hr()
    
    # 요일까지 명시된 오늘 날짜 생성 (ex: 2026년 06월 15일 (월))
    now = datetime.now(timezone('Asia/Seoul'))
    weekday_map = {0: "월", 1: "화", 2: "수", 3: "목", 4: "금", 5: "토", 6: "일"}
    today_date = now.strftime(f"%Y-%m-%d ({weekday_map[now.weekday()]})")

    if not payload:
        return (
            f"📅 *근태 현황 알림* ({today_date})\n"
            f"⚠️ 금일 재택/휴가자 데이터를 불러오지 못했습니다. (확인 시각: {now.strftime('%H:%M')})"
        )

    members = list(payload.get("members") or [])
    remote = _members_with_type(members, REMOTE_TYPE)
    vacation = _members_vacation_roster(members)
    outing = _members_leave_roster(members, OUTING_TYPE)
    overseas = _members_with_type(members, OVERSEAS_TYPE)
    
    remote_text = ", ".join(remote) if remote else "없음"
    vacation_text = ", ".join(vacation) if vacation else "없음"
    outing_text = ", ".join(outing) if outing else "없음"
    overseas_text = ", ".join(overseas) if overseas else "없음"
    
    lines = [
        f"• 금일 근태 현황 알림 ({today_date})",
        f":house: 재택 근무자: {remote_text}",
        f":beach_with_umbrella: 휴가자: {vacation_text}",
        f":car: 외근자: {outing_text}",
    ]
    if overseas:
        lines.append(f":luggage: 출장자: {overseas_text}")
    return "\n".join(lines)


def build_flex_schedule_router_block(query: str) -> str:
    """Router Turn1 — 직원·팀·직무 후보를 짧게 전달."""
    from app.services.outlook_room.schedule_reserve import match_room_in_query

    q = (query or "").strip()
    if not q:
        return ""
    if match_room_in_query(q):
        return ""

    parts: list[str] = []
    names = match_employees_in_query(q)
    teams = match_teams_in_query(q)
    roles = match_roles_in_query(q)

    if names:
        parts.append(f"감지된 직원: {', '.join(names)}")
    if teams:
        team_names = filter_roster_names(teams=teams)
        preview = ", ".join(team_names[:12])
        if len(team_names) > 12:
            preview += f" 외 {len(team_names) - 12}명"
        parts.append(f"감지된 팀: {', '.join(teams)} → {preview or '(없음)'}")
    if roles:
        role_names = filter_roster_names(roles=roles)
        parts.append(
            f"감지된 직무: {', '.join(roles)} → {', '.join(role_names) or '(없음)'}"
        )

    if not parts:
        return ""
    return "<flex_schedule_hints>" + " | ".join(parts) + "</flex_schedule_hints>"


def _roster_team_for_name(name: str) -> str:
    for entry in load_employee_roster_entries():
        if entry.get("name") == name:
            return str(entry.get("team") or "").strip()
    return ""


def _resolve_worker_members(worker_name: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    needle = (worker_name or "").strip()
    if not needle:
        return []

    members = list(data.get("members") or [])
    exact = [m for m in members if _member_name(m) == needle]
    if exact:
        return exact

    partial = [m for m in members if needle in _member_name(m)]
    if partial:
        return partial

    if len(needle) >= 2:
        return [m for m in members if _member_name(m).endswith(needle[-2:])]
    return []


def _format_schedule_block(member: dict[str, Any], data: dict[str, Any]) -> str:
    name = _member_name(member)
    date = str(data.get("date") or "")
    current_time = str(data.get("current_time") or "")
    summary_time = str(member.get("summary_time") or "")
    planned = _planned_statuses(member)
    live_items = _live_statuses(member)
    indicators = [
        s
        for s in (member.get("schedule_status") or [])
        if str(s.get("record_kind") or "") == "indicator"
    ]

    lines = [
        f"직원: {name}",
    ]
    roster_team = _roster_team_for_name(name)
    if roster_team:
        lines.append(f"팀: {roster_team}")
    lines.extend([
        f"날짜: {date}",
        f"Flex 기준 현재 시각: {current_time or '(미표시)'}",
    ])
    if _is_meaningful_summary_time(summary_time):
        lines.append(f"누적 근무시간: {summary_time}")
    lines.append("[확정 일정]")
    if not planned:
        lines.append("- (등록된 확정 일정 없음)")
    else:
        for item in planned:
            lines.append(_format_schedule_line(item))

    if live_items:
        lines.append("[현재 기록 중]")
        for item in live_items:
            item_type = str(item.get("type") or "").strip()
            start_time = str(item.get("start_time") or "").strip()
            if start_time:
                lines.append(
                    f"- {item_type}(기록 중): {start_time}~ "
                    f"(Flex 기준 {current_time or '현재'}까지, 종료 시각 미확정)"
                )
            else:
                lines.append(f"- {item_type}(기록 중)")

    if indicators:
        lines.append("[상태]")
        for item in indicators:
            item_type = str(item.get("type") or "").strip()
            lines.append(f"- {item_type}")
            if item_type in ("근무 중", "근무중"):
                lines.append(
                    f"※ {current_time or '현재'} 기준 사무실 재실 추정"
                )

    return "\n".join(lines)


def _format_leave_schedule_line(
    item_type: str, start_time: str, end_time: str
) -> str:
    """휴가·외근 등 LEAVE_TYPES — parser와 동일 집합에 시각 구간 표기."""
    start = _normalize_clock_time(start_time)
    end = _normalize_clock_time(_infer_afternoon_leave_end(start_time.strip(), end_time.strip()))

    if item_type == VACATION_TYPE:
        if start and not end:
            return f"- {item_type}(오후 반차 등): {start} ~"
        if end and not start:
            return f"- {item_type}(오전 반차 등): ~ {end}"
        if start and end:
            return f"- {item_type}: {start} ~ {end}"
        return f"- {item_type} (시각 미표기)"

    if start and end:
        return f"- {item_type}: {start} ~ {end}"
    if start:
        return f"- {item_type}: {start} ~"
    if end:
        return f"- {item_type}: ~ {end}"
    return f"- {item_type} (시각 미표기)"


def _format_schedule_line(item: dict[str, Any]) -> str:
    item_type = str(item.get("type") or "").strip()
    start_time = str(item.get("start_time") or "").strip()
    end_time = str(item.get("end_time") or "").strip()

    if item_type in LEAVE_TYPES:
        return _format_leave_schedule_line(item_type, start_time, end_time)

    if start_time and end_time:
        return f"- {item_type}: {start_time} ~ {end_time}"
    if start_time:
        return f"- {item_type}: {start_time}~"
    if end_time:
        return f"- {item_type}: ~{end_time}"
    return f"- {item_type} (시각 미표기)"


def _format_monthly_day_line(day: dict[str, Any]) -> str:
    date = str(day.get("date") or "").strip()
    weekday = str(day.get("weekday") or "").strip()
    suffix = f" ({weekday})" if weekday else ""
    items = day.get("items") or []
    if not items:
        return f"- {date}{suffix}: (기록 없음)"

    parts = []
    for item in items:
        item_type = str(item.get("type") or "").strip()
        start_time = str(item.get("start_time") or "").strip()
        end_time = str(item.get("end_time") or "").strip()
        duration = str(item.get("duration") or "").strip()

        segment = item_type
        if start_time and end_time:
            segment += f" {start_time}~{end_time}"
        if duration:
            segment += f" ({duration})" if start_time else f" {duration}"
        parts.append(segment.strip())
    return f"- {date}{suffix}: {', '.join(parts)}"


def _format_monthly_schedule_block(
    member: dict[str, Any],
    data: dict[str, Any],
    *,
    target_date: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    name = _member_name(member)
    year_month = str(data.get("year_month") or "")
    summary_time = str(member.get("summary_time") or "")

    scope_label = year_month
    if start_date and end_date and start_date[:7] != end_date[:7]:
        scope_label = f"{year_month} (기간 {start_date}~{end_date} 해당일)"

    lines = [
        f"직원: {name}",
        f"조회 월: {scope_label}",
    ]
    if _is_meaningful_summary_time(summary_time):
        lines.append(f"월 누적 근무시간: {summary_time}")
    lines.append("[일별 근태]")

    days = list(member.get("days") or [])
    if target_date:
        days = [d for d in days if str(d.get("date") or "") == target_date]
    elif start_date or end_date:
        start = start_date or "0000-01-01"
        end = end_date or "9999-12-31"
        days = [
            d for d in days
            if start <= str(d.get("date") or "") <= end
        ]

    range_filter = bool(start_date or end_date) and not target_date
    if target_date and not days:
        lines.append(f"- {target_date}: (기록 없음)")
    elif range_filter and not days:
        lines.append("- (해당 기간·월에 등록된 일별 근태 없음)")
    elif not range_filter:
        shown = [d for d in days if d.get("items")]
        if not shown:
            lines.append("- (해당 월에 등록된 일별 근태 없음)")
        else:
            for day in shown:
                lines.append(_format_monthly_day_line(day))
    else:
        for day in sorted(days, key=lambda d: str(d.get("date") or "")):
            lines.append(_format_monthly_day_line(day))

    return "\n".join(lines)


def _should_overlay_daily_today(
    target_month: str | None,
    target_date: str | None,
) -> bool:
    """월간 조회 범위에 오늘이 포함되면 일간 타임라인을 덧붙인다."""
    today = _today_iso()
    if target_month != today[:7]:
        return False
    if target_date and target_date != today:
        return False
    return True


def _format_daily_today_overlay(
    member: dict[str, Any],
    daily_payload: dict[str, Any],
) -> str:
    body = _format_schedule_block(member, daily_payload)
    return body.replace(
        "[확정 일정]",
        "비어있는 오늘자 일정: [오늘 실시간 — 일간 타임라인]",
        1,
    )


def _append_daily_today_overlays(
    monthly_text: str,
    *,
    worker_name: str,
    monthly_matches: list[dict[str, Any]],
) -> str:
    daily = load_latest_flex_hr()
    today = _today_iso()
    if not daily or str(daily.get("date") or "") != today:
        return monthly_text

    daily_matches = _resolve_worker_members(worker_name, daily)
    if not daily_matches:
        return monthly_text

    daily_by_name = {_member_name(m): m for m in daily_matches}
    overlays: list[str] = []
    for member in monthly_matches:
        name = _member_name(member)
        daily_member = daily_by_name.get(name) or daily_matches[0]
        overlays.append(_format_daily_today_overlay(daily_member, daily))

    return monthly_text + "\n\n" + "\n\n---\n\n".join(overlays)


def _parse_iso_date(value: str | None) -> date | None:
    text = (value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _months_in_date_range(start: date, end: date) -> list[str]:
    """포함 기간이 걸친 YYYY-MM 목록 (시작월~종료월)."""
    if end < start:
        start, end = end, start
    months: list[str] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


def _search_worker_schedule_date_range(
    worker_name: str,
    *,
    start_date: str,
    end_date: str,
) -> str:
    """기간 조회 — 월을 넘으면 해당 월 JSON을 각각 로드해 병합."""
    start_d = _parse_iso_date(start_date)
    end_d = _parse_iso_date(end_date)
    if not start_d or not end_d:
        return "기간 조회에 유효한 date·end_date(YYYY-MM-DD)가 필요합니다."

    start_iso = start_d.isoformat()
    end_iso = end_d.isoformat()
    months = _months_in_date_range(start_d, end_d)

    blocks: list[str] = []
    missing: list[str] = []
    updated_parts: list[str] = []

    for ym in months:
        payload = load_flex_hr_monthly(ym)
        if not payload:
            missing.append(ym)
            continue
        matches = _resolve_worker_members(worker_name, payload)
        if not matches:
            continue
        if payload.get("updated_at"):
            updated_parts.append(f"{ym}={payload.get('updated_at')}")
        for member in matches:
            blocks.append(
                _format_monthly_schedule_block(
                    member,
                    payload,
                    start_date=start_iso,
                    end_date=end_iso,
                )
            )

    if not blocks:
        if missing and len(missing) == len(months):
            available = ", ".join(list_available_monthly_year_months()) or "(없음)"
            return (
                f"Flex 월간 근태 데이터({start_iso}~{end_iso})가 아직 수집되지 않았습니다. "
                f"미수집 월: {', '.join(missing)}. 현재 보유 월: {available}"
            )
        known = ", ".join(list_employee_names(load_latest_flex_hr() or {})[:20])
        suffix = f" (등록 직원 예: {known})" if known else ""
        return f"'{worker_name}' 직원을 Flex 월간 데이터에서 찾지 못했습니다.{suffix}"

    header = (
        f"조회 범위: {start_iso}~{end_iso} (월간 일별, "
        f"조회 월: {', '.join(months)})"
    )
    if updated_parts:
        header += f" | 갱신: {'; '.join(updated_parts)}"
    if missing:
        header += f" | 미수집 월: {', '.join(missing)}"

    result = header + "\n\n" + "\n\n---\n\n".join(blocks)

    today = _today_iso()
    if start_iso <= today <= end_iso and today[:7] in months:
        first_payload = load_flex_hr_monthly(today[:7])
        if first_payload:
            matches = _resolve_worker_members(worker_name, first_payload)
            if matches and _should_overlay_daily_today(today[:7], None):
                result = _append_daily_today_overlays(
                    result,
                    worker_name=worker_name,
                    monthly_matches=matches,
                )
    return result


def normalize_flex_schedule_tool_args(
    args: dict[str, Any],
    query: str = "",
) -> dict[str, Any]:
    """search_worker_schedule tool 인자 — 주간·기간 intent는 코드에서 date/end_date 보정."""
    normalized = apply_date_range_to_tool_args(dict(args), query)
    worker_name = str(normalized.get("worker_name") or "").strip()
    team = str(normalized.get("team") or "").strip()
    role_title = str(normalized.get("role_title") or "").strip()
    normalized["worker_names"] = resolve_schedule_worker_names(
        query,
        worker_name,
        team=team,
        role_title=role_title,
    )
    normalized["matched_teams"] = match_teams_in_query(query) or ([team] if team else [])
    normalized["matched_roles"] = match_roles_in_query(query) or ([role_title] if role_title else [])
    return normalized


def _is_worker_schedule_not_found(result: str, worker_name: str) -> bool:
    needle = (worker_name or "").strip()
    if not needle:
        return False
    return (
        "찾지 못했습니다" in result
        and f"'{needle}'" in result
    )


def search_workers_schedule(
    worker_names: list[str],
    *,
    date: str | None = None,
    end_date: str | None = None,
    year_month: str | None = None,
) -> str:
    """여러 직원 근태를 한 번에 조회해 Turn2 프롬프트용 텍스트를 반환한다."""
    unique = list(dict.fromkeys(n.strip() for n in worker_names if (n or "").strip()))
    if not unique:
        return "조회할 직원 이름이 필요합니다."
    if len(unique) == 1:
        return search_worker_schedule(
            unique[0],
            date=date,
            end_date=end_date,
            year_month=year_month,
        )

    found_blocks: list[str] = []
    not_found: list[str] = []
    for name in unique:
        result = search_worker_schedule(
            name,
            date=date,
            end_date=end_date,
            year_month=year_month,
        )
        if _is_worker_schedule_not_found(result, name):
            not_found.append(name)
        else:
            found_blocks.append(result)

    parts: list[str] = []
    if found_blocks:
        parts.append("\n\n---\n\n".join(found_blocks))
    if not_found:
        parts.append(
            "다음 직원은 Flex 근태 데이터에서 찾지 못했습니다: "
            + ", ".join(not_found)
        )
    return "\n\n".join(parts) if parts else "조회 결과가 없습니다."


def search_worker_schedule(
    worker_name: str,
    *,
    date: str | None = None,
    end_date: str | None = None,
    year_month: str | None = None,
    data: dict[str, Any] | None = None,
) -> str:
    """JSON에서 직원 근무 일정을 조회해 Turn2 프롬프트용 텍스트를 반환한다."""
    target_date = (date or "").strip() or None
    target_end = (end_date or "").strip() or None

    if target_date and target_end:
        return _search_worker_schedule_date_range(
            worker_name,
            start_date=target_date,
            end_date=target_end,
        )

    target_month = _normalize_year_month(year_month) or _normalize_year_month(target_date)

    use_monthly = bool(
        data and data.get("period") == "monthly"
    ) or bool(target_month and (year_month or (target_date and not _is_today(target_date))))

    if use_monthly:
        payload = data if data and data.get("period") == "monthly" else load_flex_hr_monthly(target_month)
        if not payload:
            label = target_month or target_date or "해당 월"
            available = ", ".join(list_available_monthly_year_months()) or "(없음)"
            return (
                f"Flex 월간 근태 데이터({label})가 아직 수집되지 않았습니다. "
                f"현재 보유 월: {available}"
            )

        matches = _resolve_worker_members(worker_name, payload)
        if not matches:
            known = ", ".join(list_employee_names(payload)[:20])
            suffix = f" (등록 직원 예: {known})" if known else ""
            return f"'{worker_name}' 직원을 Flex 월간 데이터에서 찾지 못했습니다.{suffix}"

        blocks = [
            _format_monthly_schedule_block(
                member,
                payload,
                target_date=target_date if not year_month else None,
            )
            for member in matches
        ]
        scope = target_date or payload.get("year_month") or ""
        header = (
            f"조회 범위: {scope} (월간 일별) | "
            f"갱신: {payload.get('updated_at') or ''}"
        )
        result = header + "\n\n" + "\n\n---\n\n".join(blocks)
        if _should_overlay_daily_today(
            target_month,
            target_date if not year_month else None,
        ):
            result = _append_daily_today_overlays(
                result,
                worker_name=worker_name,
                monthly_matches=matches,
            )
        return result

    payload = data or load_latest_flex_hr()
    if not payload:
        return "Flex 근태 데이터가 아직 수집되지 않았습니다."

    matches = _resolve_worker_members(worker_name, payload)
    if not matches:
        known = ", ".join(list_employee_names(payload)[:20])
        suffix = f" (등록 직원 예: {known})" if known else ""
        return f"'{worker_name}' 직원을 Flex 근태 데이터에서 찾지 못했습니다.{suffix}"

    blocks = [_format_schedule_block(member, payload) for member in matches]
    header = f"조회 기준일: {payload.get('date') or ''} | 갱신: {payload.get('updated_at') or ''}"
    return header + "\n\n" + "\n\n---\n\n".join(blocks)


def send_slack_daily_roster(channel: str | None = None) -> bool:
    """매일 지정 시각 — 재택/휴가자 명단을 Slack에 전송한다 (주말·공휴일 제외)."""
    skip = should_skip_flex_hr_daily_roster()
    if skip:
        print(f"[Flex HR] 일일 명단 슬랙 전송 스킵 — {skip}")
        return True

    token = os.getenv("SLACK_BOT_TOKEN", "")
    channel = channel or os.getenv("FLEX_HR_SLACK_CHANNEL", "") or os.getenv("GOV_PROJECT_SLACK_CHANNEL", "")

    if not token or not channel:
        print("[Flex HR] SLACK_BOT_TOKEN / FLEX_HR_SLACK_CHANNEL 미설정 — 슬랙 전송 스킵")
        return False

    text = build_daily_roster_text()
    try:
        res = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "channel": channel,
                "text": text,
                "unfurl_links": False,
                "unfurl_media": False,
            },
            timeout=15,
        )
        data = res.json()
        if data.get("ok"):
            print(f"[Flex HR] 일일 명단 슬랙 전송 완료 → {channel}")
            return True
        print(f"[Flex HR] 슬랙 전송 실패: {data.get('error')}")
    except Exception as e:
        print(f"[Flex HR] 슬랙 예외: {e}")
    return False


def run_flex_hr_monthly_update(
    url: str | None = None,
    date: str | None = None,
    *,
    year_month: str | None = None,
) -> dict[str, Any]:
    """Playwright 월간 수집 → flex_hr_YYYY-MM_monthly.json 갱신."""
    from app.services.flex_hr.flex_parser import run_flex_hr_monthly_pipeline

    fetch_date = date
    normalized_month = _normalize_year_month(year_month)
    if normalized_month:
        fetch_date = f"{normalized_month}-01"

    result, html_path, json_path = run_flex_hr_monthly_pipeline(url=url, date=fetch_date)
    write_flex_hr_monthly_catalog(preferred_month=result.get("year_month"))
    members = list(result.get("members") or [])
    report = {
        "year_month": result.get("year_month"),
        "members_count": len(members),
        "day_count": (result.get("meta") or {}).get("day_count"),
        "html_path": str(html_path) if html_path else None,
        "json_path": str(json_path),
    }
    print(
        f"[Flex HR] 월간 갱신 완료 — {report['year_month']} / {report['members_count']}명 "
        f"→ {report['json_path']}"
    )
    return report


def run_flex_hr_monthly_updates_for_months(
    year_months: list[str],
    *,
    url: str | None = None,
    skip_existing: bool = False,
) -> dict[str, Any]:
    """여러 월간 Flex HR JSON을 순차 수집한다."""
    reports: dict[str, Any] = {}
    for raw_month in year_months:
        year_month = _normalize_year_month(raw_month)
        if not year_month:
            continue
        if skip_existing and _resolve_monthly_json_path(year_month):
            existing = _resolve_monthly_json_path(year_month)
            reports[year_month] = {"skipped": True, "json_path": str(existing)}
            print(f"[Flex HR] 월간 스킵(기존 파일) — {year_month}")
            continue
        try:
            reports[year_month] = run_flex_hr_monthly_update(url=url, year_month=year_month)
        except Exception as exc:
            reports[year_month] = {"error": str(exc)}
            print(f"[Flex HR] 월간 갱신 실패 — {year_month}: {exc}")
    write_flex_hr_monthly_catalog(preferred_month=current_year_month())
    return reports


def run_flex_hr_monthly_scheduled_update(url: str | None = None) -> dict[str, Any]:
    """정오 스케줄 — 이번 달·다음 달 월간 데이터 갱신."""
    months = scheduled_monthly_year_months()
    print(f"[Flex HR] 월간 정기 갱신 시작 — {', '.join(months)}")
    reports = run_flex_hr_monthly_updates_for_months(months, url=url, skip_existing=False)
    return {"months": months, "reports": reports}


def run_flex_hr_monthly_bootstrap(url: str | None = None) -> dict[str, Any]:
    """부트스트랩 — 전월·당월·익월(또는 env 지정) 중 누락분 수집."""
    months = bootstrap_monthly_year_months()
    print(f"[Flex HR] 월간 부트스트랩 — {', '.join(months)}")
    return run_flex_hr_monthly_updates_for_months(months, url=url, skip_existing=True)


def run_flex_hr_updates(url: str | None = None, date: str | None = None) -> dict[str, Any]:
    """일간 Flex HR 데이터만 갱신한다 (월간은 정오 스케줄)."""
    daily = run_flex_hr_update(url=url, date=date)
    return {"daily": daily}


def run_flex_hr_update(url: str | None = None, date: str | None = None) -> dict[str, Any]:
    """Playwright 수집 → 파싱 → JSON·latest 인덱스 갱신."""
    from app.services.flex_hr.flex_parser import run_flex_hr_pipeline

    result, html_path, json_path = run_flex_hr_pipeline(url=url, date=date)
    write_latest_flex_hr_index(result, json_path)
    members = list(result.get("members") or [])
    names = list_employee_names_from_hr_data(result)
    roster_path = write_employee_roster(
        names,
        source_date=str(result.get("date") or ""),
        members=members,
    )
    report = {
        "date": result.get("date"),
        "members_count": len(members),
        "html_path": str(html_path),
        "json_path": str(json_path),
        "latest_index": str(FLEX_HR_LATEST_INDEX),
        "employee_roster": str(roster_path),
        "roster_preview": build_daily_roster_text(result),
    }
    print(
        f"[Flex HR] 갱신 완료 — {report['date']} / {report['members_count']}명 "
        f"→ {report['latest_index']}"
    )
    return report
