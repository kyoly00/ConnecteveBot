"""
tool_policy.py — 사용자-facing tool 메시지 정제.

API 필드명·내부 tool/action명을 한글 라벨로 치환한다.
"""
from __future__ import annotations

import re

# 사용자 메시지에 노출 금지 — 오류 메시지 한글화용
FIELD_LABELS: dict[str, str] = {
    "booking_id": "예약 ID",
    "room_name": "회의실",
    "new_room_name": "변경 회의실",
    "subject": "제목",
    "new_subject": "변경 제목",
    "start_time": "시작 시각",
    "end_time": "종료 시각",
    "new_start_time": "변경 시작 시각",
    "new_end_time": "변경 종료 시각",
    "old_start_time": "기존 시작 시각",
    "old_end_time": "기존 종료 시각",
    "date": "날짜",
    "end_date": "종료 날짜",
    "event_id": "일정 ID",
    "reminder_minutes": "리마인더(분)",
    "focus_time": "관심 시각",
    "action": "작업 종류",
}


def label_field(field: str) -> str:
    """API 필드명 → 사용자-facing 한글 라벨."""
    return FIELD_LABELS.get(field.strip(), field.strip())


def format_missing_fields(fields: list[str]) -> str:
    return ", ".join(label_field(f) for f in fields)


def missing_fields_message(prefix: str, fields: list[str]) -> str:
    return f"{prefix}: {format_missing_fields(fields)}"


def sanitize_user_facing_tool_message(content: str) -> str:
    """tool 오류·안내에서 API 필드명을 한글 라벨로 치환."""
    text = (content or "").strip()
    if not text:
        return text

    for field, label in sorted(FIELD_LABELS.items(), key=lambda x: -len(x[0])):
        text = re.sub(rf"\b{re.escape(field)}\b", label, text)

    text = re.sub(
        r"지원하지 않는 action입니다:\s*\S+",
        "지원하지 않는 작업입니다.",
        text,
    )
    text = re.sub(
        r"manage_room_schedule|search_company_wiki|respond_general|"
        r"search_worker_schedule|query_gov_projects",
        "내부 처리",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(list|list_mine|check_all|set_reminder|check|book|modify|cancel|replace)\b",
        lambda m: {
            "list": "회의 일정 조회",
            "list_mine": "회의 일정 조회",
            "check_all": "전체 회의실 조회",
            "set_reminder": "리마인더 설정",
            "check": "회의실 조회",
            "book": "예약",
            "modify": "예약 변경",
            "cancel": "예약 취소",
            "replace": "재예약",
        }.get(m.group(1).lower(), m.group(1)),
        text,
        flags=re.IGNORECASE,
    )
    # 내부 저장·동기화 상태 표현 제거
    text = re.sub(r",?\s*DB 미등록", "", text)
    text = re.sub(
        r"\(DB[^)]*?\)",
        "(챗봇에서 예약 변경·취소·리마인더를 할 수 없습니다.)",
        text,
    )
    text = re.sub(
        r"Outlook에만[^.\n]*(?:DB|동기화)[^.\n]*\.?",
        "",
        text,
    )
    text = re.sub(r"동기화 후[^.\n]*\.?", "", text)
    return text
