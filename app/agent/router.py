# =============================================================================
# ConnBot — Agentic Tool Calling Unified Flow
# =============================================================================
"""
사내 AI Agent Router.

Flow:
1. LLM이 일반 답변 또는 tool 사용 여부를 결정
2. tool call이 있으면 tool 실행
3. tool 결과를 바탕으로 최종 답변 생성

현재 tool:
- search_company_wiki: 사내 문서/RAG 검색
- query_gov_projects: 정부과제·지원사업 브리핑 조회
- search_worker_schedule: Flex 근태 조회 — 당일 타임라인·과거 일별·월간(YYYY-MM)
- manage_room_schedule: Outlook Graph API 회의실 예약·조회·취소

확장 예정:
- MCP/API 기반 회의실 예약, 일정(휴가, 재택 등) 등록, 뉴스 요약, 슬랙 내부 검색, 점심 메뉴 추천, 비품 신청(운영팀 자동 슬랙 전송 - 누구에게 요청해야 되는지 헷갈려함), Invoice(pdf, image) 받아 onedrive 폴더에 종합 등
"""

from __future__ import annotations

import copy
import os
import re
import json
import logging
import asyncio
import time
import contextvars
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from openai import AsyncOpenAI
from app.agent.openai_responses_util import (
    responses_create_turn1,
    responses_create_text,
    responses_stream_text,
    usage_to_dict,
)

if TYPE_CHECKING:
    from app.slack.streaming import SlackStreamUpdater
from app.rag.vectordb import (
    hybrid_search,
    _build_context,
    collapse_hits_by_page,
)
from app.slack.ui import strip_body_citations
from app.core import rag_debug_logger  # noqa: F401
from app.core.config import (
    LLM_MODEL_NAME,
    RAG_ANSWER_MODEL_NAME,
    ROUTER_MODEL_NAME,
    RECENT_MESSAGES_LIMIT,
    RECENT_MESSAGES_LIMIT_GENERAL,
    TURN1_RECENT_MESSAGES_LIMIT,
    CONTEXT_USER_MAX_CHARS,
    CONTEXT_ASSISTANT_MAX_CHARS,
    get_router_instruction,
    get_router_flex_final_instruction,
    get_router_general_final_instruction,
    get_router_gov_final_instruction,
    get_router_rag_final_instruction,
    get_rag_planner_final_instructions,
    get_rag_evidence_planner_text_format,
    get_router_room_final_instruction,
    get_router_expense_final_instruction,
    RAG_MAX_PAGES,
    RAG_MAX_PARENTS,
    RAG_RESEARCH_ENABLED,
    RAG_EVIDENCE_PLANNER_MODEL_NAME,
    RAG_EVIDENCE_PLANNER_MAX_TOKENS,
    expense_archive_folder_codes,
    format_expense_archive_folder_guide,
    format_turn1_reroute_review,
    get_turn1_attachment_policy_instruction,
)
from app.services.gov_project.gov_project import (
    build_gov_briefing_catalog_block,
    match_gov_in_query,
    query_gov_projects,
)
from app.services.flex_hr.flex_hr import (
    build_flex_schedule_router_block,
    normalize_flex_schedule_tool_args,
    search_workers_schedule,
)
from app.services.outlook_room.schedule_reserve import (
    book_room,
    build_active_room_context_block,
    cancel_room,
    check_all_rooms,
    check_room_schedule,
    default_book_subject,
    enrich_book_tool_args,
    is_room_write_action,
    is_valid_booking_uuid,
    list_bookings,
    list_rooms,
    modify_room,
    normalize_room_tool_args,
    peek_manage_room_action,
    prepare_booking_target,
    replace_room,
    room_write_once_message,
    resolve_booking_room,
    set_room_reminder,
    room_name_maps_to_managed,
)
from app.services.outlook_room.schedule_reserve import (
    default_end_time_one_hour as room_default_end_time,
)
from app.services.tool_policy import (
    missing_fields_message,
    sanitize_user_facing_tool_message,
)
from app.services.rag_research import (
    format_planner_search_context,
    log_rag_research,
    materialize_evidence_from_planner,
    merge_wiki_search_docs,
    merged_doc_ids,
    normalize_evidence_planner_plan,
    parse_evidence_planner_output,
    planner_to_followup_plan,
    strip_insufficient_answer_signal,
)
from app.services.turn1_reroute import (
    build_room_intent_hint_block,
    build_wiki_intent_hint_block,
    describe_first_pass,
    detect_domain_signals,
    enrich_wiki_queries_with_year,
    has_lookup_tool,
    needs_turn1_reroute,
    query_should_default_to_wiki,
    tool_call_name,
)
from app.services.slack_attachments import (
    AttachmentContext,
    AttachmentPolicy,
    Turn2UserContent,
    UserAttachmentBundle,
    build_turn1_user_content,
    build_turn2_user_message_content,
    filter_business_tool_calls,
    resolve_attachment_context,
    resolve_attachment_policy,
)
from app.services.expense.onedrive_expense import archive_attachments_to_onedrive

logger = logging.getLogger(__name__)

_openai_client: AsyncOpenAI | None = None

def _get_async_openai() -> AsyncOpenAI:
    """공유 AsyncOpenAI 클라이언트 (기본 httpx 사용)."""
    global _openai_client
    if _openai_client is None:
        _openai_client = AsyncOpenAI(
            api_key=os.getenv("OPENAI_API_KEY", "")
        )
    return _openai_client


async def close_async_openai() -> None:
    """앱 shutdown 시 HTTP 클라이언트 정리."""
    global _openai_client
    if _openai_client is not None:
        await _openai_client.close()
        _openai_client = None

# =============================================================================
# Config
# =============================================================================
# 실제 tool 실행 횟수 제한
MAX_TOOL_CALLS = 3

# =============================================================================
# Tool Schema
# =============================================================================

_QUERY_GOV_PROJECTS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "query_gov_projects",
        "description": (
            "일일 외부 정부·지원사업 브리핑 조회. "
            "사내 정부과제 운영·정산·연구노트는 search_company_wiki."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "detail", "files"],
                    "description": (
                        "list=브리핑 목록, detail=상세요약 전문, "
                        "files=신청양식·공고문·첨부 다운로드 URL (파일/양식/서류 질문 시 필수)"
                    ),
                },
                "idx": {
                    "type": "integer",
                    "description": (
                        "list 결과 또는 대화에 나온 [idx] 정수. "
                        "브리핑 카드 순번(1., 2.)과 다름"
                    ),
                },
                "keyword": {
                    "type": "string",
                    "description": "공고명 (idx 없을 때)",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
}

_SEARCH_WORKER_SCHEDULE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "search_worker_schedule",
        "description": (
            "Flex 근무·재택·휴가·외근·출장 조회. "
            "회의·미팅·Outlook 회의실 일정 조회에는 사용하지 않음 → manage_room_schedule list. "
            "개인: worker_name(전체 또는 성 제외 이름). "
            "질문에 여러 이름이 있으면 코드가 모두 조회. "
            "팀: team(예: Operation, RA, SW) 또는 질문의 '운영팀' 등 — roster 기준 해당 팀 전원 조회. "
            "직무: role_title(예: CEO, CFO) 또는 질문의 '대표님' 등 — 해당 직무자 조회. "
            "당일(오늘·재실·지금)은 date 생략. "
            "과거·특정 일은 date=YYYY-MM-DD. "
            "기간(이번 주·다음 주·N일)은 date+end_date=YYYY-MM-DD (월을 넘어도 자동 병합). "
            "한 달 전체는 year_month=YYYY-MM. "
            "date+end_date가 있으면 year_month 무시."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "worker_name": {
                    "type": "string",
                    "description": "조회할 직원 이름. 팀·직무 조회 시 빈 문자열 가능.",
                },
                "team": {
                    "type": "string",
                    "description": (
                        "roster 팀명 (예: Operation, RA, SW, Sales, 경영, 사업화). "
                        "'운영팀 출근한 사람' 등 팀 단위 질문."
                    ),
                },
                "role_title": {
                    "type": "string",
                    "description": (
                        "직무 키워드 (예: CEO, CFO, CTO, CPO, 이사). "
                        "'대표님 출근하셨나' 등 임원·직무 질문."
                    ),
                },
                "date": {
                    "type": "string",
                    "description": (
                        "기간 시작일(YYYY-MM-DD). 특정 일·주간·다음 주 조회 시 시작일. "
                        "end_date와 함께 쓰면 월 경계(예: 6/29~7/3)도 한 번에 조회."
                    ),
                },
                "end_date": {
                    "type": "string",
                    "description": (
                        "기간 종료일(YYYY-MM-DD). date와 함께 사용. "
                        "'다음 주'·'이번 주'처럼 여러 날·월을 넘는 조회에 필수."
                    ),
                },
                "year_month": {
                    "type": "string",
                    "description": (
                        "월간 일별 조회(YYYY-MM). 한 달 전체. "
                        "date+end_date가 있으면 생략."
                    ),
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
}

_MANAGE_ROOM_SCHEDULE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "manage_room_schedule",
        "description": (
            "Outlook 회의·회의실 예약·조회·취소·변경. "
            "list: 회의 일정 조회(person_name 생략=본인, 타인은 person_name). "
            "하루=date만, 기간=date+end_date 또는 생략(7일). "
            "'OO님 회의'·'이번 주 소연님 미팅' → list(person_name, date, end_date). "
            "과거·참석자·누구랑 → list(date=해당일), check 금지. "
            "room_name·start_time·end_time이 모두 있으면 book 즉시 (제목 생략 시 '회의'). "
            "cancel/modify/replace/set_reminder: Turn1은 room/date/subject 등 식별 힌트 전달. "
            "tool 실행 시 Outlook→DB 동기화 후 booking_id 확정·action 수행. "
            "회의실명 없이 날짜·시간만 예약·가용 조회일 때만 check_all. "
            f"회의실: {list_rooms()}. "
            "book·cancel 등 쓰기는 요청자 Slack 이메일이 주최자. "
            "room_name은 사용자 표현(오타·별칭 포함)을 아래 힌트 중 하나로 LLM이 정규화해 전달."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "check", "check_all", "list", "book",
                        "cancel", "modify", "replace", "set_reminder",
                    ],
                    "description": (
                        "check=특정 회의실 가용·점유(현재·미래), check_all=전체 회의실 조회·슬롯 가용, "
                        "list=회의 일정(person_name 생략=본인, 하루=date, 기간=date+end_date|생략; 과거·참석 포함), "
                        "book=예약 (room_name·start_time·end_time 있으면 즉시; subject 생략 시 '회의'; 회의실명 없으면 check_all 먼저), "
                        "cancel=본인 예약 취소 (room/date/subject 힌트 → DB booking_id 확정), "
                        "modify=본인 예약 변경 (힌트로 대상 확정 + 변경 필드), "
                        "replace=취소 후 재예약 (힌트로 대상 확정 + new_start/end_time), "
                        "set_reminder=Slack 리마인더 (힌트로 대상 확정 + reminder_minutes)"
                    ),
                },
                "room_name": {
                    "type": "string",
                    "description": "회의실 이름. check·book·modify에서 사용.",
                },
                "date": {
                    "type": "string",
                    "description": "list·check용. 하루=date만, 기간=list는 date+end_date 또는 생략.",
                },
                "end_date": {
                    "type": "string",
                    "description": "list 기간 종료일(ISO). 하루·check·book은 생략.",
                },
                "person_name": {
                    "type": "string",
                    "description": (
                        "list 선택. 조회 대상 직원 이름(전체 또는 성 제외, 예: '소연'). "
                        "생략 시 요청자 본인. 'OO님 회의'·'이번 주 OO 미팅' 시 지정."
                    ),
                },
                "booking_id": {
                    "type": "string",
                    "description": (
                        "cancel/modify/replace/set_reminder 선택. "
                        "대화 맥락에서 UUID를 알면 전달. "
                        "없으면 room_name·date·subject·start_time 힌트로 "
                        "tool 실행 단계에서 DB 조회·확정. auto_detect 등 가짜 ID 금지."
                    ),
                },
                "subject": {
                    "type": "string",
                    "description": (
                        "book 선택: 회의 제목. 생략·모호('그냥 회의' 등) 시 '회의'로 예약. "
                        "cancel/modify/replace/set_reminder 시 맥락 특정 보조용."
                    ),
                },
                "start_time": {
                    "type": "string",
                    "description": "예약 시작 (book 필수). ISO 2026-06-18T14:00:00",
                },
                "end_time": {
                    "type": "string",
                    "description": "예약 종료 (book 필수). ISO 2026-06-18T15:00:00",
                },
                "old_start_time": {
                    "type": "string",
                    "description": "modify 시 기존 시작 시각 (예약 특정·검증).",
                },
                "old_end_time": {
                    "type": "string",
                    "description": "modify 시 기존 종료 시각.",
                },
                "new_start_time": {
                    "type": "string",
                    "description": "modify 시 신규 시작 시각. 시간 변경 시 new_end_time과 함께 사용.",
                },
                "new_end_time": {
                    "type": "string",
                    "description": "modify 시 신규 종료 시각, replace 시 필수. 시간 변경 시 new_start_time과 함께 사용.",
                },
                "new_room_name": {
                    "type": "string",
                    "description": "replace 시 재예약 회의실. 생략 시 기존 room_name.",
                },
                "new_subject": {
                    "type": "string",
                    "description": "modify 시 변경할 제목. 생략 시 기존 유지.",
                },
                "attendees": {
                    "type": "array",
                    "description": (
                        "book 선택: 추가 참석자. modify 선택: 요청한 참석자 목록으로 교체. "
                        "email 또는 name·slack_user_id. 주최자(요청자)는 자동 제외."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "email": {"type": "string"},
                            "name": {"type": "string"},
                            "slack_user_id": {"type": "string"},
                        },
                    },
                },
                "reminder_minutes": {
                    "type": "integer",
                    "description": "set_reminder 필수. 회의 시작 N분 전 Slack 알림 (예: 15).",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
}

