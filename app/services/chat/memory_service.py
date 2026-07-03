"""
services/memory_service.py — 세션 요약 + 장기 메모리 관리

- session_summary: 새 user 메시지 N개마다 GPT로 핵심 요약 생성 (증분)
- memories: user N턴/키워드 감지 시 장기 기억 추출
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

from openai import AsyncOpenAI
from sqlalchemy import select, update, and_

from app.db.connection import get_db_session
from app.db.models import ChatMessage, SessionSummary, Memory
from app.services.chat.chat_service import (
    get_recent_messages,
    get_session_slack_channel_id,
    get_session_turn_count,
    load_scoped_memories,
)

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from app.core.config import (
    SUMMARY_TRIGGER_TURNS,
    SUMMARY_INPUT_MESSAGE_LIMIT,
    SUMMARY_TOPIC_CHANGE_ENABLED,
    MEMORY_TRIGGER_TURNS,
    MEMORY_EXTRACTION_MODEL,
)

logger = logging.getLogger(__name__)

openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))

from app.core import rag_debug_logger


MEMORY_TRIGGER_KEYWORDS = [
    "기억해", "앞으로", "이 프로젝트에서는", "항상", "매번",
    "나는 보통", "내가 선호하는", "기본적으로", "언제나",
    "잊지 마", "메모해", "저장해",
]


def detect_memory_trigger_keywords(text: str) -> bool:
    return any(kw in text for kw in MEMORY_TRIGGER_KEYWORDS)


async def _get_latest_summary(session_id: uuid.UUID) -> SessionSummary | None:
    async with get_db_session() as session:
        stmt = (
            select(SessionSummary)
            .where(SessionSummary.session_id == session_id)
            .order_by(SessionSummary.updated_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def should_trigger_summary(
    session_id: uuid.UUID,
    user_text: str = "",
) -> bool:
    """N턴마다 또는 주제 변경 감지 시 요약."""
    turn_count = await get_session_turn_count(session_id)
    if turn_count < 4:
        return False

    if SUMMARY_TOPIC_CHANGE_ENABLED and (user_text or "").strip():
        if await _detect_topic_change(session_id, user_text):
            logger.info("주제 변경 감지 → 세션 요약 트리거: session=%s", session_id)
            return True

    if turn_count < SUMMARY_TRIGGER_TURNS:
        return False

    existing = await _get_latest_summary(session_id)
    if not existing:
        return True

    last_covered_turns = int(existing.covered_message_count or 0)
    new_user_turns = turn_count - last_covered_turns
    return new_user_turns >= SUMMARY_TRIGGER_TURNS


def _topic_tokens(text: str) -> set[str]:
    import re
    t = (text or "").strip().casefold()
    if not t:
        return set()
    parts = re.findall(r"[가-힣]{2,}|[a-z0-9]{3,}", t)
    stop = {"그리고", "있는", "없는", "해줘", "알려", "주세요", "관련", "같은", "이번", "다음"}
    return {p for p in parts if p not in stop}


async def _detect_topic_change(session_id: uuid.UUID, user_text: str) -> bool:
    existing = await _get_latest_summary(session_id)
    if not existing or not (existing.summary or existing.key_entities):
        return False

    user_tokens = _topic_tokens(user_text)
    if len(user_tokens) < 1:
        return False

    entity_text = " ".join(str(e) for e in (existing.key_entities or []))
    entity_text += " " + (existing.summary or "")
    entity_tokens = _topic_tokens(entity_text)
    if not entity_tokens:
        return False

    overlap = len(user_tokens & entity_tokens)
    ratio = overlap / max(len(user_tokens), 1)
    return ratio < 0.25 and len(user_text.strip()) >= 4


def _format_messages_for_summary(messages: list[ChatMessage]) -> str:
    lines = []
    for msg in messages:
        content = (msg.content or "")[:500]
        if msg.role == "user":
            lines.append(f"[user] {content}")
        elif msg.role == "assistant":
            # 실패 톤이 요약에 고정되지 않도록 assistant는 짧게
            if any(p in content[:80] for p in ("확인할 수 없", "확인되지 않", "죄송")):
                lines.append("[assistant] (이전 답변: 검색/확인 미완료 — 사실로 기록하지 말 것)")
            else:
                lines.append(f"[assistant] {content[:200]}")
    return "\n".join(lines)


async def update_session_summary(session_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """증분 요약: 마지막 요약 이후 메시지만 반영."""
    turn_count = await get_session_turn_count(session_id)
    existing = await _get_latest_summary(session_id)

    after_id = existing.covered_until_message_id if existing else None
    fetch_limit = max(SUMMARY_INPUT_MESSAGE_LIMIT, SUMMARY_TRIGGER_TURNS * 2)
    new_messages = await get_recent_messages(
        session_id, limit=fetch_limit, after_message_id=after_id,
    )

    if len(new_messages) < 2 and existing:
        return

    if not new_messages:
        messages_for_prompt = await get_recent_messages(
            session_id, limit=SUMMARY_INPUT_MESSAGE_LIMIT,
        )
    else:
        messages_for_prompt = new_messages

    messages_for_prompt = messages_for_prompt[-SUMMARY_INPUT_MESSAGE_LIMIT:]

    if len(messages_for_prompt) < 4:
        return

    conversation_text = _format_messages_for_summary(messages_for_prompt)
    prior_block = ""
    if existing and existing.summary:
        prior_block = f"\n기존 요약 (병합·갱신 — 이전 주제는 1문장 이하로 압축):\n{existing.summary}\n"

    prompt = f"""다음 대화를 분석하여 JSON만 반환하세요.

