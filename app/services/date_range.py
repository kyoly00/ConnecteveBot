"""자연어·질문 텍스트에서 ISO 날짜 범위 추출 (KST 기준)."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

_WEEK_RANGE_RE = re.compile(
    r"다음\s*주|차\s*주|이번\s*주|금\s*주|지난\s*주|저번\s*주|전\s*주",
)
_RECENT_DAYS_RE = re.compile(r"(?:최근|지난)\s*(\d+)\s*일")


def today_kst() -> date:
    return datetime.now(KST).date()


def week_bounds(reference: date, *, week_offset: int = 0) -> tuple[date, date]:
    """week_offset: 0=이번 주, 1=다음 주, -1=지난 주. 월~일."""
    monday = reference - timedelta(days=reference.weekday())
    monday += timedelta(weeks=week_offset)
    sunday = monday + timedelta(days=6)
    return monday, sunday


def normalize_date_range_from_text(
    text: str,
    *,
    reference: date | None = None,
) -> tuple[str | None, str | None]:
    """
    질문에서 (start_iso, end_iso) 추출.

    이번/다음/지난 주, 최근 N일 등. 매칭 없으면 (None, None).
    """
    q = (text or "").strip()
    if not q:
        return None, None
    ref = reference or today_kst()

    if re.search(r"다음\s*주|차\s*주", q):
        start, end = week_bounds(ref, week_offset=1)
        return start.isoformat(), end.isoformat()
    if re.search(r"이번\s*주|금\s*주", q):
        start, end = week_bounds(ref, week_offset=0)
        return start.isoformat(), end.isoformat()
    if re.search(r"지난\s*주|저번\s*주|전\s*주", q):
        start, end = week_bounds(ref, week_offset=-1)
        return start.isoformat(), end.isoformat()

    m = _RECENT_DAYS_RE.search(q)
    if m:
        n = max(1, int(m.group(1)))
        start = ref - timedelta(days=n - 1)
        return start.isoformat(), ref.isoformat()

    return None, None


def has_week_range_intent(text: str) -> bool:
    """이번/다음/지난 주 등 주간 범위 의도."""
    return bool(_WEEK_RANGE_RE.search(text or ""))


def apply_date_range_to_tool_args(
    args: dict,
    query: str,
    *,
    date_key: str = "date",
    end_date_key: str = "end_date",
    year_month_key: str = "year_month",
    reference: date | None = None,
    force_week_override: bool = True,
) -> dict:
    """
    tool 인자에 date/end_date 보정.

    force_week_override=True면 주간 intent 시 year_month 단독 호출을 범위 조회로 교체.
    """
    out = dict(args)
    parsed_start, parsed_end = normalize_date_range_from_text(
        query, reference=reference,
    )
    if not parsed_start:
        return out

    week_intent = has_week_range_intent(query)
    has_range = bool(parsed_end)

    if week_intent and has_range and force_week_override:
        out[date_key] = parsed_start
        out[end_date_key] = parsed_end
        out.pop(year_month_key, None)
        return out

    if not str(out.get(date_key) or "").strip():
        out[date_key] = parsed_start
    if has_range and not str(out.get(end_date_key) or "").strip():
        out[end_date_key] = parsed_end
    if out.get(date_key) and out.get(end_date_key):
        out.pop(year_month_key, None)
    return out