_ARCHIVE_EXPENSE_ATTACHMENT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "archive_expense_attachment",
        "description": (
            "경비·정산 증빙 Slack 첨부를 OneDrive 분류 폴더에 업로드. "
            "영수증·인보이스·카드전표·교통비·식비·정산 엑셀 등 첨부가 있을 때. "
            "첨부 내용(vision summary·문서)을 보고 category 선택. "
            "확신 없으면 00_ReviewNeeded.\n"
            + format_expense_archive_folder_guide()
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": expense_archive_folder_codes(),
                    "description": "OneDrive 하위 폴더 코드",
                },
                "include_attachment_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "업로드할 att_* id. 생략 시 이번 턴 첨부 전체.",
                },
                "reason": {
                    "type": "string",
                    "description": "분류 근거 (한 줄)",
                },
            },
            "required": ["category", "reason"],
            "additionalProperties": False,
        },
    },
}

_RESPOND_GENERAL_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "respond_general",
        "description": (
            "인사·스몰토크·봇 정체성/기능 확인·범용 상식·사용자가 붙인 텍스트 단순 가공. "
            "사내 위키·회의실·근태·정부과제 조회가 필요하면 이 도구가 아님."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": [
                        "greeting",
                        "smalltalk",
                        "capability",
                        "general_knowledge",
                        "text_processing",
                    ],
                    "description": "질문 유형 분류 (Turn2 답변 톤 참고용).",
                },
            },
            "required": ["category"],
            "additionalProperties": False,
        },
    },
}


def get_agent_tools() -> list[dict[str, Any]]:
    """Agent Turn1 tool schema — business 도구만 노출 (첨부는 content attachment_policy)."""
    return [
        {
            "type": "function",
            "function": {
                "name": "search_company_wiki",
                "description": (
                    "사내 Confluence 위키 문서로 확인해야 하는 내부 규정·절차·정책·시스템 사용법 질문에 사용. \n"
                    "예: 비용·결제·정산·법인카드·출장·근무·휴가·복지·총무·IT·보안·자산 관리. \n"
                    "예: 정부과제 사내 운영·정산·전자연구노트·양식·신청서·담당자·등록 위치·마감일 확인. \n"
                    "예: 취업규칙·인사규정·항목별 비교·문서 인용 질문. \n"
                    "단, 실시간 근태·회의실/Outlook·외부 공고·첨부 단순 분류는 각 전용 도구를 사용. \n"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "direct_query": {
                            "type": "string",
                            "description": (
                                "질문·행위에 가까운 검색어. 한국어·명사 중심·8자 이상·"
                                "대화체·질문형 제거. 절차·신청·시스템·대상·기한 등 구체 키워드. "
                                "재검색 시 1차 답변의 누락 항목을 반영해 원질문에 추가로 자세한 검색어를 작성한다."
                            ),
                        },
                        "policy_query": {
                            "type": "string",
                            "description": (
                                "상위 정책·규정·제도·카테고리 검색어. "
                                "동의어·상위 개념·관련 제도명 포함. "
                                "재검색 시 1차 답변의 누락 항목에 맞는 등급·부록·세부 기준·표 같이 문서용 검색어를 작성한다."
                            ),
                        },
                    },
                    "required": ["direct_query", "policy_query"],
                    "additionalProperties": False,
                },
            },
        },
        _QUERY_GOV_PROJECTS_TOOL,
        _SEARCH_WORKER_SCHEDULE_TOOL,
        _MANAGE_ROOM_SCHEDULE_TOOL,
        _ARCHIVE_EXPENSE_ATTACHMENT_TOOL,
        _RESPOND_GENERAL_TOOL,
    ]


@dataclass(frozen=True)
class _RecoveredFunction:
    name: str
    arguments: str


@dataclass(frozen=True)
class _RecoveredToolCall:
    id: str
    type: str
    function: _RecoveredFunction


def _make_recovered_tool_call(tool_name: str, args: dict[str, Any]) -> _RecoveredToolCall:
    return _RecoveredToolCall(
        id=f"recovered_{tool_name}_{int(time.time() * 1000)}",
        type="function",
        function=_RecoveredFunction(
            name=tool_name,
            arguments=json.dumps(args, ensure_ascii=False),
        ),
    )


def sanitize_turn1_assistant_content(
    content: str | None,
    *,
    has_attachments: bool,
    has_tool_calls: bool,
) -> str | None:
    """첨부 없이 tool_call만 있을 때 assistant content 강제 제거."""
    if has_attachments or not has_tool_calls:
        return content
    if (content or "").strip():
        return None
    return content


def recover_tool_calls_from_content(
    content: str | None,
    *,
    allowed_tool_names: set[str],
) -> list[_RecoveredToolCall]:
    """Turn1 LLM이 tool_call 대신 content에 JSON 인자만 쓴 경우 복구."""
    text = (content or "").strip()
    if not text.startswith("{"):
        return []

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []

    recovered: list[_RecoveredToolCall] = []

    if (
        "search_worker_schedule" in allowed_tool_names
        and data.get("worker_name")
    ):
        recovered.append(
            _make_recovered_tool_call(
                "search_worker_schedule",
                {"worker_name": str(data["worker_name"]).strip()},
            )
        )

    if "query_gov_projects" in allowed_tool_names and data.get("action"):
        gov_args: dict[str, Any] = {"action": str(data["action"]).strip()}
        if data.get("idx") is not None:
            gov_args["idx"] = int(data["idx"])
        if data.get("keyword"):
            gov_args["keyword"] = str(data["keyword"]).strip()
        if data.get("file_category"):
            gov_args["file_category"] = str(data["file_category"]).strip()
        recovered.append(_make_recovered_tool_call("query_gov_projects", gov_args))

    wiki_keys = ("direct_query", "policy_query", "search_query")
    if "search_company_wiki" in allowed_tool_names and any(k in data for k in wiki_keys):
        wiki_args = {
            k: str(data[k]).strip()
            for k in wiki_keys
            if data.get(k)
        }
        if wiki_args:
            recovered.append(_make_recovered_tool_call("search_company_wiki", wiki_args))

    return recovered


def normalize_turn1_tool_calls(
    message: Any,
    *,
    allowed_tool_names: set[str],
) -> list[_RecoveredToolCall]:
    """Turn1 1·2차 공통 — tool_calls + content JSON 복구 (강제 fallback 없음)."""
    effective: list[Any] = list(message.tool_calls or [])
    if effective:
        return effective
    return recover_tool_calls_from_content(
        message.content,
        allowed_tool_names=allowed_tool_names,
    )


def apply_turn1_tool_fallback(
    tool_calls: list[Any],
    *,
    query_stripped: str,
    general_content: str = "",
) -> list[Any]:
    """lookup tool 없을 때 wiki JSON 복구, wiki 후보 질문이면 search_company_wiki, 아니면 respond_general."""
    if tool_calls:
        return tool_calls
    if general_should_reroute_to_wiki(general_content):
        return [
            build_wiki_reroute_tool_call(
                query_stripped,
                general_content=general_content,
            ),
        ]
    if query_should_default_to_wiki(query_stripped):
        return [
            build_wiki_reroute_tool_call(
                query_stripped,
                general_content=general_content,
            ),
        ]
    return [
        _make_recovered_tool_call(
            "respond_general",
            {"category": "general_knowledge"},
        ),
    ]


_WIKI_TOOL = "search_company_wiki"


def general_should_reroute_to_wiki(content: str) -> bool:
    """
    Turn1이 general로 끝났지만 wiki tool_call JSON만 content에 쓴 경우.

    '위키 조회해 드릴게요' 등 자연어 예고는 general로 두고 재라우팅하지 않는다.
    """
    return bool(
        recover_tool_calls_from_content(
            content,
            allowed_tool_names={_WIKI_TOOL},
        )
    )


def build_wiki_reroute_tool_call(
    query_stripped: str,
    *,
    general_content: str = "",
) -> _RecoveredToolCall:
    """general → wiki 재라우팅용 tool_call."""
    recovered = recover_tool_calls_from_content(
        general_content,
        allowed_tool_names={_WIKI_TOOL},
    )
    if recovered:
        return recovered[0]

    queries = extract_wiki_search_queries_from_tool_args(
        {},
        query_stripped=query_stripped,
    )
    return _make_recovered_tool_call(
        _WIKI_TOOL,
        {
            "direct_query": queries.direct_query,
            "policy_query": queries.policy_query,
        },
    )


# =============================================================================
# Prompt Builders
# =============================================================================

def build_user_turn_content(
    query_stripped: str,
    *,
    memory_context: str = "",
    prefix_blocks: list[str] | None = None,
    suffix_blocks: list[str] | None = None,
) -> str:
    """
    Turn1/Turn2 공통 user 메시지 본문.
    순서: user_memory → prefix(대화 맥락 등) → user_question → suffix(검색·gov 결과 등).
    """
    parts: list[str] = []
    mem = (memory_context or "").strip()
    if mem:
        parts.append(mem)
    for block in prefix_blocks or []:
        b = (block or "").strip()
        if b:
            parts.append(b)
    parts.append(f"<user_question>\n{query_stripped}\n</user_question>")
    for block in suffix_blocks or []:
        b = (block or "").strip()
        if b:
            parts.append(b)
    return "\n\n".join(parts)


def build_initial_messages(
    query_stripped: str,
    conversation_history: list[dict[str, str]] | None = None,
    attachment_bundle: UserAttachmentBundle | None = None,
) -> list[dict[str, Any]]:
    """
    1차 OpenAI 호출용 messages.
    compact session summary + 최근 N턴만 주입 (전체 history append 금지).
    """
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": get_router_instruction(),
        },
    ]

    history = list(conversation_history or [])
    if (
        history
        and history[-1].get("role") == "user"
        and (history[-1].get("content") or "").strip() == query_stripped
    ):
        history = history[:-1]

    compact_blocks: list[str] = []
    turn_messages: list[dict[str, str]] = []
    for msg in history:
        role = msg.get("role")
        content = str(msg.get("content") or "").strip()
        if role == "system" and content:
            compact_blocks.append(content)
        elif role in ("user", "assistant"):
            if role == "assistant" and content in ("", "None"):
                continue
            turn_messages.append({"role": role, "content": content})

    for block in compact_blocks:
        messages.append({"role": "system", "content": block})

    room_ctx = build_active_room_context_block(turn_messages or history)
    if room_ctx:
        messages.append({"role": "system", "content": room_ctx})

    recent_ctx = build_conversation_context_snippet(
        turn_messages,
        current_query=query_stripped,
        max_messages=TURN1_RECENT_MESSAGES_LIMIT,
        for_turn1=True,
    )
    if recent_ctx:
        messages.append({"role": "system", "content": recent_ctx})

    flex_hint = build_flex_schedule_router_block(query_stripped)
    if flex_hint:
        messages.append({"role": "system", "content": flex_hint})

    if match_gov_in_query(query_stripped):
        gov_catalog = build_gov_briefing_catalog_block()
        if gov_catalog:
            messages.append({"role": "system", "content": gov_catalog})

    room_hint = build_room_intent_hint_block(query_stripped)
    if room_hint:
        messages.append({"role": "system", "content": room_hint})

    wiki_hint = build_wiki_intent_hint_block(query_stripped)
    if wiki_hint:
        messages.append({"role": "system", "content": wiki_hint})

    if attachment_bundle and attachment_bundle.has_content:
        messages.append({
            "role": "system",
            "content": get_turn1_attachment_policy_instruction(),
        })
        messages.append({
            "role": "system",
            "content": format_expense_archive_folder_guide(),
        })

    messages.append({
        "role": "user",
        "content": build_turn1_user_content(
            query_stripped,
            attachment_bundle,
        ),
    })

    return messages