규칙:
- **가장 최근 user 주제·의도**를 summary의 중심으로 두세요. 이전 주제가 있으면 1문장 이하로만 언급하세요.
- decisions·key_entities도 **현재 진행 중인 주제** 위주로 유지하고, 이미 끝난 주제(CFO 승인·출장 한도 등)는 제거하세요.
- user가 물어본 **주제·의도**만 요약하세요.
- assistant의 "확인 불가/죄송" 응답은 시스템 한계이지 사실이 아닙니다. summary에 넣지 마세요.
- open_questions는 user가 아직 궁금해하는 **주제**만 (assistant 실패 문구 복사 금지).
{prior_block}
새 대화 (최근 {len(messages_for_prompt)}개 메시지):
{conversation_text}

반환 형식:
{{
  "summary": "user가 다룬 주제와 맥락 (2~4문장, 중립적)",
  "progress": "현재 진행 상태 한 문장 (예: 메일 초안 작성 중, 회의실 예약 확인 중)",
  "decisions": ["확정된 사항"],
  "open_questions": ["아직 문서로 답하지 못한 user 주제"],
  "key_entities": ["키워드"]
}}"""

    try:
        llm_messages = [
            {"role": "system", "content": "대화 요약 전문가. JSON만 반환. assistant 거절 톤은 요약에서 제외."},
            {"role": "user", "content": prompt},
        ]

        rag_debug_logger._write("08_memory_io.jsonl", {
            "ts": rag_debug_logger._ts(),
            "type": "session_summary_input",
            "session_id": str(session_id),
            "incremental_messages": len(messages_for_prompt),
            "user_turn_count": turn_count,
            "model": MEMORY_EXTRACTION_MODEL,
            "messages": llm_messages,
        })

        response = await openai_client.chat.completions.create(
            model=MEMORY_EXTRACTION_MODEL,
            messages=llm_messages,
            temperature=0.0,
            response_format={"type": "json_object"},
        )

        result_text = response.choices[0].message.content or "{}"
        result_data = json.loads(result_text)

        rag_debug_logger._write("08_memory_io.jsonl", {
            "ts": rag_debug_logger._ts(),
            "type": "session_summary_output",
            "session_id": str(session_id),
            "raw_output": result_text,
            "parsed": result_data,
        })

        all_recent = await get_recent_messages(session_id, limit=50)
        last_msg = all_recent[-1] if all_recent else messages_for_prompt[-1]

        async with get_db_session() as session:
            stmt = (
                select(SessionSummary)
                .where(SessionSummary.session_id == session_id)
                .order_by(SessionSummary.updated_at.desc())
                .limit(1)
            )
            res = await session.execute(stmt)
            row = res.scalar_one_or_none()

            if row:
                row.summary = result_data.get("summary", "")
                row.decisions = result_data.get("decisions", [])
                row.open_questions = result_data.get("open_questions", [])
                row.key_entities = result_data.get("key_entities", [])
                row.covered_until_message_id = last_msg.id
                row.covered_message_count = turn_count
                meta = dict(row.metadata_ or {})
                progress = str(result_data.get("progress") or "").strip()
                if progress:
                    meta["progress"] = progress
                row.metadata_ = meta
                from sqlalchemy.orm.attributes import flag_modified
                flag_modified(row, "metadata_")
            else:
                progress = str(result_data.get("progress") or "").strip()
                meta = {"progress": progress} if progress else {}
                session.add(SessionSummary(
                    session_id=session_id,
                    user_id=user_id,
                    summary=result_data.get("summary", ""),
                    decisions=result_data.get("decisions", []),
                    open_questions=result_data.get("open_questions", []),
                    key_entities=result_data.get("key_entities", []),
                    covered_until_message_id=last_msg.id,
                    covered_message_count=turn_count,
                    metadata_=meta,
                ))

        logger.info("세션 요약 업데이트: session=%s, user_turns=%d", session_id, turn_count)

    except Exception as e:
        logger.error("세션 요약 생성 실패: %s", e)


async def should_trigger_memory_extraction(session_id: uuid.UUID) -> bool:
    turn_count = await get_session_turn_count(session_id)
    return turn_count > 0 and turn_count % MEMORY_TRIGGER_TURNS == 0


async def extract_memories(
    session_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    messages = await get_recent_messages(session_id, limit=30)
    if len(messages) < 4:
        return

    slack_channel_id = await get_session_slack_channel_id(session_id)
    existing_memories = await load_scoped_memories(
        user_id,
        session_id=session_id,
        slack_channel_id=slack_channel_id,
        limit=20,
    )

    existing_text = ""
    if existing_memories:
        existing_text = "\n기존 메모리:\n" + "\n".join(
            f"- [{m.id}] [{m.memory_type}] {m.content}" for m in existing_memories
        )

    conversation_text = "\n".join(
        f"[{msg.role}] {msg.content[:300]}" for msg in messages[-20:] if msg.role == "user"
    )
    if not conversation_text.strip():
        return

    prompt = f"""다음 **user 메시지**에서만 장기 기억을 추출하세요.
