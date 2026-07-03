"""
RAG 3단계 파이프라인.

1. Turn1 hybrid_search → initial parents
2. Turn2 evidence_planner — JSON만 출력, 근거 확장·재검색 계획
3. (조건부) parent mget / follow-up hybrid_search
4. Turn3 — 확정 근거로 최종 답변
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence

from app.core.config import (
    HIGH_RISK_FALLBACK_MODEL_NAME,
    LLM_MODEL_NAME,
    RAG_EXPAND_MAX_PARENTS,
    RAG_EXPAND_MAX_PARENTS_PER_PAGE,
    RAG_EXPAND_NEIGHBOR_RADIUS,
    RAG_INSUFFICIENT_ANSWER_SIGNAL,
    RAG_MAX_PARENTS,
    RAG_MAX_PARENTS_PER_PAGE,
    RAG_MIN_PAGES,
    RAG_PICKER_MAX_PARENTS_PER_PAGE,
    RAG_RESEARCH_ENABLED,
)
from app.rag.vectordb import (
    SearchHit,
    expand_parent_ids_by_page_neighbors,
    fetch_page_parent_catalog,
    page_ids_from_search_hits,
    search_hits_from_parent_ids,
    select_diverse_parents,
)
from app.services.turn1_reroute import enrich_wiki_queries_with_year

if TYPE_CHECKING:
    from openai import AsyncOpenAI

from app.agent.openai_responses_util import responses_create_text

logger = logging.getLogger(__name__)

_RESEARCH_DIRECT_QUERY = re.compile(
    r"^direct_query:\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)
_RESEARCH_POLICY_QUERY = re.compile(
    r"^policy_query:\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)
_NEEDS_FOLLOWUP = re.compile(
    r"^needs_followup:\s*(yes|no)\b",
    re.IGNORECASE | re.MULTILINE,
)
_FOLLOWUP_REASON = re.compile(
    r"^reason:\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)
_NEEDS_EXTERNAL_SEARCH = re.compile(
    r"^needs_external_search:\s*(yes|no)\b",
    re.IGNORECASE | re.MULTILINE,
)
_SELECTED_PARENT_IDS = re.compile(
    r"^selected_parent_ids:\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)

# 질문·문서 형식 힌트 (특정 업무 주제어 아님)
_STRUCTURAL_FACET_HINTS: tuple[str, ...] = (
    "구분",
    "구분표",
    "등급",
    "등급표",
    "매핑",
    "분류",
    "별표",
    "부록",
    "첨부",
    "표",
    "한도",
    "상한",
    "기한",
    "기준",
    "세부",
    "양식",
    "신청서",
    "절차",
    "지급",
    "금액",
    "비용",
)

_MISSING_IN_DOCS_CLAIMS: tuple[str, ...] = (
    "문서에 없",
    "확인되지 않",
    "명시되어 있지 않",
    "명시되지 않",
    "찾을 수 없",
    "나와 있지 않",
    "기재되어 있지 않",
    "제시되지 않",
    "확인할 수 없",
    "구체적인 상한은 없",
    "상한 없음",
    "별도 상한은",
    "해당 수치는 문서에 없",
    "확인할 수 없습니다",
)

_USER_FOCUS_STOPWORDS: frozenset[str] = frozenset({
    "알려줘", "알려주세요", "무엇", "어떻게", "얼마", "있나요", "있어",
    "관련", "문의", "정리", "표로", "테이블", "올해", "금년", "이번", "연도", "년",
    "만", "해줘", "해주세요", "알려", "대해", "대한", "경우", "때문", "이런", "저런",
    "돼", "되", "되는", "어떤", "무슨", "좀", "주세요",
})

# facet_gap 트리거에서 제외 — 키워드만 겹치면 다른 주제 문서로 오탐
_GENERIC_FACET_TERMS: frozenset[str] = frozenset({
    "지원", "방법", "관련", "안내", "기준", "규정", "정리", "알려", "문의",
    "확인", "신청", "절차", "가능", "있나", "있어",
})

_FOLLOWUP_LLM_QUERIES_ONLY_SYSTEM = """\
당신은 사내 위키 추가 검색어 작성기입니다.
재검색이 이미 확정되었으므로, needs_followup 판단은 하지 않고 follow-up hybrid_search용 검색어만 작성합니다.

[검색어 작성]
- 사용자 질문 의도(주제·범위)를 유지한다. 누락 디테일만 따로 검색하지 않는다.
  예) '프랑스 숙박비 지원' → '프랑스 나등급'만 X, '해외출장 숙박비 국가별 등급표 프랑스' O
- 1차 답변에 이미 다룬 맥락과 아직 못 찾은 내용을 함께 반영한다.
- 1차 검색어와 동일하게 쓰지 않는다. 원질문 전체 복붙 금지.
- Turn2가 이미 제시한 direct_query/policy_query가 있으면 유지·보완하고, 없는 쪽만 채운다.