def build_conversation_context_snippet(
    conversation_history: list[dict[str, str]] | None,
    *,
    current_query: str = "",
    max_messages: int | None = None,
    for_turn1: bool = False,
) -> str:
    """Turn1/Turn2 — 현재 질문 제외, 이전 user/assistant 메시지 스니펫."""
    if not conversation_history:
        return ""

    history = list(conversation_history)
    q = (current_query or "").strip()
    if (
        history
        and history[-1].get("role") == "user"
        and (history[-1].get("content") or "").strip() == q
    ):
        history = history[:-1]

    turns: list[str] = []
    for msg in history:
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        content = str(msg.get("content") or "").strip()
        if not content or content == "None":
            continue
        label = "사용자" if role == "user" else "어시스턴트"
        max_chars = (
            CONTEXT_USER_MAX_CHARS if role == "user" else CONTEXT_ASSISTANT_MAX_CHARS
        )
        turns.append(f"{label}: {content[:max_chars]}")
    if not turns:
        return ""
    cap = max_messages if max_messages is not None else RECENT_MESSAGES_LIMIT
    recent = turns[-cap:]
    header = (
        "직전 대화 원문 (Turn1: 도메인·action 연속성 판단용):\n"
        if for_turn1
        else "직전 대화:\n"
    )
    return "<conversation_context>\n" + header + "\n".join(recent) + "\n</conversation_context>"

@dataclass(frozen=True)
class WikiSearchQueries:
    """search_company_wiki — direct(BM25/semantic) + policy(BM25/semantic) 이중 검색."""
    direct_query: str
    policy_query: str


def _clip_query(text: str, *, max_len: int = 400) -> str:
    return (text or "").strip()[:max_len]


def extract_wiki_search_queries_from_tool_args(
    arguments: dict[str, Any],
    *,
    query_stripped: str,
    conversation_history: list[dict[str, str]] | None = None,
    memory_context: str = "",
) -> WikiSearchQueries:
    """Turn1 tool arguments → direct_query + policy_query."""
    _ = conversation_history, memory_context

    direct = _clip_query(str(arguments.get("direct_query") or ""))
    policy = _clip_query(str(arguments.get("policy_query") or ""))

    legacy = _clip_query(str(arguments.get("search_query") or ""))
    if legacy:
        if not direct:
            direct = legacy
        if not policy:
            policy = legacy

    if not direct and not policy:
        fallback = _clip_query(query_stripped)
        direct = fallback
        policy = fallback
    elif not direct:
        direct = policy
    elif not policy:
        policy = direct

    direct, policy = enrich_wiki_queries_with_year(
        direct,
        policy,
        user_query=query_stripped,
    )

    return WikiSearchQueries(direct_query=direct, policy_query=policy)


def _answer_user_message(turn2: Turn2UserContent) -> dict[str, Any]:
    """API용 multimodal + 디버그용 prompt_text 분리."""
    return {
        "role": "user",
        "content": turn2.to_api_content(),
        "prompt_text": turn2.text,
    }


def build_rag_answer_messages(
    *,
    query_stripped: str,
    search_context: str,
    has_docs: bool,
    memory_context: str = "",
    conversation_context: str = "",
    flex_context: str = "",
    gov_context: str = "",
    attachment_policy: AttachmentPolicy | None = None,
    attachment_context: AttachmentContext | None = None,
) -> list[dict[str, Any]]:
    """Turn3 RAG 최종 답변 messages."""
    suffix_blocks: list[str] = []
    if has_docs:
        suffix_blocks.append(f"<search_results>\n{search_context}\n</search_results>")
    flex_text = (flex_context or "").strip()
    if flex_text:
        suffix_blocks.append(f"<worker_schedule_results>\n{flex_text}\n</worker_schedule_results>")
    gov_text = (gov_context or "").strip()
    if gov_text:
        suffix_blocks.append(f"<gov_results>\n{gov_text}\n</gov_results>")

    turn2 = build_turn2_user_message_content(
        query_stripped,
        memory_context=memory_context,
        prefix_blocks=[conversation_context] if conversation_context else None,
        suffix_blocks=suffix_blocks or None,
        attachment_policy=attachment_policy,
        attachment_context=attachment_context,
    )

    return [
        {
            "role": "system",
            "content": get_router_rag_final_instruction(
                query=query_stripped,
                has_docs=has_docs,
                has_flex=bool(flex_text),
                has_gov=bool(gov_text),
            ),
        },
        _answer_user_message(turn2),
    ]


def build_rag_evidence_planner_messages(
    *,
    query_stripped: str,
    planner_context: str,
    memory_context: str = "",
    conversation_context: str = "",
) -> list[dict[str, Any]]:
    """Turn2 evidence planner — JSON만, parent 메타 snippet."""
    suffix_blocks = [
        f"<search_snippets>\n{planner_context}\n</search_snippets>",
    ]
    turn2 = build_turn2_user_message_content(
        query_stripped,
        memory_context=memory_context,
        prefix_blocks=[conversation_context] if conversation_context else None,
        suffix_blocks=suffix_blocks,
    )
    return [
        {"role": "system", "content": get_rag_planner_final_instructions()},
        _answer_user_message(turn2),
    ]


def build_flex_answer_messages(
    *,
    query_stripped: str,
    flex_context: str,
    has_context: bool,
    memory_context: str = "",
    conversation_context: str = "",
    attachment_policy: AttachmentPolicy | None = None,
    attachment_context: AttachmentContext | None = None,
) -> list[dict[str, Any]]:
    """Flex 근태 tool 결과 기반 Turn2 messages."""
    suffix_blocks: list[str] = []
    if has_context:
        suffix_blocks.append(
            f"<worker_schedule_results>\n{flex_context}\n</worker_schedule_results>"
        )

    turn2 = build_turn2_user_message_content(
        query_stripped,
        memory_context=memory_context,
        prefix_blocks=[conversation_context] if conversation_context else None,
        suffix_blocks=suffix_blocks or None,
        attachment_policy=attachment_policy,
        attachment_context=attachment_context,
    )

    return [
        {
            "role": "system",
            "content": get_router_flex_final_instruction(
                has_context=has_context,
                query=query_stripped,
            ),
        },
        _answer_user_message(turn2),
    ]


def build_gov_answer_messages(
    *,
    query_stripped: str,
    gov_context: str,
    has_context: bool,
    memory_context: str = "",
    conversation_context: str = "",
    attachment_policy: AttachmentPolicy | None = None,
    attachment_context: AttachmentContext | None = None,
) -> list[dict[str, Any]]:
    """정부과제 tool 결과 기반 Turn2 messages."""
    suffix_blocks: list[str] = []
    if has_context:
        suffix_blocks.append(f"<gov_results>\n{gov_context}\n</gov_results>")

    turn2 = build_turn2_user_message_content(
        query_stripped,
        memory_context=memory_context,
        prefix_blocks=[conversation_context] if conversation_context else None,
        suffix_blocks=suffix_blocks or None,
        attachment_policy=attachment_policy,
        attachment_context=attachment_context,
    )

    return [
        {
            "role": "system",
            "content": get_router_gov_final_instruction(
                has_context=has_context,
                query=query_stripped,
            ),
        },
        _answer_user_message(turn2),
    ]


def build_general_answer_messages(
    *,
    query_stripped: str,
    memory_context: str = "",
    conversation_context: str = "",
    attachment_policy: AttachmentPolicy | None = None,
    attachment_context: AttachmentContext | None = None,
) -> list[dict[str, Any]]:
    """respond_general tool 선택 후 Turn2 messages."""
    turn2 = build_turn2_user_message_content(
        query_stripped,
        memory_context=memory_context,
        prefix_blocks=[conversation_context] if conversation_context else None,
        attachment_policy=attachment_policy,
        attachment_context=attachment_context,
    )
    return [
        {
            "role": "system",
            "content": get_router_general_final_instruction(
                query=query_stripped,
            ),
        },
        _answer_user_message(turn2),
    ]


def build_expense_answer_messages(
    *,
    query_stripped: str,
    expense_context: str,
    has_context: bool,
    memory_context: str = "",
    conversation_context: str = "",
    attachment_policy: AttachmentPolicy | None = None,
    attachment_context: AttachmentContext | None = None,
) -> list[dict[str, Any]]:
    """경비 증빙 OneDrive 업로드 tool 결과 기반 Turn2 messages."""
    suffix_blocks: list[str] = []
    if has_context:
        suffix_blocks.append(
            f"<expense_archive_results>\n{expense_context}\n</expense_archive_results>"
        )

    turn2 = build_turn2_user_message_content(
        query_stripped,
        memory_context=memory_context,
        prefix_blocks=[conversation_context] if conversation_context else None,
        suffix_blocks=suffix_blocks or None,
        attachment_policy=attachment_policy,
        attachment_context=attachment_context,
    )

    return [
        {
            "role": "system",
            "content": get_router_expense_final_instruction(
                has_context=has_context,
                query=query_stripped,
            ),
        },
        _answer_user_message(turn2),
    ]


def build_room_answer_messages(
    *,
    query_stripped: str,
    room_context: str,
    has_context: bool,
    memory_context: str = "",
    conversation_context: str = "",
    attachment_policy: AttachmentPolicy | None = None,
    attachment_context: AttachmentContext | None = None,
) -> list[dict[str, Any]]:
    """회의실 예약 tool 결과 기반 Turn2 messages."""
    suffix_blocks: list[str] = []
    if has_context:
        suffix_blocks.append(
            f"<room_schedule_results>\n{room_context}\n</room_schedule_results>"
        )

    turn2 = build_turn2_user_message_content(
        query_stripped,
        memory_context=memory_context,
        prefix_blocks=[conversation_context] if conversation_context else None,
        suffix_blocks=suffix_blocks or None,
        attachment_policy=attachment_policy,
        attachment_context=attachment_context,
    )

    return [
        {
            "role": "system",
            "content": get_router_room_final_instruction(
                has_context=has_context,
                query=query_stripped,
            ),
        },
        _answer_user_message(turn2),
    ]


_SOURCES_USED_PATTERN = re.compile(
    r"<sources_used>\s*(.*?)\s*</sources_used>",
    re.IGNORECASE | re.DOTALL,
)
_LINKS_USED_PATTERN = re.compile(
    r"<links_used>\s*(.*?)\s*</links_used>",
    re.IGNORECASE | re.DOTALL,
)
_ATTACHMENTS_USED_PATTERN = re.compile(
    r"<attachments_used>\s*(.*?)\s*</attachments_used>",
    re.IGNORECASE | re.DOTALL,
)
_ANSWER_PATTERN = re.compile(
    r"<answer>\s*(.*?)\s*</answer>",
    re.IGNORECASE | re.DOTALL,
)


def _extract_answer_bodies(text: str) -> str:
    """<answer> 블록이 여러 개면 본문을 병합한다."""
    matches = _ANSWER_PATTERN.findall(text or "")
    if not matches:
        return ""
    parts = [m.strip() for m in matches if m.strip()]
    return "\n\n".join(parts)

_EMPTY_TAGS = frozenset({"none", "없음", "-", "n/a", "null", ""})


def _parse_int_list(raw: str) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for n in re.findall(r"\d+", raw or ""):
        no = int(n)
        if no in seen:
            continue
        seen.add(no)
        out.append(no)
    return out


def parse_links_used_raw(raw: str) -> dict[int, list[int]]:
    """
    links_used 본문 파싱.

    - '1:2,4;3:1' → {1: [2,4], 3: [1]}
    - '2,4' (콜론 없음) → {0: [2,4]}  (단일 sources_used 문서에 매핑)
    """
    body = (raw or "").strip().lower()
    if body in _EMPTY_TAGS:
        return {}

    if ":" in body:
        out: dict[int, list[int]] = {}
        for segment in re.split(r"[;\n]+", body):
            segment = segment.strip()
            if not segment:
                continue
            match = re.match(r"(\d+)\s*:\s*(.+)", segment)
            if not match:
                continue
            doc_no = int(match.group(1))
            links = _parse_int_list(match.group(2))
            if links:
                out[doc_no] = links
        return out

    links = _parse_int_list(body)
    return {0: links} if links else {}


def resolve_links_used_by_doc(
    links_raw: dict[int, list[int]],
    source_doc_numbers: list[int],
) -> dict[int, list[int]]:
    """search_results 문서 번호 → 해당 문서에서 노출할 링크 번호(링크만, 1-based)."""
    if not links_raw or not source_doc_numbers:
        return {}

    if 0 in links_raw:
        indices = links_raw[0]
        if len(source_doc_numbers) == 1:
            return {source_doc_numbers[0]: indices}
        return {doc_no: list(indices) for doc_no in source_doc_numbers}

    resolved: dict[int, list[int]] = {}
    cited = set(source_doc_numbers)
    for doc_no, indices in links_raw.items():
        if doc_no in cited and indices:
            resolved[doc_no] = indices
    return resolved


