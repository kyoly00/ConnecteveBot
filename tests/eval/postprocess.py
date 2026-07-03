"""QA 스냅샷과 debug 로그를 EvalCase로 조인."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .loaders import (
    contexts_from_pipeline_final,
    extract_eval_summary,
    extract_pipeline_final,
    extract_rag_research,
    find_session_debug_dir,
    load_llm_io_rows,
    load_search_pipeline_rows,
    load_run_manifest,
)
from .models import EvalCase


def _parse_pass_hints(pass_hint: str) -> list[str]:
    return [p.strip() for p in (pass_hint or "").split(",") if p.strip()]


def build_eval_case(
    qa_row: dict[str, Any],
    *,
    debug_root: Path,
    prefer_run_ts: str = "",
) -> EvalCase:
    session_id = str(qa_row.get("session_id") or "")
    located = find_session_debug_dir(
        debug_root,
        session_id,
        prefer_run_ts=prefer_run_ts,
    )

    eval_summary: dict[str, Any] | None = None
    rag_research: dict[str, Any] | None = None
    pipeline_final: list[dict[str, Any]] = []
    retrieval_contexts: list[str] = []
    token_usage: dict[str, Any] | None = None
    timings: dict[str, Any] | None = None
    debug_run_ts = prefer_run_ts

    if located:
        debug_run_ts, session_dir = located
        llm_rows = load_llm_io_rows(session_dir)
        search_rows = load_search_pipeline_rows(session_dir)
        eval_summary = extract_eval_summary(llm_rows)
        rag_research = extract_rag_research(llm_rows)
        if eval_summary and eval_summary.get("rag_research"):
            rag_research = rag_research or eval_summary.get("rag_research")

        primary = extract_pipeline_final(search_rows, search_kind="primary")
        followup = extract_pipeline_final(search_rows, search_kind="followup")
        pipeline_final = primary or followup
        retrieval_contexts = contexts_from_pipeline_final(pipeline_final)

        if eval_summary:
            token_usage = eval_summary.get("token_usage")
            timings = eval_summary.get("timings")

    pass_hint = str(qa_row.get("pass_hint") or "")
    docs_count = int(qa_row.get("docs_count") or 0)
    if eval_summary and eval_summary.get("docs_count") is not None:
        docs_count = int(eval_summary["docs_count"])

    intent = str(qa_row.get("intent") or "")
    if eval_summary and eval_summary.get("intent"):
        intent = str(eval_summary["intent"])

    return EvalCase(
        index=int(qa_row.get("index") or 0),
        session_id=session_id,
        question=str(qa_row.get("question") or ""),
        question_type=str(qa_row.get("question_type") or ""),
        expected_intent=str(qa_row.get("expected_intent") or ""),
        pass_hint=pass_hint,
        pass_hints=_parse_pass_hints(pass_hint),
        answer=str(qa_row.get("answer") or ""),
        intent=intent,
        docs_count=docs_count,
        latency_sec=float(qa_row.get("latency_sec") or 0.0),
        debug_run_ts=debug_run_ts,
        eval_summary=eval_summary,
        rag_research=rag_research,
        retrieval_contexts=retrieval_contexts,
        pipeline_final=pipeline_final,
        token_usage=token_usage,
        timings=timings,
    )


def build_eval_cases(
    qa_rows: list[dict[str, Any]],
    *,
    debug_root: Path,
    prefer_run_ts: str = "",
) -> list[EvalCase]:
    return [
        build_eval_case(row, debug_root=debug_root, prefer_run_ts=prefer_run_ts)
        for row in qa_rows
    ]


def collect_run_manifest(
    cases: list[EvalCase],
    debug_root: Path,
) -> dict[str, Any] | None:
    run_ts = next((c.debug_run_ts for c in cases if c.debug_run_ts), "")
    if not run_ts:
        return None
    return load_run_manifest(debug_root, run_ts)
