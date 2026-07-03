"""
Turn1 soft reroute — 1차 라우팅이 business lookup 없이 끝났을 때 2차 LLM 재판단.

키워드는 2차 Turn1 트리거용이며 tool을 강제하지 않는다.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.services.flex_hr.flex_hr import match_employees_in_query
from app.services.gov_project.gov_project import GOV_QUERY_KEYWORDS, match_gov_in_query
from app.services.outlook_room.schedule_reserve import ROOM_KEYWORDS, match_room_in_query

LOOKUP_TOOL_NAMES: frozenset[str] = frozenset({
    "search_company_wiki",
    "query_gov_projects",
    "search_worker_schedule",
    "manage_room_schedule",
    "archive_expense_attachment",
})

FLEX_SCHEDULE_KEYWORDS: tuple[str, ...] = (
    "근태",
    "출근",
    "재택",
    "휴가",
    "외근",
    "출장",
    "근무",
    "재실",
)

WIKI_QUERY_KEYWORDS: tuple[str, ...] = (
    "위키",
    "wiki",
    "confluence",
    "사내 규정",
    "복지",
    "복지포인트",
    "전자연구노트",
    "연구노트",
    "경비",
    "정산",
    "신청서",
    "양식",
    "인사 규정",
    "취업규칙",
    "총무",
    "규정",
    "규칙",
    "절차",
    "한도",
    "상한",
    "선수금",
    "휴직",
    "출장",
    "등급",
    "표로",
    "표로 정리",
    "테이블",
    "담당자",
    "마감",
    "지급 기준",
    "지급액",
    "조직도",
    "연락망",
    "비상연락망",
    "주소록",
)

_CURRENT_YEAR_HINTS: tuple[str, ...] = (
    "올해",
    "금년",
    "이번 연도",
    "당해년",
    "올 해",
)


def _matched_keywords(query: str, keywords: tuple[str, ...]) -> list[str]:
    q = (query or "").strip()
    if not q:
        return []
    ql = q.lower()
    out: list[str] = []
    for kw in keywords:
        if kw.lower() in ql:
            out.append(kw)
    return out


def detect_domain_signals(query: str) -> dict[str, list[str]]:
    """질문에서 감지된 도메인 후보 (tool 강제 아님)."""
    signals: dict[str, list[str]] = {}
    q = (query or "").strip()
    if not q:
        return signals

    gov_kws = _matched_keywords(q, GOV_QUERY_KEYWORDS)
    if gov_kws or match_gov_in_query(q):
        signals["gov"] = gov_kws or ["정부과제/지원사업"]

    flex_kws = _matched_keywords(q, FLEX_SCHEDULE_KEYWORDS)
    employees = match_employees_in_query(q)
    flex_parts = list(dict.fromkeys([*flex_kws, *employees]))
    if flex_parts:
        signals["flex"] = flex_parts

    room_kws = _matched_keywords(q.lower(), tuple(k.lower() for k in ROOM_KEYWORDS))
    if room_kws or match_room_in_query(q):
        signals["room"] = room_kws or ["회의실"]

    wiki_kws = _matched_keywords(q, WIKI_QUERY_KEYWORDS)
    if wiki_kws:
        signals["wiki"] = wiki_kws

    return signals


def tool_call_name(tool_call: Any) -> str:
    return str(getattr(getattr(tool_call, "function", None), "name", "") or "").strip()


def has_lookup_tool(tool_calls: list[Any] | None) -> bool:
    """respond_general 제외 — 실제 조회·검색 business tool."""
    for tc in tool_calls or []:
        if tool_call_name(tc) in LOOKUP_TOOL_NAMES:
            return True
    return False


def needs_turn1_reroute(
    tool_calls: list[Any] | None,
    signals: dict[str, list[str]],
) -> bool:
    """1차에 lookup tool 없고 도메인 신호가 있으면 2차 Turn1."""
    if not signals:
        return False
    return not has_lookup_tool(tool_calls)


def describe_first_pass(tool_calls: list[Any] | None) -> str:
    if not tool_calls:
        return "tool_call 없음"
    names = [tool_call_name(tc) for tc in tool_calls if tool_call_name(tc)]
    if not names:
        return "tool_call 없음"
    if names == ["respond_general"]:
        return "respond_general만 선택"
    return ", ".join(names)


def build_wiki_intent_hint_block(query: str) -> str:
    kws = _matched_keywords(query, WIKI_QUERY_KEYWORDS)
    if not kws:
        return ""
    return f"<wiki_intent_hints>사내 문서 검색 후보 키워드: {', '.join(kws)}</wiki_intent_hints>"


def build_room_intent_hint_block(query: str) -> str:
    if not match_room_in_query(query):
        return ""
    kws = _matched_keywords(query.lower(), tuple(k.lower() for k in ROOM_KEYWORDS))
    hint = ", ".join(kws[:6]) if kws else "회의실·예약"
    return f"<room_intent_hints>회의실 intent 감지: {hint}</room_intent_hints>"


def current_reference_year() -> int:
    """Asia/Seoul 기준 당해 연도."""
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Seoul")).year


def extract_reference_year(text: str) -> int | None:
    """질문·검색어에서 기준 연도(20XX 또는 올해·금년) 추출."""
    blob = (text or "").strip()
    if not blob:
        return None

    explicit = [int(y) for y in re.findall(r"(20\d{2})", blob)]
    if explicit:
        return max(explicit)

    if any(hint in blob for hint in _CURRENT_YEAR_HINTS):
        return current_reference_year()

    return None


def resolve_wiki_reference_year(*texts: str) -> int | None:
    """user_query·direct·policy 중 가장 구체적인 기준 연도."""
    years: list[int] = []
    for text in texts:
        year = extract_reference_year(text or "")
        if year is not None:
            years.append(year)
    return max(years) if years else None


def inject_reference_year_into_query(query: str, reference_year: int | None) -> str:
    """검색어에 기준 연도가 없으면 붙인다 (올해·분기별 등 시점 질문용)."""
    q = (query or "").strip()
    if not q or reference_year is None:
        return q
    if re.search(r"20\d{2}", q):
        return q
    return f"{q} {reference_year}년".strip()


def enrich_wiki_queries_with_year(
    direct_query: str,
    policy_query: str,
    *,
    user_query: str = "",
) -> tuple[str, str]:
    """Turn1 wiki 검색어에 기준 연도를 반영한다."""
    ref_year = resolve_wiki_reference_year(user_query, direct_query, policy_query)
    if ref_year is None:
        return direct_query, policy_query
    return (
        inject_reference_year_into_query(direct_query, ref_year),
        inject_reference_year_into_query(policy_query, ref_year),
    )


def query_should_default_to_wiki(query: str) -> bool:
    """Turn1 tool_call 없을 때 search_company_wiki로 보낼 만한 사내 문서 질문."""
    q = (query or "").strip()
    if not q:
        return False
    if _matched_keywords(q, WIKI_QUERY_KEYWORDS):
        return True
    return "wiki" in detect_domain_signals(q)
