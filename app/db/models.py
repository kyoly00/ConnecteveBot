"""
database/models.py — SQLAlchemy 2.0 ORM 모델

7개 테이블: users, chat_sessions, chat_messages,
session_summaries, memories, improvement_events, managed_room_events
(legacy: room_bookings, bot_jobs)

DDL과 1:1 대응한다.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """모든 ORM 모델의 공통 베이스."""
    pass


# =============================================================================
# 1. User
# =============================================================================

class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    slack_user_id: Mapped[Optional[str]] = mapped_column(
        Text, unique=True, nullable=True,
    )
    email: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    profile_image_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow,
    )
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"),
    )

    # Relationships
    sessions: Mapped[list["ChatSession"]] = relationship(back_populates="user")
    messages: Mapped[list["ChatMessage"]] = relationship(back_populates="user")
    memories: Mapped[list["Memory"]] = relationship(back_populates="user")

    def __repr__(self) -> str:
        return f"<User {self.slack_user_id or self.id}>"


# =============================================================================
# 2. ChatSession
# =============================================================================

class ChatSession(Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (
        Index("ix_chat_sessions_user_status_updated", "user_id", "status", "updated_at"),
        Index("ix_chat_sessions_user_project_updated", "user_id", "project_id", "updated_at"),
        Index("ix_chat_sessions_slack", "slack_channel_id", "slack_thread_ts"),
        Index("ix_chat_sessions_last_message", "last_message_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'"),
    )
    session_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'manual'"),
    )
    slack_channel_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    slack_thread_ts: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    project_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow,
    )
    last_message_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"),
    )

    # Relationships
    user: Mapped["User"] = relationship(back_populates="sessions")
    messages: Mapped[list["ChatMessage"]] = relationship(back_populates="session")
    summaries: Mapped[list["SessionSummary"]] = relationship(back_populates="session")

    def __repr__(self) -> str:
        return f"<ChatSession {self.id} status={self.status}>"


# =============================================================================
# 3. ChatMessage
# =============================================================================

class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_chat_messages_session_created", "session_id", "created_at"),
        Index("ix_chat_messages_user_created", "user_id", "created_at"),
        Index("ix_chat_messages_role", "role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parent_message_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"),
    )

    # Relationships
    session: Mapped["ChatSession"] = relationship(back_populates="messages")
    user: Mapped["User"] = relationship(back_populates="messages")

    def __repr__(self) -> str:
        return f"<ChatMessage {self.id} role={self.role}>"


# =============================================================================
# 4. SessionSummary
# =============================================================================

class SessionSummary(Base):
    __tablename__ = "session_summaries"
    __table_args__ = (
        Index("ix_session_summaries_session_updated", "session_id", "updated_at"),
        Index("ix_session_summaries_user_updated", "user_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    decisions: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"),
    )
    open_questions: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"),
    )
    key_entities: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"),
    )
    covered_until_message_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True,
    )
    covered_message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow,
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"),
    )

    # Relationships
    session: Mapped["ChatSession"] = relationship(back_populates="summaries")

    def __repr__(self) -> str:
        return f"<SessionSummary {self.id} session={self.session_id}>"


# =============================================================================
# 5. Memory
# =============================================================================

class Memory(Base):
    __tablename__ = "memories"
    __table_args__ = (
        Index("ix_memories_user_project_status", "user_id", "project_id", "status"),
        Index("ix_memories_user_scope_status", "user_id", "scope", "status"),
        Index("ix_memories_type_status", "memory_type", "status"),
        Index("ix_memories_importance", "importance"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    project_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    memory_type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True,
    )
    source_message_ids: Mapped[Optional[list]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True,
    )
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow,
    )
    last_accessed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"),
    )

    # Relationships
    user: Mapped["User"] = relationship(back_populates="memories")

    def __repr__(self) -> str:
        return f"<Memory {self.id} type={self.memory_type} status={self.status}>"


# =============================================================================
# 6. ImprovementEvent
# =============================================================================

class ImprovementEvent(Base):
    __tablename__ = "improvement_events"
    __table_args__ = (
        Index("ix_improvement_events_user_status_created", "user_id", "status", "created_at"),
        Index("ix_improvement_events_session_created", "session_id", "created_at"),
        Index("ix_improvement_events_type_status", "event_type", "status"),
        Index("ix_improvement_events_severity_status", "severity", "status"),
        Index("ix_improvement_events_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="SET NULL"), nullable=True,
    )
    message_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True,
    )
    assistant_message_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True,
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'medium'"),
    )
    user_query: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    assistant_answer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    similar_message_ids: Mapped[Optional[list]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True,
    )
    repeated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'open'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow,
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"),
    )

    def __repr__(self) -> str:
        return f"<ImprovementEvent {self.id} type={self.event_type} severity={self.severity}>"


# =============================================================================
# 7. ManagedRoomEvent — 회의실 캘린더 Outlook projection (read model)
# =============================================================================

class ManagedRoomEvent(Base):
    """관리 대상 회의실 3곳의 Outlook 일정 로컬 projection. 원본은 Graph."""

    __tablename__ = "managed_room_events"
    __table_args__ = (
        Index("ix_managed_room_events_room_start", "room_email", "start_time"),
        Index("ix_managed_room_events_organizer_start", "organizer_email", "start_time"),
        Index("ix_managed_room_events_status_start", "status", "start_time"),
        Index(
            "uq_managed_room_events_room_outlook",
            "room_email",
            "outlook_event_id",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    room_email: Mapped[str] = mapped_column(Text, nullable=False)
    room_name: Mapped[str] = mapped_column(Text, nullable=False)
    room_display: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    event_subject: Mapped[str] = mapped_column(Text, nullable=False)
    start_time: Mapped[str] = mapped_column(Text, nullable=False)
    end_time: Mapped[str] = mapped_column(Text, nullable=False)
    organizer_email: Mapped[str] = mapped_column(Text, nullable=False)
    organizer_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    outlook_event_id: Mapped[str] = mapped_column(Text, nullable=False)
    organizer_event_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attendee_emails: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"),
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'"),
    )
    bot_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    bot_slack_user_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    slack_channel_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reminder_minutes_before: Mapped[Optional[int]] = mapped_column(nullable=True)
    reminder_sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
    )

    def __repr__(self) -> str:
        return (
            f"<ManagedRoomEvent {self.id} room={self.room_display} "
            f"status={self.status}>"
        )


# =============================================================================
# 7b. RoomBooking — legacy (room_bookings), 신규 코드는 ManagedRoomEvent 사용
# =============================================================================

class RoomBooking(Base):
    __tablename__ = "room_bookings"
    __table_args__ = (
        Index("ix_room_bookings_user_status_created", "user_id", "status", "created_at"),
        Index("ix_room_bookings_slack_status_created", "slack_user_id", "status", "created_at"),
        Index("ix_room_bookings_outlook_event_id", "outlook_event_id"),
        Index("ix_room_bookings_room_start", "room_name", "start_time"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    slack_user_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    room_name: Mapped[str] = mapped_column(Text, nullable=False)
    room_display: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    event_subject: Mapped[str] = mapped_column(Text, nullable=False)
    start_time: Mapped[str] = mapped_column(Text, nullable=False)
    end_time: Mapped[str] = mapped_column(Text, nullable=False)
    organizer_email: Mapped[str] = mapped_column(Text, nullable=False)
    outlook_event_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'"),
    )
    room_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
    )
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    slack_channel_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reminder_minutes_before: Mapped[Optional[int]] = mapped_column(nullable=True)
    reminder_sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    def __repr__(self) -> str:
        return f"<RoomBooking {self.id} room={self.room_name} status={self.status}>"


# =============================================================================
# 8. ChatAttachment — Slack/채팅 첨부 메타 (파일은 Data/chat_attachments)
# =============================================================================

class ChatAttachment(Base):
    __tablename__ = "chat_attachments"
    __table_args__ = (
        Index("ix_chat_attachments_user_created", "user_id", "created_at"),
        Index("ix_chat_attachments_session_created", "session_id", "created_at"),
        Index("ix_chat_attachments_slack_thread", "slack_thread_ts"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="SET NULL"), nullable=True,
    )
    slack_thread_ts: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    user_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attachment_path: Mapped[str] = mapped_column(Text, nullable=False)
    attachment_title: Mapped[str] = mapped_column(Text, nullable=False)
    attachment_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attachment_kind: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    slack_file_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb"),
    )

    user: Mapped["User"] = relationship()
    session: Mapped[Optional["ChatSession"]] = relationship()

    def __repr__(self) -> str:
        return f"<ChatAttachment {self.id} title={self.attachment_title!r}>"


# =============================================================================
# 9. BotJob — 웹훅 비동기 작업 큐 (Slack / Graph / Confluence)
# =============================================================================

class BotJob(Base):
    __tablename__ = "bot_jobs"
    __table_args__ = (
        UniqueConstraint("source", "source_event_id", name="uq_bot_jobs_source_event"),
        Index("ix_bot_jobs_status_next_run", "status", "next_run_at"),
        Index("ix_bot_jobs_conversation_key", "conversation_key", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_event_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)

    team_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    channel_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    user_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    thread_ts: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    event_ts: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    conversation_key: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'queued'"),
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"),
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3"),
    )

    locked_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    locked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
    )

    error_code: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow,
    )

    def __repr__(self) -> str:
        return (
            f"<BotJob {self.id} {self.source}:{self.source_event_id} "
            f"status={self.status}>"
        )
