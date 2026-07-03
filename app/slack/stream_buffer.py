"""
stream_buffer.py — LLM 스트리밍 청크 누적 및 Slack 표시용 안전 텍스트 추출.

API stream 델타는 태그·마크다운 중간에 끊길 수 있으므로,
버퍼에 전부 누적한 뒤 '안전한 접두사'만 잘라 UI에 반영한다.
"""

from __future__ import annotations

import re
from typing import Protocol


# 불완전 XML 태그 (스트림 끝)
_INCOMPLETE_TAG_RE = re.compile(r"<[^>\n]*$")
# 줄 끝 미완성 단일 * (bold)
_UNCLOSED_SINGLE_STAR_RE = re.compile(r"(?<!\*)\*(?!\*)[^*\n]*$")
# 줄 끝 미완성 **
_UNCLOSED_DOUBLE_STAR_RE = re.compile(r"\*\*[^*\n]*$")
# 미완성 인라인 코드
_UNCLOSED_BACKTICK_RE = re.compile(r"`[^`\n]*$")
# 미완성 링크/괄호
_UNCLOSED_PAREN_RE = re.compile(r"\([^)\n]*$")

_ANSWER_OPEN_RE = re.compile(r"<answer>", re.IGNORECASE)
_ANSWER_CLOSE_RE = re.compile(r"</answer>", re.IGNORECASE)


def trim_unsafe_stream_suffix(text: str) -> str:
    """
    스트리밍 중 Slack mrkdwn이 깨지지 않도록 꼬리 불완전 조각을 제거한다.
    """
    if not text:
        return ""

    safe = text
    changed = True
    while changed and safe:
        changed = False
        for pattern in (
            _INCOMPLETE_TAG_RE,
            _UNCLOSED_DOUBLE_STAR_RE,
            _UNCLOSED_SINGLE_STAR_RE,
            _UNCLOSED_BACKTICK_RE,
            _UNCLOSED_PAREN_RE,
        ):
            m = pattern.search(safe)
            if m and m.start() < len(safe):
                safe = safe[: m.start()]
                changed = True
                break
    return safe


class StreamExtractor(Protocol):
    def push(self, delta: str) -> None: ...
    def safe_text(self) -> str: ...
    def raw_text(self) -> str: ...


class PlainTextStreamAccumulator:
    """일반 답변(general) 스트림 — 전체 버퍼 후 안전 접두사만 노출."""

    def __init__(self) -> None:
        self._buffer = ""

    def push(self, delta: str) -> None:
        if delta:
            self._buffer += delta

    def safe_text(self) -> str:
        return trim_unsafe_stream_suffix(self._buffer)

    def raw_text(self) -> str:
        return self._buffer


class RagXmlAnswerExtractor:
    """
    Turn2 XML 스트림 — <answer> 본문만 추출해 표시한다.
    <sources_used>, <attachments_used>, <links_used> 및 미완성 태그는 버퍼에만 보관한다.
    """

    def __init__(self) -> None:
        self._buffer = ""

    def push(self, delta: str) -> None:
        if delta:
            self._buffer += delta

    def raw_text(self) -> str:
        return self._buffer

    def safe_text(self) -> str:
        buf = self._buffer
        parts: list[str] = []
        pos = 0
        while True:
            m_open = _ANSWER_OPEN_RE.search(buf, pos)
            if not m_open:
                break
            content_start = m_open.end()
            rest = buf[content_start:]
            m_close = _ANSWER_CLOSE_RE.search(rest)
            if m_close:
                parts.append(rest[: m_close.start()])
                pos = content_start + m_close.end()
            else:
                parts.append(rest)
                break
        if not parts:
            return ""
        return trim_unsafe_stream_suffix("\n\n".join(parts))
