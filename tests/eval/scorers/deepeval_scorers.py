"""L2(proxy)·L5·L7 DeepEval GEval 스코어러."""

from __future__ import annotations

from typing import Any

from ..models import EvalCase, LayerScore

DEEPEVAL_THRESHOLD = 0.7

# pass_hint → GEval criteria (Turn2 답변만 판정)
HINT_CRITERIA: dict[str, str] = {
    "tool_wiki": (
        "답변이 사내 위키/규정/가이드 문서 근거로 질문에 실질적으로 답했는가. "
        "'위키 검색이 필요합니다', '키워드를 알려주세요', '페이지명을 지정해 주세요' 같은 "
        "회피·역질문만 하면 실패."
    ),
    "tool_gov": (
        "답변이 정부 지원사업/브리핑 정보를 실제로 제공했는가. "
        "gov 도구 없이 wiki만 언급하거나 '공고가 없습니다'만 반복하면 실패."
    ),
    "tool_flex": (
        "답변이 Flex 근태/일정 조회 결과를 포함하는가. "
        "wiki 규정 설명만 하고 근태 조회를 하지 않으면 실패."
    ),
    "tool_room": (
        "답변이 회의실 일정/예약 관련 결과를 제공하는가."
    ),
    "tool_general": (
        "답변이 일반 대화/능력 안내로 적절히 응답했는가. "
        "불필요하게 wiki/gov/flex/room 도구를 요구하지 않아야 한다."
    ),
    "wiki_no_flex": (
        "질문이 Flex 메뉴/절차/신청 경로 등 위키 내용인데, "
        "답변이 Flex 근태 조회 결과만 제공하고 위키 절차를 설명하지 않으면 실패."
    ),
    "gov_no_wiki": (
        "질문이 사내 운영·정산·증빙 등 내부 wiki 주제인데, "
        "답변이 외부 gov 브리핑만 제공하고 사내 wiki 근거가 없으면 실패."
    ),
    "parallel_tools": (
        "질문에 독립적인 하위 요청이 2개 이상 있는데, "
        "답변이 모든 하위 요청에 대해 실질적 내용을 포함했는가. "
        "한쪽만 답하고 다른 쪽은 '검색 결과 없음'·역질문만 하면 실패."
    ),
}

TASK_SUCCESS_CRITERIA = (
    "ConnBot 내부 QA 기준으로 이 답변이 사용자 질문에 실용적으로 충족되는가. "
    "핵심 정보가 빠졌거나, 역질문·확인 요청으로 끝나거나, "
    "요청 형식(표·목록)을 무시했으면 실패."
)


def _make_geval(name: str, criteria: str, *, model: str, with_context: bool = False):
    from deepeval.metrics import GEval
    from deepeval.test_case import SingleTurnParams

    params = [SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT]
    if with_context:
        params.append(SingleTurnParams.RETRIEVAL_CONTEXT)

    return GEval(
        name=name,
        criteria=criteria,
        evaluation_params=params,
        model=model,
        threshold=DEEPEVAL_THRESHOLD,
    )


def _compact_context(case: EvalCase) -> list[str]:
    if case.retrieval_contexts:
        return case.retrieval_contexts[:8]
    return []


def score_deepeval_case(
    case: EvalCase,
    *,
    judge_model: str = "gpt-4o-mini",
) -> list[LayerScore]:
    from deepeval.test_case import LLMTestCase

    layers: list[LayerScore] = []

    # L2 proxy: pass_hint의 tool_* / routing hints
    proxy_hints = [
        h
        for h in case.pass_hints
        if h in HINT_CRITERIA
    ]
    if proxy_hints:
        checks: dict[str, Any] = {}
        scores: list[float] = []
        for hint in proxy_hints:
            metric = _make_geval(
                f"L2_{hint}",
                HINT_CRITERIA[hint],
                model=judge_model,
                with_context=hint.startswith("tool_wiki") or hint == "gov_no_wiki",
            )
            test = LLMTestCase(
                input=case.question,
                actual_output=case.answer,
                retrieval_context=_compact_context(case),
            )
            metric.measure(test)
            score = float(metric.score or 0.0)
            scores.append(score)
            checks[hint] = {
                "score": score,
                "passed": score >= DEEPEVAL_THRESHOLD,
                "reason": getattr(metric, "reason", ""),
            }

        avg = sum(scores) / len(scores) if scores else 1.0
        failures = [h for h, c in checks.items() if not c["passed"]]
        layers.append(
            LayerScore(
                layer="L2_deepeval",
                score=round(avg, 4),
                passed=not failures,
                details={"checks": checks, "failures": failures},
            )
        )

    # L5 task success
    task_metric = _make_geval(
        "L5_task_success",
        TASK_SUCCESS_CRITERIA,
        model=judge_model,
        with_context=bool(case.retrieval_contexts),
    )
    task_case = LLMTestCase(
        input=case.question,
        actual_output=case.answer,
        retrieval_context=_compact_context(case),
    )
    task_metric.measure(task_case)
    task_score = float(task_metric.score or 0.0)
    layers.append(
        LayerScore(
            layer="L5_deepeval",
            score=round(task_score, 4),
            passed=task_score >= DEEPEVAL_THRESHOLD,
            details={"reason": getattr(task_metric, "reason", "")},
        )
    )

    return layers


def score_deepeval_batch(
    cases: list[EvalCase],
    *,
    judge_model: str = "gpt-4o-mini",
    max_cases: int | None = None,
) -> dict[int, list[LayerScore]]:
    selected = cases[:max_cases] if max_cases else cases
    out: dict[int, list[LayerScore]] = {}
    for case in selected:
        out[case.index] = score_deepeval_case(case, judge_model=judge_model)
    return out