assistant의 검색 실패/사과 응답은 무시하세요.
{existing_text}

user 메시지:
{conversation_text}

추출 기준: 선호도, 습관, 프로젝트 결정, 반복 워크플로우

JSON 객체로 반환: {{"memories": [...]}} 각 항목:
{{
  "action": "create" | "update" | "deprecate",
  "existing_id": null 또는 UUID,
  "memory_type": "preference | decision | constraint | workflow | fact",
  "scope": "channel | session",
  "title": "짧은 제목",
  "content": "자연어 한 문장",
  "importance": 0.1 ~ 1.0
}}

없으면 {{"memories": []}}"""

    try:
        llm_messages = [
            {"role": "system", "content": "메모리 추출 전문가. JSON만 반환."},
            {"role": "user", "content": prompt},
        ]

        rag_debug_logger._write("08_memory_io.jsonl", {
            "ts": rag_debug_logger._ts(),
            "type": "memory_extraction_input",
            "session_id": str(session_id),
            "user_id": str(user_id),
            "model": MEMORY_EXTRACTION_MODEL,
            "messages": llm_messages,
        })

        response = await openai_client.chat.completions.create(
            model=MEMORY_EXTRACTION_MODEL,
            messages=llm_messages,
            temperature=0.0,
            response_format={"type": "json_object"},
        )

        raw = response.choices[0].message.content or '{"memories": []}'
        data = json.loads(raw)

        rag_debug_logger._write("08_memory_io.jsonl", {
            "ts": rag_debug_logger._ts(),
            "type": "memory_extraction_output",
            "session_id": str(session_id),
            "raw_output": raw,
            "parsed": data,
        })

        if isinstance(data, dict):
            data = data.get("memories", data.get("items", []))
        if not isinstance(data, list) or not data:
            return

        async with get_db_session() as session:
            for item in data:
                action = item.get("action", "create")

                if action == "deprecate" and item.get("existing_id"):
                    try:
                        existing_uuid = uuid.UUID(item["existing_id"])
                        await session.execute(
                            update(Memory)
                            .where(Memory.id == existing_uuid)
                            .values(status="deprecated")
                        )
                    except (ValueError, Exception) as e:
                        logger.warning("메모리 deprecate 실패: %s", e)

                elif action == "update" and item.get("existing_id"):
                    try:
                        existing_uuid = uuid.UUID(item["existing_id"])
                        await session.execute(
                            update(Memory)
                            .where(Memory.id == existing_uuid)
                            .values(
                                content=item.get("content", ""),
                                title=item.get("title"),
                                importance=item.get("importance", 0.5),
                            )
                        )
                    except (ValueError, Exception) as e:
                        logger.warning("메모리 update 실패: %s", e)

                elif action == "create":
                    memory_metadata: dict[str, Any] = {}
                    if slack_channel_id:
                        memory_metadata["slack_channel_id"] = slack_channel_id
                    session.add(Memory(
                        user_id=user_id,
                        scope=item.get("scope", "channel"),
                        memory_type=item.get("memory_type", "fact"),
                        title=item.get("title"),
                        content=item.get("content", ""),
                        source_session_id=session_id,
                        importance=item.get("importance", 0.5),
                        confidence=0.7,
                        metadata_=memory_metadata,
                    ))

        logger.info("메모리 추출 완료: session=%s, items=%d", session_id, len(data))

    except Exception as e:
        logger.error("메모리 추출 실패: %s", e)


async def check_and_run_background_tasks(
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    user_text: str,
) -> None:
    if detect_memory_trigger_keywords(user_text):
        logger.info("메모리 키워드 감지: session=%s", session_id)
        await extract_memories(session_id, user_id)
        return

    if await should_trigger_summary(session_id, user_text=user_text):
        await update_session_summary(session_id, user_id)

    if await should_trigger_memory_extraction(session_id):
        await extract_memories(session_id, user_id)
