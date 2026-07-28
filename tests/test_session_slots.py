"""session_slots — 회의실/개인일정/정부과제 세션 슬롯."""
from __future__ import annotations

from app.services.session_slots import (
    apply_slots_to_tool_args,
    build_session_slots_block,
    empty_agent_slots,
    merge_slots_from_tool_result,
    should_override_with_slot_followup,
    suggest_slot_followup_call,
)


def test_merge_room_book_and_cancel_followup():
    slots = empty_agent_slots()
    slots = merge_slots_from_tool_result(
        slots,
        tool_name="manage_room_schedule",
        tool_result={
            "action": "book",
            "booking_id": "11111111-1111-1111-1111-111111111111",
            "room_name": "코넥홀",
            "subject": "회의",
            "date": "2026-07-30",
            "start_time": "2026-07-30T08:00:00",
            "end_time": "2026-07-30T09:00:00",
        },
        tool_args={"action": "book", "room_name": "코넥홀"},
        tool_content="booking_id: 11111111-1111-1111-1111-111111111111",
    )
    assert slots["active_schedule"]["domain"] == "room"
    assert slots["active_schedule"]["booking_id"].startswith("11111111")

    forced = suggest_slot_followup_call("취소해줘", slots)
    assert forced is not None
    assert forced["tool"] == "manage_room_schedule"
    assert forced["args"]["action"] == "cancel"
    assert forced["args"]["booking_id"].startswith("11111111")

    assert should_override_with_slot_followup(["respond_general"], "취소해줘", slots)

    args = apply_slots_to_tool_args(
        "manage_room_schedule",
        {"action": "cancel"},
        slots,
        "취소해줘",
    )
    assert args["booking_id"].startswith("11111111")
    assert args["room_name"] == "코넥홀"

    slots = merge_slots_from_tool_result(
        slots,
        tool_name="manage_room_schedule",
        tool_result={"action": "cancel", "booking_id": slots["active_schedule"]["booking_id"]},
        tool_args={"action": "cancel"},
        tool_content="✅ 예약 취소 완료: ok",
    )
    assert slots["active_schedule"] is None


def test_personal_modify_followup_uses_event_id():
    slots = empty_agent_slots()
    slots = merge_slots_from_tool_result(
        slots,
        tool_name="manage_personal_schedule",
        tool_result={
            "action": "create",
            "event_id": "AAMkADExAMPLE",
            "subject": "코넥플리이 활동",
            "date": "2026-07-31",
            "start_time": "2026-07-31T18:00:00",
        },
        tool_args={"action": "create", "subject": "코넥플리이 활동"},
        tool_content="event_id=AAMkADExAMPLE",
    )
    forced = suggest_slot_followup_call("코넥플리이 -> 코넥플레이로 수정해줘", slots)
    assert forced is not None
    assert forced["tool"] == "manage_personal_schedule"
    assert forced["args"]["action"] == "modify"
    assert forced["args"]["event_id"] == "AAMkADExAMPLE"

    args = apply_slots_to_tool_args(
        "manage_personal_schedule",
        {"action": "modify", "new_subject": "코넥플레이 활동"},
        slots,
        "수정해줘",
    )
    assert args["event_id"] == "AAMkADExAMPLE"


def test_gov_detail_files_followup():
    slots = empty_agent_slots()
    slots = merge_slots_from_tool_result(
        slots,
        tool_name="query_gov_projects",
        tool_result={
            "action": "detail",
            "idx": 3,
            "keyword": "AI",
            "target_date": "2026-07-28",
            "matched_count": 1,
        },
        tool_args={"action": "detail", "idx": 3},
    )
    forced = suggest_slot_followup_call("첨부파일 보여줘", slots)
    assert forced is not None
    assert forced["tool"] == "query_gov_projects"
    assert forced["args"]["action"] == "files"
    assert forced["args"]["idx"] == 3

    args = apply_slots_to_tool_args(
        "query_gov_projects",
        {"action": "files"},
        slots,
        "첨부",
    )
    assert args["idx"] == 3


def test_attendee_followup_not_reminder():
    slots = empty_agent_slots()
    slots = merge_slots_from_tool_result(
        slots,
        tool_name="manage_room_schedule",
        tool_result={
            "action": "book",
            "booking_id": "22222222-2222-2222-2222-222222222222",
            "room_name": "Femur",
            "date": "2026-07-30",
            "start_time": "2026-07-30T11:00:00",
        },
        tool_args={"action": "book"},
        tool_content="booking_id: 22222222-2222-2222-2222-222222222222",
    )
    forced = suggest_slot_followup_call("네 추가해주세요", slots)
    assert forced is not None
    assert forced["args"]["action"] == "modify"
    assert should_override_with_slot_followup(
        ["manage_room_schedule"],
        "네 추가해주세요",
        slots,
    )


def test_other_date_cancel_does_not_use_slot_booking_id():
    slots = empty_agent_slots()
    slots = merge_slots_from_tool_result(
        slots,
        tool_name="manage_room_schedule",
        tool_result={
            "action": "book",
            "booking_id": "11111111-1111-1111-1111-111111111111",
            "room_name": "코넥홀",
            "date": "2026-07-31",
            "start_time": "2026-07-31T18:00:00",
        },
        tool_args={"action": "book"},
        tool_content="booking_id: 11111111-1111-1111-1111-111111111111",
    )
    # 다른 날짜 명시 → 슬롯 강제 없음
    assert suggest_slot_followup_call("7/30 코넥홀 8시 회의 취소해줘", slots) is None

    args = apply_slots_to_tool_args(
        "manage_room_schedule",
        {
            "action": "cancel",
            "room_name": "코넥홀",
            "date": "2026-07-30",
            "start_time": "2026-07-30T08:00:00",
        },
        slots,
        "7/30 코넥홀 8시 회의 취소해줘",
    )
    assert "booking_id" not in args or not args.get("booking_id")


