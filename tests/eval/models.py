"""평가 파이프라인 공용 데이터 모델."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class EvalCase:
    """QA 스냅샷 + 디버그 로그를 조인한 단일 케이스."""

    index: int
    session_id: str
    question: str
    question_type: str
    expected_intent: str
    pass_hint: str
    answer: str
    intent: str
    docs_count: int
    latency_sec: float
    debug_run_ts: str = ""
    eval_summary: dict[str, Any] | None = None
    rag_research: dict[str, Any] | None = None
    retrieval_contexts: list[str] = field(default_factory=list)
    pipeline_final: list[dict[str, Any]] = field(default_factory=list)
    token_usage: dict[str, Any] | None = None
    timings: dict[str, Any] | None = None
    pass_hints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LayerScore:
    """레이어별 점수·통과 여부."""

    layer: str
    score: float | None
    passed: bool
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CaseResult:
    """케이스 단위 종합 평가 결과."""

    case: EvalCase
    layers: list[LayerScore] = field(default_factory=list)
    overall_passed: bool = False
    failure_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case": self.case.to_dict(),
            "layers": [layer.to_dict() for layer in self.layers],
            "overall_passed": self.overall_passed,
            "failure_reasons": self.failure_reasons,
        }
