"""bot_jobs 큐 — enqueue / claim / complete / fail."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select, text, update, delete
from sqlalchemy.dialects.postgresql import insert

from app.db.connection import get_db_session
from app.db.models import BotJob
from app.services.bot_jobs.constants import JobStatus

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def retry_delay_seconds(attempt_count: int) -> int:
    """지수 백오프 (최대 5분)."""
    return min(300, max(5, 5 * (2 ** max(0, attempt_count - 1))))


async def enqueue_job(
    *,
    source: str,
    source_event_id: str,
    event_type: str,
    conversation_key: str,
    payload: dict[str, Any],
    team_id: str | None = None,
    channel_id: str | None = None,
    user_id: str | None = None,
    thread_ts: str | None = None,
    event_ts: str | None = None,
    max_attempts: int = 3,
) -> tuple[bool, int | None]:
    """
    작업을 큐에 넣는다.

    Returns:
        (created, job_id) — created=False면 (source, source_event_id) 중복.
    """
    stmt = (
        insert(BotJob)
        .values(
            source=source,
            source_event_id=source_event_id,
            event_type=event_type,
            team_id=team_id,
            channel_id=channel_id,
            user_id=user_id,
            thread_ts=thread_ts,
            event_ts=event_ts,
            conversation_key=conversation_key,
            payload=payload,
            status=JobStatus.QUEUED,
            max_attempts=max_attempts,
            next_run_at=_utcnow(),
        )
        .on_conflict_do_nothing(constraint="uq_bot_jobs_source_event")
        .returning(BotJob.id)
    )
    async with get_db_session() as session:
        result = await session.execute(stmt)
        job_id = result.scalar_one_or_none()
    created = job_id is not None
    if created:
        logger.info(
            "[BotJob] enqueued id=%s source=%s event=%s key=%s",
            job_id,
            source,
            source_event_id,
            conversation_key,
        )
    else:
        logger.info(
            "[BotJob] duplicate ignored source=%s event=%s",
            source,
            source_event_id,
        )
    return created, job_id


async def reclaim_stale_processing_jobs(
    lock_timeout_sec: int = 900,
) -> int:
    """processing 상태가 lock_timeout 초과 시 queued로 복구."""
    cutoff = _utcnow() - timedelta(seconds=lock_timeout_sec)
    stmt = (
        update(BotJob)
        .where(
            BotJob.status == JobStatus.PROCESSING,
            BotJob.locked_at.is_not(None),
            BotJob.locked_at < cutoff,
        )
        .values(
            status=JobStatus.QUEUED,
            locked_by=None,
            locked_at=None,
            updated_at=_utcnow(),
            error_code="stale_lock",
            error_message="processing lock expired — requeued",
        )
    )
    async with get_db_session() as session:
        result = await session.execute(stmt)
        count = result.rowcount or 0
    if count:
        logger.warning("[BotJob] reclaimed %d stale processing jobs", count)
    return count


async def claim_next_job(
    worker_id: str,
    *,
    source: str | None = None,
) -> Optional[BotJob]:
    """queued 작업 1건을 원자적으로 claim. source 지정 시 해당 소스만."""
    source_clause = "AND source = :source_filter" if source else ""
    sql = text(
        f"""
        UPDATE bot_jobs
        SET status = :processing,
            locked_by = :worker_id,
            locked_at = now(),
            updated_at = now(),
            attempt_count = attempt_count + 1
        WHERE id = (
            SELECT id FROM bot_jobs
            WHERE status = :queued
              AND next_run_at <= now()
              {source_clause}
            ORDER BY next_run_at ASC, id ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING id
        """
    )
    params: dict[str, Any] = {
        "processing": JobStatus.PROCESSING,
        "queued": JobStatus.QUEUED,
        "worker_id": worker_id,
    }
    if source:
        params["source_filter"] = source
    async with get_db_session() as session:
        result = await session.execute(sql, params)
        row = result.first()
        if not row:
            return None
        job_id = row[0]
        job_result = await session.execute(
            select(BotJob).where(BotJob.id == job_id)
        )
        job = job_result.scalar_one()
    logger.info(
        "[BotJob] claimed id=%s attempt=%s worker=%s",
        job.id,
        job.attempt_count,
        worker_id,
    )
    return job


async def complete_job(job_id: int) -> None:
    async with get_db_session() as session:
        await session.execute(
            update(BotJob)
            .where(BotJob.id == job_id)
            .values(
                status=JobStatus.COMPLETED,
                locked_by=None,
                locked_at=None,
                error_code=None,
                error_message=None,
                updated_at=_utcnow(),
            )
        )
    logger.info("[BotJob] completed id=%s", job_id)


async def fail_job(
    job_id: int,
    *,
    attempt_count: int,
    max_attempts: int,
    error_code: str | None = None,
    error_message: str | None = None,
) -> str:
    """실패 처리. attempt 한도 내면 queued+백오프, 초과면 failed."""
    if attempt_count < max_attempts:
        next_status = JobStatus.QUEUED
        next_run = _utcnow() + timedelta(seconds=retry_delay_seconds(attempt_count))
    else:
        next_status = JobStatus.FAILED
        next_run = _utcnow()

    async with get_db_session() as session:
        await session.execute(
            update(BotJob)
            .where(BotJob.id == job_id)
            .values(
                status=next_status,
                locked_by=None,
                locked_at=None,
                next_run_at=next_run,
                error_code=error_code,
                error_message=(error_message or "")[:4000] or None,
                updated_at=_utcnow(),
            )
        )
    logger.warning(
        "[BotJob] failed id=%s status=%s attempt=%s/%s err=%s: %s",
        job_id,
        next_status,
        attempt_count,
        max_attempts,
        error_code,
        error_message,
    )
    return next_status


async def delete_jobs_by_source(source: str) -> int:
    """소스별 작업 일괄 삭제 (벤치마크 정리용)."""
    async with get_db_session() as session:
        result = await session.execute(
            delete(BotJob).where(BotJob.source == source)
        )
        count = result.rowcount or 0
    if count:
        logger.info("[BotJob] deleted %d jobs source=%s", count, source)
    return count


async def _purge_old_jobs_batch(
    *,
    status: str,
    cutoff: datetime,
    batch_size: int,
) -> int:
    ids_subq = (
        select(BotJob.id)
        .where(
            BotJob.status == status,
            BotJob.created_at < cutoff,
        )
        .limit(batch_size)
    )
    async with get_db_session() as session:
        result = await session.execute(
            delete(BotJob).where(BotJob.id.in_(ids_subq))
        )
        return result.rowcount or 0


async def purge_old_bot_jobs(
    *,
    completed_retention_days: int = 3,
    failed_retention_days: int = 7,
    batch_size: int = 5000,
) -> dict[str, int]:
    """completed/failed 상태의 오래된 작업을 배치 삭제."""
    now = _utcnow()
    totals = {"completed": 0, "failed": 0}

    for status, retention_days in (
        (JobStatus.COMPLETED, completed_retention_days),
        (JobStatus.FAILED, failed_retention_days),
    ):
        if retention_days < 0:
            continue
        cutoff = now - timedelta(days=retention_days)
        while True:
            count = await _purge_old_jobs_batch(
                status=status,
                cutoff=cutoff,
                batch_size=batch_size,
            )
            totals[status] += count
            if count < batch_size:
                break

    if totals["completed"] or totals["failed"]:
        logger.info(
            "[BotJob] purged completed=%d failed=%d "
            "(retention %dd/%dd)",
            totals["completed"],
            totals["failed"],
            completed_retention_days,
            failed_retention_days,
        )
    return totals
