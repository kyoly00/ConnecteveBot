"""Turn2 evidence planner JSON 파싱·정규화 테스트."""

import sys
from pathlib import Path

_CONN_BOT = Path(__file__).resolve().parents[1]
_ROOT = Path(__file__).resolve().parents[2]
for _path in (_ROOT, _CONN_BOT):
    _s = str(_path)
    if _s not in sys.path:
        sys.path.insert(0, _s)

from app.core.config import (
    get_rag_evidence_planner_json_schema,
    get_rag_evidence_planner_text_format,
)
from app.services.rag_research import (
    EvidencePlannerPlan,
    format_planner_search_context,
    normalize_evidence_planner_plan,
    parse_evidence_planner_output,
)


def test_evidence_planner_text_format_schema():
    schema = get_rag_evidence_planner_json_schema()
    text_format = get_rag_evidence_planner_text_format()
    assert text_format["format"]["type"] == "json_schema"
    assert text_format["format"]["strict"] is True
    assert "answerable" in schema["properties"]["decision"]["enum"]
    assert "external_search_queries" in schema["required"]


def test_parse_evidence_planner_json():
    raw = """
    {
      "decision": "need_parent_expansion",
      "page_ids_for_expansion": ["pg1"],
      "external_search_queries": {
        "direct_query": "none",
        "policy_query": "none"
      },
      "missing_evidence": ["등급표"]
    }
    """
    plan = parse_evidence_planner_output(raw)
    assert plan.parse_ok is True
    assert plan.decision == "need_parent_expansion"
    assert plan.page_ids_for_expansion == ("pg1",)
    assert plan.missing_evidence == ("등급표",)


def test_parse_empty_planner_output():
    plan = parse_evidence_planner_output("")
    assert plan.parse_ok is False


def test_normalize_invalid_planner_keeps_answerable():
    from app.rag.vectordb import SearchHit

    hits = [
        SearchHit(
            doc_id="p1",
            payload={"page_id": "pg1", "section_title": "출장 등급", "text": "가등급"},
            score=0.9,
        ),
    ]
    plan = EvidencePlannerPlan(decision="answerable", parse_ok=False)
    normalized = normalize_evidence_planner_plan(
        plan,
        initial_hits=hits,
        user_query="스페인 출장",
    )
    assert normalized.decision == "answerable"
    assert normalized.parse_ok is False


def test_normalize_answerable_unchanged():
    from app.rag.vectordb import SearchHit

    hits = [
        SearchHit(
            doc_id="773554177:parent_section:parent__007:00007:abc",
            payload={
                "page_id": "773554177",
                "section_title": "국외출장 예산표",
                "text": "가등급 나등급",
            },
            score=0.9,
        ),
    ]
    plan = EvidencePlannerPlan(decision="answerable", parse_ok=True)
    normalized = normalize_evidence_planner_plan(
        plan,
        initial_hits=hits,
        user_query="스페인 출장 경비 지원 항목",
    )
    assert normalized.decision == "need_parent_expansion"
    assert normalized.page_ids_for_expansion == ("773554177",)
    assert "스페인" in normalized.missing_evidence


def test_normalize_answerable_no_hint_stays_answerable():
    from app.rag.vectordb import SearchHit

    hits = [
        SearchHit(
            doc_id="p1",
            payload={
                "page_id": "773554177",
                "section_title": "스페인 출장 안내",
                "text": "스페인 마드리드",
            },
            score=0.9,
        ),
    ]
    plan = EvidencePlannerPlan(decision="answerable", parse_ok=True)
    normalized = normalize_evidence_planner_plan(
        plan,
        initial_hits=hits,
        user_query="스페인 출장 경비",
    )
    assert normalized.decision == "answerable"


def test_format_planner_search_context_dedup():
    from app.rag.vectordb import SearchHit

    hits = [
        SearchHit(
            doc_id="same_parent",
            payload={"page_id": "pg1", "section_title": "A", "page_summary": "요약1"},
            score=0.9,
        ),
        SearchHit(
            doc_id="same_parent",
            payload={"page_id": "pg1", "section_title": "A", "page_summary": "요약1"},
            score=0.8,
        ),
        SearchHit(
            doc_id="other_parent",
            payload={"page_id": "pg1", "section_title": "B", "page_summary": "요약2"},
            score=0.7,
        ),
    ]
    ctx = format_planner_search_context(hits)
    assert ctx.count("[doc=") == 2
    assert "same_parent" in ctx
    assert "other_parent" in ctx


def test_format_planner_search_context_minimal():
    from app.rag.vectordb import SearchHit

    hits = [
        SearchHit(
            doc_id="p1",
            payload={
                "page_id": "773554177",
                "section_title": "운임",
                "page_title": "출장 및 외근 규칙",
                "page_summary": "긴 요약 " * 50,
                "title_path": "Home > Ground Rule > 출장",
                "text": "가등급 180만원",
            },
            score=0.9,
        ),
    ]
    ctx = format_planner_search_context(hits)
    assert "page_id=773554177" in ctx
    assert "section_title=운임" in ctx
    assert "summary=" not in ctx
    assert "title_path=" not in ctx
    assert "page_title=" not in ctx


def test_normalize_parent_expansion_page_ids():
    from app.rag.vectordb import SearchHit

    hits = [
        SearchHit(
            doc_id="p1",
            payload={"page_id": "pg1"},
            score=0.9,
        ),
    ]
    plan = EvidencePlannerPlan(
        decision="need_parent_expansion",
        page_ids_for_expansion=("pg1",),
    )
    normalized = normalize_evidence_planner_plan(
        plan,
        initial_hits=hits,
    )
    assert normalized.decision == "need_parent_expansion"
    assert normalized.page_ids_for_expansion == ("pg1",)


def test_normalize_external_search_empty_queries_unchanged():
    plan = EvidencePlannerPlan(
        decision="need_external_search",
        direct_query="",
        policy_query="",
    )
    normalized = normalize_evidence_planner_plan(
        plan,
        initial_hits=[],
        user_query="도쿄 출장 등급",
    )
    assert normalized.decision == "need_external_search"
    assert normalized.direct_query == ""
    assert normalized.policy_query == ""
