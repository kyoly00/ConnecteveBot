"""응답 후 메모리·품질 개선 작업을 bot_jobs 큐에 등록."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.services.bot_jobs.constants import JobSource
from app.services.bot_jobs.queue import enqueue_job

logger = logging.getLogger(__name__)

POST_RESPONSE_EVENT = "post_response"


async def enqueue_post_response_job(
    *,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    conversation_key: str,
    user_msg_id: uuid.UUID | None,
    assistant_msg_id: uuid.UUID | None,
    user_text: str,
    assistant_text: str,
    intent: str,
    docs: list[Any] | None = None,
) -> tuple[bool, int | None]:
    """Slack 응답 전송 직후 호출 — 워커가 메모리/요약/품질 이벤트 처리."""
    dedupe_id = str(assistant_msg_id or user_msg_id or uuid.uuid4())
    source_event_id = f"post:{session_id}:{dedupe_id}"

    docs_payload = [
        {"score": float(getattr(d, "score", 0) or 0)}
        for d in (docs or [])
    ]

    return await enqueue_job(
        source=JobSource.INTERNAL,
        source_event_id=source_event_id,
        event_type=POST_RESPONSE_EVENT,
        conversation_key=conversation_key,
        user_id=str(user_id),
        thread_ts=conversation_key.split(":")[-1] if ":" in conversation_key else None,
        payload={
            "user_id": str(user_id),
            "session_id": str(session_id),
            "user_msg_id": str(user_msg_id) if user_msg_id else None,
            "assistant_msg_id": str(assistant_msg_id) if assistant_msg_id else None,
            "user_text": user_text,
            "assistant_text": assistant_text,
            "intent": intent,
            "docs": docs_payload,
        },
        max_attempts=3,
    )
