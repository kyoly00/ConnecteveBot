"""참석자 정규화 — 이름 문자열도 book/modify 공통으로 DB resolve."""
from __future__ import annotations

from app.services.outlook_room.attendee_resolver import normalize_attendee_items


def test_normalize_name_string_and_email():
    items = normalize_attendee_items(["이소연", "sylee@connecteve.com"])
    assert items[0] == {"name": "이소연"}
    assert items[1]["email"] == "sylee@connecteve.com"


def test_normalize_dict_and_slack_mailto():
    items = normalize_attendee_items(
        [
            {"name": "소연"},
            "<mailto:sylee@connecteve.com|sylee@connecteve.com>",
            {"email": "<mailto:a@b.com|a@b.com>", "name": "A"},
        ]
    )
    assert items[0] == {"name": "소연"}
    assert items[1]["email"] == "sylee@connecteve.com"
    assert items[2]["email"] == "a@b.com"
    assert items[2]["name"] == "A"


def test_normalize_empty_skipped():
    assert normalize_attendee_items([None, "", {}, "  "]) == []  # type: ignore[list-item]
