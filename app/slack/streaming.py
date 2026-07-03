"""
slack_streaming.py — Slack chat_update 스트리밍 출력 (버퍼 + 스로틀).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.slack.stream_buffer import PlainTextStreamAccumulator, RagXmlAnswerExtractor, StreamExtractor
from app.slack.ui import SlackFormatter, make_section, clean_blocks

logger = logging.getLogger(__name__)

_SLACK_SECTION_MAX = 2900
_STREAM_CURSOR = " ▍"
_DEFAULT_MIN_INTERVAL_SEC = 0.2


def make_streaming_blocks(display_text: str, *, show_cursor: bool = True) -> list[dict[str, Any]]:
    """스트리밍 중 단일 section 블록 (테이블·헤더 파싱 없이 안정적으로 갱신)."""
    text = (display_text or "").strip()
    if not text:
        return [
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "_답변을 작성하고 있습니다…_"}],
            }
        ]

    if show_cursor:
        text = f"{text}{_STREAM_CURSOR}"

    if len(text) > _SLACK_SECTION_MAX:
        text = text[: _SLACK_SECTION_MAX - 1] + "…"

    section = make_section(text, docs=None)
    return clean_blocks([section]) if section else []


class SlackStreamUpdater:
    """
    LLM 스트림 델타 → 버퍼 누적 → Slack mrkdwn 변환 → chat_update (스로틀).
    """

    def __init__(
        self,
        client: Any,
        *,
        channel_id: str,
        message_ts: str,
        min_interval_sec: float = _DEFAULT_MIN_INTERVAL_SEC,
    ) -> None:
        self._client = client
        self._channel_id = channel_id
        self._message_ts = message_ts
        self._min_interval = min_interval_sec
        self._extractor: StreamExtractor = PlainTextStreamAccumulator()
        self._last_slack_text = ""
        self._last_update_mono = 0.0
        self._lock = asyncio.Lock()
        self._streaming = False

    def use_plain_extractor(self) -> None:
        self._extractor = PlainTextStreamAccumulator()

    def use_rag_extractor(self) -> None:
        self._extractor = RagXmlAnswerExtractor()

    async def set_status(self, status_text: str) -> None:
        """검색 중 등 중간 상태 메시지."""
        blocks = [
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": status_text}],
            }
        ]
        async with self._lock:
            await self._client.chat_update(
                channel=self._channel_id,
                ts=self._message_ts,
                blocks=blocks,
                text=status_text,
            )
            self._last_slack_text = ""
            self._last_update_mono = time.monotonic()

    async def feed(self, delta: str) -> None:
        """API stream 델타 1개 수신."""
        if not delta:
            return
        self._streaming = True
        self._extractor.push(delta)
        await self._maybe_update(force=False, show_cursor=True)

    async def flush(self) -> str:
        """스트림 종료 후 마지막 갱신. 반환: raw safe text (포맷 전)."""
        self._streaming = False
        await self._maybe_update(force=True, show_cursor=False)
        return self._extractor.safe_text()

    @property
    def raw_full_text(self) -> str:
        return self._extractor.raw_text()

    async def _maybe_update(self, *, force: bool, show_cursor: bool) -> None:
        raw_safe = self._extractor.safe_text()
        slack_text = SlackFormatter.to_slack(raw_safe)
        if not slack_text and not force:
            return

        now = time.monotonic()
        if not force and (now - self._last_update_mono) < self._min_interval:
            return
        if slack_text == self._last_slack_text and not show_cursor:
            return

        blocks = make_streaming_blocks(slack_text, show_cursor=show_cursor)
        fallback = slack_text or "답변을 생성하고 있습니다…"

        async with self._lock:
            try:
                await self._client.chat_update(
                    channel=self._channel_id,
                    ts=self._message_ts,
                    blocks=blocks,
                    text=fallback[:_SLACK_SECTION_MAX],
                )
                self._last_slack_text = slack_text
                self._last_update_mono = time.monotonic()
            except Exception as exc:
                logger.warning("Slack 스트리밍 업데이트 실패: %s", exc)
