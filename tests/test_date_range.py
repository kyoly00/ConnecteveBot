"""date_range 단위 테스트 (외부 의존성 없음)."""

from datetime import date

from app.services.date_range import (
    apply_date_range_to_tool_args,
    normalize_date_range_from_text,
    week_bounds,
)


def test_next_week_from_monday_reference():
    ref = date(2026, 6, 23)  # Tuesday
    start, end = normalize_date_range_from_text("소연님 다음 주", reference=ref)
    mon, sun = week_bounds(ref, week_offset=1)
    assert start == mon.isoformat()
    assert end == sun.isoformat()


def test_this_week():
    ref = date(2026, 6, 23)
    start, end = normalize_date_range_from_text("이번 주 회의", reference=ref)
    mon, sun = week_bounds(ref, week_offset=0)
    assert start == mon.isoformat()
    assert end == sun.isoformat()


def test_recent_days():
    ref = date(2026, 6, 23)
    start, end = normalize_date_range_from_text("최근 3일", reference=ref)
    assert start == "2026-06-21"
    assert end == "2026-06-23"


def test_apply_overrides_year_month_on_week_intent():
    ref = date(2026, 6, 23)
    out = apply_date_range_to_tool_args(
        {"worker_name": "소연", "year_month": "2026-06"},
        "소연님 다음 주",
        reference=ref,
    )
    assert out["date"] == "2026-06-29"
    assert out["end_date"] == "2026-07-05"
    assert "year_month" not in out


def test_list_room_args():
    ref = date(2026, 6, 23)
    out = apply_date_range_to_tool_args(
        {"action": "list"},
        "다음주 내 회의",
        reference=ref,
    )
    assert out["date"] == "2026-06-29"
    assert out["end_date"] == "2026-07-05"


def test_list_mine_alias_normalized():
    from app.services.outlook_room.schedule_reserve import normalize_room_tool_args

    out = normalize_room_tool_args({"action": "list_mine"}, "소연님 이번 주 회의")
    assert out["action"] == "list"