def parse_rag_structured_response(
    raw: str,
    *,
    max_docs: int,
) -> tuple[str, list[int], dict[int, list[int]], dict[int, list[int]]]:
    """
    Turn2 XML 응답 파싱.

    Returns:
        (answer 본문, search_results 기준 문서 번호 1-indexed,
         links_used_by_doc, attachments_used_by_doc)
    """
    text = (raw or "").strip()
    if not text:
        return "", [], {}, {}

    source_numbers: list[int] = []
    links_used_by_doc: dict[int, list[int]] = {}
    attachments_used_by_doc: dict[int, list[int]] = {}
    answer = text

    src_match = _SOURCES_USED_PATTERN.search(text)
    if src_match:
        raw_nums = src_match.group(1).strip().lower()
        if raw_nums not in _EMPTY_TAGS:
            for no in _parse_int_list(src_match.group(1)):
                if max_docs and not (1 <= no <= max_docs):
                    continue
                source_numbers.append(no)

    links_match = _LINKS_USED_PATTERN.search(text)
    if links_match:
        raw_links = links_match.group(1).strip().lower()
        if raw_links not in _EMPTY_TAGS:
            links_used_by_doc = resolve_links_used_by_doc(
                parse_links_used_raw(links_match.group(1)),
                source_numbers,
            )

    att_match = _ATTACHMENTS_USED_PATTERN.search(text)
    if att_match:
        raw_atts = att_match.group(1).strip().lower()
        if raw_atts not in _EMPTY_TAGS:
            attachments_used_by_doc = resolve_links_used_by_doc(
                parse_links_used_raw(att_match.group(1)),
                source_numbers,
            )

    ans_match = _ANSWER_PATTERN.search(text)
    merged_answer = _extract_answer_bodies(text)
    if merged_answer:
        answer = merged_answer
    elif ans_match:
        answer = ans_match.group(1).strip()
    elif src_match or links_match or att_match:
        answer = text
        if src_match:
            answer = _SOURCES_USED_PATTERN.sub("", answer)
        if links_match:
            answer = _LINKS_USED_PATTERN.sub("", answer)
        if att_match:
            answer = _ATTACHMENTS_USED_PATTERN.sub("", answer)
        answer = answer.strip()

    answer = re.sub(r"</?sources_used>", "", answer, flags=re.IGNORECASE).strip()
    answer = re.sub(r"</?links_used>", "", answer, flags=re.IGNORECASE).strip()
    answer = re.sub(r"</?attachments_used>", "", answer, flags=re.IGNORECASE).strip()
    answer = re.sub(r"</?answer>", "", answer, flags=re.IGNORECASE).strip()
    answer = strip_body_citations(answer)

    return answer, source_numbers, links_used_by_doc, attachments_used_by_doc


def format_llm_io_full(messages: list[dict[str, Any]]) -> str:
    """09_llm_io용 — prompt_text 우선, multimodal base64 redact."""
    blocks: list[str] = []
    for m in messages:
        role = str(m.get("role") or "?")
        if m.get("prompt_text") is not None:
            content = m.get("prompt_text")
        else:
            content = m.get("content")
            if isinstance(content, list):
                redacted: list[Any] = []
                for part in content:
                    if not isinstance(part, dict):
                        redacted.append(part)
                        continue
                    if part.get("type") == "image_url":
                        redacted.append({
                            "type": "image_url",
                            "image_url": {"url": "[vision:image]", "detail": "low"},
                        })
                    else:
                        redacted.append(part)
                content = json.dumps(redacted, ensure_ascii=False, indent=2)
        if content is None and m.get("tool_calls"):
            content = json.dumps(m["tool_calls"], ensure_ascii=False)
        blocks.append(f"### {role}\n{content or ''}")
    return "\n\n".join(blocks)


def format_turn1_llm_output(
    message: Any,
    *,
    effective_tool_calls: list | None = None,
) -> str:
    """Turn1 assistant 응답만 (본문 + tool_calls)."""
    parts: list[str] = []
    if message.content and str(message.content).strip():
        parts.append(str(message.content).strip())
    tool_calls = effective_tool_calls if effective_tool_calls is not None else message.tool_calls
    serialized = serialize_tool_calls(tool_calls)
    if serialized:
        parts.append(json.dumps(serialized, ensure_ascii=False, indent=2))
    return "\n\n".join(parts)


def format_tool_io_output(messages: list[dict[str, Any]]) -> str:
    """role=tool 메시지 본문만."""
    blocks: list[str] = []
    for m in messages:
        if m.get("role") != "tool":
            continue
        name = str(m.get("name") or "tool")
        blocks.append(f"### tool:{name}\n{m.get('content') or ''}")
    return "\n\n".join(blocks)


