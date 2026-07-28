"""managed_room_events 단위 테스트."""

from datetime import date, timedelta

from app.services.outlook_room.managed_room_events import (
    DB_RETENTION_PAST_DAYS,
    SYNC_WINDOW_DAYS,
    event_in_retention_window,
    parse_graph_event,
    retention_window,
    sync_window,
    validate_availability_query_date,
    validate_list_query_range,
)


def test_sync_window_seven_days_forward():
    ref = date(2026, 6, 23)
    start, end = sync_window(ref)
    assert start == date(2026, 6, 23)
    assert end == date(2026, 6, 30)


def test_retention_window_keeps_past_ten_days():
    ref = date(2026, 6, 23)
    start, end = retention_window(ref)
    assert start == date(2026, 6, 23) - timedelta(days=DB_RETENTION_PAST_DAYS)
    assert end == date(2026, 6, 23) + timedelta(days=SYNC_WINDOW_DAYS)


def test_validate_availability_query_date_in_window():
    today = date.today()
    assert validate_availability_query_date(today) is None
    assert validate_availability_query_date(
        today + timedelta(days=SYNC_WINDOW_DAYS - 1),
    ) is None


def test_validate_availability_query_date_out_of_window():
    today = date.today()
    msg = validate_availability_query_date(today + timedelta(days=SYNC_WINDOW_DAYS))
    assert msg is not None
    assert "범위 밖" in msg


def test_validate_list_query_range_future_cap():
    today = date.today()
    start = today
    end_exclusive = today + timedelta(days=SYNC_WINDOW_DAYS + 2)
    msg = validate_list_query_range(start, end_exclusive)
    assert msg is not None


def test_parse_graph_event_skips_out_of_window():
    ref = date(2026, 6, 23)
    far = {
        "id": "evt1",
        "subject": "[FEMUR] test",
        "start": {"dateTime": "2026-12-01T10:00:00"},
        "end": {"dateTime": "2026-12-01T11:00:00"},
        "organizer": {"emailAddress": {"address": "a@connecteve.com"}},
        "attendees": [],
    }
    assert parse_graph_event("femur@connecteve.com", far) is None
    assert not event_in_retention_window("2026-12-01T10:00:00", reference=ref)


def test_parse_graph_event_in_window():
    day = (date.today() + timedelta(days=1)).isoformat()
    event = {
        "id": "evt2",
        "subject": "[FEMUR] standup",
        "start": {"dateTime": f"{day}T10:00:00"},
        "end": {"dateTime": f"{day}T11:00:00"},
        "organizer": {"emailAddress": {"address": "user@connecteve.com", "name": "User"}},
        "attendees": [
            {"emailAddress": {"address": "femur@connecteve.com"}, "type": "resource"},
        ],
    }
    parsed = parse_graph_event("femur@connecteve.com", event)
    assert parsed is not None
    assert parsed["room_name"] == "femur"
    assert parsed["organizer_email"] == "user@connecteve.com"
    assert parsed["subject"] == "standup"
