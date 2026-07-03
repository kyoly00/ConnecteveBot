"""L1·L2(rule)·L4·L6·L7 규칙 기반 스코어러."""

from __future__ import annotations

import re
from typing import Any

from ..models import EvalCase, LayerScore

# expected_intent → 실제 intent 허용 집합
INTENT_ALIASES: dict[str, set[str]] = {
    "general": {"general"},
    "rag": {"rag", "rag_no_docs"},
    "gov_project": {"gov_project", "gov_project_no_data"},
    "flex_schedule": {"flex_schedule", "flex_schedule_no_data"},
    "room_schedule": {"room_schedule", "room_schedule_no_data"},
    "expense_archive": {"expense_archive", "expense_archive_no_data"},
}

CONFIRM_PATTERNS = [
    r"알려\s*주",
    r"지정\s*해\s*주",
    r"키워드.*(알려|지정|입력)",
    r"페이지명",
    r"확인\s*해\s*주",
    r"무엇을\s*원하",
    r"어떤\s*정보를",
    r"추가로\s*확인",
]

INTERNAL_VAR_PATTERNS = [
    r"\b[a-z_]{3,}=",
    r"\bpayload\b",
    r"\bdoc_id\b",
    r"\bparent_id\b",
    r"\bsession_id\b",
    r"\btool_call",
    r"\bfunction\.",
]

OPTION_PATTERNS = [
    r"다음\s*중",
    r"선택\s*해\s*주",
    r"A\)|B\)|C\)",
    r"①|②|③",
]

WIKI_DEFLECT_PATTERNS = [
    r"위키\s*검색.*필요",
    r"검색\s*결과.*제공.*않",
    r"위키.*찾.*수\s*없",
    r"키워드.*지정",
    r"페이지명.*알려",
]

DEFAULT_THRESHOLDS = {
    "L1_routing": 1.0,
    "L2_rules": 1.0,
    "L4_research": 1.0,
    "L6_citation": 0.5,
    "L7_latency_sec": 30.0,
    "L7_token_total": 50_000,
}


def _has_markdown_table(text: str) -> bool:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    pipe_lines = [ln for ln in lines if ln.count("|") >= 2]
    if len(pipe_lines) < 2:
        return False
    return any("---" in ln or re.search(r"\|[\s\-:]+\|", ln) for ln in pipe_lines)


def _matches_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def score_l1_routing(case: EvalCase) -> LayerScore:
    expected = (case.expected_intent or "").strip()
    actual = (case.intent or "").strip()
    allowed = INTENT_ALIASES.get(expected, {expected} if expected else set())
    passed = not expected or actual in allowed
    return LayerScore(
        layer="L1_routing",
        score=1.0 if passed else 0.0,
        passed=passed,
        details={
            "expected_intent": expected,
            "actual_intent": actual,
            "allowed": sorted(allowed),
        },
    )


def _check_pass_hint(hint: str, case: EvalCase) -> tuple[bool, str]:
    answer = case.answer or ""
    summary = case.eval_summary or {}

    if hint == "docs>0":
        ok = case.docs_count > 0
        return ok, f"docs_count={case.docs_count}"
    if hint == "docs=0":
        ok = case.docs_count == 0
        return ok, f"docs_count={case.docs_count}"
    if hint == "has_table":
        ok = _has_markdown_table(answer) or bool(summary.get("has_table"))
        return ok, "markdown_table"
    if hint == "has_links":
        ok = "http" in answer or bool(summary.get("sources_used"))
        return ok, "links_or_sources"
    if hint == "no_confirm":
        ok = not _matches_any(answer, CONFIRM_PATTERNS)
        return ok, "no_reverse_question"
    if hint == "no_internal_vars":
        ok = not _matches_any(answer, INTERNAL_VAR_PATTERNS)
        return ok, "no_internal_exposure"
    if hint == "no_options":
        ok = not _matches_any(answer, OPTION_PATTERNS)
        return ok, "no_option_list"
    if hint == "capability":
        ok = len(answer.strip()) > 20
        return ok, "non_empty_capability_answer"
    if hint == "general_knowledge":
        ok = len(answer.strip()) > 10
        return ok, "non_empty_general_answer"
    if hint == "no_wiki":
        ok = not _matches_any(answer, WIKI_DEFLECT_PATTERNS)
        return ok, "no_wiki_deflection"
    if hint == "no_flex":
        ok = "flex" not in answer.lower() or "위키" in answer
        return ok, "flex_not_primary_when_forbidden"
    if hint == "no_room":
        ok = "회의실" in answer or case.intent.startswith("room")
        return ok, "room_context_when_needed"

    # tool_* / parallel / param hints → DeepEval(L2)에서 처리
    return True, f"deferred:{hint}"


