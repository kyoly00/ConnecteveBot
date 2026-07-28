"""managed_room_sync DB↔API 검증 단위 테스트."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.outlook_room.managed_room_sync import room_day_events_match


def _row(eid: str, start: str, end: str, subject: str):
    return SimpleNamespace(
        outlook_event_id=eid,
        start_time=start,
        end_time=end,
        event_subject=subject,
        subject=subject,
    )


def test_room_day_events_match_identical():
    db_rows = [_row("e1", "2026-07-28T11:00:00", "2026-07-28T12:00:00", "[SPINE] Team")]
    api_events = [
        {
            "id": "e1",
            "subject": "[SPINE] Team",
            "start": {"dateTime": "2026-07-28T11:00:00"},
            "end": {"dateTime": "2026-07-28T12:00:00"},
        },
    ]
    assert room_day_events_match(db_rows, api_events)


def test_room_day_events_match_detects_missing_in_db():
    db_rows: list = []
    api_events = [
        {
            "id": "e2",
            "subject": "Standup",
            "start": {"dateTime": "2026-07-28T10:00:00"},
            "end": {"dateTime": "2026-07-28T11:00:00"},
        },
    ]
    assert not room_day_events_match(db_rows, api_events)


def test_room_day_events_match_detects_time_change():
    db_rows = [_row("e1", "2026-07-28T11:00:00", "2026-07-28T12:00:00", "A")]
    api_events = [
        {
            "id": "e1",
            "subject": "A",
            "start": {"dateTime": "2026-07-28T11:30:00"},
            "end": {"dateTime": "2026-07-28T12:30:00"},
        },
    ]
    assert not room_day_events_match(db_rows, api_events)
