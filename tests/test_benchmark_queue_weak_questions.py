"""benchmark_queue 메트릭·dry-run 전략 단위 테스트."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

tests_dir = Path(__file__).resolve().parent
connbot_root = tests_dir.parent
sys.path.insert(0, str(connbot_root))
sys.path.insert(0, str(tests_dir))

from benchmark_queue.metrics import (  # noqa: E402
    build_report,
    format_comparison_table,
    percentile,
    summarize_timings,
)
from benchmark_queue.strategies import (  # noqa: E402
    new_run_context,
    run_async_parallel,
    run_memory_queue,
    run_sequential,
)


def test_percentile_and_summarize():
    assert percentile([1.0, 2.0, 3.0, 4.0, 10.0], 50) == pytest.approx(3.0, rel=0.1)
    s = summarize_timings([1.0, 2.0, 3.0])
    assert s["min"] == 1.0
    assert s["max"] == 3.0
    assert s["total"] == 6.0


def test_format_comparison_table():
    from benchmark_queue.metrics import JobTiming

    jobs = [
        JobTiming(index=1, session_id="s1", question="q1", inference_sec=1.0),
    ]
    r = build_report(strategy="sequential", worker_count=1, jobs=jobs, wall_sec=2.0)
    table = format_comparison_table([r])
    assert "sequential" in table
    assert "wall(s)" in table


async def _mock_infer(item: dict, session_id: str) -> dict:
    await asyncio.sleep(0.05)
    return {"intent": "mock", "type": item.get("type", "")}


def test_dry_run_strategies_parallel():
    async def _run():
        questions = [
            {"question": f"질문 {i}", "type": "RAG"}
            for i in range(1, 6)
        ]
        ctx = new_run_context("test_bench")

        seq = await run_sequential(questions, worker_count=1, infer=_mock_infer, ctx=ctx)
        par = await run_async_parallel(questions, worker_count=3, infer=_mock_infer, ctx=ctx)
        mem = await run_memory_queue(questions, worker_count=3, infer=_mock_infer, ctx=ctx)

        assert seq.question_count == 5
        assert par.question_count == 5
        assert mem.question_count == 5
        assert par.wall_sec <= seq.wall_sec
        assert par.errors == 0
        assert mem.queue_wait_sec.get("p50", 0) >= 0

    asyncio.run(_run())


@pytest.mark.integration
@pytest.mark.slow
def test_postgres_queue_dry_run():
    """PostgreSQL + bot_jobs 테이블 필요. 없으면 skip."""
    async def _run():
        from app.db.connection import init_db, close_db
        from benchmark_queue.strategies import run_postgres_queue

        try:
            await init_db()
        except Exception as e:
            pytest.skip(f"PostgreSQL unavailable: {e}")

        questions = [{"question": f"벤치 {i}", "type": "RAG"} for i in range(1, 4)]
        ctx = new_run_context("pg_bench")
        try:
            report = await run_postgres_queue(
                questions,
                worker_count=2,
                infer=_mock_infer,
                ctx=ctx,
                poll_sec=0.02,
            )
        finally:
            await close_db()

        assert report.question_count == 3
        assert report.errors == 0
        assert report.wall_sec > 0

    asyncio.run(_run())
