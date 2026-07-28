"""회의실 예약 헬퍼 단위 테스트."""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from app.services.outlook_room.ms_graph_room import slot_conflicts_with_events
from app.services.outlook_room.managed_room_events import normalize_date_filter
from app.services.outlook_room.schedule_reserve import (
    extract_booking_id,
    has_recent_operational_room_context,
    is_room_status_query,
    normalize_room_tool_args,
    parse_time_range_from_query,
    should_reroute_list_to_room_check,
    _normalize_check_slot,
)


class TestNormalizeDateFilter(unittest.TestCase):
    def test_today_korean(self):
        self.assertEqual(normalize_date_filter("오늘"), date.today().isoformat())

    def test_iso(self):
        self.assertEqual(normalize_date_filter("2026-06-18"), "2026-06-18")


class TestSlotExclude(unittest.TestCase):
    def test_exclude_own_booking_by_time_range(self):
        events = [
            {
                "subject": "Keum kyohyun",
                "start": {"dateTime": "2026-06-18T21:00:00"},
                "end": {"dateTime": "2026-06-18T22:00:00"},
            },
        ]
        conflict = slot_conflicts_with_events(
            events,
            "2026-06-18T21:00:00",
            "2026-06-18T23:00:00",
            exclude_event_subjects=["[ATLAS] 회의"],
            exclude_time_ranges=[("2026-06-18T21:00:00", "2026-06-18T22:00:00")],
        )
        self.assertIsNone(conflict)


class TestTimeParsing(unittest.TestCase):
    def test_range(self):
        start, end, _ = parse_time_range_from_query("18시부터 20시까지")
        self.assertEqual(start, f"{date.today().isoformat()}T18:00:00")
        self.assertEqual(end, f"{date.today().isoformat()}T20:00:00")

    def test_iso_datetime(self):
        start, end, day = parse_time_range_from_query(
            "2026-06-18 22:00의 회의실 예약현황 조회해줘",
        )
        self.assertEqual(day.isoformat(), "2026-06-18")
        self.assertEqual(start, "2026-06-18T22:00:00")
        self.assertEqual(end, "2026-06-18T23:00:00")

    @patch("app.services.outlook_room.schedule_reserve._now_kst")
    def test_now_uses_current_minute(self, mock_now):
        from datetime import datetime

        mock_now.return_value = datetime(2026, 6, 18, 21, 43)
        start, end, day = parse_time_range_from_query("지금")
        self.assertEqual(start, "2026-06-18T21:43:00")
        self.assertEqual(end, "2026-06-18T22:43:00")


class TestNormalizeRoomArgs(unittest.TestCase):
    def test_status_query_full_day(self):
        q = "2026-06-18 22:00의 회의실 예약현황 조회해줘"
        self.assertTrue(is_room_status_query(q))
        args = normalize_room_tool_args({
            "action": "check_all",
            "date": "2026-06-18",
            "start_time": "2026-06-18T22:00:00",
            "end_time": "2026-06-18T22:00:00",
        }, q)
        self.assertNotIn("start_time", args)
        self.assertEqual(args.get("focus_time"), "2026-06-18 22:00")

    @patch("app.services.outlook_room.schedule_reserve._now_kst")
    def test_equal_slot_bumps_end(self, mock_now):
        from datetime import datetime

        mock_now.return_value = datetime(2026, 6, 18, 21, 0)
        result = _normalize_check_slot(
            "2026-06-18T22:00:00",
            "2026-06-18T22:00:00",
        )
        start, end = result
        self.assertEqual(end, "2026-06-18T23:00:00")

    def test_extract_booking_id(self):
        text = "booking_id: 4ef79359-0d89-4243-810c-d60c0fda3fff"
        self.assertEqual(
            extract_booking_id(text),
            "4ef79359-0d89-4243-810c-d60c0fda3fff",
        )

    def test_operational_context_not_wiki_only(self):
        wiki_hist = [{"role": "user", "content": "회의실 관련해서도 있나?"}]
        self.assertFalse(has_recent_operational_room_context(wiki_hist))

    def test_list_with_room_name_reroutes_to_check(self):
        q = "내일 spine 회의실 일정 알려줘"
        self.assertTrue(should_reroute_list_to_room_check(q, {
            "action": "list",
            "room_name": "Spine 회의실",
        }))
        args = normalize_room_tool_args({
            "action": "list",
            "room_name": "Spine 회의실",
            "date": "2026-07-28",
            "end_date": "2026-07-28",
        }, q)
        self.assertEqual(args["action"], "check")
        self.assertEqual(args["room_name"], "Spine 회의실")
        self.assertNotIn("end_date", args)

    def test_list_with_person_name_stays_list(self):
        q = "이번 주 소연님 회의 일정"
        args = normalize_room_tool_args({
            "action": "list",
            "room_name": "Spine 회의실",
            "date": "2026-07-28",
            "person_name": "소연",
        }, q)
        self.assertEqual(args["action"], "list")
        self.assertEqual(args["person_name"], "소연")


if __name__ == "__main__":
    unittest.main()
