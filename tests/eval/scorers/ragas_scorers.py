"""L3·L5 RAGAS 메트릭 (reference-free) — RAGAS 불가 시 DeepEval 동등 메트릭 폴백."""

from __future__ import annotations

import os
from typing import Any

from ..models import EvalCase, LayerScore

RAGAS_THRESHOLDS = {
    "faithfulness": 0.70,
    "answer_relevancy": 0.70,
}

_RAGAS_BACKEND: str | None = None


def _detect_ragas_backend() -> str:
    global _RAGAS_BACKEND
    if _RAGAS_BACKEND is not None:
        return _RAGAS_BACKEND
    try:
        import ragas  # noqa: F401

        _RAGAS_BACKEND = "ragas"
    except Exception:
        _RAGAS_BACKEND = "deepeval"
    return _RAGAS_BACKEND


def _build_ragas_llm(model: str):
    from langchain_openai import ChatOpenAI
    from ragas.llms import LangchainLLMWrapper

    llm = ChatOpenAI(
        model=model,
        api_key=os.getenv("OPENAI_API_KEY", ""),
        temperature=0,
    )
    return LangchainLLMWrapper(llm)


def _build_ragas_embeddings(model: str = "text-embedding-3-small"):
    from langchain_openai import OpenAIEmbeddings
    from ragas.embeddings import LangchainEmbeddingsWrapper

    emb = OpenAIEmbeddings(
        model=model,
        api_key=os.getenv("OPENAI_API_KEY", ""),
    )
    return LangchainEmbeddingsWrapper(emb)


def _import_ragas_metrics():
    from ragas.metrics import answer_relevancy, faithfulness

    return [faithfulness, answer_relevancy]


def _score_with_ragas(
    cases: list[EvalCase],
    *,
    judge_model: str,
) -> dict[int, dict[str, float]]:
    from datasets import Dataset
    from ragas import evaluate

    dataset = Dataset.from_dict(
        {
            "question": [c.question for c in cases],
            "answer": [c.answer for c in cases],
            "contexts": [c.retrieval_contexts for c in cases],
        }
    )

    llm = _build_ragas_llm(judge_model)
    embeddings = _build_ragas_embeddings()
    metrics = _import_ragas_metrics()
    for metric in metrics:
        if hasattr(metric, "llm"):
            metric.llm = llm
        if hasattr(metric, "embeddings"):
            metric.embeddings = embeddings

    result = evaluate(dataset, metrics=metrics, llm=llm, embeddings=embeddings)
    result_dict = dict(result)

    out: dict[int, dict[str, float]] = {}
    for i, case in enumerate(cases):
        scores: dict[str, float] = {}
        for key, values in result_dict.items():
            if isinstance(values, list) and i < len(values):
                val = values[i]
                if isinstance(val, (int, float)):
                    scores[key] = float(val)
            elif isinstance(values, (int, float)) and len(cases) == 1:
                scores[key] = float(values)
        out[case.index] = scores
    return out


def _score_with_deepeval(
    case: EvalCase,
    *,
    judge_model: str,
) -> dict[str, float]:
    from deepeval.metrics import AnswerRelevancyMetric, FaithfulnessMetric
    from deepeval.test_case import LLMTestCase

    test = LLMTestCase(
        input=case.question,
        actual_output=case.answer,
        retrieval_context=case.retrieval_contexts[:8],
    )
    faith = FaithfulnessMetric(threshold=0.7, model=judge_model)
    relevancy = AnswerRelevancyMetric(threshold=0.7, model=judge_model)

    faith.measure(test)
    relevancy.measure(test)

    return {
        "faithfulness": float(faith.score or 0.0),
        "answer_relevancy": float(relevancy.score or 0.0),
    }


def _layer_from_scores(
    scores: dict[str, float],
    *,
    backend: str,
) -> LayerScore:
    failures: list[str] = []
    for metric_name, threshold in RAGAS_THRESHOLDS.items():
        val = scores.get(metric_name)
        if val is not None and val < threshold:
            failures.append(f"{metric_name}={val:.3f}<{threshold}")

    faith = scores.get("faithfulness")
    relevancy = scores.get("answer_relevancy") or scores.get("answer_relevance")
    component_scores = [s for s in (faith, relevancy) if s is not None]
    avg = sum(component_scores) / len(component_scores) if component_scores else None

    return LayerScore(
        layer="L3_L5_ragas",
        score=round(avg, 4) if avg is not None else None,
        passed=not failures,
        details={
            "backend": backend,
            "scores": scores,
            "failures": failures,
            "thresholds": RAGAS_THRESHOLDS,
        },
    )


def score_ragas_case(
    case: EvalCase,
    *,
    judge_model: str = "gpt-4o-mini",
) -> LayerScore:
    if not case.retrieval_contexts:
        return LayerScore(
            layer="L3_L5_ragas",
            score=None,
            passed=True,
            details={"skipped": "no_contexts"},
        )

    backend = _detect_ragas_backend()
    if backend == "ragas":
        scores = _score_with_ragas([case], judge_model=judge_model).get(case.index, {})
        return _layer_from_scores(scores, backend="ragas")

    scores = _score_with_deepeval(case, judge_model=judge_model)
    return _layer_from_scores(scores, backend="deepeval_ragas_equiv")


def score_ragas_batch(
    cases: list[EvalCase],
    *,
    judge_model: str = "gpt-4o-mini",
) -> dict[int, LayerScore]:
    eligible = [c for c in cases if c.retrieval_contexts]
    if not eligible:
        return {}

    backend = _detect_ragas_backend()
    out: dict[int, LayerScore] = {}

    if backend == "ragas":
        try:
            batch_scores = _score_with_ragas(eligible, judge_model=judge_model)
            for case in eligible:
                scores = batch_scores.get(case.index, {})
                out[case.index] = _layer_from_scores(scores, backend="ragas")
            return out
        except Exception as exc:
            print(f"[eval] RAGAS 배치 실패 → DeepEval 폴백: {exc}")

    for case in eligible:
        scores = _score_with_deepeval(case, judge_model=judge_model)
        out[case.index] = _layer_from_scores(scores, backend="deepeval_ragas_equiv")
    return out
