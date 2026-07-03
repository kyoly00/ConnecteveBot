"""managed_room_events 단위 테스트."""

from datetime import date

from app.services.outlook_room.managed_room_events import (
    event_in_retention_window,
    parse_graph_event,
    retention_window,
)


def test_retention_window():
    ref = date(2026, 6, 23)
    start, end = retention_window(ref)
    assert start == date(2026, 6, 16)
    assert end == date(2026, 7, 24)


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
    event = {
        "id": "evt2",
        "subject": "[FEMUR] standup",
        "start": {"dateTime": "2026-06-25T10:00:00"},
        "end": {"dateTime": "2026-06-25T11:00:00"},
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
