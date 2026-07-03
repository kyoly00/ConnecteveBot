"""weak_question QA — 큐 전략별 벤치마크 실행기."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.services.bot_jobs.queue import (
    claim_next_job,
    complete_job,
    delete_jobs_by_source,
    enqueue_job,
    fail_job,
)

from benchmark_queue.metrics import JobTiming, StrategyReport, build_report

BENCHMARK_SOURCE = "benchmark"
InferenceFn = Callable[[dict[str, str], str], Awaitable[dict[str, Any]]]


@dataclass
class RunContext:
    run_id: str
    session_prefix: str


async def run_sequential(
    questions: list[dict[str, str]],
    *,
    worker_count: int,
    infer: InferenceFn,
    ctx: RunContext,
) -> StrategyReport:
    del worker_count  # sequential는 항상 1
    started = time.perf_counter()
    jobs: list[JobTiming] = []

    for idx, item in enumerate(questions, start=1):
        session_id = f"{ctx.session_prefix}_{ctx.run_id}_{idx:04d}"
        t0 = time.perf_counter()
        try:
            out = await infer(item, session_id)
            jobs.append(JobTiming(
                index=idx,
                session_id=session_id,
                question=item["question"],
                inference_sec=round(time.perf_counter() - t0, 3),
                intent=str(out.get("intent") or ""),
            ))
        except Exception as e:
            jobs.append(JobTiming(
                index=idx,
                session_id=session_id,
                question=item["question"],
                inference_sec=round(time.perf_counter() - t0, 3),
                error=str(e),
            ))

    return build_report(
        strategy="sequential",
        worker_count=1,
        jobs=jobs,
        wall_sec=time.perf_counter() - started,
        notes="run_qa_weak_questions.py와 동일한 직렬 baseline",
    )


async def run_async_parallel(
    questions: list[dict[str, str]],
    *,
    worker_count: int,
    infer: InferenceFn,
    ctx: RunContext,
) -> StrategyReport:
    started = time.perf_counter()
    sem = asyncio.Semaphore(max(1, worker_count))
    jobs: list[JobTiming] = []
    lock = asyncio.Lock()

    async def one(idx: int, item: dict[str, str]) -> None:
        session_id = f"{ctx.session_prefix}_{ctx.run_id}_{idx:04d}"
        async with sem:
            t0 = time.perf_counter()
            try:
                out = await infer(item, session_id)
                timing = JobTiming(
                    index=idx,
                    session_id=session_id,
                    question=item["question"],
                    inference_sec=round(time.perf_counter() - t0, 3),
                    intent=str(out.get("intent") or ""),
                )
            except Exception as e:
                timing = JobTiming(
                    index=idx,
                    session_id=session_id,
                    question=item["question"],
                    inference_sec=round(time.perf_counter() - t0, 3),
                    error=str(e),
                )
        async with lock:
            jobs.append(timing)

    await asyncio.gather(*(one(i, q) for i, q in enumerate(questions, start=1)))
    jobs.sort(key=lambda j: j.index)

    return build_report(
        strategy="async_parallel",
        worker_count=worker_count,
        jobs=jobs,
        wall_sec=time.perf_counter() - started,
        notes="큐 없이 Semaphore로 동시 추론 — Redis 대비 상한선",
    )


async def run_memory_queue(
    questions: list[dict[str, str]],
    *,
    worker_count: int,
    infer: InferenceFn,
    ctx: RunContext,
    poll_sec: float = 0.001,
) -> StrategyReport:
    """asyncio.Queue 워커 — Redis Streams/List 패턴 근사."""
    started = time.perf_counter()
    queue: asyncio.Queue[tuple[int, dict[str, str], float] | None] = asyncio.Queue()
    jobs: list[JobTiming] = []
    lock = asyncio.Lock()
    done = asyncio.Event()
    total = len(questions)

    enqueue_started = time.perf_counter()
    for idx, item in enumerate(questions, start=1):
        await queue.put((idx, item, time.perf_counter()))
    for _ in range(max(1, worker_count)):
        await queue.put(None)
    enqueue_sec = time.perf_counter() - enqueue_started

    async def worker() -> None:
        while True:
            row = await queue.get()
            if row is None:
                return
            idx, item, enqueued_at = row
            session_id = f"{ctx.session_prefix}_{ctx.run_id}_{idx:04d}"
            wait_sec = time.perf_counter() - enqueued_at
            t0 = time.perf_counter()
            try:
                out = await infer(item, session_id)
                timing = JobTiming(
                    index=idx,
                    session_id=session_id,
                    question=item["question"],
                    inference_sec=round(time.perf_counter() - t0, 3),
                    queue_wait_sec=round(wait_sec, 3),
                    intent=str(out.get("intent") or ""),
                )
            except Exception as e:
                timing = JobTiming(
                    index=idx,
                    session_id=session_id,
                    question=item["question"],
                    inference_sec=round(time.perf_counter() - t0, 3),
                    queue_wait_sec=round(wait_sec, 3),
                    error=str(e),
                )
            async with lock:
                jobs.append(timing)
                if len(jobs) >= total:
                    done.set()

    workers = [asyncio.create_task(worker()) for _ in range(max(1, worker_count))]
    try:
        await asyncio.wait_for(done.wait(), timeout=3600)
    except asyncio.TimeoutError:
        pass
    for task in workers:
        if not task.done():
            task.cancel()
    await asyncio.gather(*workers, return_exceptions=True)
    jobs.sort(key=lambda j: j.index)

    return build_report(
        strategy="memory_queue",
        worker_count=worker_count,
        jobs=jobs,
        wall_sec=time.perf_counter() - started,
        enqueue_sec=enqueue_sec,
        notes=f"in-process asyncio.Queue (Redis 후보 근사, poll={poll_sec}s)",
    )


async def run_postgres_queue(
    questions: list[dict[str, str]],
    *,
    worker_count: int,
    infer: InferenceFn,
    ctx: RunContext,
    poll_sec: float = 0.05,
) -> StrategyReport:
    """bot_jobs PostgreSQL 큐 + N 워커."""
    await delete_jobs_by_source(BENCHMARK_SOURCE)
    started = time.perf_counter()
    jobs: list[JobTiming] = []
    lock = asyncio.Lock()
    done = asyncio.Event()
    total = len(questions)
    enqueued_at: dict[str, float] = {}

    enqueue_started = time.perf_counter()
    for idx, item in enumerate(questions, start=1):
        event_id = f"{ctx.run_id}:{idx:04d}"
        session_id = f"{ctx.session_prefix}_{ctx.run_id}_{idx:04d}"
        payload = {
            "index": idx,
            "session_id": session_id,
            "question": item["question"],
            "question_type": item.get("type", ""),
            "expected_intent": item.get("expected_intent", ""),
        }
        created, job_id = await enqueue_job(
            source=BENCHMARK_SOURCE,
            source_event_id=event_id,
            event_type="qa_benchmark",
            conversation_key=session_id,
            payload=payload,
        )
        if created and job_id is not None:
            enqueued_at[str(job_id)] = time.perf_counter()
    enqueue_sec = time.perf_counter() - enqueue_started

    async def worker(wid: str) -> None:
        while not done.is_set():
            job = await claim_next_job(wid, source=BENCHMARK_SOURCE)
            if not job:
                await asyncio.sleep(poll_sec)
                continue

            payload = job.payload or {}
            idx = int(payload.get("index") or 0)
            session_id = str(payload.get("session_id") or "")
            question = str(payload.get("question") or "")
            item = {
                "question": question,
                "type": str(payload.get("question_type") or ""),
                "expected_intent": str(payload.get("expected_intent") or ""),
            }
            wait_sec = 0.0
            key = str(job.id)
            if key in enqueued_at:
                wait_sec = time.perf_counter() - enqueued_at[key]

            t0 = time.perf_counter()
            try:
                out = await infer(item, session_id)
                await complete_job(job.id)
                timing = JobTiming(
                    index=idx,
                    session_id=session_id,
                    question=question,
                    inference_sec=round(time.perf_counter() - t0, 3),
                    queue_wait_sec=round(wait_sec, 3),
                    intent=str(out.get("intent") or ""),
                )
            except Exception as e:
                await fail_job(
                    job.id,
                    attempt_count=job.attempt_count,
                    max_attempts=job.max_attempts,
                    error_code=type(e).__name__,
                    error_message=str(e),
                )
                timing = JobTiming(
                    index=idx,
                    session_id=session_id,
                    question=question,
                    inference_sec=round(time.perf_counter() - t0, 3),
                    queue_wait_sec=round(wait_sec, 3),
                    error=str(e),
                )

            async with lock:
                jobs.append(timing)
                if len(jobs) >= total:
                    done.set()

    worker_tasks = [
        asyncio.create_task(worker(f"bench-{i}"))
        for i in range(max(1, worker_count))
    ]
    try:
        await asyncio.wait_for(done.wait(), timeout=3600)
    except asyncio.TimeoutError:
        pass
    for task in worker_tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*worker_tasks, return_exceptions=True)
    jobs.sort(key=lambda j: j.index)

    return build_report(
        strategy="postgres_queue",
        worker_count=worker_count,
        jobs=jobs,
        wall_sec=time.perf_counter() - started,
        enqueue_sec=enqueue_sec,
        notes=f"bot_jobs FOR UPDATE SKIP LOCKED (poll={poll_sec}s)",
    )


STRATEGIES: dict[str, Callable[..., Awaitable[StrategyReport]]] = {
    "sequential": run_sequential,
    "async_parallel": run_async_parallel,
    "memory_queue": run_memory_queue,
    "postgres_queue": run_postgres_queue,
}


def new_run_context(session_prefix: str) -> RunContext:
    return RunContext(run_id=uuid.uuid4().hex[:10], session_prefix=session_prefix)
