"""
weak_question QA — 큐 전략 벤치마크 (PostgreSQL vs in-memory vs 직렬).

run_qa_weak_questions.py와 동일한 질문 파일·async_agent_chat을 사용해
약 10건 병렬 처리 시 wall time / queue wait / throughput 을 비교한다.

전략:
  - sequential       : run_qa_weak_questions 직렬 baseline
  - async_parallel   : 큐 없이 Semaphore 병렬 (상한선)
  - memory_queue     : asyncio.Queue 워커 (Redis 후보 근사)
  - postgres_queue   : bot_jobs FOR UPDATE SKIP LOCKED

사용 예:
  cd ConnBot
  python tests/benchmark_queue_weak_questions.py --limit 10 --workers 3
  python tests/benchmark_queue_weak_questions.py --dry-run --strategies memory_queue,postgres_queue
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

tests_dir = Path(__file__).resolve().parent
connbot_root = tests_dir.parent
sys.path.insert(0, str(connbot_root))
sys.path.insert(0, str(tests_dir))

from benchmark_queue.metrics import format_comparison_table  # noqa: E402
from benchmark_queue.strategies import (  # noqa: E402
    BENCHMARK_SOURCE,
    STRATEGIES,
    new_run_context,
)
from eval.loaders import load_weak_questions  # noqa: E402

kst = timezone(timedelta(hours=9))


def _kst_now_iso() -> str:
    return datetime.now(kst).strftime("%Y%m%d_%H%M%S")


async def _real_inference(item: dict[str, str], session_id: str) -> dict[str, Any]:
    from app.agent.router import async_agent_chat

    (
        answer,
        docs,
        intent,
        _source_docs,
        _links_used,
        _attachments_used,
        gov_attachments,
    ) = await async_agent_chat(item["question"], session_id=session_id)
    return {
        "intent": intent,
        "docs_count": len(docs) if docs else 0,
        "gov_attachments_count": len(gov_attachments) if gov_attachments else 0,
        "answer_length": len((answer or "").strip()),
    }


async def _dry_inference(item: dict[str, str], session_id: str) -> dict[str, Any]:
    """API 없이 큐 오버헤드만 측정 (기본 0.3s 가짜 추론)."""
    delay = float(__import__("os").getenv("BENCHMARK_MOCK_LATENCY_SEC", "0.3"))
    await asyncio.sleep(delay)
    return {
        "intent": "mock",
        "session_id": session_id,
        "question_type": item.get("type", ""),
    }


async def _ensure_postgres() -> bool:
    try:
        from app.db.connection import init_db

        await init_db()
        return True
    except Exception as e:
        print(f"PostgreSQL 연결 실패 - postgres_queue 제외: {e}")
        return False


async def run_benchmarks(args: argparse.Namespace) -> dict[str, Any]:
    questions_path = (tests_dir / args.questions).resolve()
    if not questions_path.exists():
        raise FileNotFoundError(f"질문 파일 없음: {questions_path}")

    all_questions = load_weak_questions(questions_path)
    questions = all_questions[: args.limit]
    if not questions:
        raise ValueError("실행할 질문이 없습니다.")

    strategy_names = [s.strip() for s in args.strategies.split(",") if s.strip()]
    unknown = [s for s in strategy_names if s not in STRATEGIES]
    if unknown:
        raise ValueError(f"알 수 없는 전략: {unknown} (가능: {list(STRATEGIES)})")

    if "postgres_queue" in strategy_names:
        if not await _ensure_postgres():
            strategy_names = [s for s in strategy_names if s != "postgres_queue"]

    if not strategy_names:
        raise ValueError("실행 가능한 전략이 없습니다. PostgreSQL/asyncpg 확인.")

    infer = _dry_inference if args.dry_run else _real_inference
    reports = []
    started_all = time.perf_counter()

    for name in strategy_names:
        fn = STRATEGIES[name]
        workers = 1 if name == "sequential" else args.workers
        ctx = new_run_context(args.session_prefix)
        print(f"\n=== {name} (workers={workers}, n={len(questions)}) ===")
        report = await fn(
            questions,
            worker_count=workers,
            infer=infer,
            ctx=ctx,
        )
        reports.append(report)
        print(
            f"  wall={report.wall_sec}s | enqueue={report.enqueue_sec}s | "
            f"qps={report.throughput_qps} | inf_p50={report.inference_sec.get('p50')}s | "
            f"wait_p50={report.queue_wait_sec.get('p50')}s | errors={report.errors}"
        )
        if name == "postgres_queue":
            from app.services.bot_jobs.queue import delete_jobs_by_source

            await delete_jobs_by_source(BENCHMARK_SOURCE)

    comparison = format_comparison_table(reports)
    print("\n=== 비교 요약 ===")
    print(comparison)

    speedup = {}
    if len(reports) >= 2:
        base = reports[0].wall_sec
        for r in reports[1:]:
            if r.wall_sec > 0:
                speedup[r.strategy] = round(base / r.wall_sec, 2)

    result = {
        "ts_kst": _kst_now_iso(),
        "questions_file": str(questions_path),
        "question_count": len(questions),
        "workers": args.workers,
        "dry_run": args.dry_run,
        "strategies": strategy_names,
        "total_benchmark_sec": round(time.perf_counter() - started_all, 3),
        "speedup_vs_first_strategy": speedup,
        "comparison_table": comparison,
        "reports": [r.to_dict() for r in reports],
        "notes": {
            "redis": (
                "memory_queue 전략이 Redis Streams/BullMQ 대체 후보. "
                "postgres_queue는 영속·멱등·재시도에 유리, "
                "memory/redis는 낮은 enqueue latency·높은 fan-out에 유리."
            ),
        },
    }
    return result


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="weak_question QA 큐 벤치마크 (run_qa_weak_questions 연동)",
    )
    parser.add_argument(
        "--questions",
        default="weak_question2.txt",
        help="질문 파일 (tests/ 기준, run_qa_weak_questions와 동일)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="벤치마크 질문 수 (기본 10)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="병렬 워커 수 (sequential 제외, 기본 4)",
    )
    parser.add_argument(
        "--strategies",
        default="sequential,async_parallel,memory_queue,postgres_queue",
        help="쉼표 구분 전략 목록",
    )
    parser.add_argument(
        "--session-prefix",
        default="bench_qa",
        help="세션 ID prefix",
    )
    parser.add_argument(
        "--output",
        default=f"qa_results/benchmark_queue_{_kst_now_iso()}.json",
        help="결과 JSON 저장 경로 (tests/ 기준)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="async_agent_chat 대신 sleep mock (큐 오버헤드만 측정)",
    )
    args = parser.parse_args()

    output_path = (tests_dir / args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"질문 파일: {(tests_dir / args.questions).resolve()}")
    print(f"limit={args.limit} workers={args.workers} dry_run={args.dry_run}")
    print(f"결과 저장: {output_path}")

    if not args.dry_run:
        from app.ml.inference import shutdown_ml_inference, start_ml_inference
        from app.rag.vectordb import init_vectordb

        print("ML inference 워커 시작...")
        await start_ml_inference()
        print("VectorDB 초기화...")
        init_vectordb(force_rebuild=False)
        try:
            result = await run_benchmarks(args)
        finally:
            await shutdown_ml_inference()
    else:
        result = await run_benchmarks(args)

    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n저장 완료: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