[출력] 아래 3줄만 (다른 텍스트 금지):
reason: (한 줄, 재검색 목적)
direct_query: (400자 이내)
policy_query: (400자 이내)
"""


@dataclass(frozen=True)
class FacetStatus:
    """질문 항목 하나에 대한 자료·답변 상태."""

    facet: str
    evidence_in_hits: bool
    covered_in_answer: bool

    @property
    def needs_followup(self) -> bool:
        """자료에 있을 법한데 답이 안 다룬 항목."""
        return self.evidence_in_hits and not self.covered_in_answer


@dataclass(frozen=True)
class RagResearchDecision:
    needs_research: bool
    reason: str = ""
    missing_facets: tuple[str, ...] = ()


@dataclass(frozen=True)
class WikiFollowupPlan:
    needs_research: bool
    reason: str = ""
    direct_query: str = ""
    policy_query: str = ""
    missing_facets: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidencePlannerPlan:
    """Turn2 evidence planner JSON 출력."""

    decision: str = "answerable"
    selected_parent_ids: tuple[str, ...] = ()
    page_ids_for_expansion: tuple[str, ...] = ()
    direct_query: str = ""
    policy_query: str = ""
    missing_evidence: tuple[str, ...] = ()
    parse_ok: bool = True

    @property
    def needs_parent_expansion(self) -> bool:
        return self.decision == "need_parent_expansion"

    @property
    def needs_external_search(self) -> bool:
        return self.decision == "need_external_search"


@dataclass(frozen=True)
class ParentPickerPlan:
    """Turn2b parent catalog 선별 결과."""

    selected_parent_ids: tuple[str, ...] = ()
    reason: str = ""
    needs_external_search: bool = False
    direct_query: str = ""
    policy_query: str = ""

    @property
    def needs_research(self) -> bool:
        return self.needs_external_search or not self.selected_parent_ids


def answer_contains_research_signal(text: str) -> bool:
    if not text or not RAG_INSUFFICIENT_ANSWER_SIGNAL:
        return False
    return RAG_INSUFFICIENT_ANSWER_SIGNAL in text


def extract_research_followup_queries(text: str) -> tuple[str, str]:
    body = text or ""
    direct = ""
    policy = ""
    m_direct = _RESEARCH_DIRECT_QUERY.search(body)
    m_policy = _RESEARCH_POLICY_QUERY.search(body)
    if m_direct:
        direct = m_direct.group(1).strip()
    if m_policy:
        policy = m_policy.group(1).strip()
    return direct, policy


def _queries_too_similar(a: str, b: str) -> bool:
    left = (a or "").strip().casefold()
    right = (b or "").strip().casefold()
    return bool(left and right and left == right)


def strip_insufficient_answer_signal(answer: str) -> str:
    signal = RAG_INSUFFICIENT_ANSWER_SIGNAL
    if not answer:
        return ""

    lines: list[str] = []
    for line in answer.splitlines():
        stripped = line.strip()
        if signal in stripped:
            continue
        if _RESEARCH_DIRECT_QUERY.match(stripped):
            continue
        if _RESEARCH_POLICY_QUERY.match(stripped):
            continue
        lines.append(line)

    cleaned = "\n".join(lines).strip()
    cleaned = re.sub(re.escape(signal), "", cleaned).strip()
    return cleaned


def _hit_text_blob(hit: SearchHit) -> str:
    payload = hit.payload or {}
    return " ".join(
        str(payload.get(key) or "")
        for key in ("section_title", "page_title", "text")
    )


def _split_section_title_phrases(title: str) -> list[str]:
    phrases: list[str] = []
    for part in re.split(r"[—·|｜/>\-]+", title or ""):
        part = part.strip()
        if len(part) >= 2:
            phrases.append(part)
    return phrases


def _extract_user_focus_terms(user_query: str) -> list[str]:
    q = (user_query or "").strip()
    if not q:
        return []

    terms: list[str] = []
    for m in re.finditer(r"[가-힣A-Za-z]{2,20}", q):
        token = m.group(0).strip()
        if not token or token in _USER_FOCUS_STOPWORDS:
            continue
        if token not in terms:
            terms.append(token)
    return terms[:12]


def extract_question_facets(user_query: str) -> list[str]:
    """질문에서 답해야 할 항목 — 도메인 중립 토큰 + 질문에 드러난 형식 힌트."""
    facets: list[str] = list(_extract_user_focus_terms(user_query))
    q = (user_query or "").strip()
    for hint in _STRUCTURAL_FACET_HINTS:
        if hint in q and hint not in facets:
            facets.append(hint)
    return facets[:12]


def answer_claims_missing_in_docs(answer: str) -> bool:
    text = (answer or "").strip()
    if not text:
        return False
    return any(claim in text for claim in _MISSING_IN_DOCS_CLAIMS)


def _facet_in_text(facet: str, text: str) -> bool:
    return bool(facet and facet in (text or ""))


def _hits_have_facet_evidence(facet: str, hits: Sequence[SearchHit]) -> bool:
    for hit in hits:
        if _facet_in_text(facet, _hit_text_blob(hit)):
            return True
    return False


def _answer_covers_facet(facet: str, answer: str) -> bool:
    """답변이 해당 facet을 실질적으로 다뤘는지 (용어 포함 여부)."""
    return _facet_in_text(facet, answer)


def analyze_facet_statuses(
    user_query: str,
    answer: str,
    hits: Sequence[SearchHit] | None,
) -> list[FacetStatus]:
    facets = extract_question_facets(user_query)
    if not facets:
        return []

    answer_text = (answer or "").strip()
    hit_list = list(hits or [])
    statuses: list[FacetStatus] = []

    for facet in facets:
        in_hits = _hits_have_facet_evidence(facet, hit_list) if hit_list else False
        in_answer = _answer_covers_facet(facet, answer_text)
        statuses.append(
            FacetStatus(
                facet=facet,
                evidence_in_hits=in_hits,
                covered_in_answer=in_answer,
            )
        )
    return statuses


def _filter_generic_facets(facets: list[str]) -> list[str]:
    return [
        f for f in facets
        if f and f not in _GENERIC_FACET_TERMS and f not in _USER_FOCUS_STOPWORDS
    ]


def missing_facets_for_followup(
    user_query: str,
    answer: str,
    hits: Sequence[SearchHit] | None,
) -> list[str]:
    """추가 검색이 필요한 facet — 자료에 흔적이 있는데 답이 안 다룬 항목."""
    statuses = analyze_facet_statuses(user_query, answer, hits)
    missing = [s.facet for s in statuses if s.needs_followup]

    if answer_claims_missing_in_docs(answer) and hits:
        for s in statuses:
            if s.evidence_in_hits and s.facet not in missing:
                missing.append(s.facet)

    return _filter_generic_facets(missing)[:8]


def _section_hints_for_facets(
    facets: list[str],
    hits: Sequence[SearchHit],
    answer: str,
) -> list[str]:
    """facet과 맞는데 답에 반영되지 않은 섹션 제목 조각."""
    answer_text = answer or ""
    hints: list[str] = []
    for hit in hits:
        title = str((hit.payload or {}).get("section_title") or "").strip()
        for phrase in _split_section_title_phrases(title):
            if phrase in answer_text:
                continue
            if any(f in phrase for f in facets):
                hints.append(phrase)
    return list(dict.fromkeys(hints))[:5]


def _merge_query_terms(*parts: str, max_len: int = 400) -> str:
    tokens: list[str] = []
    for part in parts:
        for token in re.split(r"\s+", (part or "").strip()):
            token = token.strip()
            if token and token not in tokens:
                tokens.append(token)
    return " ".join(tokens)[:max_len]


def _format_hits_for_followup_llm(hits: Sequence[SearchHit] | None, *, limit: int = 5) -> str:
    lines: list[str] = []
    for idx, hit in enumerate(list(hits or [])[:limit], start=1):
        payload = hit.payload or {}
        title = str(payload.get("page_title") or "").strip()
        section = str(payload.get("section_title") or "").strip()
        summary = str(payload.get("page_summary") or payload.get("text") or "").strip()
        if len(summary) > 200:
            summary = summary[:200] + "…"
        lines.append(f"[{idx}] {title} / {section}\n{summary}")
    return "\n\n".join(lines) if lines else "(없음)"


def _parse_followup_llm_output(text: str) -> WikiFollowupPlan:
    body = (text or "").strip()
    needs = False
    m_needs = _NEEDS_FOLLOWUP.search(body)
    if m_needs:
        needs = m_needs.group(1).casefold() == "yes"

    reason = ""
    m_reason = _FOLLOWUP_REASON.search(body)
    if m_reason:
        reason = m_reason.group(1).strip()

    direct, policy = extract_research_followup_queries(body)
    return WikiFollowupPlan(
        needs_research=needs,
        reason=reason,
        direct_query=direct[:400],
        policy_query=policy[:400],
    )


def _fallback_followup_queries(
    *,
    user_query: str,
    prior_answer: str,
    prior_direct: str,
    prior_policy: str,
    search_hits: Sequence[SearchHit] | None,
    missing_facets: Sequence[str] | None = None,
    reason: str = "fallback",
) -> WikiFollowupPlan:
    """LLM 실패 시 최소 키워드 fallback."""
    base = (user_query or "").strip()[:400]
    answer_text = (prior_answer or "").strip()
    facets = list(missing_facets or ()) or missing_facets_for_followup(
        base, answer_text, search_hits
    )
    section_hints = _section_hints_for_facets(facets, search_hits or [], answer_text)
    facet_blob = " ".join(facets).strip()
    section_blob = " ".join(section_hints).strip()

    direct = _merge_query_terms(base, facet_blob)
    policy = _merge_query_terms(section_blob, facet_blob, prior_policy)

    if _queries_too_similar(direct, prior_direct):
        policy = _merge_query_terms(policy, "세부 기준")
    if not direct:
        direct = base
    if not policy:
        policy = prior_policy or base

    direct, policy = enrich_wiki_queries_with_year(direct, policy, user_query=base)
    return WikiFollowupPlan(
        needs_research=True,
        reason=reason,
        direct_query=direct[:400],
        policy_query=policy[:400],
        missing_facets=tuple(facets),
    )


async def _llm_build_followup_queries(
    client: AsyncOpenAI,
    *,
    user_query: str,
    prior_answer: str,
    prior_raw: str,
    prior_direct: str,
    prior_policy: str,
    search_hits: Sequence[SearchHit] | None,
    hinted_direct: str = "",
    hinted_policy: str = "",
    trigger_reason: str = "llm_queries",
    missing_facets: Sequence[str] | None = None,
) -> WikiFollowupPlan:
    """재검색이 이미 확정된 뒤 follow-up 검색어만 LLM으로 작성 (fallback)."""
    user_block = (
        f"<user_question>\n{user_query.strip()}\n</user_question>\n\n"
        f"<prior_search>\n"
        f"direct_query: {prior_direct or '(없음)'}\n"
        f"policy_query: {prior_policy or '(없음)'}\n"
        f"</prior_search>\n\n"
        f"<first_answer_draft>\n{(prior_answer or prior_raw or '').strip()[:2000]}\n</first_answer_draft>\n\n"
        f"<first_search_results>\n{_format_hits_for_followup_llm(search_hits)}\n</first_search_results>"
    )
    if hinted_direct or hinted_policy:
        user_block += (
            "\n\n<turn2_hints>\n"
            f"direct_query: {hinted_direct or '(없음)'}\n"
            f"policy_query: {hinted_policy or '(없음)'}\n"
            "</turn2_hints>"
        )
    if missing_facets:
        user_block += (
            "\n\n<missing_facets>\n"
            + ", ".join(missing_facets)
            + "\n</missing_facets>"
        )

    try:
        raw = await responses_create_text(
            client=client,
            model=HIGH_RISK_FALLBACK_MODEL_NAME,
            messages=[
                {"role": "system", "content": _FOLLOWUP_LLM_QUERIES_ONLY_SYSTEM},
                {"role": "user", "content": user_block},
            ],
            reasoning={"effort": "lowest"},
        )
        plan = _parse_followup_llm_output(raw)
        direct = (plan.direct_query or hinted_direct).strip()
        policy = (plan.policy_query or hinted_policy).strip()
        if direct or policy:
            direct, policy = enrich_wiki_queries_with_year(
                direct or policy,
                policy or direct,
                user_query=user_query,
            )
            return WikiFollowupPlan(
                needs_research=True,
                reason=plan.reason or trigger_reason,
                direct_query=direct[:400],
                policy_query=policy[:400],
                missing_facets=tuple(missing_facets or ()),
            )
        logger.warning("[RAG] follow-up query LLM returned empty: %r", raw[:300])
    except Exception:
        logger.exception("[RAG] follow-up query LLM failed")

    return _fallback_followup_queries(
        user_query=user_query,
        prior_answer=prior_answer,
        prior_direct=prior_direct,
        prior_policy=prior_policy,
        search_hits=search_hits,
        missing_facets=missing_facets,
        reason=f"{trigger_reason}_keyword_fallback",
    )


def _plan_from_turn2_queries(
    *,
    user_query: str,
    hinted_direct: str,
    hinted_policy: str,
) -> WikiFollowupPlan:
    direct, policy = enrich_wiki_queries_with_year(
        hinted_direct,
        hinted_policy,
        user_query=user_query,
    )
    return WikiFollowupPlan(
        needs_research=True,
        reason="research_signal",
        direct_query=direct[:400],
        policy_query=policy[:400],
    )


async def plan_from_rag_gate_turn2b(
    client: AsyncOpenAI,
    *,
    raw_gate: str,
    user_query: str,
    prior_direct: str = "",
    prior_policy: str = "",
    search_hits: Sequence[SearchHit] | None = None,
) -> WikiFollowupPlan:
    """
    Turn2b gate 출력 → WikiFollowupPlan.

    needs_followup / [[WIKI_RESEARCH_NEEDED]] + direct/policy 를 파싱하고,
    쿼리가 불완전하면 검색어 보완 LLM fallback을 호출한다.
    """
    body = (raw_gate or "").strip()
    if not body:
        gaps = missing_facets_for_followup(user_query, "", search_hits)
        if gaps:
            return await _llm_build_followup_queries(
                client,
                user_query=user_query,
                prior_answer="",
                prior_raw="",
                prior_direct=prior_direct,
                prior_policy=prior_policy,
                search_hits=search_hits,
                trigger_reason="gate_empty_facet_gap",
                missing_facets=gaps,
            )
        return WikiFollowupPlan(False, "gate_empty")

    has_signal = answer_contains_research_signal(body)
    hinted_direct, hinted_policy = extract_research_followup_queries(body)
    plan = _parse_followup_llm_output(body)

    needs = plan.needs_research or has_signal
    if not needs:
        return WikiFollowupPlan(False, plan.reason or "gate_ok")

    reason = plan.reason or ("research_signal" if has_signal else "gate_followup")
    direct = (plan.direct_query or hinted_direct).strip()
    policy = (plan.policy_query or hinted_policy).strip()

    if direct and policy:
        enriched_direct, enriched_policy = enrich_wiki_queries_with_year(
            direct,
            policy,
            user_query=user_query,
        )
        return WikiFollowupPlan(
            needs_research=True,
            reason=reason,
            direct_query=enriched_direct[:400],
            policy_query=enriched_policy[:400],
            missing_facets=plan.missing_facets,
        )

    return await _llm_build_followup_queries(
        client,
        user_query=user_query,
        prior_answer="",
        prior_raw=body,
        prior_direct=prior_direct,
        prior_policy=prior_policy,
        search_hits=search_hits,
        hinted_direct=direct,
        hinted_policy=policy,
        trigger_reason=f"turn2b_{reason}",
    )


def format_used_parents_for_picker(hits: Sequence[SearchHit] | None) -> str:
    """Turn2 planner — search_results [문서 N] ↔ parent_id 매핑 (경량)."""
    lines: list[str] = []
    for idx, hit in enumerate(list(hits or []), start=1):
        doc_id = str(hit.doc_id or "").strip()
        if not doc_id:
            continue
        payload = hit.payload or {}
        section_type = str(payload.get("section_type") or "other").strip()
        lines.append(f"[used_{idx}] doc={idx} parent_id={doc_id} section_type={section_type}")
    return "\n".join(lines) if lines else "(없음)"


_PLANNER_SIGNAL_KEYWORDS: tuple[str, ...] = (
    "등급", "가등급", "나등급", "다등급", "라등급", "국가별", "도시별",
    "증빙", "정산", "서류", "영수증", "표", "양식", "별표", "부록",
    "국외", "국내", "출장", "휴직", "복직", "한도", "상한",
)

# signals에 있으면 같은 page 이웃 확장을 고려할 힌트
_PLANNER_EXPANSION_HINT_SIGNALS: frozenset[str] = frozenset({
    "등급", "가등급", "나등급", "다등급", "라등급", "국가별", "도시별", "표", "별표", "부록",
})

_PLANNER_QUERY_FOCUS_SKIP: frozenset[str] = frozenset({
    "출장", "비용", "지원", "목록", "다음주", "항목", "경비", "알려", "알려줘",
    "해외", "국내", "외근", "규정", "기준", "정리", "표로",
})


def _planner_evidence_signals(payload: dict[str, Any]) -> str:
    meta = " ".join(
        str(payload.get(key) or "")
        for key in ("section_title", "page_summary", "page_title", "title_path")
    )
    body = str(payload.get("text") or "")[:300]
    text = f"{meta} {body}"
    found: list[str] = []
    for hint in _PLANNER_SIGNAL_KEYWORDS:
        if hint in text and hint not in found:
            found.append(hint)
    return ",".join(found[:10])


def _section_id_from_parent_id(parent_id: str) -> str:
    match = re.search(r":(\d{5}):", parent_id or "")
    return match.group(1) if match else ""


def _planner_snippet_blob(payload: dict[str, Any]) -> str:
    signals = _planner_evidence_signals(payload)
    return " ".join([
        str(payload.get("section_title") or ""),
        str(payload.get("page_title") or ""),
        str(payload.get("page_summary") or "")[:200],
        signals,
    ])


def _expansion_hint_signals(payload: dict[str, Any]) -> list[str]:
    signals = _planner_evidence_signals(payload)
    return [
        s for s in signals.split(",")
        if s and s in _PLANNER_EXPANSION_HINT_SIGNALS
    ]


def _page_ids_with_expansion_hint(hits: Sequence[SearchHit] | None) -> list[str]:
    pages: set[str] = set()
    for hit in hits or []:
        payload = hit.payload or {}
        if not _expansion_hint_signals(payload):
            continue
        page_id = str(payload.get("page_id") or "").strip()
        if page_id:
            pages.add(page_id)
    return sorted(pages)


def _apply_expansion_signal_hint(
    plan: EvidencePlannerPlan,
    *,
    initial_hits: Sequence[SearchHit] | None,
    user_query: str,
) -> EvidencePlannerPlan:
    """signals 힌트(등급·표 등) + 질문 엔티티 누락 시 같은 page 확장."""
    if plan.decision != "answerable" or not plan.parse_ok:
        return plan
    hit_list = list(initial_hits or [])
    page_ids = _page_ids_with_expansion_hint(hit_list)
    if not page_ids:
        return plan

    focus = [
        t for t in _extract_user_focus_terms(user_query)
        if t not in _PLANNER_QUERY_FOCUS_SKIP
    ]
    if not focus:
        return plan

    blob = " ".join(_planner_snippet_blob(h.payload or {}) for h in hit_list)
    missing = [t for t in focus if t not in blob]
    if not missing:
        return plan

    return EvidencePlannerPlan(
        decision="need_parent_expansion",
        page_ids_for_expansion=tuple(page_ids),
        missing_evidence=tuple(missing),
        parse_ok=True,
    )


def format_planner_search_context(hits: Sequence[SearchHit] | None) -> str:
    """Turn2 planner — 식별자·섹션명·signals만 (본문·summary 제외)."""
    lines: list[str] = []
    seen_parents: set[str] = set()
    doc_num = 0
    for hit in hits or []:
        parent_id = str(hit.doc_id or "").strip()
        if not parent_id or parent_id in seen_parents:
            continue
        seen_parents.add(parent_id)
        doc_num += 1
        payload = hit.payload or {}
        page_id = str(payload.get("page_id") or "").strip()
        section_title = str(payload.get("section_title") or "").strip()
        section_type = str(payload.get("section_type") or "other").strip()
        section_id = _section_id_from_parent_id(parent_id)
        signals = _planner_evidence_signals(payload)

        block = [f"[doc={doc_num}] page_id={page_id} parent_id={parent_id}"]
        if section_id:
            block.append(f"section_id={section_id} section_type={section_type}")
        if section_title:
            block.append(f"section_title={section_title}")
        if signals:
            block.append(f"signals={signals}")
        lines.append("\n".join(block))
    return "\n\n".join(lines) if lines else "(없음)"


_EVIDENCE_PLANNER_DECISIONS = frozenset({
    "answerable",
    "need_parent_expansion",
    "need_external_search",
})


def _extract_json_object(text: str) -> dict[str, Any]:
    body = (text or "").strip()
    if not body:
        return {}

    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", body, re.DOTALL | re.IGNORECASE)
    if fence:
        body = fence.group(1).strip()
    else:
        brace = re.search(r"\{.*\}", body, re.DOTALL)
        if brace:
            body = brace.group(0).strip()

    parsed = json.loads(body)
    return parsed if isinstance(parsed, dict) else {}


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.strip()
        if not raw or raw.casefold() in ("none", "-", "없음"):
            return []
        return [part.strip() for part in re.split(r"[,;]+", raw) if part.strip()]
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if text and text not in out:
                out.append(text)
        return out
    return []


def _query_from_planner_field(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.casefold() in ("none", "-", "없음", "null"):
        return ""
    return text


def parse_evidence_planner_output(raw: str) -> EvidencePlannerPlan:
    """Turn2 evidence planner JSON 파싱 (검증 전 raw)."""
    body = (raw or "").strip()
    if not body:
        logger.warning("[RAG] evidence planner empty output")
        return EvidencePlannerPlan(decision="answerable", parse_ok=False)

    try:
        data = _extract_json_object(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("[RAG] evidence planner JSON parse failed")
        return EvidencePlannerPlan(decision="answerable", parse_ok=False)

    if not data or "decision" not in data:
        logger.warning("[RAG] evidence planner missing decision field")
        return EvidencePlannerPlan(decision="answerable", parse_ok=False)

    decision = str(data.get("decision") or "").strip().casefold()
    if decision not in _EVIDENCE_PLANNER_DECISIONS:
        logger.warning("[RAG] evidence planner unknown decision: %s", decision)
        return EvidencePlannerPlan(decision="answerable", parse_ok=False)

    queries = data.get("external_search_queries")
    if not isinstance(queries, dict):
        queries = {}

    return EvidencePlannerPlan(
        decision=decision,
        page_ids_for_expansion=tuple(_as_str_list(data.get("page_ids_for_expansion"))),
        direct_query=_query_from_planner_field(queries.get("direct_query")),
        policy_query=_query_from_planner_field(queries.get("policy_query")),
        missing_evidence=tuple(_as_str_list(data.get("missing_evidence"))),
        parse_ok=True,
    )


def _page_ids_from_hits(hits: Sequence[SearchHit] | None) -> list[str]:
    return sorted({
        str((h.payload or {}).get("page_id") or "").strip()
        for h in (hits or [])
        if str((h.payload or {}).get("page_id") or "").strip()
    })


def normalize_evidence_planner_plan(
    plan: EvidencePlannerPlan,
    *,
    initial_hits: Sequence[SearchHit] | None,
    user_query: str = "",
) -> EvidencePlannerPlan:
    """planner 출력 정규화 — page_id 검증·연도 보정만 (decision은 LLM 그대로)."""
    if not plan.parse_ok:
        return EvidencePlannerPlan(
            decision="answerable",
            missing_evidence=plan.missing_evidence,
            parse_ok=False,
        )

    hit_page_ids = _page_ids_from_hits(initial_hits)

    if plan.decision == "need_parent_expansion":
        page_ids = [p for p in plan.page_ids_for_expansion if p in hit_page_ids]
        if not page_ids and hit_page_ids:
            page_ids = hit_page_ids
        return EvidencePlannerPlan(
            decision="need_parent_expansion",
            page_ids_for_expansion=tuple(page_ids),
            missing_evidence=plan.missing_evidence,
            parse_ok=True,
        )

    if plan.decision == "need_external_search":
        direct = plan.direct_query
        policy = plan.policy_query
        if direct or policy:
            direct, policy = enrich_wiki_queries_with_year(
                direct or policy,
                policy or direct,
                user_query=user_query,
            )
        return EvidencePlannerPlan(
            decision="need_external_search",
            direct_query=direct[:400],
            policy_query=policy[:400],
            missing_evidence=plan.missing_evidence,
            parse_ok=True,
        )

    normalized = EvidencePlannerPlan(
        decision=plan.decision,
        page_ids_for_expansion=plan.page_ids_for_expansion,
        direct_query=plan.direct_query,
        policy_query=plan.policy_query,
        missing_evidence=plan.missing_evidence,
        parse_ok=True,
    )
    return _apply_expansion_signal_hint(
        normalized,
        initial_hits=initial_hits,
        user_query=user_query,
    )


def materialize_evidence_from_planner(
    plan: EvidencePlannerPlan,
    *,
    initial_hits: Sequence[SearchHit],
) -> list[SearchHit]:
    """need_parent_expansion → used page 기준 이웃 섹션 확장."""
    hit_list = list(initial_hits or [])
    if plan.decision != "need_parent_expansion" or not hit_list:
        return hit_list

    page_set = set(plan.page_ids_for_expansion) if plan.page_ids_for_expansion else None
    scoped = hit_list
    if page_set:
        scoped = [
            h for h in hit_list
            if str((h.payload or {}).get("page_id") or "").strip() in page_set
        ] or hit_list

    expanded = expand_search_hits_with_page_neighbors(scoped)
    return expanded or hit_list


def planner_to_followup_plan(plan: EvidencePlannerPlan) -> WikiFollowupPlan:
    return WikiFollowupPlan(
        needs_research=True,
        reason="evidence_planner_external_search",
        direct_query=plan.direct_query,
        policy_query=plan.policy_query,
        missing_facets=plan.missing_evidence,
    )


def parse_parent_picker_output(
    text: str,
    *,
    valid_ids: set[str],
) -> ParentPickerPlan:
    body = (text or "").strip()
    reason = ""
    m_reason = _FOLLOWUP_REASON.search(body)
    if m_reason:
        reason = m_reason.group(1).strip()

    needs_ext = False
    m_ext = _NEEDS_EXTERNAL_SEARCH.search(body)
    if m_ext:
        needs_ext = m_ext.group(1).casefold() == "yes"

    selected: list[str] = []
    m_sel = _SELECTED_PARENT_IDS.search(body)
    if m_sel:
        raw = m_sel.group(1).strip()
        if raw.casefold() not in ("none", "-", "없음", ""):
            for part in re.split(r"[,;]+", raw):
                pid = part.strip().strip("'\"")
                if pid in valid_ids and pid not in selected:
                    selected.append(pid)

    for line in body.splitlines():
        m_line = re.match(r"^\s*[-*]?\s*parent_id:\s*(.+)$", line, re.IGNORECASE)
        if not m_line:
            continue
        pid = m_line.group(1).strip().strip("'\"")
        if pid in valid_ids and pid not in selected:
            selected.append(pid)

    direct, policy = extract_research_followup_queries(body)
    return ParentPickerPlan(
        selected_parent_ids=tuple(selected),
        reason=reason,
        needs_external_search=needs_ext,
        direct_query=direct,
        policy_query=policy,
    )

def cap_picker_parents_per_page(
    parent_ids: Sequence[str],
    catalog: Sequence[dict] | None,
    *,
    max_per_page: int,
    prefer_ids: Sequence[str] | None = None,
) -> tuple[str, ...]:
    if max_per_page <= 0:
        return tuple(parent_ids)

    page_by_id: dict[str, str] = {}
    for item in catalog or []:
        pid = str(item.get("id") or "").strip()
        page = str(item.get("page_id") or "").strip()
        if pid and page:
            page_by_id[pid] = page

    ordered: list[str] = []
    for pid in prefer_ids or ():
        if pid and pid not in ordered:
            ordered.append(pid)
    for pid in parent_ids:
        if pid not in ordered:
            ordered.append(pid)

    per_page: dict[str, int] = {}
    capped: list[str] = []
    for pid in ordered:
        page = page_by_id.get(pid, "__unknown__")
        if per_page.get(page, 0) >= max_per_page:
            continue
        capped.append(pid)
        per_page[page] = per_page.get(page, 0) + 1
    return tuple(capped)


async def resolve_parent_picker_turn2b(
    client: AsyncOpenAI,
    *,
    raw_picker: str,
    user_query: str,
    used_hits: Sequence[SearchHit] | None,
    catalog: Sequence[dict] | None,
    prior_direct: str = "",
    prior_policy: str = "",
    max_per_page: int | None = None,
) -> ParentPickerPlan | WikiFollowupPlan:
    """Turn2b picker 출력 → ParentPickerPlan 또는 재검색 WikiFollowupPlan."""
    valid_ids = {
        str(item.get("id") or "").strip()
        for item in (catalog or [])
        if str(item.get("id") or "").strip()
    }
    plan = parse_parent_picker_output(raw_picker, valid_ids=valid_ids)
    prefer = [str(h.doc_id) for h in (used_hits or []) if h.doc_id]
    capped = cap_picker_parents_per_page(
        plan.selected_parent_ids,
        catalog,
        max_per_page=max_per_page or RAG_PICKER_MAX_PARENTS_PER_PAGE,
        prefer_ids=prefer,
    )

    if capped:
        return ParentPickerPlan(
            selected_parent_ids=capped,
            reason=plan.reason or "parent_picker",
            needs_external_search=False,
            direct_query=plan.direct_query,
            policy_query=plan.policy_query,
        )

    if plan.direct_query or plan.policy_query:
        direct, policy = enrich_wiki_queries_with_year(
            plan.direct_query or plan.policy_query,
            plan.policy_query or plan.direct_query,
            user_query=user_query,
        )
        return WikiFollowupPlan(
            needs_research=True,
            reason=plan.reason or "picker_empty",
            direct_query=direct[:400],
            policy_query=policy[:400],
        )

    return await _llm_build_followup_queries(
        client,
        user_query=user_query,
        prior_answer="",
        prior_raw=raw_picker,
        prior_direct=prior_direct,
        prior_policy=prior_policy,
        search_hits=used_hits,
        trigger_reason=plan.reason or "picker_empty",
    )


async def resolve_wiki_followup(
    client: AsyncOpenAI,
    *,
    user_query: str,
    prior_answer: str = "",
    prior_raw: str = "",
    prior_direct: str = "",
    prior_policy: str = "",
    search_hits: Sequence[SearchHit] | None = None,
    already_researched: bool = False,
) -> WikiFollowupPlan:
    """
    Turn2 우선 + LLM fallback.

    1. Turn2 [[WIKI_RESEARCH_NEEDED]] + direct/policy → 그대로 사용 (LLM 없음)
    2. Turn2 신호만 있거나 쿼리 일부만 있음 → LLM으로 검색어만 보완
    3. 신호 없음 → facet_gap 휴리스틱으로 재검색 여부만 판단, 필요 시 LLM으로 검색어 작성
    """
    if not RAG_RESEARCH_ENABLED or already_researched:
        return WikiFollowupPlan(False, "disabled" if not RAG_RESEARCH_ENABLED else "already_researched")

    context_text = "\n".join(t for t in (prior_raw, prior_answer) if (t or "").strip())
    probe = (prior_answer or prior_raw or "").strip()
    has_signal = answer_contains_research_signal(context_text)
    hinted_direct, hinted_policy = extract_research_followup_queries(context_text)

    # 1) Turn2 primary — 신호 + 검색어 완비
    if has_signal and hinted_direct and hinted_policy:
        return _plan_from_turn2_queries(
            user_query=user_query,
            hinted_direct=hinted_direct,
            hinted_policy=hinted_policy,
        )

    # 2) Turn2 신호 — 재검색 확정, 검색어가 불완전하면 LLM fallback
    if has_signal:
        return await _llm_build_followup_queries(
            client,
            user_query=user_query,
            prior_answer=prior_answer,
            prior_raw=prior_raw,
            prior_direct=prior_direct,
            prior_policy=prior_policy,
            search_hits=search_hits,
            hinted_direct=hinted_direct,
            hinted_policy=hinted_policy,
            trigger_reason="research_signal_queries_llm",
        )

    # 3) Turn2 신호 없음 — 휴리스틱으로 재검색 필요 여부만 판단
    gaps = missing_facets_for_followup(user_query, probe, search_hits)
    if not gaps:
        return WikiFollowupPlan(False, "ok")

    # 4) facet_gap — 검색어는 LLM fallback (실패 시 키워드 fallback)
    return await _llm_build_followup_queries(
        client,
        user_query=user_query,
        prior_answer=prior_answer,
        prior_raw=prior_raw,
        prior_direct=prior_direct,
        prior_policy=prior_policy,
        search_hits=search_hits,
        trigger_reason=f"facet_gap:{','.join(gaps[:4])}",
        missing_facets=gaps,
    )


def should_retry_wiki_search(
    *,
    answer: str,
    raw: str = "",
    already_researched: bool,
    user_query: str = "",
    search_hits: Sequence[SearchHit] | None = None,
) -> RagResearchDecision:
    """동기 휴리스틱 — resolve_wiki_followup 사용을 권장."""
    if not RAG_RESEARCH_ENABLED:
        return RagResearchDecision(False, "disabled")
    if already_researched:
        return RagResearchDecision(False, "already_researched")

    probe = (answer or raw or "").strip()
    if answer_contains_research_signal(probe):
        return RagResearchDecision(True, "research_signal")

    if search_hits and user_query:
        gaps = missing_facets_for_followup(user_query, probe, search_hits)
        if gaps:
            return RagResearchDecision(
                True,
                f"facet_gap:{','.join(gaps[:4])}",
                missing_facets=tuple(gaps),
            )

    return RagResearchDecision(False, "ok")


def build_followup_wiki_queries(
    *,
    user_query: str,
    prior_direct: str = "",
    prior_policy: str = "",
    prior_answer: str = "",
    prior_raw: str = "",
    search_hits: Sequence[SearchHit] | None = None,
    missing_facets: Sequence[str] | None = None,
) -> tuple[str, str]:
    """동기 fallback — resolve_wiki_followup(async) 사용을 권장."""
    plan = _fallback_followup_queries(
        user_query=user_query,
        prior_answer=prior_answer,
        prior_direct=prior_direct,
        prior_policy=prior_policy,
        search_hits=search_hits,
        missing_facets=missing_facets,
    )
    return plan.direct_query, plan.policy_query


def merge_wiki_search_docs(
    primary: Sequence[SearchHit],
    secondary: Sequence[SearchHit],
) -> list[SearchHit]:
    combined = list(primary) + list(secondary)
    if not combined:
        return []

    seen: set[str] = set()
    deduped: list[SearchHit] = []
    for hit in sorted(combined, key=lambda h: float(h.score or 0.0), reverse=True):
        doc_id = (hit.doc_id or "").strip()
        if not doc_id or doc_id in seen:
            continue
        seen.add(doc_id)
        deduped.append(hit)

    return select_diverse_parents(
        deduped,
        max_total=RAG_MAX_PARENTS,
        min_pages=RAG_MIN_PAGES,
        max_per_page=RAG_MAX_PARENTS_PER_PAGE,
    )


def expand_search_hits_with_page_neighbors(
    hits: Sequence[SearchHit],
    *,
    neighbor_radius: int | None = None,
    max_total: int | None = None,
    max_per_page: int | None = None,
) -> list[SearchHit]:
    """Turn3 — seed parent 기준 같은 page catalog ±radius 이웃 mget."""
    hit_list = list(hits or [])
    if not hit_list:
        return []

    page_ids = page_ids_from_search_hits(hit_list)
    catalog = fetch_page_parent_catalog(page_ids)
    if not catalog:
        return hit_list

    seed_ids = [str(h.doc_id) for h in hit_list if h.doc_id]
    expanded_ids = expand_parent_ids_by_page_neighbors(
        seed_ids,
        catalog,
        neighbor_radius=neighbor_radius if neighbor_radius is not None else RAG_EXPAND_NEIGHBOR_RADIUS,
        max_total=max_total if max_total is not None else RAG_EXPAND_MAX_PARENTS,
        max_per_page=max_per_page if max_per_page is not None else RAG_EXPAND_MAX_PARENTS_PER_PAGE,
    )
    if not expanded_ids or expanded_ids == seed_ids:
        return hit_list

    score_by_id = {str(h.doc_id): float(h.score or 0.0) for h in hit_list if h.doc_id}
    expanded_hits = search_hits_from_parent_ids(
        expanded_ids,
        score_by_id=score_by_id,
    )
    return expanded_hits or hit_list


def merged_doc_ids(docs: Sequence[SearchHit]) -> set[str]:
    return {(d.doc_id or "").strip() for d in docs if (d.doc_id or "").strip()}


def log_rag_research(
    *,
    decision: RagResearchDecision | WikiFollowupPlan,
    followup_direct: str,
    followup_policy: str,
    primary_count: int,
    secondary_count: int,
    merged_count: int,
    new_doc_count: int,
) -> None:
    reason = getattr(decision, "reason", "")
    facets = getattr(decision, "missing_facets", ())
    logger.info(
        "[RAG] re-search. reason=%s, facets=%s, followup_direct=%r, followup_policy=%r, "
        "primary=%s, secondary=%s, merged=%s, new_docs=%s",
        reason,
        list(facets),
        followup_direct,
        followup_policy,
        primary_count,
        secondary_count,
        merged_count,
        new_doc_count,
    )
