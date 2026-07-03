"""
services/improvement_service.py — 챗봇 품질 개선 이벤트 감지/기록

LLM tool calling 기반 불만족 감지 + 패턴 매칭 기반 fallback/RAG 품질 감지.
improvement_events 테이블에 이벤트를 기록하여 운영자가 나중에 확인할 수 있게 한다.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import Any

from openai import AsyncOpenAI
from sqlalchemy import select, and_, func

from app.db.connection import get_db_session
from app.db.models import ChatMessage, ImprovementEvent

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from app.core.config import MEMORY_EXTRACTION_MODEL

logger = logging.getLogger(__name__)

openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))


# =============================================================================
# Fallback / No-answer 패턴 (규칙 기반)
# =============================================================================

FALLBACK_PATTERNS = [
    "모르겠습니다", "문서에서 찾을 수 없습니다",
    "관련된 문서를 찾을 수 없습니다", "확인하기 어렵",
    "정보가 부족", "해당 정보를 찾지 못",
    "검색된 문서에서는 확인되지 않습니다",
    "정확한 정보를 제공하기 어렵",
]

LOW_CONFIDENCE_THRESHOLD = 0.1


# =============================================================================
# LLM Tool Calling 기반 불만족 감지
# =============================================================================

_DISSATISFACTION_TOOL = {
    "type": "function",
    "function": {
        "name": "report_user_dissatisfaction",
        "description": (
            "사용자 메시지가 이전 답변에 대한 불만족, 수정 요청, "
            "오류 지적, 재질문을 표현하고 있을 때 호출합니다."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "is_dissatisfied": {
                    "type": "boolean",
                    "description": "사용자가 불만족을 표현했는지 여부",
                },
                "reason": {
                    "type": "string",
                    "description": "불만족 사유 (한국어, 1~2문장)",
                },
                "severity": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "불만족 심각도",
                },
            },
            "required": ["is_dissatisfied", "reason", "severity"],
        },
    },
}


async def detect_dissatisfaction_via_llm(
    user_text: str,
    assistant_text: str | None = None,
) -> dict[str, Any] | None:
    """
    LLM tool calling으로 사용자 불만족 여부를 판단한다.
    불만족이 감지되면 {is_dissatisfied, reason, severity} dict를 반환.
    """
    messages = [
        {
            "role": "system",
            "content": (
                "사용자의 최신 메시지가 이전 어시스턴트 답변에 대한 "
                "불만족, 오류 지적, 수정 요청, 재질문인지 판단하세요.\n"
                "불만족이라면 report_user_dissatisfaction 도구를 호출하세요.\n"
                "불만족이 아니라면 도구를 호출하지 마세요."
            ),
        },
    ]

    if assistant_text:
        messages.append({
            "role": "assistant",
            "content": assistant_text[:500],
        })

    messages.append({
        "role": "user",
        "content": user_text,
    })

    # try:
    #     response = await openai_client.chat.completions.create(
    #         model=MEMORY_EXTRACTION_MODEL,
    #         messages=messages,
    #         tools=[_DISSATISFACTION_TOOL],
    #         tool_choice="auto",
    #         temperature=0.0,
    #         max_tokens=200,
    #     )

    #     msg = response.choices[0].message
    #     if not msg.tool_calls:
    #         return None

    #     for tc in msg.tool_calls:
    #         if tc.function.name == "report_user_dissatisfaction":
    #             args = json.loads(tc.function.arguments)
    #             if args.get("is_dissatisfied"):
    #                 return args
    #     return None

    # except Exception as e:
    # logger.warning("LLM 불만족 감지 실패: %s", e)
    return None


# =============================================================================
# 규칙 기반 감지 함수들
# =============================================================================

def detect_fallback_answer(assistant_content: str) -> bool:
    """assistant 답변이 fallback 패턴에 해당하는지 확인."""
    if not assistant_content:
        return False
    content_lower = assistant_content.lower()
    return any(p in content_lower for p in FALLBACK_PATTERNS)


def detect_no_rag_result(docs: list | None) -> bool:
    """RAG 검색 결과가 없거나 비어있는지 확인."""
    return not docs or len(docs) == 0


def detect_low_confidence_rag(docs: list | None) -> bool:
    """RAG 검색 결과의 점수가 모두 낮은지 확인."""
    if not docs:
        return False
    scores = []
    for doc in docs:
        score = getattr(doc, "score", None)
        if score is not None:
            scores.append(float(score))
    if not scores:
        return False
    return max(scores) < LOW_CONFIDENCE_THRESHOLD


async def detect_repeated_question(
    session_id: uuid.UUID,
    user_content: str,
) -> tuple[bool, int]:
    """같은 세션에서 유사한 질문이 반복되었는지 확인. 단순 문자열 유사도 사용."""
    async with get_db_session() as session:
        stmt = (
            select(ChatMessage)
            .where(and_(
                ChatMessage.session_id == session_id,
                ChatMessage.role == "user",
                ChatMessage.deleted_at.is_(None),
            ))
            .order_by(ChatMessage.created_at.desc())
            .limit(10)
        )
        result = await session.execute(stmt)
        prev_messages = result.scalars().all()

    if len(prev_messages) < 2:
        return False, 0

    # 간단한 유사도: 공통 단어 비율
    current_words = set(user_content.lower().split())
    similar_count = 0

    for msg in prev_messages[1:]:  # 현재 메시지 제외
        prev_words = set(msg.content.lower().split())
        if not current_words or not prev_words:
            continue
        overlap = len(current_words & prev_words)
        union = len(current_words | prev_words)
        if union > 0 and overlap / union > 0.6:
            similar_count += 1

    return similar_count >= 2, similar_count


# =============================================================================
# 이벤트 기록
# =============================================================================

async def _create_event(
    user_id: uuid.UUID,
    session_id: uuid.UUID | None,
    message_id: uuid.UUID | None,
    assistant_message_id: uuid.UUID | None,
    event_type: str,
    severity: str = "medium",
    user_query: str | None = None,
    assistant_answer: str | None = None,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
    repeated_count: int = 1,
) -> None:
    """improvement_events 테이블에 이벤트를 기록한다."""
    async with get_db_session() as session:
        event = ImprovementEvent(
            user_id=user_id,
            session_id=session_id,
            message_id=message_id,
            assistant_message_id=assistant_message_id,
            event_type=event_type,
            severity=severity,
            user_query=user_query[:2000] if user_query else None,
            assistant_answer=assistant_answer[:2000] if assistant_answer else None,
            reason=reason,
            repeated_count=repeated_count,
            metadata_=metadata or {},
        )
        session.add(event)

    logger.info(
        "Improvement event 생성: type=%s, severity=%s, reason=%s",
        event_type, severity, reason,
    )


# =============================================================================
# 통합 감지 파이프라인
# =============================================================================

async def detect_and_log_improvement_events(
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    user_msg_id: uuid.UUID | None,
    assistant_msg_id: uuid.UUID | None,
    user_text: str,
    assistant_text: str,
    docs: list | None = None,
    intent: str = "rag",
) -> None:
    """
    답변 생성 후 호출. 여러 감지 규칙을 순회하며 해당하는 이벤트를 기록.
    """
    try:
        # 1. Fallback 답변 감지
        if detect_fallback_answer(assistant_text):
            await _create_event(
                user_id=user_id, session_id=session_id,
                message_id=user_msg_id,
                assistant_message_id=assistant_msg_id,
                event_type="fallback_answer",
                severity="medium",
                user_query=user_text,
                assistant_answer=assistant_text,
                reason="어시스턴트가 fallback 답변을 반환함",
            )

        # 2. RAG 결과 없음
        if intent == "rag" and detect_no_rag_result(docs):
            await _create_event(
                user_id=user_id, session_id=session_id,
                message_id=user_msg_id,
                assistant_message_id=assistant_msg_id,
                event_type="no_rag_result",
                severity="high",
                user_query=user_text,
                reason="RAG 검색 결과가 없음",
            )

        # 3. RAG 점수 낮음
        if intent == "rag" and detect_low_confidence_rag(docs):
            scores = [
                round(float(getattr(d, "score", 0)), 3)
                for d in (docs or [])
            ]
            await _create_event(
                user_id=user_id, session_id=session_id,
                message_id=user_msg_id,
                assistant_message_id=assistant_msg_id,
                event_type="low_confidence_answer",
                severity="medium",
                user_query=user_text,
                reason=f"RAG 검색 점수가 낮음: {scores}",
                metadata={"rag_scores": scores},
            )

        # 4. 반복 질문
        is_repeated, repeat_count = await detect_repeated_question(
            session_id, user_text,
        )
        if is_repeated:
            await _create_event(
                user_id=user_id, session_id=session_id,
                message_id=user_msg_id,
                assistant_message_id=assistant_msg_id,
                event_type="repeated_question",
                severity="medium",
                user_query=user_text,
                reason=f"유사 질문 {repeat_count}회 반복",
                repeated_count=repeat_count,
            )

        # 5. LLM 기반 사용자 불만족 감지 (tool calling)
        dissatisfaction = await detect_dissatisfaction_via_llm(
            user_text, assistant_text,
        )
        if dissatisfaction:
            await _create_event(
                user_id=user_id, session_id=session_id,
                message_id=user_msg_id,
                assistant_message_id=assistant_msg_id,
                event_type="user_dissatisfaction",
                severity=dissatisfaction.get("severity", "medium"),
                user_query=user_text,
                assistant_answer=assistant_text,
                reason=dissatisfaction.get("reason", "LLM이 불만족으로 판단"),
                metadata={"detection_method": "llm_tool_calling"},
            )

    except Exception as e:
        logger.error("Improvement event 감지 실패: %s", e)
