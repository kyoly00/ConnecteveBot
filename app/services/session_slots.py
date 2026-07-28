"""
세션 단기 슬롯 — 회의실·본인 Outlook·정부과제 follow-up용.

chat_sessions.metadata.agent_slots 에 저장.
Turn1에 주입하고, tool args에 ID/힌트를 서버가 보강한다.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

AGENT_SLOTS_KEY = "agent_slots"
SCHEDULE_KEY = "active_schedule"
GOV_KEY = "active_gov"

_FOLLOWUP_CANCEL = re.compile(
    r"(취소|지워|삭제|없애)",
    re.IGNORECASE,
)
_FOLLOWUP_MODIFY = re.compile(
    r"(수정|변경|바꿔|바꾸|고쳐|제목)",
    re.IGNORECASE,
)
_FOLLOWUP_ATTENDEE = re.compile(
    r"(참석|초대|추가)",
    re.IGNORECASE,
)
_FOLLOWUP_REMINDER = re.compile(
    r"(리마인더|reminder|알림)",
    re.IGNORECASE,
)
_FOLLOWUP_GOV_DETAIL = re.compile(
    r"(상세|자세히|내용|디테일|detail)",
    re.IGNORECASE,
)
_FOLLOWUP_GOV_FILES = re.compile(
    r"(첨부|파일|양식|서류|신청서)",
    re.IGNORECASE,
)
_SHORT_AFFIRM = re.compile(
    r"^(네|예|응|어|ㅇㅇ|좋아|해줘|해둬|부탁|진행|추가해|초대해)\s*[.!]*$",
    re.IGNORECASE,
)

_EVENT_ID_RE = re.compile(r"(?:event_id|일정 ID)\s*[=:]\s*(\S+)", re.IGNORECASE)
_BOOKING_ID_RE = re.compile(
    r"(?:booking_id|예약 ID)\s*[=:]\s*"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
_BOOKING_ID_INLINE_RE = re.compile(
    r"booking_id=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
_DATE_IN_TEXT_RE = re.compile(
    r"(?:"
    r"\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}\s*월\s*\d{1,2}\s*일"
    r"|\d{1,2}\s*/\s*\d{1,2}"
    r"|오늘|내일|모레|어제"
    r")",
)
_ROOM_IN_TEXT_RE = re.compile(
    r"(spine|femur|atlas|코넥홀|코넥 홀)",
    re.IGNORECASE,
)


def _date_key(value: Any) -> str | None:
    s = str(value or "").strip()
    if not s:
        return None
    if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-":
        return s[:10]
    m = re.search(r"(\d{1,2})\s*/\s*(\d{1,2})", s)
    if m:
        return f"{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", s)
    if m:
        return f"{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None


def _room_key(value: Any) -> str | None:
    s = str(value or "").strip().lower().replace(" ", "")
    if not s:
        return None
    for name in ("spine", "femur", "atlas", "코넥홀"):
        if name in s:
            return name
    return s


def _time_hhmm(value: Any) -> str | None:
    s = str(value or "").strip()
    if not s:
        return None
    m = re.search(r"T(\d{2}):(\d{2})", s)
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", s)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return None


def args_conflict_with_schedule_slot(
    args: dict[str, Any] | None,
    sched: dict[str, Any] | None,
) -> bool:
    """tool args가 슬롯과 다른 예약/일정을 가리키면 True."""
    if not isinstance(sched, dict) or not isinstance(args, dict):
        return False
    slot_date = _date_key(sched.get("date") or sched.get("start_time"))
    arg_date = _date_key(args.get("date") or args.get("start_time"))
    if slot_date and arg_date and slot_date != arg_date:
        return True
    slot_room = _room_key(sched.get("room_name"))
    arg_room = _room_key(args.get("room_name") or args.get("new_room_name"))
    if slot_room and arg_room and slot_room != arg_room:
        return True
    slot_t = _time_hhmm(sched.get("start_time"))
    arg_t = _time_hhmm(args.get("start_time") or args.get("new_start_time"))
    if slot_date and arg_date and slot_date == arg_date and slot_t and arg_t and slot_t != arg_t:
        return True
    return False


def query_conflicts_with_schedule_slot(
    query: str,
    sched: dict[str, Any] | None,
) -> bool:
    """질문에 슬롯과 다른 날짜/회의실이 명시되면 True."""
    if not isinstance(sched, dict):
        return False
    q = query or ""
    slot_date = _date_key(sched.get("date") or sched.get("start_time"))
    slot_room = _room_key(sched.get("room_name"))

    for raw in _DATE_IN_TEXT_RE.findall(q):
        if raw in ("오늘", "내일", "모레", "어제"):
            continue
        q_key = _date_key(raw)
        if not q_key or not slot_date:
            continue
        if len(slot_date) == 10 and len(q_key) == 5 and slot_date.endswith(q_key):
            continue
        if len(q_key) == 10 and len(slot_date) == 5 and q_key.endswith(slot_date):
            continue
        if q_key != slot_date:
            return True

    for raw in _ROOM_IN_TEXT_RE.findall(q):
        rk = _room_key(raw)
        if rk and slot_room and rk != slot_room:
            return True
    return False


def query_has_explicit_schedule_target(query: str) -> bool:
    """날짜·회의실 등 구체 대상이 질문에 있으면 True → LLM이 힌트로 resolve."""
    q = query or ""
    if _ROOM_IN_TEXT_RE.search(q):
        return True
    for raw in _DATE_IN_TEXT_RE.findall(q):
        if raw not in ("오늘",):  # '오늘'만으로는 슬롯 follow-up일 수 있음
            return True
    if re.search(r"\d{1,2}\s*시", q):
        return True
    return False


def is_ambiguous_schedule_followup(query: str) -> bool:
    """'취소해줘'처럼 대상 없이 직전 슬롯을 가리키는 짧은 후속."""
    q = (query or "").strip()
    if not q or len(q) > 24:
        return False
    if query_has_explicit_schedule_target(q):
        return False
    return bool(
        _FOLLOWUP_CANCEL.search(q)
        or _FOLLOWUP_MODIFY.search(q)
        or _FOLLOWUP_ATTENDEE.search(q)
        or _FOLLOWUP_REMINDER.search(q)
        or _SHORT_AFFIRM.search(q)
    )


def empty_agent_slots() -> dict[str, Any]:
    return {SCHEDULE_KEY: None, GOV_KEY: None}


def load_agent_slots(metadata: dict[str, Any] | None) -> dict[str, Any]:
    meta = metadata if isinstance(metadata, dict) else {}
    raw = meta.get(AGENT_SLOTS_KEY)
    if not isinstance(raw, dict):
        return empty_agent_slots()
    return {
        SCHEDULE_KEY: raw.get(SCHEDULE_KEY) if isinstance(raw.get(SCHEDULE_KEY), dict) else None,
        GOV_KEY: raw.get(GOV_KEY) if isinstance(raw.get(GOV_KEY), dict) else None,
    }


def dump_agent_slots_into_metadata(
    metadata: dict[str, Any] | None,
    slots: dict[str, Any],
) -> dict[str, Any]:
    meta = dict(metadata or {})
    meta[AGENT_SLOTS_KEY] = {
        SCHEDULE_KEY: slots.get(SCHEDULE_KEY),
        GOV_KEY: slots.get(GOV_KEY),
    }
    return meta


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def set_active_schedule(slots: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(slots or empty_agent_slots())
    data = {k: v for k, v in payload.items() if v not in (None, "", [], {})}
    data["updated_at"] = _now_iso()
    out[SCHEDULE_KEY] = data
    return out


def clear_active_schedule(slots: dict[str, Any]) -> dict[str, Any]:
    out = dict(slots or empty_agent_slots())
    out[SCHEDULE_KEY] = None
    return out


def set_active_gov(slots: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(slots or empty_agent_slots())
    data = {k: v for k, v in payload.items() if v not in (None, "", [], {})}
    data["updated_at"] = _now_iso()
    out[GOV_KEY] = data
    return out


def extract_event_id_from_text(text: str) -> str | None:
    m = _EVENT_ID_RE.search(text or "")
    if not m:
        return None
    eid = m.group(1).strip().rstrip(".)],")
    return eid or None


def extract_booking_id_from_text(text: str) -> str | None:
    m = _BOOKING_ID_RE.search(text or "")
    return m.group(1) if m else None


def build_session_slots_block(slots: dict[str, Any] | None) -> str:
    """Turn1 system — 구조화 슬롯 (UUID 포함, 사용자 노출용 아님)."""
    slots = slots or empty_agent_slots()
    sched = slots.get(SCHEDULE_KEY)
    gov = slots.get(GOV_KEY)
    if not sched and not gov:
        return ""

    lines = [
        "<session_agent_slots>",
        "직전 성공한 예약/일정/공고의 *참고 후보*다. 사용자가 가리키는 대상을 먼저 파악한다.",
        "- 질문/대화에 다른 날짜·회의실·제목이 있으면 슬롯 ID를 쓰지 말고, "
        "room_name·date·start_time·subject 힌트로 tool을 호출해 대상을 resolve한다.",
        "- '취소해줘'·'수정해줘'처럼 대상이 생략된 짧은 follow-up만 아래 슬롯 ID를 사용한다.",
        "- 새 주제·다른 예약이면 슬롯을 무시한다.",
    ]
    if isinstance(sched, dict):
        domain = sched.get("domain") or ""
        lines.append("[active_schedule] 가장 최근 조작 후보 (기본값일 뿐 강제 아님)")
        lines.append(f"domain={domain}")
        if domain == "room" and sched.get("booking_id"):
            lines.append(f"booking_id={sched['booking_id']}")
        if domain == "personal" and sched.get("event_id"):
            lines.append(f"event_id={sched['event_id']}")
        for key in ("room_name", "subject", "date", "start_time", "end_time"):
            if sched.get(key):
                lines.append(f"{key}={sched[key]}")
        if sched.get("attendees"):
            lines.append(f"attendees={sched['attendees']}")
        if domain == "room":
            lines.append(
                "생략형 follow-up → manage_room_schedule. "
                "날짜/회의실이 명시되면 booking_id 없이 힌트만 전달."
            )
        elif domain == "personal":
            lines.append(
                "생략형 follow-up → manage_personal_schedule. "
                "다른 일정이면 event_id 없이 subject+date 힌트."
            )
    if isinstance(gov, dict):
        lines.append("[active_gov] 최근 공고 후보 (강제 아님)")
        for key in ("target_date", "idx", "keyword", "last_action"):
            if gov.get(key) is not None and gov.get(key) != "":
                lines.append(f"{key}={gov[key]}")
        lines.append(
            "상세/첨부가 이 공고를 가리키면 idx 사용. 다른 공고면 list/keyword로 다시 고른다."
        )
    lines.append("</session_agent_slots>")
    return "\n".join(lines)


def _fill_if_empty(args: dict[str, Any], key: str, value: Any) -> None:
    if value in (None, "", [], {}):
        return
    cur = args.get(key)
    if cur in (None, "", [], {}):
        args[key] = value


def apply_slots_to_tool_args(
    tool_name: str,
    tool_args: dict[str, Any],
    slots: dict[str, Any] | None,
    query: str = "",
) -> dict[str, Any]:
    """
    슬롯 ID 보강은 *생략형 follow-up*에만.
    날짜·회의실 등이 이미 있으면 LLM 힌트를 존중하고 booking_id를 넣지 않는다
    (prepare_booking_target이 room/date/start로 resolve).
    """
    args = dict(tool_args or {})
    slots = slots or empty_agent_slots()
    q = (query or "").strip()

    if tool_name == "manage_room_schedule":
        sched = slots.get(SCHEDULE_KEY)
        if isinstance(sched, dict) and sched.get("domain") == "room":
            action = str(args.get("action") or "").strip()
            if action not in ("cancel", "modify", "replace", "set_reminder"):
                return args
            # LLM이 이미 대상을 힌트로 고른 경우 → ID 주입 금지
            has_llm_target = bool(
                args.get("date")
                or args.get("start_time")
                or args.get("room_name")
                or args.get("subject")
                or args.get("booking_id")
            )
            if has_llm_target and not is_ambiguous_schedule_followup(q):
                return args
            if args_conflict_with_schedule_slot(args, sched) or query_conflicts_with_schedule_slot(q, sched):
                return args
            if not is_ambiguous_schedule_followup(q) and has_llm_target:
                return args
            # 생략형: 슬롯으로 보강
            if is_ambiguous_schedule_followup(q) or not has_llm_target:
                _fill_if_empty(args, "booking_id", sched.get("booking_id"))
                _fill_if_empty(args, "room_name", sched.get("room_name"))
                _fill_if_empty(args, "subject", sched.get("subject"))
                _fill_if_empty(args, "date", sched.get("date"))
                _fill_if_empty(args, "start_time", sched.get("start_time"))
                _fill_if_empty(args, "end_time", sched.get("end_time"))

    elif tool_name == "manage_personal_schedule":
        sched = slots.get(SCHEDULE_KEY)
        if isinstance(sched, dict) and sched.get("domain") == "personal":
            action = str(args.get("action") or "").strip()
            if action not in ("modify", "cancel"):
                return args
            has_llm_target = bool(
                args.get("date")
                or args.get("start_time")
                or args.get("subject")
                or args.get("event_id")
            )
            if has_llm_target and not is_ambiguous_schedule_followup(q):
                return args
            if args_conflict_with_schedule_slot(args, sched) or query_conflicts_with_schedule_slot(q, sched):
                return args
            if is_ambiguous_schedule_followup(q) or not has_llm_target:
                _fill_if_empty(args, "event_id", sched.get("event_id"))
                _fill_if_empty(args, "subject", sched.get("subject"))
                _fill_if_empty(args, "date", sched.get("date"))
                _fill_if_empty(args, "start_time", sched.get("start_time"))

    elif tool_name == "query_gov_projects":
        gov = slots.get(GOV_KEY)
        if isinstance(gov, dict):
            action = str(args.get("action") or "list").strip()
            if action in ("detail", "files"):
                if args.get("idx") is None and gov.get("idx") is not None:
                    args["idx"] = gov["idx"]
                _fill_if_empty(args, "keyword", gov.get("keyword"))

    return args


def suggest_slot_followup_call(
    query: str,
    slots: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """
    대상이 생략된 짧은 follow-up만 슬롯 기반으로 tool 강제.
    날짜·회의실이 있으면 None → LLM이 맥락으로 tool_call.
    """
    q = (query or "").strip()
    if not q or len(q) > 80:
        return None
    slots = slots or empty_agent_slots()
    sched = slots.get(SCHEDULE_KEY) if isinstance(slots.get(SCHEDULE_KEY), dict) else None
    gov = slots.get(GOV_KEY) if isinstance(slots.get(GOV_KEY), dict) else None

    # 정부과제: 상세/첨부 키워드만 (공고 번호 등이 있으면 LLM에 맡김)
    if gov and (_FOLLOWUP_GOV_FILES.search(q) or _FOLLOWUP_GOV_DETAIL.search(q)):
        if re.search(r"\b\d+\b|번", q) and not (gov.get("idx") is not None and str(gov.get("idx")) in q):
            # 다른 idx를 말할 수 있음 → LLM
            if re.search(r"\d+", q) and str(gov.get("idx")) not in q:
                return None
        action = "files" if _FOLLOWUP_GOV_FILES.search(q) else "detail"
        args: dict[str, Any] = {"action": action}
        if gov.get("idx") is not None:
            args["idx"] = gov["idx"]
        if gov.get("keyword"):
            args["keyword"] = gov["keyword"]
        return {"tool": "query_gov_projects", "args": args}

    if not sched:
        return None

    # 구체 대상이 있으면 LLM이 room/date로 고르게 둠
    if query_has_explicit_schedule_target(q) or query_conflicts_with_schedule_slot(q, sched):
        return None
    if not is_ambiguous_schedule_followup(q):
        return None

    domain = sched.get("domain")
    wants_cancel = bool(_FOLLOWUP_CANCEL.search(q))
    wants_modify = bool(_FOLLOWUP_MODIFY.search(q))
    wants_attendee = bool(_FOLLOWUP_ATTENDEE.search(q))
    wants_reminder = bool(_FOLLOWUP_REMINDER.search(q))

    if domain == "room":
        if wants_cancel:
            return {
                "tool": "manage_room_schedule",
                "args": {
                    "action": "cancel",
                    "booking_id": sched.get("booking_id"),
                    "room_name": sched.get("room_name"),
                    "date": sched.get("date"),
                    "start_time": sched.get("start_time"),
                    "subject": sched.get("subject"),
                },
            }
        if wants_reminder and not wants_attendee:
            return {
                "tool": "manage_room_schedule",
                "args": {
                    "action": "set_reminder",
                    "booking_id": sched.get("booking_id"),
                    "room_name": sched.get("room_name"),
                    "date": sched.get("date"),
                    "start_time": sched.get("start_time"),
                    "reminder_minutes": 15,
                },
            }
        if wants_modify or (wants_attendee and not wants_reminder):
            args = {
                "action": "modify",
                "booking_id": sched.get("booking_id"),
                "room_name": sched.get("room_name"),
                "date": sched.get("date"),
                "start_time": sched.get("start_time"),
                "subject": sched.get("subject"),
            }
            return {"tool": "manage_room_schedule", "args": args}
        return None

    if domain == "personal":
        if wants_cancel:
            return {
                "tool": "manage_personal_schedule",
                "args": {
                    "action": "cancel",
                    "event_id": sched.get("event_id"),
                    "subject": sched.get("subject"),
                    "date": sched.get("date"),
                    "start_time": sched.get("start_time"),
                },
            }
        if wants_modify or wants_attendee:
            args = {
                "action": "modify",
                "event_id": sched.get("event_id"),
                "subject": sched.get("subject"),
                "date": sched.get("date"),
                "start_time": sched.get("start_time"),
            }
            return {"tool": "manage_personal_schedule", "args": args}

    return None


def should_override_with_slot_followup(
    tool_names: list[str],
    query: str,
    slots: dict[str, Any] | None,
) -> bool:
    """respond_general만 있거나 빈 경우, 슬롯 follow-up이면 강제."""
    if not suggest_slot_followup_call(query, slots):
        return False
    names = [n for n in tool_names if n]
    if not names:
        return True
    if names == ["respond_general"]:
        return True
    # reminder로 잘못 간 참석자 추가
    q = query or ""
    if (
        "manage_room_schedule" in names
        and _FOLLOWUP_ATTENDEE.search(q)
        and not _FOLLOWUP_REMINDER.search(q)
    ):
        return True
    if (
        "manage_personal_schedule" not in names
        and "manage_room_schedule" not in names
        and "query_gov_projects" not in names
        and suggest_slot_followup_call(query, slots)
    ):
        # flex 등으로 새면 강제
        forced = suggest_slot_followup_call(query, slots)
        if forced and forced["tool"] not in names:
            if _FOLLOWUP_CANCEL.search(q) or _FOLLOWUP_MODIFY.search(q) or _FOLLOWUP_ATTENDEE.search(q):
                return True
    return False


def merge_slots_from_tool_result(
    slots: dict[str, Any] | None,
    *,
    tool_name: str,
    tool_result: dict[str, Any],
    tool_args: dict[str, Any] | None = None,
    tool_content: str = "",
) -> dict[str, Any]:
    """성공 tool 결과로 슬롯 갱신."""
    slots = dict(slots or empty_agent_slots())
    args = tool_args or {}
    result = tool_result or {}
    if result.get("error") or result.get("skipped"):
        return slots

    if tool_name == "manage_room_schedule":
        action = str(result.get("action") or args.get("action") or "").strip()
        if action == "cancel":
            content = tool_content or ""
            if "찾지 못" in content or "실패" in content:
                return slots
            if "예약 취소 완료" in content or "취소 완료" in content:
                return clear_active_schedule(slots)
            return slots
        if action in ("list", "list_mine"):
            # 조회 결과로 슬롯 갱신: 단건이면 그 예약, 다건이면 슬롯 비움(오취소 방지)
            found = _BOOKING_ID_INLINE_RE.findall(tool_content or "")
            if not found:
                found = _BOOKING_ID_RE.findall(tool_content or "")
            uniq = list(dict.fromkeys(found))
            if len(uniq) == 1:
                return set_active_schedule(
                    slots,
                    {
                        "domain": "room",
                        "booking_id": uniq[0],
                        "room_name": args.get("room_name") or (slots.get(SCHEDULE_KEY) or {}).get("room_name"),
                        "date": args.get("date") or (slots.get(SCHEDULE_KEY) or {}).get("date"),
                        "start_time": args.get("start_time"),
                        "subject": args.get("subject"),
                    },
                )
            if len(uniq) > 1:
                return clear_active_schedule(slots)
            return slots
        if action in ("book", "modify", "replace", "set_reminder"):
            bid = result.get("booking_id") or extract_booking_id_from_text(tool_content)
            if action == "book" and not bid:
                return slots
            payload = {
                "domain": "room",
                "booking_id": bid or (slots.get(SCHEDULE_KEY) or {}).get("booking_id"),
                "room_name": result.get("room_name") or args.get("room_name") or args.get("new_room_name"),
                "subject": args.get("new_subject") or args.get("subject") or result.get("subject"),
                "date": args.get("date") or result.get("date"),
                "start_time": args.get("new_start_time") or args.get("start_time") or result.get("start_time"),
                "end_time": args.get("new_end_time") or args.get("end_time") or result.get("end_time"),
            }
            if not payload.get("date") and payload.get("start_time"):
                st = str(payload["start_time"])
                if len(st) >= 10 and st[4:5] == "-":
                    payload["date"] = st[:10]
            if bid or payload.get("room_name") or payload.get("start_time"):
                return set_active_schedule(slots, payload)

    elif tool_name == "manage_personal_schedule":
        action = str(result.get("action") or args.get("action") or "").strip()
        eid = result.get("event_id") or extract_event_id_from_text(tool_content)
        if action == "cancel":
            content = tool_content or ""
            if any(x in content for x in ("취소했습니다", "삭제했습니다", "일정을 취소")):
                return clear_active_schedule(slots)
            return slots
        if action in ("create", "modify", "list"):
            if action == "create" and not eid:
                return slots
            if action == "list" and not eid:
                return slots
            if action in ("create", "modify") or eid:
                payload = {
                    "domain": "personal",
                    "event_id": eid or (slots.get(SCHEDULE_KEY) or {}).get("event_id"),
                    "subject": args.get("new_subject") or args.get("subject") or result.get("subject"),
                    "date": args.get("date") or result.get("date"),
                    "start_time": args.get("new_start_time") or args.get("start_time") or result.get("start_time"),
                    "end_time": args.get("new_end_time") or args.get("end_time") or result.get("end_time"),
                    "attendees": args.get("attendees") or result.get("attendees"),
                }
                if payload.get("event_id") or action == "create":
                    return set_active_schedule(slots, payload)

    elif tool_name == "query_gov_projects":
        action = str(result.get("action") or args.get("action") or "list").strip()
        idx = result.get("idx")
        if idx is None:
            idx = args.get("idx")
        payload = {
            "target_date": result.get("target_date") or args.get("target_date"),
            "idx": idx,
            "keyword": result.get("keyword") or args.get("keyword"),
            "last_action": action,
            "matched_count": result.get("matched_count"),
        }
        # list에서 단건이면 idx 유지; multi면 idx는 detail 이후에만
        if action == "list" and result.get("matched_count") == 1 and idx is None:
            # leave idx empty; keyword may help
            pass
        return set_active_gov(slots, payload)

    return slots