def test_list_single_refreshes_slot_multi_clears():
    slots = empty_agent_slots()
    slots = merge_slots_from_tool_result(
        slots,
        tool_name="manage_room_schedule",
        tool_result={"action": "book", "booking_id": "11111111-1111-1111-1111-111111111111"},
        tool_args={"action": "book"},
        tool_content="booking_id: 11111111-1111-1111-1111-111111111111",
    )
    slots = merge_slots_from_tool_result(
        slots,
        tool_name="manage_room_schedule",
        tool_result={"action": "list"},
        tool_args={"action": "list", "date": "2026-07-30"},
        tool_content=(
            "1. [본인] booking_id=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa | Spine | 회의 | "
            "2026-07-30 10:00"
        ),
    )
    assert slots["active_schedule"]["booking_id"].startswith("aaaaaaaa")

    slots = merge_slots_from_tool_result(
        slots,
        tool_name="manage_room_schedule",
        tool_result={"action": "list"},
        tool_args={"action": "list"},
        tool_content=(
            "1. booking_id=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa | Spine\n"
            "2. booking_id=bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb | Femur\n"
        ),
    )
    assert slots["active_schedule"] is None


def test_other_date_cancel_lets_llm_resolve_without_slot_id():
    """다른 날짜 명시 시 슬롯 booking_id를 넣지 않고 LLM 힌트 resolve에 맡긴다."""
    slots = empty_agent_slots()
    slots = merge_slots_from_tool_result(
        slots,
        tool_name="manage_room_schedule",
        tool_result={
            "action": "book",
            "booking_id": "11111111-1111-1111-1111-111111111111",
            "room_name": "코넥홀",
            "date": "2026-07-31",
            "start_time": "2026-07-31T18:00:00",
        },
        tool_args={"action": "book"},
        tool_content="booking_id: 11111111-1111-1111-1111-111111111111",
    )
    q = "7/30 코넥홀 8시 회의 취소해줘"
    assert suggest_slot_followup_call(q, slots) is None

    args = apply_slots_to_tool_args(
        "manage_room_schedule",
        {
            "action": "cancel",
            "room_name": "코넥홀",
            "date": "2026-07-30",
            "start_time": "2026-07-30T08:00:00",
        },
        slots,
        q,
    )
    assert not args.get("booking_id")


def test_slots_block_says_candidate_not_force():
    block = build_session_slots_block(
        {
            "active_schedule": {
                "domain": "room",
                "status": "booked",
                "booking_id": "33333333-3333-3333-3333-333333333333",
                "room_name": "Spine",
                "date": "2026-07-30",
            },
            "active_gov": None,
        }
    )
    assert "참고 후보" in block or "강제 아님" in block
    assert "booking_id=" in block
    assert "사용자에게" in block or "노출" in block


def test_cancel_and_modify_fill_booking_id_when_compatible():
    """cancel·modify 모두 슬롯 booking_id를 빈 인자에 보강 (사용자 노출용 아님)."""
    slots = {
        "active_schedule": {
            "domain": "room",
            "status": "booked",
            "booking_id": "f5d492aa-df54-474b-aec5-dfc599576fff",
            "room_name": "Spine 회의실",
            "date": "2026-07-29",
            "start_time": "2026-07-29T18:00:00",
            "subject": "회의",
        },
        "active_gov": None,
    }
    cancel_args = apply_slots_to_tool_args(
        "manage_room_schedule",
        {
            "action": "cancel",
            "date": "2026-07-29",
            "start_time": "2026-07-29T18:00:00",
            "subject": "회의",
        },
        slots,
        "내일 6시 회의 삭제해줘",
    )
    assert cancel_args["booking_id"].startswith("f5d492aa")
    assert "Spine" in (cancel_args.get("room_name") or "")

    modify_args = apply_slots_to_tool_args(
        "manage_room_schedule",
        {
            "action": "modify",
            "date": "2026-07-29",
            "start_time": "2026-07-29T18:00:00",
            "attendees": ["이소연"],
        },
        slots,
        "소연님 초대해줘",
    )
    assert modify_args["booking_id"].startswith("f5d492aa")


def test_check_inquiry_slot_for_book_followup():
    """가용 조회 후 관심 시각이 세션 슬롯에 남아 Turn1 LLM이 book에 이어 쓰게 한다."""
    slots = empty_agent_slots()
    slots = merge_slots_from_tool_result(
        slots,
        tool_name="manage_room_schedule",
        tool_result={"action": "check_all"},
        tool_args={
            "action": "check_all",
            "date": "2026-07-28T15:00:00",
            "end_date": "2026-07-28T16:00:00",
        },
        tool_content="가용",
    )
    sched = slots["active_schedule"]
    assert sched["status"] == "inquiry"
    assert sched["start_time"].startswith("2026-07-28T15:00")
    assert sched["date"] == "2026-07-28"
    block = build_session_slots_block(slots)
    assert "inquiry" in block
    assert "15:00" in block
