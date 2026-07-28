"""월간 JSON → 특정일 재택·휴가·외근 브리핑 단위 테스트."""

from __future__ import annotations

from app.services.flex_hr.flex_hr import (
    build_roster_text_for_date,
    query_wants_all_workers_attendance,
    search_workers_schedule,
)


def _monthly_payload() -> dict:
    return {
        "period": "monthly",
        "year_month": "2026-07",
        "updated_at": "2026-07-27 18:00:00 KST",
        "members": [
            {
                "user": {"name": "김재택"},
                "days": [
                    {
                        "date": "2026-07-28",
                        "items": [
                            {
                                "type": "재택근무",
                                "start_time": "09:00",
                                "end_time": "18:00",
                            }
                        ],
                    }
                ],
            },
            {
                "user": {"name": "이휴가"},
                "days": [
                    {
                        "date": "2026-07-28",
                        "items": [
                            {
                                "type": "휴가",
                                "start_time": "13:00",
                                "end_time": "18:00",
                            }
                        ],
                    }
                ],
            },
            {
                "user": {"name": "박외근"},
                "days": [
                    {
                        "date": "2026-07-28",
                        "items": [
                            {
                                "type": "외근",
                                "start_time": "10:00",
                                "end_time": "17:00",
                            }
                        ],
                    }
                ],
            },
            {
                "user": {"name": "최출장"},
                "days": [
                    {
                        "date": "2026-07-28",
                        "items": [{"type": "출장"}],
                    }
                ],
            },
            {
                "user": {"name": "정근무"},
                "days": [
                    {
                        "date": "2026-07-28",
                        "items": [{"type": "근무"}],
                    }
                ],
            },
        ],
    }


def test_build_roster_text_for_date_briefing_format():
    text = build_roster_text_for_date("2026-07-28", monthly=_monthly_payload())
    assert "2026-07-28" in text
    assert "재택 근무자: 김재택" in text
    assert "이휴가(13:00 ~ 18:00)" in text
    assert "박외근(10:00 ~ 17:00)" in text
    assert "출장자: 최출장" in text
    assert "정근무" not in text


def test_build_roster_text_for_date_empty_day():
    text = build_roster_text_for_date("2026-07-15", monthly=_monthly_payload())
    assert "재택 근무자: 없음" in text
    assert "휴가자: 없음" in text
    assert "외근자: 없음" in text
    assert "출장자" not in text


def test_build_roster_text_for_date_empty_members():
    text = build_roster_text_for_date(
        "2026-07-28",
        monthly={"period": "monthly", "members": []},
    )
    assert "재택 근무자: 없음" in text


def test_build_roster_text_for_date_no_monthly_file(monkeypatch):
    monkeypatch.setattr(
        "app.services.flex_hr.flex_hr.load_flex_hr_monthly",
        lambda year_month=None: None,
    )
    text = build_roster_text_for_date("2026-07-28")
    assert "월간 근태 데이터가 없습니다" in text


def test_query_wants_all_workers_leave_markers():
    assert query_wants_all_workers_attendance("내일 휴가자")
    assert query_wants_all_workers_attendance("7월 15일 재택인")
    assert query_wants_all_workers_attendance("모레 외근자")


def test_search_workers_schedule_all_workers_with_date(monkeypatch):
    monkeypatch.setattr(
        "app.services.flex_hr.flex_hr.load_flex_hr_monthly",
        lambda year_month=None: _monthly_payload(),
    )
    monkeypatch.setattr(
        "app.services.flex_hr.flex_hr._is_today",
        lambda d: False,
    )
    text = search_workers_schedule([], date="2026-07-28", all_workers=True)
    assert "김재택" in text
    assert "이휴가" in text
    assert "박외근" in text
