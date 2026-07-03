"""주말·대한민국 공휴일 판별 — 일일 알림/파이프라인 스케줄 공통."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache

import holidays
from pytz import timezone

KST = timezone("Asia/Seoul")
_WEEKDAY_KO = ("월", "화", "수", "목", "금", "토", "일")


@lru_cache(maxsize=8)
def _kr_holidays(year: int) -> holidays.HolidayBase:
    return holidays.KR(years=year)


def _to_local_date(when: datetime | date | None) -> date:
    if when is None:
        return datetime.now(KST).date()
    if isinstance(when, datetime):
        return when.astimezone(KST).date() if when.tzinfo else when.date()
    return when


def should_skip_daily_notification(when: datetime | date | None = None) -> str | None:
    """주말·공휴일이면 스킵 사유, 평일 업무일이면 None."""
    d = _to_local_date(when)
    if d.weekday() >= 5:
        return f"주말({_WEEKDAY_KO[d.weekday()]})"
    name = _kr_holidays(d.year).get(d)
    if name:
        return f"공휴일({name})"
    return None


def next_business_day_run(
    now: datetime | None = None,
    *,
    hour: int,
    minute: int,
    earliest_date: date | None = None,
) -> datetime:
    """다음 실행 시각 — 주말·공휴일은 건너뛴다."""
    if now is None:
        now = datetime.now()
    if earliest_date and now.date() < earliest_date:
        now = datetime.combine(earliest_date, datetime.min.time())
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    while should_skip_daily_notification(candidate.date()):
        candidate += timedelta(days=1)
    return candidate
