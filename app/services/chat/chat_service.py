"""
services/chat_service.py — 채팅 세션/메시지 CRUD + LLM Context 조립
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.connection import get_db_session
from app.db.models import User, ChatSession, ChatMessage, SessionSummary, Memory, ChatAttachment

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from app.core.config import (
    RECENT_MESSAGES_LIMIT,
    SESSION_METADATA_WELCOME_SENT,
    format_session_summary_for_router,
)

logger = logging.getLogger(__name__)


def _session_has_welcome_sent(session: ChatSession) -> bool:
    meta = session.metadata_ if isinstance(session.metadata_, dict) else {}
    return bool(meta.get(SESSION_METADATA_WELCOME_SENT))


# =============================================================================
# User CRUD
# =============================================================================

async def get_or_create_user(
    slack_user_id: str,
    name: str | None = None,
    email: str | None = None,
    profile_image_url: str | None = None,
) -> User:
    """slack_user_id로 사용자를 조회/생성한다."""
    async with get_db_session() as session:
        stmt = select(User).where(User.slack_user_id == slack_user_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        if user:
            user.last_seen_at = datetime.now(timezone.utc)
            if name and not user.name:
                user.name = name
            if email:
                new_email = email.strip()
                if not user.email or user.email.strip().lower() != new_email.lower():
                    user.email = new_email
            if profile_image_url and not user.profile_image_url:
                user.profile_image_url = profile_image_url
            await session.flush()
            return user

        user = User(
            slack_user_id=slack_user_id,
            name=name, email=email,
            profile_image_url=profile_image_url,
            last_seen_at=datetime.now(timezone.utc),
        )
        session.add(user)
        await session.flush()
        logger.info("새 사용자 생성: slack_user_id=%s, id=%s", slack_user_id, user.id)
        return user


# =============================================================================
# Session CRUD
# =============================================================================

async def get_or_create_session(
    user_id: uuid.UUID,
    slack_channel_id: str | None = None,
    slack_thread_ts: str | None = None,
) -> ChatSession:
    """
    (user_id, slack_channel_id, slack_thread_ts)가 모두 일치하는 active 세션만 재사용한다.
    일치하는 세션이 없으면 항상 새 세션을 생성한다 (fallback 없음).
    """
    channel = (slack_channel_id or "").strip() or None
    thread = (slack_thread_ts or "").strip() or None

    if channel and thread:
        async with get_db_session() as session:
            stmt = (
                select(ChatSession)
                .where(and_(
                    ChatSession.user_id == user_id,
                    ChatSession.slack_channel_id == channel,
                    ChatSession.slack_thread_ts == thread,
                    ChatSession.status == "active",
                ))
                .limit(1)
            )
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()
            if existing:
                return existing

    async with get_db_session() as session:
        new_session = ChatSession(
            user_id=user_id,
            session_type="slack_thread" if channel and thread else "manual",
            slack_channel_id=channel,
            slack_thread_ts=thread,
        )
        session.add(new_session)
        await session.flush()
        logger.info(
            "새 세션 생성: id=%s, user=%s, channel=%s, thread=%s",
            new_session.id,
            user_id,
            channel,
            thread,
        )
        return new_session


async def should_skip_app_welcome(
    *,
    user_id: uuid.UUID,
    slack_channel_id: str,
) -> bool:
    """
    App Home welcome DM 생략 여부.
    해당 DM 채널의 active 세션 중 welcome_sent 또는 user/assistant 메시지가 있으면 True.
    """
    channel = (slack_channel_id or "").strip()
    if not channel:
        return False

    async with get_db_session() as session:
        stmt = select(ChatSession).where(and_(
            ChatSession.user_id == user_id,
            ChatSession.slack_channel_id == channel,
            ChatSession.status == "active",
        ))
        result = await session.execute(stmt)
        chat_sessions = list(result.scalars().all())
        if not chat_sessions:
            return False

        if any(_session_has_welcome_sent(cs) for cs in chat_sessions):
            return True

        session_ids = [cs.id for cs in chat_sessions]
        msg_stmt = (
            select(func.count())
            .select_from(ChatMessage)
            .where(and_(
                ChatMessage.session_id.in_(session_ids),
                ChatMessage.role.in_(("user", "assistant")),
                ChatMessage.deleted_at.is_(None),
            ))
        )
        msg_count = (await session.execute(msg_stmt)).scalar_one()
        return msg_count > 0


async def mark_app_welcome_sent(
    *,
    user_id: uuid.UUID,
    slack_channel_id: str,
) -> None:
    """App Home welcome 전송 후 chat_sessions.metadata에 welcome_sent=True 저장."""
    from sqlalchemy.orm.attributes import flag_modified

    channel = (slack_channel_id or "").strip()
    if not channel:
        return

    async with get_db_session() as session:
        stmt = (
            select(ChatSession)
            .where(and_(
                ChatSession.user_id == user_id,
                ChatSession.slack_channel_id == channel,
                ChatSession.status == "active",
            ))
            .order_by(ChatSession.updated_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        chat_session = result.scalar_one_or_none()

        if chat_session is None:
            chat_session = ChatSession(
                user_id=user_id,
                slack_channel_id=channel,
                session_type="slack_dm",
                metadata_={SESSION_METADATA_WELCOME_SENT: True},
            )
            session.add(chat_session)
        else:
            meta = dict(chat_session.metadata_ or {})
            meta[SESSION_METADATA_WELCOME_SENT] = True
            chat_session.metadata_ = meta
            flag_modified(chat_session, "metadata_")

        await session.flush()
        logger.info(
            "App welcome_sent 저장: user_id=%s channel=%s session_id=%s",
            user_id,
            channel,
            chat_session.id,
        )


async def create_new_session(
    user_id: uuid.UUID, **kwargs
) -> ChatSession:
    """새 채팅 생성. 기존 active session을 모두 archived."""
    async with get_db_session() as session:
        stmt = (
            update(ChatSession)
            .where(and_(
                ChatSession.user_id == user_id,
                ChatSession.status == "active",
            ))
            .values(status="archived")
        )
        await session.execute(stmt)

        new_session = ChatSession(user_id=user_id, **kwargs)
        session.add(new_session)
        await session.flush()
        return new_session


# =============================================================================
# Message CRUD
# =============================================================================

async def save_message(
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
    content: str,
    model_name: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
    parent_message_id: uuid.UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> ChatMessage:
    """메시지를 저장하고 세션의 last_message_at을 갱신한다."""
    async with get_db_session() as session:
        msg = ChatMessage(
            session_id=session_id, user_id=user_id,
            role=role, content=content,
            model_name=model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            parent_message_id=parent_message_id,
            metadata_=metadata or {},
        )
        session.add(msg)
        await session.execute(
            update(ChatSession)
            .where(ChatSession.id == session_id)
            .values(last_message_at=datetime.now(timezone.utc))
        )
        await session.flush()
        return msg


async def get_recent_messages(
    session_id: uuid.UUID,
    limit: int = RECENT_MESSAGES_LIMIT,
    after_message_id: uuid.UUID | None = None,
) -> list[ChatMessage]:
    """세션의 최근 메시지를 시간순으로 반환. after_message_id 이후만 가져올 수 있다."""
    async with get_db_session() as session:
        conditions = [
            ChatMessage.session_id == session_id,
            ChatMessage.deleted_at.is_(None),
        ]
        if after_message_id:
            ref_stmt = select(ChatMessage.created_at).where(
                ChatMessage.id == after_message_id
            )
            ref_result = await session.execute(ref_stmt)
            ref_time = ref_result.scalar_one_or_none()
            if ref_time:
                conditions.append(ChatMessage.created_at > ref_time)

        stmt = (
            select(ChatMessage)
            .where(and_(*conditions))
            .order_by(ChatMessage.created_at.desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        messages = list(result.scalars().all())
        messages.reverse()
        return messages


async def get_session_turn_count(session_id: uuid.UUID) -> int:
    """세션의 user 메시지 수(= 턴 수)."""
    async with get_db_session() as session:
        stmt = (
            select(func.count()).select_from(ChatMessage)
            .where(and_(
                ChatMessage.session_id == session_id,
                ChatMessage.role == "user",
                ChatMessage.deleted_at.is_(None),
            ))
        )
        result = await session.execute(stmt)
        return result.scalar() or 0


# =============================================================================
# LLM Context 조립
# =============================================================================

_USER_MEMORY_HEADER = (
    "이 사용자에 대해 기억하고 있는 정보입니다. "
    "선호·워크플로우 참고용이며, 회사 정책·수치·절차의 근거로 사용하지 마십시오.\n"
    "직접 언급하지 마십시오."
)


def _memory_record_to_line(mem: Any) -> str:
    if isinstance(mem, dict):
        memory_type = str(mem.get("memory_type") or "").strip()
        title = str(mem.get("title") or "").strip()
        content = str(mem.get("content") or "").strip()
    else:
        memory_type = str(getattr(mem, "memory_type", None) or "").strip()
        title = str(getattr(mem, "title", None) or "").strip()
        content = str(getattr(mem, "content", None) or "").strip()
    if not content:
        return ""
    prefix = f"[{memory_type}]" if memory_type else ""
    title_part = f" {title}:" if title else ""
    return f"- {prefix}{title_part} {content}"


def format_user_memory_block(lines: list[str]) -> str:
    """장기 메모리 항목을 <user_memory> 태그로 감싼다. 항목 없으면 빈 문자열."""
    cleaned = [line.strip() for line in lines if line and line.strip()]
    if not cleaned:
        return ""
    return (
        "<user_memory>\n"
        f"{_USER_MEMORY_HEADER}\n\n"
        + "\n".join(cleaned)
        + "\n</user_memory>"
    )


def format_user_memory_from_records(memories: list[Any]) -> str:
    """Memory ORM 또는 memories_raw dict 목록 → <user_memory> 블록."""
    lines = [_memory_record_to_line(mem) for mem in memories]
    return format_user_memory_block([line for line in lines if line])


def normalize_user_memory_context(memory_context: str) -> str:
    """이미 태그된 블록은 유지, 태그 없는 본문만 있으면 <user_memory>로 감싼다."""
    mem = (memory_context or "").strip()
    if not mem:
        return ""
    if "<user_memory>" in mem:
        return mem
    lines = [
        line if line.startswith("- ") else f"- {line}"
        for line in mem.splitlines()
        if line.strip()
    ]
    return format_user_memory_block(lines)


def resolve_memory_context_for_prompt(
    memory_context: str = "",
    memories_raw: list | None = None,
) -> str:
    """memory_context 우선, 없으면 memories_raw로 <user_memory> 블록 생성."""
    normalized = normalize_user_memory_context(memory_context)
    if normalized:
        return normalized
    return format_user_memory_from_records(memories_raw or [])


def _memory_matches_scope(
    mem: Memory,
    *,
    session_id: uuid.UUID | None,
    slack_channel_id: str | None,
) -> bool:
    """현재 세션·채널에 속한 메모리만 허용한다."""
    if session_id and mem.source_session_id == session_id:
        return True
    channel = (slack_channel_id or "").strip()
    if not channel:
        return False
    meta = mem.metadata_ if isinstance(mem.metadata_, dict) else {}
    return str(meta.get("slack_channel_id") or "").strip() == channel


async def load_scoped_memories(
    user_id: uuid.UUID,
    *,
    session_id: uuid.UUID | None = None,
    slack_channel_id: str | None = None,
    limit: int = 15,
) -> list[Memory]:
    """
    장기 메모리를 세션·채널 단위로 필터링한다.
    - source_session_id == 현재 session
    - metadata.slack_channel_id == 현재 channel
    사용자 전역(global) 메모리는 포함하지 않는다.
    """
    if not session_id and not (slack_channel_id or "").strip():
        return []

    async with get_db_session() as session:
        stmt = (
            select(Memory)
            .where(and_(Memory.user_id == user_id, Memory.status == "active"))
            .order_by(Memory.importance.desc())
            .limit(max(limit * 4, 40))
        )
        result = await session.execute(stmt)
        candidates = list(result.scalars().all())

    scoped: list[Memory] = []
    for mem in candidates:
        if _memory_matches_scope(
            mem,
            session_id=session_id,
            slack_channel_id=slack_channel_id,
        ):
            scoped.append(mem)
        if len(scoped) >= limit:
            break
    return scoped


async def get_session_slack_channel_id(session_id: uuid.UUID) -> str | None:
    """세션에 연결된 Slack channel_id를 반환한다."""
    async with get_db_session() as session:
        stmt = select(ChatSession.slack_channel_id).where(ChatSession.id == session_id)
        result = await session.execute(stmt)
        channel = result.scalar_one_or_none()
    channel_str = str(channel or "").strip()
    return channel_str or None


async def get_memory_context_for_prompt(
    user_id: uuid.UUID,
    *,
    session_id: uuid.UUID | None = None,
    slack_channel_id: str | None = None,
) -> str:
    """프롬프트에 삽입할 메모리 블록 (세션·채널 스코프). 없으면 빈 문자열."""
    memories = await load_scoped_memories(
        user_id,
        session_id=session_id,
        slack_channel_id=slack_channel_id,
        limit=15,
    )
    return format_user_memory_from_records(memories)


async def get_conversation_history(
    session_id: uuid.UUID,
) -> list[dict[str, str]]:
    """세션 히스토리를 OpenAI messages 형식으로 반환. summary가 있으면 요약+이후 메시지만."""
    async with get_db_session() as session:
        stmt = (
            select(SessionSummary)
            .where(SessionSummary.session_id == session_id)
            .order_by(SessionSummary.updated_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        summary = result.scalar_one_or_none()

    messages: list[dict[str, str]] = []
    after_message_id = None

    if summary and summary.summary:
        meta = summary.metadata_ if isinstance(summary.metadata_, dict) else {}
        progress = str(meta.get("progress") or "")
        messages.append({
            "role": "system",
            "content": format_session_summary_for_router(
                summary.summary,
                key_entities=summary.key_entities,
                decisions=summary.decisions,
                open_questions=summary.open_questions,
                progress=progress,
            ),
        })
        after_message_id = summary.covered_until_message_id

    recent = await get_recent_messages(
        session_id=session_id, after_message_id=after_message_id,
    )
    for msg in recent:
        if msg.role in ("user", "assistant"):
            messages.append({"role": msg.role, "content": msg.content})

    return messages


async def get_debug_context(
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    slack_channel_id: str | None = None,
) -> tuple[dict | None, list[dict]]:
    """디버그 로깅 전용: 세션 요약과 장기 메모리를 raw dict로 반환."""
    session_summary = None
    memories_list = []

    try:
        async with get_db_session() as session:
            stmt = (
                select(SessionSummary)
                .where(SessionSummary.session_id == session_id)
                .order_by(SessionSummary.updated_at.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            summary = result.scalar_one_or_none()
            if summary:
                session_summary = {
                    "summary": summary.summary,
                    "decisions": summary.decisions,
                    "open_questions": summary.open_questions,
                    "key_entities": summary.key_entities,
                    "covered_message_count": summary.covered_message_count,
                }

        memories = await load_scoped_memories(
            user_id,
            session_id=session_id,
            slack_channel_id=slack_channel_id,
            limit=20,
        )
        memories_list = [
            {
                "id": str(m.id),
                "memory_type": m.memory_type,
                "scope": m.scope,
                "title": m.title,
                "content": m.content,
                "importance": float(m.importance) if m.importance else 0.5,
                "source_session_id": str(m.source_session_id) if m.source_session_id else None,
                "slack_channel_id": (m.metadata_ or {}).get("slack_channel_id"),
            }
            for m in memories
        ]
    except Exception as e:
        logger.debug("get_debug_context 실패 (무시): %s", e)

    return session_summary, memories_list


# =============================================================================
# Chat attachments
# =============================================================================

async def save_chat_attachments(
    *,
    user_id: uuid.UUID,
    session_id: uuid.UUID | None,
    slack_thread_ts: str | None,
    user_text: str | None,
    records: list[dict[str, Any]],
) -> list[ChatAttachment]:
    """ingest된 첨부 메타를 chat_attachments 테이블에 저장."""
    if not records:
        return []

    saved: list[ChatAttachment] = []
    async with get_db_session() as session:
        for row in records:
            att = ChatAttachment(
                user_id=user_id,
                session_id=session_id,
                slack_thread_ts=slack_thread_ts,
                user_text=(user_text or "").strip() or None,
                attachment_path=str(row.get("attachment_path") or ""),
                attachment_title=str(row.get("attachment_title") or ""),
                attachment_summary=str(row.get("attachment_summary") or "") or None,
                attachment_kind=str(row.get("attachment_kind") or "") or None,
                slack_file_id=str(row.get("slack_file_id") or "") or None,
                metadata_=dict(row.get("metadata") or {}),
            )
            session.add(att)
            saved.append(att)
        await session.flush()
    logger.info("chat_attachments 저장: count=%s session=%s", len(saved), session_id)
    return saved


async def get_attachments_by_logical_ids(
    attachment_ids: list[str],
    *,
    session_id: uuid.UUID | None = None,
) -> list[ChatAttachment]:
    """metadata.attachment_id(att_*)로 chat_attachments 조회."""
    ids = [str(x).strip() for x in attachment_ids if str(x).strip()]
    if not ids:
        return []

    async with get_db_session() as session:
        stmt = select(ChatAttachment).where(
            ChatAttachment.metadata_["attachment_id"].astext.in_(ids),
        )
        if session_id is not None:
            stmt = stmt.where(ChatAttachment.session_id == session_id)
        stmt = stmt.order_by(ChatAttachment.created_at.desc())
        result = await session.execute(stmt)
        rows = list(result.scalars().all())

    # 동일 logical id는 최신 1건만
    seen: set[str] = set()
    unique: list[ChatAttachment] = []
    for row in rows:
        meta = row.metadata_ if isinstance(row.metadata_, dict) else {}
        logical_id = str(meta.get("attachment_id") or "")
        if not logical_id or logical_id in seen:
            continue
        seen.add(logical_id)
        unique.append(row)
    return unique


async def get_recent_session_attachments(
    session_id: uuid.UUID,
    *,
    limit: int = 10,
) -> list[ChatAttachment]:
    """세션 최근 첨부 (follow-up용)."""
    async with get_db_session() as session:
        stmt = (
            select(ChatAttachment)
            .where(ChatAttachment.session_id == session_id)
            .order_by(ChatAttachment.created_at.desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())
