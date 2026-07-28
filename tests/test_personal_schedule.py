"""본인 Outlook personal_schedule 단위 테스트."""

from __future__ import annotations

from app.services.outlook_room.personal_schedule import (
    _match_event,
    is_personal_write_action,
    normalize_personal_tool_args,
)


def test_normalize_book_alias_to_create():
    out = normalize_personal_tool_args({"action": "book", "start_time": "2026-07-28T15:00:00"})
    assert out["action"] == "create"
    assert out["subject"] == "회의"
    assert out["end_time"].startswith("2026-07-28T16:00")


def test_normalize_list_keeps_dates():
    out = normalize_personal_tool_args(
        {"action": "list", "date": "2026-07-28", "end_date": "2026-07-30"},
        "이번 주 내 일정",
    )
    assert out["action"] == "list"
    assert out.get("date")


def test_match_event_by_id():
    events = [
        {"id": "e1", "subject": "A", "start": {"dateTime": "2026-07-28T10:00:00"}, "end": {"dateTime": "2026-07-28T11:00:00"}},
        {"id": "e2", "subject": "B", "start": {"dateTime": "2026-07-28T15:00:00"}, "end": {"dateTime": "2026-07-28T16:00:00"}},
    ]
    hit, err = _match_event(events, event_id="e2")
    assert err is None
    assert hit and hit["id"] == "e2"


def test_match_event_by_subject_and_start():
    events = [
        {
            "id": "e1",
            "subject": "주간 미팅",
            "start": {"dateTime": "2026-07-28T10:00:00"},
            "end": {"dateTime": "2026-07-28T11:00:00"},
            "sensitivity": "normal",
        },
    ]
    hit, err = _match_event(
        events,
        subject="주간",
        start_time="2026-07-28T10:00:00",
    )
    assert err is None
    assert hit and hit["id"] == "e1"


def test_is_personal_write_action():
    assert is_personal_write_action("create")
    assert is_personal_write_action("cancel")
    assert not is_personal_write_action("list")