def score_l2_rules(case: EvalCase) -> LayerScore:
    """pass_hint 중 규칙으로 판정 가능한 항목만."""
    rule_hints = [
        h
        for h in case.pass_hints
        if not h.startswith("tool_")
        and h
        not in {
            "parallel_tools",
            "flex_today",
            "flex_date",
            "flex_year_month",
            "gov_list",
            "gov_detail",
            "gov_files",
            "gov_no_wiki",
            "room_list_mine",
            "room_check",
            "room_check_all",
            "room_book",
            "room_no_check",
            "wiki_no_flex",
            "wiki_direct_policy",
        }
    ]

    if not rule_hints:
        return LayerScore(
            layer="L2_rules",
            score=1.0,
            passed=True,
            details={"skipped": "no_rule_hints"},
        )

    checks: dict[str, Any] = {}
    failures: list[str] = []
    for hint in rule_hints:
        ok, note = _check_pass_hint(hint, case)
        checks[hint] = {"passed": ok, "note": note}
        if not ok:
            failures.append(hint)

    passed = not failures
    score = 1.0 - (len(failures) / max(len(rule_hints), 1))
    return LayerScore(
        layer="L2_rules",
        score=round(score, 4),
        passed=passed,
        details={"checks": checks, "failures": failures},
    )


def score_l4_research(case: EvalCase) -> LayerScore:
    """재검색 루프 유용성 — 트리거 시 new_doc 또는 answer 길이 증가."""
    summary = case.eval_summary or {}
    researched = bool(summary.get("rag_researched"))
    meta = case.rag_research or summary.get("rag_research") or {}

    if not researched:
        return LayerScore(
            layer="L4_research",
            score=1.0,
            passed=True,
            details={"skipped": "no_rag_research"},
        )

    new_docs = int(meta.get("new_doc_count") or 0)
    before_len = int(meta.get("answer_before_len") or 0)
    after_len = int(meta.get("answer_after_len") or len(case.answer))
    useful = new_docs > 0 or after_len > before_len + 20

    return LayerScore(
        layer="L4_research",
        score=1.0 if useful else 0.0,
        passed=useful,
        details={
            "trigger_reason": meta.get("trigger_reason") or summary.get("research_reason"),
            "new_doc_count": new_docs,
            "answer_before_len": before_len,
            "answer_after_len": after_len,
            "missing_facets": meta.get("missing_facets") or summary.get("missing_facets"),
        },
    )


def score_l6_citation(case: EvalCase) -> LayerScore:
    if "has_links" not in case.pass_hints:
        return LayerScore(
            layer="L6_citation",
            score=None,
            passed=True,
            details={"skipped": "not_in_pass_hint"},
        )

    summary = case.eval_summary or {}
    sources = summary.get("sources_used") or []
    parent_ids = summary.get("parent_ids") or []

    cited = bool(sources) or bool(parent_ids) or "http" in (case.answer or "")
    return LayerScore(
        layer="L6_citation",
        score=1.0 if cited else 0.0,
        passed=cited,
        details={
            "sources_used": sources,
            "parent_ids_count": len(parent_ids),
        },
    )


def score_l7_ops(case: EvalCase, *, max_latency_sec: float = 30.0) -> LayerScore:
    summary = case.eval_summary or {}
    timings = case.timings or summary.get("timings") or {}
    token_usage = case.token_usage or summary.get("token_usage") or {}
    totals = token_usage.get("totals") or {}
    total_tokens = int(totals.get("total_tokens") or 0)
    total_sec = float(timings.get("total_sec") or case.latency_sec or 0.0)

    latency_ok = total_sec <= max_latency_sec
    token_ok = total_tokens == 0 or total_tokens <= DEFAULT_THRESHOLDS["L7_token_total"]

    passed = latency_ok and token_ok
    return LayerScore(
        layer="L7_ops",
        score=1.0 if passed else 0.5,
        passed=passed,
        details={
            "total_sec": total_sec,
            "max_latency_sec": max_latency_sec,
            "total_tokens": total_tokens,
            "latency_ok": latency_ok,
            "token_ok": token_ok,
        },
    )


def score_all_rules(case: EvalCase) -> list[LayerScore]:
    return [
        score_l1_routing(case),
        score_l2_rules(case),
        score_l4_research(case),
        score_l6_citation(case),
        score_l7_ops(case),
    ]
