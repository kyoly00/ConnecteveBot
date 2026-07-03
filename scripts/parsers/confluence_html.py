"""Confluence body.storage / view HTML → Markdown (표는 GFM 테이블)."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup
from markdownify import markdownify as _markdownify

TABLE_OPEN, TABLE_CLOSE = "[표 시작]", "[표 끝]"

# GFM 표: 헤더 행 + 구분선 + 데이터 행(1행 이상)
_MD_TABLE_BLOCK_RE = re.compile(
    r"(?:(?:^\|[^\n]+\|\s*\n)+(?:^\|[-:\s|]+\|\s*\n)(?:(?:^\|[^\n]+\|\s*\n))+)",
    re.MULTILINE,
)


def _strip_noise(soup: BeautifulSoup) -> None:
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()


def html_to_markdown(html: str, *, already_cleaned: bool = False) -> str:
    """HTML 조각을 마크다운으로 변환 (표·목록·헤딩 보존)."""
    if not (html or "").strip():
        return ""
    if already_cleaned:
        inner = html
    else:
        soup = BeautifulSoup(html, "html.parser")
        _strip_noise(soup)
        inner = soup.body.decode_contents() if soup.body else str(soup)
    if not inner.strip():
        return ""
    return _markdownify(
        inner,
        heading_style="ATX",
        bullets="-",
        strip=["script", "style"],
        escape_asterisks=False,
        escape_underscores=False,
    )


def wrap_markdown_tables(md: str) -> str:
    """GFM 표 블록을 [표 시작/끝]으로 감싸 청킹·분할 로직과 호환."""
    if not md or "|" not in md:
        return md

    def repl(m: re.Match[str]) -> str:
        block = m.group(0).strip()
        if TABLE_OPEN in block:
            return block
        return f"{TABLE_OPEN}\n{block}\n{TABLE_CLOSE}"

    return _MD_TABLE_BLOCK_RE.sub(repl, md)