def _ts_sec() -> str:
    """09_llm_io용 타임스탬프 (초 단위, UTC)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _duration_sec(elapsed: float) -> float:
    return round(elapsed, 2)


def write_llm_io_phase(
    *,
    session_id: str,
    user_input: str,
    phase: str,
    final_input: str,
    output: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """09_llm_io.jsonl — phase별 입·출력만 기록 (turn1 / tool / turn2 / turn3)."""
    output_key = "tool_output" if phase == "tool" else "llm_output"
    row: dict[str, Any] = {
        "ts": _ts_sec(),
        "session_id": session_id,
        "phase": phase,
        "user_input": user_input,
        "final_input": final_input,
        output_key: output,
    }
    if extra:
        row.update(extra)
    rag_debug_logger._write("09_llm_io.jsonl", row)


# =============================================================================
# Policy / debug helpers
# =============================================================================

def summarize_retrieval_for_log(docs: list) -> dict[str, Any]:
    """디버그 로그용 검색 요약 (Turn2 호출 여부는 결정하지 않음)."""
    if not docs:
        return {"doc_count": 0, "top_score": 0.0, "page_titles": []}

    scores = [float(getattr(d, "score", 0.0) or 0.0) for d in docs]
    titles = [
        str((getattr(d, "payload", None) or {}).get("page_title") or "")
        for d in docs
    ]
    return {
        "doc_count": len(docs),
        "top_score": round(max(scores), 4),
        "page_titles": [t for t in titles if t],
    }


# =============================================================================
# OpenAI Call Helpers
# =============================================================================

def _usage_to_dict(usage: Any) -> dict[str, int]:
    return usage_to_dict(usage)


def _parent_ids_from_docs(docs: list) -> list[str]:
    ids: list[str] = []
    for doc in docs or []:
        doc_id = (getattr(doc, "doc_id", None) or "").strip()
        if doc_id:
            ids.append(doc_id)
    return ids


def _detect_turn1_fallback_reason(
    *,
    had_tool_calls: bool,
    effective_tool_calls: list[Any],
    query_stripped: str,
    general_content: str,
    turn1_retry: bool,
) -> str:
    if had_tool_calls:
        return "none"
    if general_should_reroute_to_wiki(general_content):
        return "wiki_json_recovery"
    if query_should_default_to_wiki(query_stripped):
        return "wiki_query_fallback"
    if turn1_retry:
        return "respond_general_after_reroute"
    return "respond_general"


def write_eval_summary_row(
    *,
    session_id: str,
    user_input: str,
    route_label: str,
    intent: str,
    turn1_tool_names: list[str],
    turn1_fallback: str,
    turn1_reroute: bool,
    domain_signals: dict[str, list[str]],
    wiki_tool_used: bool,
    rag_researched: bool,
    research_reason: str,
    missing_facets: list[str],
    docs_count: int,
    parent_ids: list[str],
    sources_used: list[int],
    answer_length: int,
    has_table: bool,
    timings: dict[str, float],
    token_usage: dict[str, Any],
    rag_research_meta: dict[str, Any] | None = None,
) -> None:
    row: dict[str, Any] = {
        "ts": _ts_sec(),
        "session_id": session_id,
        "phase": "eval_summary",
        "user_input": user_input,
        "route_label": route_label,
        "intent": intent,
        "turn1_tools": turn1_tool_names,
        "turn1_fallback": turn1_fallback,
        "turn1_reroute": turn1_reroute,
        "domain_signals": list(domain_signals.keys()),
        "wiki_used": wiki_tool_used,
        "rag_researched": rag_researched,
        "research_reason": research_reason or "",
        "missing_facets": missing_facets or [],
        "docs_count": docs_count,
        "parent_ids": parent_ids,
        "sources_used": sources_used,
        "answer_length": answer_length,
        "has_table": has_table,
        "timings": timings,
        "token_usage": token_usage,
    }
    if rag_research_meta:
        row["rag_research"] = rag_research_meta
    rag_debug_logger._write("09_llm_io.jsonl", row)


def write_rag_research_phase_row(
    *,
    session_id: str,
    user_input: str,
    research_meta: dict[str, Any],
) -> None:
    rag_debug_logger._write(
        "09_llm_io.jsonl",
        {
            "ts": _ts_sec(),
            "session_id": session_id,
            "phase": "rag_research",
            "user_input": user_input,
            **research_meta,
        },
    )


def _sum_token_usage(buckets: list[list[dict[str, int]]]) -> dict[str, int]:
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for bucket in buckets:
        for usage in bucket:
            for key in totals:
                totals[key] += int(usage.get(key) or 0)
    return totals


async def call_llm_for_tool_decision(
    *,
    client: AsyncOpenAI,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    usage_out: list[dict[str, int]] | None = None,
):
    """Turn1: tool 사용 여부 판단 (Responses API)."""
    return await responses_create_turn1(
        client=client,
        model=ROUTER_MODEL_NAME,
        messages=messages,
        tools=tools,
        usage_out=usage_out,
        reasoning={"effort": "lowest"},
    )


async def stream_chat_completion_text(
    *,
    client: AsyncOpenAI,
    model: str,
    messages: list[dict[str, Any]],
    on_delta=None,
    usage_out: list[dict[str, int]] | None = None,
    **create_kwargs: Any,
) -> str:
    """Responses API stream — 전체 텍스트 조립."""
    if "reasoning" not in create_kwargs and "reasoning_effort" not in create_kwargs:
        create_kwargs = {**create_kwargs, "reasoning": {"effort": "lowest"}}
    return await responses_stream_text(
        client=client,
        model=model,
        messages=messages,
        on_delta=on_delta,
        usage_out=usage_out,
        **create_kwargs,
    )


async def _push_text_via_stream(
    stream: "SlackStreamUpdater",
    text: str,
    *,
    chunk_size: int = 12,
) -> None:
    """general — Turn1 비스트림 답변을 Slack에 점진 표시."""
    stream.use_plain_extractor()
    for i in range(0, len(text), chunk_size):
        await stream.feed(text[i : i + chunk_size])
    await stream.flush()


# =============================================================================
# Tool Execution Helpers
# =============================================================================

def serialize_tool_calls(tool_calls) -> list[dict[str, Any]]:
    """
    OpenAI SDK의 tool_call 객체를 messages에 넣을 수 있는 dict로 변환.
    """
    if not tool_calls:
        return []
    return [
        {
            "id": tc.id,
            "type": tc.type,
            "function": {
                "name": tc.function.name,
                "arguments": tc.function.arguments,
            },
        }
        for tc in tool_calls
    ]


def parse_tool_arguments(arguments: str | None) -> dict[str, Any]:
    """
    tool_call.function.arguments JSON 파싱.
    """
    if not arguments:
        return {}

    try:
        parsed = json.loads(arguments)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        logger.warning(f"[Agent] 도구 arguments JSON 파싱 실패: {arguments}")
        return {}


async def _run_hybrid_search_once(
    queries: WikiSearchQueries,
    *,
    search_kind: str = "primary",
) -> list:
    sc_id = rag_debug_logger.next_search_call_id()
    rag_debug_logger.set_search_context(
        search_call_id=sc_id,
        search_kind=search_kind,
    )
    rerank_q = f"{queries.direct_query} | {queries.policy_query}".strip(" |")

    def _invoke() -> list:
        return hybrid_search(
            direct_query=queries.direct_query,
            policy_query=queries.policy_query,
            rerank_query=rerank_q,
        )

    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    return await loop.run_in_executor(None, ctx.run, _invoke)


async def run_company_wiki_search(queries: WikiSearchQueries) -> list:
    """사내 문서 검색 — direct_query·policy_query 각각 semantic+BM25 후 RRF."""
    if not queries.direct_query.strip() and not queries.policy_query.strip():
        return []

    return await _run_hybrid_search_once(queries)


def _prior_wiki_queries_from_tool_results(
    tool_results: list[dict[str, Any]],
) -> tuple[str, str]:
    for tr in tool_results:
        if tr.get("tool_name") == "search_company_wiki":
            return (
                str(tr.get("direct_query") or ""),
                str(tr.get("policy_query") or ""),
            )
    return "", ""


async def _wiki_followup_search(
    *,
    followup_plan: WikiFollowupPlan,
) -> tuple[list, str, str]:
    followup_queries = WikiSearchQueries(
        direct_query=followup_plan.direct_query,
        policy_query=followup_plan.policy_query,
    )
    docs = await _run_hybrid_search_once(followup_queries, search_kind="followup")
    return docs, followup_plan.direct_query, followup_plan.policy_query


async def execute_search_company_wiki(
    *,
    tool_call,
    query_stripped: str,
    conversation_history: list[dict[str, str]] | None = None,
    memory_context: str = "",
) -> tuple[str, list, dict[str, Any]]:
    """
    search_company_wiki tool 실행.
    반환:
        tool_content, docs, tool_result
    """
    tool_args = parse_tool_arguments(tool_call.function.arguments)
    wiki_queries = extract_wiki_search_queries_from_tool_args(
        tool_args,
        query_stripped=query_stripped,
        conversation_history=conversation_history,
        memory_context=memory_context,
    )

    logger.info(
        "[Agent] 도구 실행. "
        f"name=search_company_wiki, direct_query={wiki_queries.direct_query!r}, "
        f"policy_query={wiki_queries.policy_query!r}, "
        f"user_message={query_stripped!r}, tool_call_id={tool_call.id}"
    )

    docs = await run_company_wiki_search(wiki_queries)

    context_str = _build_context(docs) if docs else "검색 결과가 없습니다."

    tool_content = (
        f"direct_query: {wiki_queries.direct_query}\n"
        f"policy_query: {wiki_queries.policy_query}\n\n"
        f"검색 결과:\n{context_str}"
    )

    tool_result = {
        "tool_name": "search_company_wiki",
        "tool_call_id": tool_call.id,
        "direct_query": wiki_queries.direct_query,
        "policy_query": wiki_queries.policy_query,
        "docs_count": len(docs),
    }

    return tool_content, docs, tool_result


async def execute_query_gov_projects(
    *,
    tool_call,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """query_gov_projects tool 실행."""
    tool_args = parse_tool_arguments(tool_call.function.arguments)
    action = str(tool_args.get("action") or "list").strip()
    idx_raw = tool_args.get("idx")
    idx = int(idx_raw) if idx_raw is not None else None
    keyword = str(tool_args.get("keyword") or "").strip()
    file_category = str(tool_args.get("file_category") or "전체").strip()

    logger.info(
        "[Agent] 도구 실행. name=query_gov_projects, action=%r, idx=%s, keyword=%r",
        action,
        idx,
        keyword,
    )

    result = query_gov_projects(
        action=action,
        idx=idx,
        keyword=keyword,
        file_category=file_category,
    )

    tool_result = {
        "tool_name": "query_gov_projects",
        "tool_call_id": tool_call.id,
        "action": action,
        "idx": idx,
        "keyword": keyword,
        "matched_count": result.matched_count,
        "target_date": result.target_date,
        "attachments_count": len(result.attachments),
        "suggest_wiki": result.suggest_wiki,
    }

    return result.content, result.attachments, tool_result


async def execute_search_worker_schedule(
    *,
    tool_call,
    query_stripped: str = "",
) -> tuple[str, dict[str, Any]]:
    """search_worker_schedule tool 실행."""
    tool_args = normalize_flex_schedule_tool_args(
        parse_tool_arguments(tool_call.function.arguments),
        query_stripped,
    )
    worker_names = list(tool_args.get("worker_names") or [])
    worker_name = str(tool_args.get("worker_name") or "").strip()
    team = str(tool_args.get("team") or "").strip()
    role_title = str(tool_args.get("role_title") or "").strip()
    date = str(tool_args.get("date") or "").strip() or None
    end_date = str(tool_args.get("end_date") or "").strip() or None
    year_month = str(tool_args.get("year_month") or "").strip() or None

    logger.info(
        "[Agent] 도구 실행. name=search_worker_schedule, worker_names=%r, "
        "team=%r, role_title=%r, date=%r, end_date=%r, year_month=%r",
        worker_names,
        team,
        role_title,
        date,
        end_date,
        year_month,
    )

    content = search_workers_schedule(
        worker_names or ([worker_name] if worker_name else []),
        date=date,
        end_date=end_date,
        year_month=year_month,
    )
    tool_result = {
        "tool_name": "search_worker_schedule",
        "tool_call_id": tool_call.id,
        "worker_name": worker_name,
        "worker_names": worker_names,
        "team": team,
        "role_title": role_title,
        "matched_teams": tool_args.get("matched_teams") or [],
        "matched_roles": tool_args.get("matched_roles") or [],
        "date": date,
        "end_date": end_date,
        "year_month": year_month,
    }
    return content, tool_result


def execute_respond_general(*, tool_call) -> tuple[str, dict[str, Any]]:
    """respond_general — 외부 조회 없음, Turn2에서 답변 생성."""
    tool_args = parse_tool_arguments(tool_call.function.arguments)
    category = str(tool_args.get("category") or "general_knowledge").strip()
    return (
        f"respond_general: category={category}",
        {"tool_name": "respond_general", "category": category},
    )


async def execute_manage_room_schedule(
    *,
    tool_call,
    query_stripped: str = "",
    conversation_history: list[dict[str, str]] | None = None,
    requester_email: str | None = None,
    requester_name: str | None = None,
    requester_user_id: Any = None,
    requester_slack_user_id: str | None = None,
    requester_slack_channel_id: str | None = None,
    room_write_allowed: bool = True,
) -> tuple[str, dict[str, Any]]:
    """manage_room_schedule tool 실행."""
    from app.services.outlook_room.attendee_resolver import resolve_organizer_email
    from app.services.outlook_room.schedule_reserve import _NO_EMAIL_MSG

    tool_args = normalize_room_tool_args(
        parse_tool_arguments(tool_call.function.arguments),
        query_stripped,
    )
    action = str(tool_args.get("action") or "check").strip()

    if is_room_write_action(action) and not room_write_allowed:
        tool_result = {
            "tool_name": "manage_room_schedule",
            "tool_call_id": tool_call.id,
            "action": action,
            "skipped": "room_write_once",
        }
        return sanitize_user_facing_tool_message(room_write_once_message()), tool_result

    if action == "book":
        tool_args = enrich_book_tool_args(
            tool_args, query_stripped, conversation_history,
        )

    organizer_email, organizer_name_resolved = await resolve_organizer_email(
        slack_user_id=requester_slack_user_id,
        fallback_email=requester_email,
        fallback_name=requester_name,
    )
    organizer_name = organizer_name_resolved or requester_name or ""

    room_name = str(tool_args.get("room_name") or "").strip()
    date_str = str(tool_args.get("date") or "").strip() or None
    end_date_str = str(tool_args.get("end_date") or "").strip() or None
    booking_id = str(tool_args.get("booking_id") or "").strip()
    subject = str(tool_args.get("subject") or "").strip()
    start_time = str(tool_args.get("start_time") or "").strip()
    end_time = str(tool_args.get("end_time") or "").strip()
    focus_time = str(tool_args.get("focus_time") or "").strip() or None
    old_start_time = str(tool_args.get("old_start_time") or "").strip()
    old_end_time = str(tool_args.get("old_end_time") or "").strip()
    new_start_time = str(tool_args.get("new_start_time") or "").strip()
    new_end_time = str(tool_args.get("new_end_time") or "").strip()
    new_subject = str(tool_args.get("new_subject") or "").strip() or None
    event_id = str(tool_args.get("event_id") or "").strip()
    extra_attendees = tool_args.get("attendees") or []
    reminder_minutes_raw = tool_args.get("reminder_minutes")
    reminder_minutes = int(reminder_minutes_raw) if reminder_minutes_raw is not None else 0
    person_name = str(tool_args.get("person_name") or "").strip() or None

    write_or_target = is_room_write_action(action) or action in (
        "cancel", "modify", "replace", "set_reminder",
    )
    if write_or_target and not (
        organizer_email and "@" in (organizer_email or "")
    ):
        tool_result = {
            "tool_name": "manage_room_schedule",
            "tool_call_id": tool_call.id,
            "action": action,
            "error": "no_organizer_email",
        }
        return sanitize_user_facing_tool_message(_NO_EMAIL_MSG), tool_result

    def _attendee_list() -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        if isinstance(extra_attendees, list):
            for item in extra_attendees:
                if isinstance(item, dict):
                    cleaned = {k: str(v) for k, v in item.items() if v}
                    if cleaned:
                        out.append(cleaned)
        return out

    logger.info(
        "[Agent] 도구 실행. name=manage_room_schedule, action=%r, room=%r",
        action,
        room_name,
    )

    resolved_booking_id: str | None = None

    def _booking_target_hints_present() -> bool:
        return any([
            is_valid_booking_uuid(booking_id),
            event_id,
            room_name,
            subject,
            start_time,
            old_start_time,
            date_str,
        ])

    async def _resolve_booking_target() -> tuple[Any | None, str | None]:
        return await prepare_booking_target(
            organizer_email=organizer_email,
            user_id=requester_user_id,
            slack_user_id=requester_slack_user_id,
            slack_channel_id=requester_slack_channel_id,
            booking_id=booking_id or None,
            event_id=event_id or None,
            room_name=room_name or None,
            subject=subject or None,
            start_time=start_time or None,
            old_start_time=old_start_time or None,
            date_str=date_str,
            end_date_str=end_date_str,
            query=query_stripped,
            prefer_recent=not _booking_target_hints_present(),
        )

    def _format_resolved_target(booking) -> str:
        start = booking.start_time[:16].replace("T", " ")
        end = booking.end_time[11:16] if booking.end_time else ""
        time_range = f"{start}~{end}" if end else start
        return (
            f"(처리 대상: {booking.room_display} | {booking.subject} | {time_range})\n"
        )

    if action == "check":
        if not room_name:
            content = "회의실 이름을 지정해 주세요."
        else:
            content = await check_room_schedule(
                room_name,
                date_str,
                start_time=start_time or None,
                end_time=end_time or None,
                focus_time=focus_time,
            )

    elif action == "check_all":
        content = await check_all_rooms(
            date_str,
            start_time=start_time or None,
            end_time=end_time or None,
            focus_time=focus_time,
        )

    elif action in ("list", "list_mine"):
        content = await list_bookings(
            user_id=requester_user_id,
            slack_user_id=requester_slack_user_id,
            date_str=date_str,
            end_date_str=end_date_str,
            organizer_email=organizer_email,
            slack_channel_id=requester_slack_channel_id,
            person_name=person_name,
            query=query_stripped,
        )

    elif action == "book":
        book_subject = default_book_subject(query_stripped, subject or None)
        resolved_room = resolve_booking_room(
            room_name or None, query_stripped, conversation_history,
        )
        missing_labels: list[str] = []
        if not resolved_room:
            missing_labels.append("회의실")
        if not start_time:
            missing_labels.append("시작 시각")
        if missing_labels:
            content = (
                "예약하려면 "
                + "·".join(missing_labels)
                + "을(를) 알려주세요. (종료 시각 없으면 1시간으로 예약합니다)"
            )
        elif not room_name_maps_to_managed(resolved_room):
            content = (
                f"회의실 '{resolved_room}'을(를) 인식하지 못했습니다. "
                f"다음 중 하나로 다시 요청해 주세요: {list_rooms()}"
            )
        else:
            book_end = end_time or room_default_end_time(start_time)
            content, _booking = await book_room(
                resolved_room,
                book_subject,
                start_time,
                book_end,
                organizer_email=organizer_email,
                required_attendees=_attendee_list() or None,
                organizer_name=organizer_name,
                user_id=requester_user_id,
                slack_user_id=requester_slack_user_id,
                slack_channel_id=requester_slack_channel_id,
            )

    elif action == "set_reminder":
        if reminder_minutes <= 0:
            content = "알림 시각(몇 분 전)을 1 이상으로 지정해 주세요."
        else:
            target, resolve_err = await _resolve_booking_target()
            if resolve_err:
                content = resolve_err
            elif not target:
                content = (
                    "리마인더를 설정할 본인 예약을 찾지 못했습니다. "
                    "회의실·날짜·제목 중 하나 이상을 포함해 주세요."
                )
            else:
                resolved_booking_id = str(target.id)
                content = _format_resolved_target(target) + await set_room_reminder(
                    booking_id=resolved_booking_id,
                    reminder_minutes=reminder_minutes,
                    organizer_email=organizer_email,
                    user_id=requester_user_id,
                    slack_user_id=requester_slack_user_id,
                    slack_channel_id=requester_slack_channel_id,
                )

    elif action == "cancel":
        target, resolve_err = await _resolve_booking_target()
        if resolve_err:
            content = resolve_err
        elif not target:
            content = (
                "취소할 본인 예약을 찾지 못했습니다. "
                "회의실·날짜·제목 중 하나 이상을 포함해 주세요."
            )
        else:
            resolved_booking_id = str(target.id)
            content = _format_resolved_target(target) + await cancel_room(
                organizer_email=organizer_email,
                user_id=requester_user_id,
                slack_user_id=requester_slack_user_id,
                booking_id=resolved_booking_id,
                event_id=event_id or None,
            )

    elif action == "modify":
        attendees = _attendee_list()
        has_time_change = bool(new_start_time or new_end_time)
        has_change = bool(new_subject or attendees or has_time_change)
        if not has_change:
            content = "변경할 내용이 필요합니다. 변경할 시간, 제목 또는 참석자를 알려주세요."
        elif has_time_change and not (new_start_time and new_end_time):
            missing = [
                f for f, v in [
                    ("new_start_time", new_start_time),
                    ("new_end_time", new_end_time),
                ] if not v
            ]
            content = missing_fields_message("시간 변경에 필요한 정보가 없습니다", missing)
        else:
            target, resolve_err = await _resolve_booking_target()
            if resolve_err:
                content = resolve_err
            elif not target:
                content = (
                    "변경할 본인 예약을 찾지 못했습니다. "
                    "회의실·날짜·제목 중 하나 이상을 포함해 주세요."
                )
            else:
                resolved_booking_id = str(target.id)
                content = _format_resolved_target(target) + await modify_room(
                    organizer_email=organizer_email,
                    organizer_name=organizer_name,
                    user_id=requester_user_id,
                    slack_user_id=requester_slack_user_id,
                    booking_id=resolved_booking_id,
                    room_name=room_name or None,
                    subject=subject or None,
                    old_start_time=old_start_time or start_time or None,
                    old_end_time=old_end_time or end_time or None,
                    new_start_time=new_start_time,
                    new_end_time=new_end_time,
                    new_subject=new_subject,
                    date_str=date_str,
                    required_attendees=attendees or None,
                )

    elif action == "replace":
        missing = [
            f for f, v in [("new_start_time", new_start_time), ("new_end_time", new_end_time)]
            if not v
        ]
        if missing:
            content = missing_fields_message("재예약에 필요한 정보가 없습니다", missing)
        else:
            target, resolve_err = await _resolve_booking_target()
            if resolve_err:
                content = resolve_err
            elif not target:
                content = (
                    "재예약할 본인 예약을 찾지 못했습니다. "
                    "회의실·날짜·제목 중 하나 이상을 포함해 주세요."
                )
            else:
                resolved_booking_id = str(target.id)
                content = _format_resolved_target(target) + await replace_room(
                    organizer_email=organizer_email,
                    organizer_name=organizer_name,
                    user_id=requester_user_id,
                    slack_user_id=requester_slack_user_id,
                    slack_channel_id=requester_slack_channel_id,
                    booking_id=resolved_booking_id,
                    room_name=room_name or None,
                    subject=subject or None,
                    start_time=start_time or None,
                    date_str=date_str,
                    new_room_name=str(tool_args.get("new_room_name") or room_name or "").strip() or None,
                    new_subject=new_subject or subject or None,
                    new_start_time=new_start_time,
                    new_end_time=new_end_time,
                    required_attendees=_attendee_list() or None,
                )

    else:
        content = "지원하지 않는 작업입니다."

    tool_result = {
        "tool_name": "manage_room_schedule",
        "tool_call_id": tool_call.id,
        "action": action,
        "room_name": room_name,
    }
    if resolved_booking_id:
        tool_result["booking_id"] = resolved_booking_id
    return sanitize_user_facing_tool_message(content), tool_result


async def execute_archive_expense_attachment(
    *,
    tool_call: Any,
    attachment_bundle: UserAttachmentBundle | None,
) -> tuple[str, dict[str, Any]]:
    try:
        tool_args = json.loads(tool_call.function.arguments or "{}")
    except json.JSONDecodeError:
        tool_args = {}

    category = str(tool_args.get("category") or "").strip()
    ids = tool_args.get("include_attachment_ids") or []
    if isinstance(ids, str):
        ids = [x.strip() for x in ids.split(",") if x.strip()]
    reason = str(tool_args.get("reason") or "").strip()

    content, uploads = archive_attachments_to_onedrive(
        category=category,
        attachment_ids=[str(x) for x in ids if x],
        bundle=attachment_bundle,
        reason=reason,
    )
    tool_result = {
        "tool_name": "archive_expense_attachment",
        "tool_call_id": tool_call.id,
        "category": category,
        "upload_count": len(uploads),
    }
    return sanitize_user_facing_tool_message(content), tool_result


async def execute_tool_calls(
    *,
    messages: list[dict[str, Any]],
    tool_calls,
    query_stripped: str,
    conversation_history: list[dict[str, str]] | None = None,
    memory_context: str = "",
    max_tool_calls: int = MAX_TOOL_CALLS,
    requester_email: str | None = None,
    requester_name: str | None = None,
    requester_user_id: Any = None,
    requester_slack_user_id: str | None = None,
    requester_slack_channel_id: str | None = None,
    attachment_bundle: UserAttachmentBundle | None = None,
) -> tuple[list, list[dict[str, Any]], list[dict[str, Any]], str, str, str, str]:
    """
    assistant가 요청한 tool_calls 실행.

    Returns:
        unique_docs, tool_results, gov_attachments, gov_context_text,
        flex_context_text, room_context_text, expense_context_text
    """

    all_docs = []
    tool_results: list[dict[str, Any]] = []
    gov_attachments: list[dict[str, Any]] = []
    gov_context_parts: list[str] = []
    flex_context_parts: list[str] = []
    room_context_parts: list[str] = []
    expense_context_parts: list[str] = []

    if not tool_calls:
        return all_docs, tool_results, gov_attachments, "", "", "", ""

    messages.append(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": serialize_tool_calls(tool_calls),
        }
    )

    executed_count = 0
    room_write_executed = False

    for tool_call in tool_calls:
        tool_name = tool_call.function.name
        tool_call_id = tool_call.id

        if executed_count >= max_tool_calls:
            logger.warning(
                "[Agent] 도구 실행 횟수 제한에 도달했습니다. "
                f"max_tool_calls={max_tool_calls}, skipped_tool_call_id={tool_call_id}"
            )

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "content": (
                        "도구 실행 횟수 제한에 도달하여 이 도구 호출은 실행하지 않았습니다. "
                        "이미 실행된 도구 결과만 사용해 답변하세요."
                    ),
                }
            )
            continue

        try:
            if tool_name == "search_company_wiki":
                tool_content, docs, tool_result = await execute_search_company_wiki(
                    tool_call=tool_call,
                    query_stripped=query_stripped,
                    conversation_history=conversation_history,
                    memory_context=memory_context,
                )
                executed_count += 1
                all_docs.extend(docs)
                tool_results.append(tool_result)

            elif tool_name == "query_gov_projects":
                tool_content, attachments, tool_result = await execute_query_gov_projects(
                    tool_call=tool_call,
                )
                executed_count += 1
                gov_attachments.extend(attachments)
                gov_context_parts.append(tool_content)
                tool_results.append(tool_result)

            elif tool_name == "search_worker_schedule":
                tool_content, tool_result = await execute_search_worker_schedule(
                    tool_call=tool_call,
                    query_stripped=query_stripped,
                )
                executed_count += 1
                flex_context_parts.append(tool_content)
                tool_results.append(tool_result)

            elif tool_name == "manage_room_schedule":
                peek_args = normalize_room_tool_args(
                    parse_tool_arguments(tool_call.function.arguments),
                    query_stripped,
                )
                peek_action = peek_manage_room_action(peek_args)
                allow_room_write = True
                if is_room_write_action(peek_action):
                    if room_write_executed:
                        allow_room_write = False
                    else:
                        room_write_executed = True

                tool_content, tool_result = await execute_manage_room_schedule(
                    tool_call=tool_call,
                    query_stripped=query_stripped,
                    conversation_history=conversation_history,
                    requester_email=requester_email,
                    requester_name=requester_name,
                    requester_user_id=requester_user_id,
                    requester_slack_user_id=requester_slack_user_id,
                    requester_slack_channel_id=requester_slack_channel_id,
                    room_write_allowed=allow_room_write,
                )
                if allow_room_write or not is_room_write_action(peek_action):
                    executed_count += 1
                room_context_parts.append(tool_content)
                tool_results.append(tool_result)

            elif tool_name == "archive_expense_attachment":
                tool_content, tool_result = await execute_archive_expense_attachment(
                    tool_call=tool_call,
                    attachment_bundle=attachment_bundle,
                )
                executed_count += 1
                expense_context_parts.append(tool_content)
                tool_results.append(tool_result)

            elif tool_name == "respond_general":
                tool_content, tool_result = execute_respond_general(
                    tool_call=tool_call,
                )
                executed_count += 1
                tool_results.append(tool_result)

            else:
                logger.warning(f"[Agent] 지원하지 않는 도구 호출 감지: {tool_name}")
                tool_content = sanitize_user_facing_tool_message(
                    "요청을 처리할 수 없습니다. 다시 시도해 주세요."
                )
                tool_result = {
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "error": "unsupported_tool",
                }
                tool_results.append(tool_result)

        except Exception as tool_error:
            logger.exception(
                "[Agent] 도구 실행 실패. "
                f"tool_name={tool_name}, error={tool_error}"
            )
            executed_count += 1
            tool_content = sanitize_user_facing_tool_message(
                "작업 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
            )
            tool_result = {
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "error": str(tool_error),
            }
            tool_results.append(tool_result)

        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": tool_name,
                "content": tool_content,
            }
        )

    # hybrid_search가 이미 parent_section 단위로 collapse한 경우 재병합하지 않음
    if all_docs and all((getattr(d, "payload") or {}).get("merged_chunks") for d in all_docs):
        unique_docs = sorted(
            all_docs,
            key=lambda d: float(getattr(d, "score", 0.0) or 0.0),
            reverse=True,
        )[:RAG_MAX_PAGES]
    else:
        unique_docs = collapse_hits_by_page(all_docs)

    gov_context_text = "\n\n---\n\n".join(p for p in gov_context_parts if p.strip())
    flex_context_text = "\n\n---\n\n".join(p for p in flex_context_parts if p.strip())
    room_context_text = "\n\n---\n\n".join(p for p in room_context_parts if p.strip())
    expense_context_text = "\n\n---\n\n".join(p for p in expense_context_parts if p.strip())
    return (
        unique_docs,
        tool_results,
        gov_attachments,
        gov_context_text,
        flex_context_text,
        room_context_text,
        expense_context_text,
    )


# =============================================================================
# Main Router
# =============================================================================

async def async_agent_chat(
    query: str,
    session_id: str,
    conversation_history: list[dict[str, str]] | None = None,
    memory_context: str = "",
    session_summary_raw: dict | None = None,
    memories_raw: list | None = None,
    stream: "SlackStreamUpdater | None" = None,
    requester_email: str | None = None,
    requester_name: str | None = None,
    requester_user_id: Any = None,
    requester_slack_user_id: str | None = None,
    requester_slack_channel_id: str | None = None,
    attachment_bundle: UserAttachmentBundle | None = None,
    db_session_id: Any = None,
) -> tuple[str, list, str, list[int], dict[int, list[int]], dict[int, list[int]], list[dict[str, Any]]]:
    """
    한 번의 대화 세션으로 의도 파악과 답변 생성을 처리.

    Returns:
        tuple(final_answer, docs, intent, source_doc_numbers,
              links_used_by_doc, attachments_used_by_doc, gov_attachments)
    """
    query_stripped = query.strip()
    has_attachments = bool(attachment_bundle and attachment_bundle.has_content)

    if not query_stripped and not has_attachments:
        return "질문을 입력해주세요.", [], "general", [], {}, {}, []

    if not query_stripped and has_attachments:
        query_stripped = "첨부 파일 내용을 참고해 주세요."

    tools = get_agent_tools()
    messages = build_initial_messages(
        query_stripped,
        conversation_history=conversation_history,
        attachment_bundle=attachment_bundle,
    )

    client = _get_async_openai()

    try:
        rag_debug_logger.reset_search_call_seq()
        if session_id:
            rag_debug_logger.set_session_id(session_id)
        token_usage_turn1: list[dict[str, int]] = []
        token_usage_turn2_planner: list[dict[str, int]] = []
        token_usage_turn2: list[dict[str, int]] = []
        turn1_fallback_reason = "none"
        research_reason = ""
        missing_facets_logged: list[str] = []
        evidence_planner_decision = ""
        planner_parse_ok: bool | None = None
        rag_research_meta: dict[str, Any] | None = None

        turn1_started_at = _ts_sec()
        turn1_t0 = time.perf_counter()

        message = await call_llm_for_tool_decision(
            client=client,
            messages=messages,
            tools=tools,
            usage_out=token_usage_turn1,
        )
        turn1_duration_sec = _duration_sec(time.perf_counter() - turn1_t0)
        turn1_finished_at = _ts_sec()

        allowed_tool_names = {t["function"]["name"] for t in tools}
        domain_signals = detect_domain_signals(query_stripped)
        effective_tool_calls = normalize_turn1_tool_calls(
            message,
            allowed_tool_names=allowed_tool_names,
        )
        if effective_tool_calls and not has_lookup_tool(effective_tool_calls):
            logger.info(
                "[Agent] Turn1 pass1 without lookup tool. session_id='%s', tools=%s",
                session_id,
                [getattr(getattr(tc, 'function', None), 'name', '') for tc in effective_tool_calls],
            )

        turn1_retry = False
        if needs_turn1_reroute(effective_tool_calls, domain_signals):
            turn1_retry = True
            reroute_block = format_turn1_reroute_review(
                domain_signals,
                first_pass=describe_first_pass(effective_tool_calls),
            )
            retry_messages = [
                *messages,
                {"role": "system", "content": reroute_block},
            ]
            turn1_retry_t0 = time.perf_counter()
            retry_message = await call_llm_for_tool_decision(
                client=client,
                messages=retry_messages,
                tools=tools,
                usage_out=token_usage_turn1,
            )
            turn1_duration_sec += _duration_sec(time.perf_counter() - turn1_retry_t0)
            retry_calls = normalize_turn1_tool_calls(
                retry_message,
                allowed_tool_names=allowed_tool_names,
            )
            if retry_calls:
                effective_tool_calls = retry_calls
                message = retry_message
                logger.info(
                    "[Agent] Turn1 reroute pass2. session_id='%s', signals=%s, tools=%s",
                    session_id,
                    list(domain_signals.keys()),
                    [getattr(getattr(tc, 'function', None), 'name', '') for tc in effective_tool_calls],
                )
            else:
                logger.info(
                    "[Agent] Turn1 reroute pass2 still empty. session_id='%s', signals=%s",
                    session_id,
                    list(domain_signals.keys()),
                )

        if not effective_tool_calls:
            turn1_fallback_reason = _detect_turn1_fallback_reason(
                had_tool_calls=False,
                effective_tool_calls=[],
                query_stripped=query_stripped,
                general_content=message.content or "",
                turn1_retry=turn1_retry,
            )
            effective_tool_calls = apply_turn1_tool_fallback(
                [],
                query_stripped=query_stripped,
                general_content=message.content or "",
            )
            if any(tool_call_name(tc) == _WIKI_TOOL for tc in effective_tool_calls):
                if general_should_reroute_to_wiki(message.content or ""):
                    logger.info(
                        "[Agent] Turn1 wiki JSON recovery. session_id='%s'",
                        session_id,
                    )
                elif query_should_default_to_wiki(query_stripped):
                    logger.info(
                        "[Agent] Turn1 wiki query fallback. session_id='%s'",
                        session_id,
                    )
                else:
                    logger.info(
                        "[Agent] Turn1 wiki tool recovered. session_id='%s'",
                        session_id,
                    )
            elif turn1_retry:
                logger.info(
                    "[Agent] Turn1→respond_general fallback after reroute. session_id='%s'",
                    session_id,
                )
            else:
                logger.info(
                    "[Agent] Turn1→respond_general fallback. session_id='%s'",
                    session_id,
                )

        sanitized_content = sanitize_turn1_assistant_content(
            message.content,
            has_attachments=has_attachments,
            has_tool_calls=bool(effective_tool_calls),
        )
        if sanitized_content != message.content and (message.content or "").strip():
            logger.info(
                "[Agent] Turn1 content cleared (no attachments). session_id='%s'",
                session_id,
            )
            message.content = sanitized_content

        business_tool_calls = filter_business_tool_calls(effective_tool_calls)
        if not business_tool_calls:
            business_tool_calls = effective_tool_calls

        tool_names = [tc.function.name for tc in business_tool_calls]
        gov_tool_used = "query_gov_projects" in tool_names
        wiki_tool_used = "search_company_wiki" in tool_names
        flex_tool_used = "search_worker_schedule" in tool_names
        room_tool_used = "manage_room_schedule" in tool_names
        expense_tool_used = "archive_expense_attachment" in tool_names
        general_tool_used = "respond_general" in tool_names
        if room_tool_used:
            route_label = "room"
        elif expense_tool_used and not wiki_tool_used and not gov_tool_used and not flex_tool_used and not room_tool_used:
            route_label = "expense"
        elif general_tool_used and not wiki_tool_used and not gov_tool_used and not flex_tool_used:
            route_label = "general"
        elif gov_tool_used and not wiki_tool_used and not flex_tool_used:
            route_label = "gov"
        elif flex_tool_used and not wiki_tool_used and not gov_tool_used:
            route_label = "flex"
        else:
            route_label = "rag"
        logger.info(
            "[Agent] Intent=tool/%s. session_id='%s', query='%s', tools=%s",
            route_label,
            session_id,
            query_stripped,
            tool_names,
        )

        attachment_policy = resolve_attachment_policy(
            message.content,
            query_stripped,
            attachment_bundle if has_attachments else None,
        )
        turn2_attachment_policy = (
            attachment_policy if attachment_policy.applies_to_turn2() else None
        )
        attachment_context = await resolve_attachment_context(
            turn2_attachment_policy,
            bundle=attachment_bundle if has_attachments else None,
            db_session_id=db_session_id,
        )
        logger.info(
            "[Agent] attachment_mode=%s include_ids=%s ctx_ids=%s",
            attachment_policy.mode,
            attachment_policy.include_attachment_ids,
            attachment_context.ids() if attachment_context else [],
        )

        turn1_messages = copy.deepcopy(messages)

        if stream is not None:
            if route_label == "gov":
                status = "_정부과제 브리핑을 조회하고 있습니다…_"
            elif route_label == "flex":
                status = "_근무 일정을 조회하고 있습니다…_"
            elif route_label == "room":
                status = "_회의실 일정을 처리하고 있습니다…_"
            elif route_label == "expense":
                status = "_경비 증빙을 OneDrive에 정리하고 있습니다…_"
            elif route_label == "general":
                status = "_답변을 작성하고 있습니다…_"
            else:
                status = "_관련 문서를 검색하고 있습니다…_"
            await stream.set_status(status)

        search_started_at = _ts_sec()
        search_t0 = time.perf_counter()
        (
            all_docs,
            tool_results,
            gov_attachments,
            gov_context_text,
            flex_context_text,
            room_context_text,
            expense_context_text,
        ) = await execute_tool_calls(
            messages=messages,
            tool_calls=business_tool_calls,
            query_stripped=query_stripped,
            conversation_history=conversation_history,
            memory_context=memory_context,
            max_tool_calls=MAX_TOOL_CALLS,
            requester_email=requester_email,
            requester_name=requester_name,
            requester_user_id=requester_user_id,
            requester_slack_user_id=requester_slack_user_id,
            requester_slack_channel_id=requester_slack_channel_id,
            attachment_bundle=attachment_bundle if has_attachments else None,
        )
        search_duration_sec = _duration_sec(time.perf_counter() - search_t0)
        search_finished_at = _ts_sec()
        has_docs = bool(all_docs)

        logger.info(
            "[Agent] 도구 실행 완료. tool_results=%s, docs=%s, gov_files=%s",
            len(tool_results),
            len(all_docs),
            len(gov_attachments),
        )

        if message.content and str(message.content).strip():
            logger.info(
                "[Agent] Turn1 검색 문장/메모: %s",
                str(message.content).strip()[:300],
            )

        context_msg_limit = (
            RECENT_MESSAGES_LIMIT_GENERAL
            if route_label in ("general", "expense")
            else RECENT_MESSAGES_LIMIT
        )
        conversation_context = build_conversation_context_snippet(
            conversation_history,
            current_query=query_stripped,
            max_messages=context_msg_limit,
        )

        # 4. 최종 답변 — 정부과제 / Flex 근태 / 회의실 예약 / 사내 RAG
        has_flex_context = bool(flex_context_text.strip())
        has_room_context = bool(room_context_text.strip())
        has_expense_context = bool(expense_context_text.strip())
        rag_researched = False
        rag_three_phase = False
        turn2_planner_messages: list[dict[str, Any]] | None = None
        raw_turn2_planner = ""
        turn2_planner_started_at = ""
        turn2_planner_finished_at = ""
        turn2_planner_duration_sec = 0.0
        rag_neighbor_expanded = False
        if route_label == "gov":
            has_gov_context = bool(gov_context_text.strip())
            answer_messages = build_gov_answer_messages(
                query_stripped=query_stripped,
                gov_context=gov_context_text,
                has_context=has_gov_context,
                memory_context=memory_context,
                conversation_context=conversation_context,
                attachment_policy=turn2_attachment_policy,
                attachment_context=attachment_context,
            )
        elif route_label == "flex":
            answer_messages = build_flex_answer_messages(
                query_stripped=query_stripped,
                flex_context=flex_context_text,
                has_context=has_flex_context,
                memory_context=memory_context,
                conversation_context=conversation_context,
                attachment_policy=turn2_attachment_policy,
                attachment_context=attachment_context,
            )
        elif route_label == "room":
            answer_messages = build_room_answer_messages(
                query_stripped=query_stripped,
                room_context=room_context_text,
                has_context=has_room_context,
                memory_context=memory_context,
                conversation_context=conversation_context,
                attachment_policy=turn2_attachment_policy,
                attachment_context=attachment_context,
            )
        elif route_label == "expense":
            answer_messages = build_expense_answer_messages(
                query_stripped=query_stripped,
                expense_context=expense_context_text,
                has_context=has_expense_context,
                memory_context=memory_context,
                conversation_context=conversation_context,
                attachment_policy=turn2_attachment_policy,
                attachment_context=attachment_context,
            )
        elif route_label == "general":
            answer_messages = build_general_answer_messages(
                query_stripped=query_stripped,
                memory_context=memory_context,
                conversation_context=conversation_context,
                attachment_policy=turn2_attachment_policy,
                attachment_context=attachment_context,
            )
        else:
            search_context = _build_context(all_docs) if has_docs else ""
            rag_three_phase = (
                route_label == "rag"
                and has_docs
                and wiki_tool_used
                and RAG_RESEARCH_ENABLED
            )

            if rag_three_phase:
                planner_context = format_planner_search_context(all_docs)

                turn2_planner_messages = build_rag_evidence_planner_messages(
                    query_stripped=query_stripped,
                    planner_context=planner_context,
                    memory_context=memory_context,
                    conversation_context=conversation_context,
                )
                if stream is not None:
                    await stream.set_status("_관련 문서를 확인하고 있습니다…_")

                turn2_planner_started_at = _ts_sec()
                turn2_planner_t0 = time.perf_counter()
                raw_turn2_planner = await responses_create_text(
                    client=client,
                    model=RAG_EVIDENCE_PLANNER_MODEL_NAME,
                    messages=turn2_planner_messages,
                    max_tokens=RAG_EVIDENCE_PLANNER_MAX_TOKENS,
                    text_format=get_rag_evidence_planner_text_format(),
                    usage_out=token_usage_turn2_planner,
                    reasoning={"effort": "lowest"},
                )
                raw_plan = parse_evidence_planner_output(raw_turn2_planner)
                if not raw_plan.parse_ok:
                    raw_turn2_planner = await responses_create_text(
                        client=client,
                        model=RAG_EVIDENCE_PLANNER_MODEL_NAME,
                        messages=turn2_planner_messages,
                        max_tokens=RAG_EVIDENCE_PLANNER_MAX_TOKENS,
                        text_format=get_rag_evidence_planner_text_format(),
                        usage_out=token_usage_turn2_planner,
                        reasoning={"effort": "lowest"},
                    )
                    raw_plan = parse_evidence_planner_output(raw_turn2_planner)
                planner_parse_ok = raw_plan.parse_ok
                turn2_planner_duration_sec = _duration_sec(
                    time.perf_counter() - turn2_planner_t0
                )
                turn2_planner_finished_at = _ts_sec()

                planner_plan = normalize_evidence_planner_plan(
                    raw_plan,
                    initial_hits=all_docs,
                    user_query=query_stripped,
                )
                evidence_planner_decision = planner_plan.decision
                research_reason = planner_plan.decision
                missing_facets_logged = list(planner_plan.missing_evidence)

                if planner_plan.decision == "need_parent_expansion":
                    before_ids = merged_doc_ids(all_docs)
                    expanded_docs = materialize_evidence_from_planner(
                        planner_plan,
                        initial_hits=all_docs,
                    )
                    all_docs = expanded_docs
                    after_ids = merged_doc_ids(all_docs)
                    rag_neighbor_expanded = after_ids != before_ids
                    has_docs = bool(all_docs)
                    search_context = _build_context(all_docs) if has_docs else ""
                    rag_research_meta = {
                        "trigger_reason": "need_parent_expansion",
                        "evidence_planner_decision": planner_plan.decision,
                        "selected_parent_ids": list(planner_plan.selected_parent_ids),
                        "page_ids_for_expansion": list(
                            planner_plan.page_ids_for_expansion
                        ),
                        "primary_parent_ids": sorted(before_ids),
                        "merged_parent_ids": _parent_ids_from_docs(all_docs),
                        "merged_count": len(all_docs),
                    }

                elif planner_plan.decision == "need_external_search":
                    before_ids = merged_doc_ids(all_docs)
                    if stream is not None:
                        await stream.set_status("_추가 문서 검색 진행 중입니다…_")

                    followup_plan = planner_to_followup_plan(planner_plan)
                    followup_docs, followup_direct, followup_policy = (
                        await _wiki_followup_search(followup_plan=followup_plan)
                    )
                    all_docs = merge_wiki_search_docs(all_docs, followup_docs)
                    new_ids = merged_doc_ids(all_docs) - before_ids
                    has_docs = bool(all_docs)
                    rag_researched = True
                    search_context = _build_context(all_docs) if has_docs else ""
                    rag_research_meta = {
                        "trigger_reason": "need_external_search",
                        "evidence_planner_decision": planner_plan.decision,
                        "missing_evidence": missing_facets_logged,
                        "followup_direct": followup_direct,
                        "followup_policy": followup_policy,
                        "primary_parent_ids": sorted(before_ids),
                        "new_parent_ids": sorted(new_ids),
                        "merged_parent_ids": _parent_ids_from_docs(all_docs),
                        "primary_count": len(before_ids),
                        "secondary_count": len(followup_docs),
                        "merged_count": len(all_docs),
                        "new_doc_count": len(new_ids),
                    }
                    log_rag_research(
                        decision=followup_plan,
                        followup_direct=followup_direct,
                        followup_policy=followup_policy,
                        primary_count=len(before_ids),
                        secondary_count=len(followup_docs),
                        merged_count=len(all_docs),
                        new_doc_count=len(new_ids),
                    )
                else:
                    rag_research_meta = {
                        "trigger_reason": "answerable",
                        "evidence_planner_decision": planner_plan.decision,
                        "merged_parent_ids": _parent_ids_from_docs(all_docs),
                        "merged_count": len(all_docs),
                    }

            answer_messages = build_rag_answer_messages(
                query_stripped=query_stripped,
                search_context=search_context,
                has_docs=has_docs,
                memory_context=memory_context,
                conversation_context=conversation_context,
                flex_context=flex_context_text,
                gov_context=gov_context_text,
                attachment_policy=turn2_attachment_policy,
                attachment_context=attachment_context,
            )

        answer_started_at = _ts_sec()
        answer_t0 = time.perf_counter()

        if stream is not None:
            await stream.set_status("_답변을 작성하고 있습니다…_")
            if route_label not in ("gov", "flex", "room", "expense", "general"):
                stream.use_rag_extractor()

            async def _on_turn2_delta(chunk_text: str) -> None:
                await stream.feed(chunk_text)

            raw_turn2 = await stream_chat_completion_text(
                client=client,
                model=RAG_ANSWER_MODEL_NAME if route_label == "rag" else LLM_MODEL_NAME,
                messages=answer_messages,
                on_delta=_on_turn2_delta,
                reasoning_effort="low",
                usage_out=token_usage_turn2,
            )
            await stream.flush()
        else:
            raw_turn2 = await stream_chat_completion_text(
                client=client,
                model=RAG_ANSWER_MODEL_NAME if route_label == "rag" else LLM_MODEL_NAME,
                messages=answer_messages,
                reasoning_effort="low",
                usage_out=token_usage_turn2,
            )

        answer_duration_sec = _duration_sec(time.perf_counter() - answer_t0)
        answer_finished_at = _ts_sec()

        if not (raw_turn2 or "").strip():
            logger.warning(
                "[Agent] Turn2 LLM 스트림 응답이 비어 있음. session_id=%s",
                session_id,
            )

        source_doc_numbers: list[int] = []
        links_used_by_doc: dict[int, list[int]] = {}
        attachments_used_by_doc: dict[int, list[int]] = {}

        if route_label == "gov":
            final_answer = raw_turn2.strip() or "답변을 생성하지 못했습니다."
            intent = "gov_project" if has_gov_context else "gov_project_no_data"
        elif route_label == "flex":
            final_answer = raw_turn2.strip() or "답변을 생성하지 못했습니다."
            intent = "flex_schedule" if has_flex_context else "flex_schedule_no_data"
        elif route_label == "room":
            final_answer = raw_turn2.strip() or "답변을 생성하지 못했습니다."
            intent = "room_schedule" if has_room_context else "room_schedule_no_data"
        elif route_label == "expense":
            final_answer = raw_turn2.strip() or "답변을 생성하지 못했습니다."
            intent = "expense_archive" if has_expense_context else "expense_archive_no_data"
        elif route_label == "general":
            final_answer = raw_turn2.strip() or "답변을 생성하지 못했습니다."
            intent = "general"
        elif bool(all_docs):
            final_answer, source_doc_numbers, links_used_by_doc, attachments_used_by_doc = (
                parse_rag_structured_response(
                    raw_turn2,
                    max_docs=len(all_docs),
                )
            )
            if not final_answer:
                final_answer = strip_body_citations(raw_turn2) or "답변을 생성하지 못했습니다."
            intent = "rag"
        else:
            final_answer = raw_turn2.strip() or "답변을 생성하지 못했습니다."
            intent = "rag_no_docs"

        if rag_researched and rag_research_meta is not None:
            rag_research_meta["answer_after_len"] = len(final_answer or "")
            write_rag_research_phase_row(
                session_id=session_id,
                user_input=query_stripped,
                research_meta=rag_research_meta,
            )

        final_answer = strip_insufficient_answer_signal(final_answer)
        if not final_answer:
            final_answer = "제공된 문서에서 관련 내용을 확인할 수 없습니다."

        search_queries = [
            {
                "direct_query": tr.get("direct_query"),
                "policy_query": tr.get("policy_query"),
            }
            for tr in tool_results
            if tr.get("direct_query") or tr.get("policy_query")
        ]

        # 09_llm_io: turn1 → tool → turn2 (각 phase 본문만)
        write_llm_io_phase(
            session_id=session_id,
            user_input=query_stripped,
            phase="turn1",
            final_input=format_llm_io_full(turn1_messages),
            output=format_turn1_llm_output(
                message,
                effective_tool_calls=effective_tool_calls,
            ),
            extra={
                "intent": "tool_decision",
                "turn1_started_at": turn1_started_at,
                "turn1_finished_at": turn1_finished_at,
                "turn1_duration_sec": turn1_duration_sec,
            },
        )
        write_llm_io_phase(
            session_id=session_id,
            user_input=query_stripped,
            phase="tool",
            final_input=json.dumps(
                serialize_tool_calls(effective_tool_calls),
                ensure_ascii=False,
                indent=2,
            ),
            output=format_tool_io_output(messages),
            extra={
                "search_queries": search_queries,
                "docs_count": len(all_docs),
                "search_started_at": search_started_at,
                "search_finished_at": search_finished_at,
                "search_duration_sec": search_duration_sec,
            },
        )
        if turn2_planner_messages is not None:
            write_llm_io_phase(
                session_id=session_id,
                user_input=query_stripped,
                phase="turn2",
                final_input=format_llm_io_full(turn2_planner_messages),
                output=raw_turn2_planner,
                extra={
                    "intent": "rag_evidence_planner",
                    "model": RAG_EVIDENCE_PLANNER_MODEL_NAME,
                    "evidence_planner_decision": evidence_planner_decision,
                    "planner_parse_ok": planner_parse_ok,
                    "turn2_planner_started_at": turn2_planner_started_at,
                    "turn2_planner_finished_at": turn2_planner_finished_at,
                    "turn2_planner_duration_sec": turn2_planner_duration_sec,
                    "rag_researched": rag_researched,
                    "research_reason": research_reason,
                },
            )
        final_phase = "turn3" if rag_three_phase else "turn2"
        write_llm_io_phase(
            session_id=session_id,
            user_input=query_stripped,
            phase=final_phase,
            final_input=format_llm_io_full(answer_messages),
            output=raw_turn2,
            extra={
                "intent": intent,
                "evidence_planner_decision": evidence_planner_decision,
                "rag_neighbor_expanded": rag_neighbor_expanded,
                "answer_started_at": answer_started_at,
                "answer_finished_at": answer_finished_at,
                "answer_duration_sec": answer_duration_sec,
                "streamed": stream is not None,
                "stream_mode": "api_stream" if stream is not None else None,
                "model": (
                    RAG_ANSWER_MODEL_NAME
                    if route_label == "rag"
                    else LLM_MODEL_NAME
                ),
            },
        )
        rag_debug_logger._write(
            "09_llm_io.jsonl",
            {
                "ts": _ts_sec(),
                "session_id": session_id,
                "phase": "timing_summary",
                "user_input": query_stripped,
                "intent": intent,
                "route_label": route_label,
                "turn1_duration_sec": turn1_duration_sec,
                "turn2_planner_duration_sec": turn2_planner_duration_sec,
                "search_duration_sec": search_duration_sec,
                "answer_duration_sec": answer_duration_sec,
                "total_duration_sec": _duration_sec(
                    turn1_duration_sec
                    + turn2_planner_duration_sec
                    + search_duration_sec
                    + answer_duration_sec
                ),
                "token_usage": {
                    "turn1": token_usage_turn1,
                    "turn2_planner": token_usage_turn2_planner,
                    "turn3" if rag_three_phase else "turn2": token_usage_turn2,
                    "totals": _sum_token_usage(
                        [
                            token_usage_turn1,
                            token_usage_turn2_planner,
                            token_usage_turn2,
                        ]
                    ),
                },
            },
        )
        write_eval_summary_row(
            session_id=session_id,
            user_input=query_stripped,
            route_label=route_label,
            intent=intent,
            turn1_tool_names=tool_names,
            turn1_fallback=turn1_fallback_reason,
            turn1_reroute=turn1_retry,
            domain_signals=domain_signals,
            wiki_tool_used=wiki_tool_used,
            rag_researched=rag_researched,
            research_reason=research_reason,
            missing_facets=missing_facets_logged,
            docs_count=len(all_docs),
            parent_ids=_parent_ids_from_docs(all_docs),
            sources_used=source_doc_numbers,
            answer_length=len(final_answer or ""),
            has_table="|" in (final_answer or ""),
            timings={
                "turn1_sec": turn1_duration_sec,
                "turn2_planner_sec": turn2_planner_duration_sec,
                "search_sec": search_duration_sec,
                "turn3_sec" if rag_three_phase else "turn2_sec": answer_duration_sec,
                "total_sec": _duration_sec(
                    turn1_duration_sec
                    + turn2_planner_duration_sec
                    + search_duration_sec
                    + answer_duration_sec
                ),
            },
            token_usage={
                "turn1": token_usage_turn1,
                "turn2_planner": token_usage_turn2_planner,
                "turn3" if rag_three_phase else "turn2": token_usage_turn2,
                "totals": _sum_token_usage(
                    [
                        token_usage_turn1,
                        token_usage_turn2_planner,
                        token_usage_turn2,
                    ]
                ),
            },
            rag_research_meta=rag_research_meta,
        )
        if route_label != "gov" and has_docs:
            rag_debug_logger._write(
                "09_llm_io.jsonl",
                {
                    "ts": _ts_sec(),
                    "session_id": session_id,
                    "phase": "turn2",
                    "type": "turn2_parsed",
                    "intent": intent,
                    "user_input": query_stripped,
                    "source_doc_numbers": source_doc_numbers,
                    "links_used_by_doc": links_used_by_doc,
                    "attachments_used_by_doc": attachments_used_by_doc,
                    "answer_preview": (final_answer or "")[:500],
                    "search_duration_sec": search_duration_sec,
                    "answer_duration_sec": answer_duration_sec,
                },
            )

        return (
            final_answer,
            all_docs,
            intent,
            source_doc_numbers,
            links_used_by_doc,
            attachments_used_by_doc,
            gov_attachments,
        )

    except Exception as e:
        logger.exception(f"[Agent] 통합 API 호출 실패: {e}")
        return "답변 생성 중 오류가 발생했습니다.", [], "general", [], {}, {}, []