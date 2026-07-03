"""회의실 주최자 resolve·예약 가드 단위 테스트."""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from app.services.outlook_room import ms_graph_room as graph
from app.services.outlook_room.schedule_reserve import (
    default_end_time_one_hour,
    enrich_book_tool_args,
    extract_room_name_from_tool_history,
    is_room_write_action,
    normalize_room_tool_args,
    peek_manage_room_action,
    resolve_booking_room,
    room_name_maps_to_managed,
    room_write_once_message,
)


class TestRoomResolve(unittest.TestCase):
    def test_llm_canonical_femur(self):
        display, email = graph.resolve_room("femur")
        self.assertEqual(email, "femur@connecteve.com")

    def test_fuzzy_typo_feemur(self):
        display, email = graph.resolve_room("feemur")
        self.assertEqual(email, "femur@connecteve.com")

    def test_managed_room_check(self):
        self.assertTrue(room_name_maps_to_managed("spine"))
        self.assertFalse(room_name_maps_to_managed("unknown-xyz"))


class TestBookEnrich(unittest.TestCase):
    def test_start_only_defaults_one_hour(self):
        day = date.today().isoformat()
        out = enrich_book_tool_args(
            {"action": "book", "room_name": "atlas", "start_time": f"{day}T14:00:00"},
            "2시 예약",
        )
        self.assertEqual(out["end_time"], f"{day}T15:00:00")

    def test_llm_room_name_preserved(self):
        out = enrich_book_tool_args(
            {"action": "book", "room_name": "feemur"},
            "feemur 3시",
        )
        self.assertEqual(out["room_name"], "feemur")

    def test_no_room_without_tool_arg(self):
        self.assertIsNone(resolve_booking_room(None, "내일 2시 예약해줘", None))

    def test_room_from_tool_history(self):
        hist = [
            {
                "role": "assistant",
                "content": '{"room_name": "connechall", "action": "check"}',
            },
        ]
        self.assertEqual(extract_room_name_from_tool_history(hist), "connechall")
        self.assertEqual(
            resolve_booking_room(None, "", hist),
            "connechall",
        )


class TestRoomWriteGuard(unittest.TestCase):
    def test_write_actions(self):
        self.assertTrue(is_room_write_action("book"))
        self.assertTrue(is_room_write_action("cancel"))
        self.assertFalse(is_room_write_action("check"))

    def test_peek_action(self):
        args = normalize_room_tool_args(
            {"action": "book", "room_name": "spine"},
            "2시",
        )
        self.assertEqual(peek_manage_room_action(args), "book")

    def test_once_message(self):
        self.assertIn("하나만", room_write_once_message())


class TestOrganizerResolve(unittest.IsolatedAsyncioTestCase):
    async def test_slack_user_id_db_first(self):
        from app.services.outlook_room.attendee_resolver import resolve_organizer_email

        with patch(
            "app.services.outlook_room.attendee_resolver._lookup_user_in_db",
            new_callable=AsyncMock,
            return_value={"email": "user@connecteve.com", "name": "User"},
        ):
            email, name = await resolve_organizer_email(
                slack_user_id="U123",
                fallback_email="other@connecteve.com",
            )
        self.assertEqual(email, "user@connecteve.com")
        self.assertEqual(name, "User")


class TestAttendeeNameVariants(unittest.TestCase):
    def test_roster_expands_given_name(self):
        from app.services.outlook_room.attendee_resolver import name_lookup_variants

        with patch(
            "app.services.outlook_room.attendee_resolver._match_employees_in_query",
            return_value=["이소연"],
        ):
            self.assertEqual(name_lookup_variants("소연"), ["이소연", "소연"])

    def test_full_name_includes_without_surname(self):
        from app.services.outlook_room.attendee_resolver import name_lookup_variants

        with patch(
            "app.services.outlook_room.attendee_resolver._match_employees_in_query",
            return_value=["이소연"],
        ):
            self.assertEqual(name_lookup_variants("이소연"), ["이소연", "소연"])

    def test_unknown_name_keeps_original(self):
        from app.services.outlook_room.attendee_resolver import name_lookup_variants

        with patch(
            "app.services.outlook_room.attendee_resolver._match_employees_in_query",
            return_value=[],
        ):
            self.assertEqual(name_lookup_variants("금교현"), ["금교현"])


class TestResolveAttendees(unittest.IsolatedAsyncioTestCase):
    async def test_skips_empty_attendee_entry(self):
        from app.services.outlook_room.attendee_resolver import resolve_attendees

        with patch(
            "app.services.outlook_room.attendee_resolver._resolve_attendee_hit",
            new_callable=AsyncMock,
            return_value={"email": "soyeon@connecteve.com", "name": "이소연"},
        ) as mock_hit:
            resolved, unresolved = await resolve_attendees(
                [
                    {"email": None, "name": None, "slack_user_id": None},
                    {"name": "소연"},
                ],
                organizer_email="khkeum@connecteve.com",
            )

        self.assertEqual(resolved, [
            {"email": "soyeon@connecteve.com", "name": "이소연"},
        ])
        self.assertEqual(unresolved, [])
        mock_hit.assert_awaited_once()

    async def test_tries_name_variants_for_db_lookup(self):
        from app.services.outlook_room.attendee_resolver import (
            _lookup_user_by_name_variants,
        )

        calls: list[str] = []

        async def fake_lookup(*, name: str = "", **kwargs):
            calls.append(name)
            if name == "소연":
                return {"email": "soyeon@connecteve.com", "name": "소연"}
            return None

        with patch(
            "app.services.outlook_room.attendee_resolver._lookup_user_in_db",
            side_effect=fake_lookup,
        ), patch(
            "app.services.outlook_room.attendee_resolver._match_employees_in_query",
            return_value=["이소연"],
        ):
            hit = await _lookup_user_by_name_variants("소연")

        self.assertEqual(hit, {"email": "soyeon@connecteve.com", "name": "소연"})
        self.assertEqual(calls, ["이소연", "소연"])


class TestDefaultEnd(unittest.TestCase):
    def test_one_hour(self):
        self.assertEqual(
            default_end_time_one_hour("2026-06-18T14:00:00"),
            "2026-06-18T15:00:00",
        )


if __name__ == "__main__":
    unittest.main()
