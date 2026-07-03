"""bot_jobs 워커 — PostgreSQL 큐 폴링·상태머신 실행 (동시 N 워커)."""

from __future__ import annotations

import asyncio
import logging
import os
import socket

from app.services.bot_jobs.handlers import dispatch_bot_job
from app.services.bot_jobs.queue import (
    claim_next_job,
    complete_job,
    fail_job,
    reclaim_stale_processing_jobs,
)

logger = logging.getLogger(__name__)

_WORKER_TASKS: list[asyncio.Task] = []


def worker_id(suffix: str = "0") -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{suffix}"


def worker_count() -> int:
    return max(1, int(os.getenv("BOT_JOB_WORKER_COUNT", "4")))


async def _process_one_job(wid: str) -> bool:
    job = await claim_next_job(wid)
    if not job:
        return False
    try:
        await dispatch_bot_job(job)
        await complete_job(job.id)
    except Exception as e:
        await fail_job(
            job.id,
            attempt_count=job.attempt_count,
            max_attempts=job.max_attempts,
            error_code=type(e).__name__,
            error_message=str(e),
        )
    return True


async def bot_job_worker_loop(
    *,
    worker_suffix: str,
    poll_interval_sec: float = 0.2,
    lock_timeout_sec: int = 900,
) -> None:
    wid = worker_id(worker_suffix)
    logger.info("[BotJobWorker] started worker_id=%s", wid)
    while True:
        try:
            await reclaim_stale_processing_jobs(lock_timeout_sec)
            processed = await _process_one_job(wid)
            if not processed:
                await asyncio.sleep(poll_interval_sec)
        except asyncio.CancelledError:
            logger.info("[BotJobWorker] cancelled worker_id=%s", wid)
            raise
        except Exception as e:
            logger.error("[BotJobWorker] loop error worker_id=%s: %s", wid, e)
            await asyncio.sleep(poll_interval_sec)


def start_bot_job_worker() -> list[asyncio.Task]:
    """startup에서 호출 — 동시 사용자 수만큼 워커 태스크 시작 (기본 4)."""
    global _WORKER_TASKS

    count = worker_count()
    poll = float(os.getenv("BOT_JOB_POLL_INTERVAL_SEC", "0.2"))
    lock_timeout = int(os.getenv("BOT_JOB_LOCK_TIMEOUT_SEC", "900"))

    active = [t for t in _WORKER_TASKS if not t.done()]
    if len(active) >= count:
        return active

    _WORKER_TASKS = active
    start_idx = len(_WORKER_TASKS)
    for i in range(start_idx, count):
        suffix = str(i)
        task = asyncio.create_task(
            bot_job_worker_loop(
                worker_suffix=suffix,
                poll_interval_sec=poll,
                lock_timeout_sec=lock_timeout,
            )
        )
        _WORKER_TASKS.append(task)

    logger.info("[BotJobWorker] %d workers running (poll=%.2fs)", len(_WORKER_TASKS), poll)
    return _WORKER_TASKS
