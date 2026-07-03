import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.core.config import resolve_attachment_public_url, resolve_attachment_url

IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".heic", ".gif",
    ".tif", ".tiff", ".svg",
}


# =============================================================================
# Slack Formatting Utils
# =============================================================================

# Slack mrkdwn: *굵게* 직후 한글(조사 등)이 붙으면 bold가 깨져 *가 그대로 보임
_ZWSP = "\u200b"
_SLACK_BOLD_BEFORE_HANGUL = re.compile(
    r"(\*[^*\n]+?\*)(?=[\uac00-\ud7a3])"
)


class SlackFormatter:
    """
    LLM의 기본 Markdown 출력을 Slack mrkdwn 형태로 변경한다.

    핵심 원칙:
    - 일반 답변은 section + mrkdwn으로 처리한다.
    - Markdown table은 별도 parser에서 Slack native table block으로 처리한다.
    - Perplexity-style citation [1]은 여기서 건드리지 않고,
      render_citations_to_slack()에서 문서 링크로 변환한다.
    """

    @staticmethod
    def _fix_slack_bold_before_hangul(text: str) -> str:
        """
        닫는 * 바로 뒤에 한글이 오면 Slack이 bold를 인식하지 못하는 경우가 많다.
        조사(에, 이, 은 …)뿐 아니라 붙는 음절 전체에 zero-width space를 넣는다.
        """
        return _SLACK_BOLD_BEFORE_HANGUL.sub(rf"\1{_ZWSP}", text)

    @staticmethod
    def to_slack(text: str) -> str:
        if not text:
            return ""

        text = str(text)

        # 코드블록 내부는 변환하지 않는다.
        code_blocks: list[str] = []

        def stash_code_block(match: re.Match) -> str:
            code_blocks.append(match.group(0))
            return f"@@CODE_BLOCK_{len(code_blocks) - 1}@@"

        text = re.sub(r"```[\s\S]*?```", stash_code_block, text)

        # Markdown link 변환: [텍스트](URL) -> <URL|텍스트>
        # citation [1], [2]는 제외
        text = re.sub(
            r"\[((?!\d+\]).+?)\]\((https?://[^)\s]+)\)",
            r"<\2|\1>",
            text,
        )

        # 굵게 변환: **텍스트** -> *텍스트*
        text = re.sub(r'\*\*(.*?)\*\*', r'*\1*', text)
        text = SlackFormatter._fix_slack_bold_before_hangul(text)

        # 취소선 변환: ~~텍스트~~ -> ~텍스트~
        text = re.sub(r"~~(.*?)~~", r"~\1~", text)

        # 일반 텍스트에서는 <br>을 실제 개행으로 변환
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)

        # 코드블록 복구
        for i, code in enumerate(code_blocks):
            text = text.replace(f"@@CODE_BLOCK_{i}@@", code)

        return text.strip()

# =============================================================================
# Header Utils
# =============================================================================

HEADER_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def make_header_block(text: str, level: int = 1) -> dict[str, Any]:
    """
    Slack rich_text header block 생성.

    예:
    # 제목
    ->
    {
        "type": "header",
        "text": {
            "type": "plain_text",
            "text": "제목",
            "emoji": True
        },
        "level": 1
    }
    """
    level = max(1, min(int(level), 6))

    return {
        "type": "header",
        "text": {
            "type": "plain_text",
            "text": str(text).strip(),
            "emoji": True,
        },
        "level": level,
    }


def parse_markdown_header(line: str) -> tuple[int, str] | None:
    """
    Markdown header 파싱.

    예:
    ## 휴가 정책
    -> (2, "휴가 정책")
    """
    if not line:
        return None

    match = HEADER_PATTERN.match(str(line).strip())
    if not match:
        return None

    hashes, title = match.groups()
    return len(hashes), title.strip()

# =============================================================================
# Markdown Table Detection / Splitting
# =============================================================================

def _table_row_cells(line: str) -> list[str]:
    """파이프 구분 행을 셀 목록으로 분리한다."""
    stripped = str(line).strip()
    if stripped.startswith("|"):
        if stripped.endswith("|"):
            body = stripped[1:-1]
        else:
            body = stripped[1:]
        return [cell.strip() for cell in body.split("|")]
    return [cell.strip() for cell in stripped.split("|")]


def _is_separator_cell(cell: str) -> bool:
    return re.fullmatch(r":?\s*-{3,}\s*:?", (cell or "").strip()) is not None


def normalize_table_row(line: str) -> str:
    """느슨한 pipe row를 GFM 표준 형식(| ... |)으로 맞춘다."""
    stripped = str(line).strip()
    if not stripped:
        return stripped

    if not stripped.startswith("|"):
        stripped = f"| {stripped}"
    if not stripped.endswith("|"):
        stripped = f"{stripped} |"
    return stripped


def is_table_line(line: str) -> bool:
    """
    정상적인 Markdown table row인지 판단한다.

    예:
    | 평가 항목 | 우리 회사 | 일반 스타트업 |
    |---|---|---|
    """
    line = str(line).strip()

    if is_table_separator_line(line):
        return False

    if line.startswith("|") and line.count("|") >= 2:
        return line.endswith("|") or line.count("|") >= 3

    if "|" in line:
        cells = _table_row_cells(line)
        return len(cells) >= 2 and any(cells)

    return False


def is_table_separator_line(line: str) -> bool:
    """
    Markdown table separator line인지 확인한다.

    예:
    |---|---|---|
    |:---|---:|:---:|
    ---|---|---
    """
    line = str(line).strip()

    if "|" not in line:
        return False

    cells = _table_row_cells(line)
    if len(cells) < 2:
        return False

    return all(_is_separator_cell(cell) for cell in cells)


def looks_like_table_start(line: str) -> bool:
    """
    표 row 후보인지 느슨하게 판단한다.

    GFM(| ... |)과 앞뒤 pipe 없는 느슨한 형식(항목 | 값 | ...) 모두 지원.
    """
    line = str(line).strip()

    if is_table_separator_line(line):
        return True

    if line.startswith("|") and line.count("|") >= 2:
        return True

    if "|" in line:
        cells = _table_row_cells(line)
        return len(cells) >= 2 and any(cells)

    return False


def split_answer_into_parts(answer: str) -> list[dict[str, Any]]:
    """
    답변 전체를 text/table part로 분리한다.

    핵심:
    - 표 row 안에 실제 \\n이 있어도 section으로 빼지 않는다.
    - 깨진 table row는 다음 줄들을 계속 붙여서 하나의 row로 만든다.
    - 표 내부 줄바꿈은 최종 Slack table cell text의 "\\n"으로 유지한다.

    반환 예:
    [
        {"type": "text", "text": "..."},
        {"type": "table", "rows": ["| h1 | h2 |", "|---|---|", "| a\\nb | c |"]},
        {"type": "text", "text": "..."},
    ]
    """
    lines = str(answer).splitlines()

    parts: list[dict[str, Any]] = []
    text_buffer: list[str] = []
    table_rows: list[str] = []

    in_table = False
    pending_row: str | None = None

    def flush_text() -> None:
        nonlocal text_buffer

        text = "\n".join(text_buffer).strip()
        if text:
            parts.append(
                {
                    "type": "text",
                    "text": text,
                }
            )

        text_buffer = []

    def flush_pending_row() -> None:
        nonlocal pending_row

        if pending_row is not None:
            table_rows.append(normalize_table_row(pending_row))
            pending_row = None

    def flush_table() -> None:
        nonlocal table_rows, in_table

        flush_pending_row()

        if table_rows:
            parts.append(
                {
                    "type": "table",
                    "rows": table_rows,
                }
            )

        table_rows = []
        in_table = False

    for line in lines:
        stripped = line.strip()

        # 빈 줄이면 현재 table/text part를 구분한다.
        if not stripped:
            if in_table:
                flush_table()
            else:
                text_buffer.append(line)
            continue

        # 표 시작 또는 표 내부 row 후보
        if looks_like_table_start(stripped):
            if not in_table:
                # 구분선만 먼저 나오면 직전 줄을 헤더로 본다.
                if (
                    is_table_separator_line(stripped)
                    and text_buffer
                    and "|" in text_buffer[-1]
                ):
                    header_line = text_buffer.pop()
                    flush_text()
                    in_table = True
                    table_rows.append(normalize_table_row(header_line))
                    table_rows.append(normalize_table_row(stripped))
                    continue

                flush_text()
                in_table = True

            flush_pending_row()

            # 정상 row, separator, 또는 느슨한 pipe row
            if (
                is_table_line(stripped)
                or is_table_separator_line(stripped)
                or ("|" in stripped and len(_table_row_cells(stripped)) >= 2)
            ):
                table_rows.append(normalize_table_row(stripped))
            else:
                # | 로 시작하지만 | 로 끝나지 않는 깨진 row 시작
                pending_row = stripped

            continue

        # 표 내부 continuation line
        if in_table:
            if pending_row is not None:
                # 중요:
                # 여기서는 <br>가 아니라 실제 \n을 유지한다.
                # 단, 이 \n은 row 문자열 내부에만 존재하고 다시 splitlines()로 쪼개지지 않는다.
                pending_row = pending_row.rstrip() + "\n" + stripped

                # row가 닫혔으면 table row로 확정
                if pending_row.strip().endswith("|") and pending_row.count("|") >= 2:
                    flush_pending_row()

                continue

            # pending row가 없는데 일반 줄이 나오면 표 종료 후 일반 텍스트로 처리
            flush_table()
            text_buffer.append(line)
            continue

        # 일반 텍스트
        text_buffer.append(line)

    if in_table:
        flush_table()
    else:
        flush_text()

    return parts


# =============================================================================
# Citation / Source Utils
# =============================================================================

CITATION_PATTERN = re.compile(r"\[(\d+)\]")
SLACK_CITATION_LINK_PATTERN = re.compile(
    r"<https?://[^|>\s]+\|\[(\d+)\]>",
    re.IGNORECASE,
)


def strip_body_citations(text: str) -> str:
    """답변 본문에서 문서 출처 표기·XML 태그 잔여를 제거한다."""
    if not text:
        return ""

    out = str(text)
    out = re.sub(
        r"<sources_used>[\s\S]*?</sources_used>",
        "",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(r"</?answer>", "", out, flags=re.IGNORECASE)
    out = re.sub(r"</?sources_used>", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\[문서\s*(\d+)\]", "", out)
    out = SLACK_CITATION_LINK_PATTERN.sub("", out)
    out = CITATION_PATTERN.sub("", out)
    out = re.sub(
        r"답변은\s+.+?\s+문서를\s+참고하였습니다\.?\s*",
        "",
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def prepare_rag_slack_display(
    answer: str,
    docs: list | None,
    source_doc_numbers: list[int] | None,
) -> str:
    """Slack 본문용 — 출처 표기 제거된 answer만 반환."""
    return strip_body_citations(answer)


def get_doc_payload(doc: Any) -> dict[str, Any]:
    """
    Qdrant hit, dict payload, custom doc object 모두 처리하기 위한 payload accessor.
    """
    if hasattr(doc, "payload"):
        return getattr(doc, "payload", {}) or {}

    if isinstance(doc, dict):
        if "payload" in doc and isinstance(doc["payload"], dict):
            return doc["payload"]
        return doc

    return {}


def get_doc_title(payload: dict[str, Any], fallback: str) -> str:
    """
    문서 제목 후보를 우선순위대로 가져온다.
    parent_section은 페이지 제목 + 섹션 제목을 함께 표시한다.
    """
    page_title = str(
        payload.get("page_title")
        or payload.get("semantic_title")
        or payload.get("title")
        or payload.get("document_title")
        or payload.get("source_title")
        or ""
    ).strip()
    section_title = str(payload.get("section_title") or "").strip()

    if page_title and section_title:
        return f"{page_title} — {section_title}"
    if page_title:
        return page_title
    if section_title:
        return section_title
    return fallback


def get_doc_url(payload: dict[str, Any]) -> str:
    """
    문서 URL 후보를 우선순위대로 가져온다.
    """
    url = (
        payload.get("page_url")
        or payload.get("url")
        or payload.get("source_url")
        or payload.get("web_url")
        or ""
    )

    url = str(url).strip()

    if not url.startswith(("http://", "https://")):
        return ""

    return url


def normalize_perplexity_citations(answer: str) -> str:
    """
    LLM이 생성한 citation을 Perplexity-style [1] 형식으로 보정한다.

    - [문서 1] -> [1]
    - [1, 2] -> [1][2]
    - 문장 [1] -> 문장[1]
    """
    if not answer:
        return ""

    answer = str(answer)

    # [문서 1] -> [1]
    answer = re.sub(r"\[문서\s*(\d+)\]", r"[\1]", answer)

    # [1, 2] -> [1][2]
    def split_multi_citation(match: re.Match) -> str:
        nums = re.findall(r"\d+", match.group(1))
        return "".join(f"[{n}]" for n in nums)

    answer = re.sub(r"\[([\d,\s]+)\]", split_multi_citation, answer)

    # citation 앞 공백 제거: "내용 [1]" -> "내용[1]"
    answer = re.sub(r"\s+(\[\d+\])", r"\1", answer)

    return answer.strip()


def extract_used_citation_numbers(answer: str, docs: list | None = None) -> list[int]:
    """
    답변 본문에 실제로 사용된 citation 번호를 순서 유지 + 중복 제거하여 반환한다.
    docs가 있으면 docs 범위를 벗어난 번호는 제외한다.
    """
    if not answer:
        return []

    answer = str(answer)

    max_doc_no = len(docs) if docs else None
    seen: set[int] = set()
    result: list[int] = []

    for match in CITATION_PATTERN.finditer(answer):
        no = int(match.group(1))

        if no in seen:
            continue

        if max_doc_no is not None and not (1 <= no <= max_doc_no):
            continue

        seen.add(no)
        result.append(no)

    return result


def build_citation_renumber_map(answer: str, docs: list | None = None) -> dict[int, int]:
    """
    본문에 실제 인용된 문서 번호만 1, 2, 3… 으로 다시 매긴다.
    검색 풀에서의 원래 번호(예: [2])와 표시 번호를 분리한다.
    """
    used = extract_used_citation_numbers(answer, docs)
    return {old: new for new, old in enumerate(used, start=1)}


def renumber_citations(text: str, remap: dict[int, int]) -> str:
    """remap에 있는 [N]만 새 번호로 치환한다."""
    if not text or not remap:
        return text

    def replace(match: re.Match) -> str:
        old = int(match.group(1))
        new = remap.get(old)
        if new is None:
            return match.group(0)
        return f"[{new}]"

    return CITATION_PATTERN.sub(replace, str(text))


def prepare_rag_display_citations(
    answer: str,
    docs: list | None,
) -> tuple[str, list]:
    """
    Slack 표시용: 인용된 문서만 남기고 citation 번호를 1부터 연속으로 맞춘다.

    Returns:
        (display_answer, display_docs)
    """
    if not docs:
        return normalize_perplexity_citations(answer), []

    normalized = normalize_perplexity_citations(answer)
    remap = build_citation_renumber_map(normalized, docs)
    if not remap:
        return normalized, list(docs)

    display_answer = renumber_citations(normalized, remap)
    used_order = extract_used_citation_numbers(normalized, docs)
    display_docs = [docs[old_no - 1] for old_no in used_order]
    return display_answer, display_docs


def render_citations_to_slack(text: str, docs: list | None = None) -> str:
    """
    Perplexity-style citation [1], [2]를 Slack 하이퍼링크 형태로 변환한다.

    예:
    랜덤 런치는 친목 목적입니다[1].
    ->
    랜덤 런치는 친목 목적입니다<https://...|[1]>.
    """
    if not text or not docs:
        return text

    text = str(text)

    def replace(match: re.Match) -> str:
        citation_no = int(match.group(1))
        doc_idx = citation_no - 1

        if doc_idx < 0 or doc_idx >= len(docs):
            return match.group(0)

        payload = get_doc_payload(docs[doc_idx])
        url = get_doc_url(payload)

        if not url:
            return match.group(0)

        return f"<{url}|[{citation_no}]>"

    return CITATION_PATTERN.sub(replace, text)


def make_inline_source_sentence(
    docs: list | None,
    source_doc_numbers: list[int] | None = None,
) -> str:
    """
    하단 출처 문장 — Turn2 <sources_used>에 적힌 문서 번호만 사용한다.
    """
    if not docs or not source_doc_numbers:
        return ""

    used_numbers = [n for n in source_doc_numbers if 1 <= n <= len(docs)]
    if not used_numbers:
        return ""

    grouped_sources: dict[tuple[str, str], list[int]] = {}

    for no in used_numbers:
        payload = get_doc_payload(docs[no - 1])
        title = get_doc_title(payload, fallback=f"참고 문서 {no}")
        url = get_doc_url(payload)

        key = (url, title)
        if key not in grouped_sources:
            grouped_sources[key] = []
        grouped_sources[key].append(no)

    source_parts: list[str] = []

    for (url, title), nos in grouped_sources.items():
        safe_title = SlackFormatter.to_slack(title)
        nos_str = ",".join(str(n) for n in nos)

        if url:
            source_parts.append(f"<{url}|[{nos_str}] {safe_title}>")
        else:
            source_parts.append(f"[{nos_str}] {safe_title}")

    if not source_parts:
        return ""

    if len(source_parts) == 1:
        joined = source_parts[0]
    elif len(source_parts) == 2:
        joined = f"{source_parts[0]}와 {source_parts[1]}"
    else:
        joined = ", ".join(source_parts[:-1]) + f", 그리고 {source_parts[-1]}"

    return f"답변은 {joined} 문서를 참고하였습니다."


# =============================================================================
# Slack Native Table Utils
# =============================================================================

def clean_table_cell_text(text: str) -> str:
    """
    Slack native table cell에 넣기 전 최소 정리만 한다.

    중요:
    - 표 셀 내부 줄바꿈은 실제 "\\n"으로 유지한다.
    - <br>도 "\\n"으로 변환한다.

    처리:
    - <br> -> 실제 줄바꿈
    - Markdown 스타일 제거
    - Markdown 링크는 label만 남김
    - 표 안 citation [n] 제거
    """
    text = str(text or "").strip()

    # <br>도 실제 줄바꿈으로 변환
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)

    # Markdown link: [text](url) -> text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

    # Slack link: <url|text> -> text
    text = re.sub(r"<https?://[^|>]+\|([^>]+)>", r"\1", text)

    # 표 안 citation 제거
    text = re.sub(r"\[\d+\]", "", text)

    # Markdown style 제거
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"~~(.*?)~~", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"_(.*?)_", r"\1", text)
    text = re.sub(r"\*(.*?)\*", r"\1", text)

    # 탭/스페이스만 정리한다. \n은 유지해야 한다.
    text = re.sub(r"[ \t]+", " ", text)

    # 줄마다 앞뒤 공백 정리
    text = "\n".join(part.strip() for part in text.splitlines())

    return text.strip() or " "


def parse_markdown_table_rows(table_rows: list[str]) -> tuple[list[str], list[list[str]]] | None:
    """
    조립이 끝난 Markdown table rows를 headers, rows로 파싱한다.

    table_rows 안의 각 원소는 하나의 table row다.
    row 문자열 내부에는 셀 줄바꿈용 "\\n"이 포함될 수 있다.
    """
    if len(table_rows) < 2:
        return None

    parsed_rows: list[list[str]] = []

    for row_text in table_rows:
        row_text = normalize_table_row(str(row_text).strip())
        if not row_text.startswith("|"):
            continue

        cells = _table_row_cells(row_text)

        is_separator = all(
            re.fullmatch(r":?\s*-{3,}\s*:?", cell or "") is not None
            for cell in cells
        )

        if is_separator:
            continue

        parsed_rows.append(cells)

    if len(parsed_rows) < 2:
        return None

    headers = parsed_rows[0]
    rows = parsed_rows[1:]

    fixed_rows: list[list[str]] = []

    for row in rows:
        if len(row) < len(headers):
            row = row + [""] * (len(headers) - len(row))

        if len(row) > len(headers):
            row = row[: len(headers) - 1] + [" | ".join(row[len(headers) - 1:])]

        fixed_rows.append(row)

    return headers, fixed_rows


def make_table_cell(text: str, *, is_header: bool = False) -> dict[str, Any]:
    """
    Slack native table cell 생성.
    header만 bold 처리한다.
    """
    cell_text = clean_table_cell_text(text)

    text_element: dict[str, Any] = {
        "type": "text",
        "text": cell_text,
    }

    if is_header:
        text_element["style"] = {"bold": True}

    return {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_section",
                "elements": [text_element],
            }
        ],
    }


def parse_markdown_table_to_slack_block(table_rows: list[str]) -> dict[str, Any] | None:
    """
    조립된 Markdown table rows를 Slack native table block으로 변환한다.
    """
    parsed = parse_markdown_table_rows(table_rows)

    if not parsed:
        return None

    headers, rows = parsed

    slack_rows: list[list[dict[str, Any]]] = []

    slack_rows.append([
        make_table_cell(header, is_header=True)
        for header in headers
    ])

    for row in rows:
        slack_rows.append([
            make_table_cell(cell, is_header=False)
            for cell in row
        ])

    return {
        "type": "table",
        "rows": slack_rows,
    }


# =============================================================================
# Block Builders
# =============================================================================

def make_section(text: str, docs: list | None = None) -> dict[str, Any] | None:
    """
    일반 Markdown 텍스트를 Slack section block으로 변환한다.
    Perplexity-style citation [1]은 문서 링크로 변환한다.
    """
    slack_text = SlackFormatter.to_slack(text)
    slack_text = render_citations_to_slack(slack_text, docs)

    if not slack_text:
        return None

    return {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": slack_text,
        },
        "expand": True,
    }


def make_context(text: str) -> dict[str, Any] | None:
    """
    Slack context block 생성.
    하단 출처 문장처럼 짧은 보조 정보에 사용한다.
    """
    if not text:
        return None

    return {
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": text,
            }
        ],
    }


def _normalize_attachment_entry(att: Any) -> dict[str, Any] | None:
    """
    parent_section attachments 필드 → Slack 표시용 dict.

    rag_tree_hybrid: {id, title, download_url, saved_path, ...}
    vectordb _attachments: {title, url, saved_path, ...}
    """
    if not isinstance(att, dict):
        return None

    title = str(att.get("title") or att.get("filename") or "").strip()
    download_url = str(att.get("download_url") or att.get("url") or "").strip()
    saved_path = str(att.get("saved_path") or "").strip()
    if not (title or download_url or saved_path):
        return None

    return {
        "title": title or "첨부파일",
        "download_url": download_url,
        "url": download_url,
        "saved_path": saved_path,
    }


def _normalize_link_entry(link: Any) -> dict[str, Any] | None:
    """parent urls / vectordb _links 항목 정규화."""
    if not isinstance(link, dict):
        return None

    url = str(link.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return None

    desc = str(link.get("description") or "").strip()
    text = str(link.get("text") or link.get("purpose") or desc or url).strip()
    out: dict[str, Any] = {"text": text, "url": url}
    if desc:
        out["description"] = desc
    return out


def _get_page_resources(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    collapse된 parent_section payload에서 첨부·링크 목록.

    parent_store·ES의 attachments / urls를 우선 사용하고,
    vectordb가 채운 _attachments / _links는 보조(fallback·병합)로 사용한다.
    """
    attachments: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    seen_urls: set[str] = set()

    def _add_attachment(raw: Any) -> None:
        entry = _normalize_attachment_entry(raw)
        if not entry:
            return
        key = entry["title"].casefold()
        if key in seen_titles:
            return
        seen_titles.add(key)
        attachments.append(entry)

    def _add_link(raw: Any) -> None:
        entry = _normalize_link_entry(raw)
        if not entry:
            return
        key = entry["url"]
        if key in seen_urls:
            return
        seen_urls.add(key)
        links.append(entry)

    for att in payload.get("attachments") or []:
        _add_attachment(att)
    for att in payload.get("_attachments") or []:
        _add_attachment(att)

    for link in payload.get("urls") or []:
        _add_link(link)
    for link in payload.get("_links") or []:
        _add_link(link)

    return attachments, links


def _is_image_attachment(att: dict[str, Any]) -> bool:
    name = str(att.get("title") or att.get("filename") or "").strip()
    return Path(name).suffix.lower() in IMAGE_EXTENSIONS


def _attachment_image_public_url(att: dict[str, Any]) -> str:
    """Slack context 썸네일용 — 로컬 Static 공개 URL만 (Confluence download URL 제외)."""
    url = resolve_attachment_public_url(str(att.get("saved_path") or ""))
    return _normalize_slack_url(url)


def make_attachment_image_context_block(
    *,
    filename: str,
    image_url: str,
    view_url: str,
) -> dict[str, Any] | None:
    """
    이미지 1건 — context 블록(작은 image + 파일명 + 웹으로 보기 링크).
    예: [이미지] image-20240930-060812.png 웹으로 보기
    """
    img = _normalize_slack_url(image_url)
    view = _normalize_slack_url(view_url) or img
    name = SlackFormatter.to_slack(filename or "image")
    if view:
        caption = f"{name} <{view}|웹으로 보기>"
    else:
        caption = f"{name}"

    elements: list[dict[str, Any]] = [
        {"type": "mrkdwn", "text": caption[:3000]},
    ]
    if img:
        elements.append({
            "type": "image",
            "image_url": img,
            "alt_text": (filename or "image")[:2000],
        })
    return {"type": "context", "elements": elements}


def make_attachment_image_context_blocks(
    image_items: list[tuple[str, str, str]],
) -> list[dict[str, Any]]:
    """
    image_items: (filename, thumbnail_url, view_url)
    """
    if not image_items:
        return []
    max_items = int(os.getenv("SLACK_IMAGE_CONTEXT_MAX", "10") or "10")
    blocks: list[dict[str, Any]] = []
    for filename, image_url, view_url in image_items[:max_items]:
        block = make_attachment_image_context_block(
            filename=filename,
            image_url=image_url,
            view_url=view_url,
        )
        if block:
            blocks.append(block)
    return blocks


def make_related_resources_blocks(
    docs: list | None = None,
    *,
    source_doc_numbers: list[int] | None = None,
    links_used_by_doc: dict[int, list[int]] | None = None,
    attachments_used_by_doc: dict[int, list[int]] | None = None,
) -> list[dict[str, Any]]:
    """
    RAG 검색 문서에 연결된 리소스를 Slack에 표시한다.

    - 이미지(.png 등) + 공개 Static URL: context(썸네일 + 파일명 + 웹으로 보기)
      → <attachments_used>에 포함된 첨부 번호만
    - 그 외 첨부·링크: mrkdwn URL
      → 첨부는 <attachments_used>, 링크는 <links_used> 기준
    """
    if not docs or not source_doc_numbers:
        return []

    doc_items = [
        (no, docs[no - 1])
        for no in source_doc_numbers
        if 1 <= no <= len(docs)
    ]
    if not doc_items:
        return []

    section_blocks: list[dict[str, Any]] = []
    image_context_items: list[tuple[str, str, str]] = []
    seen_image_urls: set[str] = set()

    for idx, doc in doc_items:
        payload = get_doc_payload(doc)
        attachments, links = _get_page_resources(payload)
        if not attachments and not links:
            continue

        page_title = get_doc_title(payload, fallback=f"문서 {idx}")
        section_lines: list[str] = [f"*[{idx}] {SlackFormatter.to_slack(page_title)}*"]

        resource_no = 1
        has_section_content = False
        allowed_attachment_nums = (attachments_used_by_doc or {}).get(idx) or []

        for att_idx, att in enumerate(attachments, start=1):
            if att_idx not in allowed_attachment_nums:
                continue

            raw_title = str(att.get("title") or att.get("filename") or "첨부파일")
            title = SlackFormatter.to_slack(raw_title)

            if _is_image_attachment(att):
                img_url = _attachment_image_public_url(att)
                if img_url and img_url not in seen_image_urls:
                    seen_image_urls.add(img_url)
                    image_context_items.append((
                        raw_title,
                        img_url,
                        _attachment_display_url(att) or img_url,
                    ))
                    continue
                url = _attachment_display_url(att)
                label = f"이미지 {resource_no}"
                if url:
                    section_lines.append(f"{resource_no}. 🖼 <{url}|{label}> — {title}")
                else:
                    section_lines.append(f"{resource_no}. 🖼 {label} — {title}")
                resource_no += 1
                has_section_content = True
                continue

            url = _attachment_display_url(att)
            label = f"첨부자료 {resource_no}"
            if url:
                section_lines.append(f"{resource_no}. 📎 <{url}|{label}> — {title}")
            else:
                section_lines.append(f"{resource_no}. 📎 {label} — {title}")
            resource_no += 1
            has_section_content = True

        allowed_link_nums = (links_used_by_doc or {}).get(idx) or []

        for link_idx, link in enumerate(links, start=1):
            if link_idx not in allowed_link_nums:
                continue

            url = _normalize_slack_url(str(link.get("url") or ""))
            if not url:
                continue

            raw_label = str(
                link.get("description") or link.get("text") or ""
            ).strip()
            if (
                not raw_label
                or raw_label == url
                or raw_label.startswith(("http://", "https://"))
            ):
                raw_label = "링크"
            slack_label = SlackFormatter.to_slack(f"링크{link_idx}: {raw_label}")
            section_lines.append(f"{resource_no}. 🔗 <{url}|{slack_label}>")
            resource_no += 1
            has_section_content = True

        if has_section_content and len(section_lines) > 1:
            section = make_section("\n".join(section_lines), docs=None)
            if section:
                section_blocks.append(section)

    image_context_blocks = make_attachment_image_context_blocks(image_context_items)
    if not image_context_blocks and not section_blocks:
        return []

    out: list[dict[str, Any] | None] = [{"type": "divider"}]
    if image_context_blocks:
        header = make_context("🖼 *관련 이미지*")
        if header:
            out.append(header)
        out.extend(image_context_blocks)
    if section_blocks:
        ctx = make_context("📎 *관련 첨부·링크*")
        if ctx:
            out.append(ctx)
        out.extend(section_blocks)

    return clean_blocks(out)

def _normalize_slack_url(url: str) -> str:
    u = (url or "").strip()
    if u and u.startswith(("http://", "https://")):
        return u
    return ""


def _attachment_display_url(att: dict[str, Any]) -> str:
    """로컬 saved_path → 공개 Static URL 우선, 없으면 download_url / url."""
    url = resolve_attachment_url(
        saved_path=str(att.get("saved_path") or ""),
        fallback_url=str(
            att.get("download_url") or att.get("url") or ""
        ),
    )
    return _normalize_slack_url(url)


def make_inline_source_blocks(
    docs: list | None = None,
    source_doc_numbers: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Turn2 sources_used 기준 하단 출처 context 블록."""
    sentence = make_inline_source_sentence(docs, source_doc_numbers)
    if not sentence:
        return []

    context = make_context(sentence)
    if not context:
        return []

    return [
        {"type": "divider"},
        context,
    ]


def clean_blocks(blocks: list[dict[str, Any] | None]) -> list[dict[str, Any]]:
    """
    Slack invalid_blocks 방지를 위해 None 블록을 제거한다.
    """
    return [block for block in blocks if block]


def make_answer_blocks(answer: str, docs: list | None = None) -> list[dict[str, Any]]:
    answer = normalize_perplexity_citations(answer)

    parts = split_answer_into_parts(answer)

    blocks: list[dict[str, Any] | None] = []

    for part in parts:
        if part["type"] == "text":
            text = str(part["text"]).strip()

            if not text:
                continue

            # ---------------------------------
            # Markdown header 처리
            # ---------------------------------
            lines = text.splitlines()

            buffer: list[str] = []

            def flush_buffer():
                if not buffer:
                    return

                section = make_section("\n".join(buffer), docs=docs)
                if section:
                    blocks.append(section)

                buffer.clear()

            for line in lines:
                parsed_header = parse_markdown_header(line)

                if parsed_header:
                    flush_buffer()

                    level, title = parsed_header
                    blocks.append(
                        make_header_block(title, level)
                    )
                    continue

                buffer.append(line)

            flush_buffer()
            continue

        if part["type"] == "table":
            table_block = parse_markdown_table_to_slack_block(part["rows"])

            if table_block:
                blocks.append(table_block)
            else:
                fallback_text = "\n".join(part["rows"])
                blocks.append(make_section(fallback_text, docs=docs))

            continue

    return clean_blocks(blocks)


# =============================================================================
# Public Builders
# =============================================================================

async def build_general_response_blocks(user_id, answer):
    """
    일반 답변용 Block Kit 빌더.
    일반 답변에도 Markdown table이 포함될 수 있으므로 make_answer_blocks를 사용한다.
    """
    return make_answer_blocks(answer, docs=None)


def make_gov_file_blocks(gov_attachments: list[dict] | None) -> list[dict[str, Any]]:
    """정부과제 첨부파일 다운로드 링크 블록."""
    if not gov_attachments:
        return []

    lines: list[str] = ["*📎 정부과제 첨부파일*"]
    for i, att in enumerate(gov_attachments, start=1):
        title = SlackFormatter.to_slack(str(att.get("title") or "첨부파일"))
        url = str(att.get("download_url") or "").strip()
        category = str(att.get("category") or "").strip()
        prefix = f"[{category}] " if category else ""
        if url:
            lines.append(f"{i}. 📎 <{url}|첨부자료 {i}> — {prefix}{title}")
        else:
            lines.append(f"{i}. 📎 첨부자료 {i} — {prefix}{title}")

    return [make_section("\n".join(lines))]


_GOV_BRIEFING_FOOTER_END = "챗봇을 실행해 주세요."


def _truncate_gov_briefing_at_footer(text: str) -> str:
    """푸터(챗봇 안내) 이후 잘린 내용·출처 잔여를 제거합니다."""
    t = (text or "").strip()
    pos = t.find(_GOV_BRIEFING_FOOTER_END)
    if pos >= 0:
        return t[: pos + len(_GOV_BRIEFING_FOOTER_END)].strip()
    return t


def build_gov_briefing_blocks(briefing_md: str) -> list[dict[str, Any]]:
    """정부과제 브리핑 마크다운 → Slack Block Kit (카드형 section + 하이퍼링크)."""
    text = _truncate_gov_briefing_at_footer((briefing_md or "").strip())
    if not text:
        return []
    return make_answer_blocks(text, docs=None)


async def build_gov_response_blocks(user_id, answer, gov_attachments=None):
    """정부과제 브리핑 답변 + 첨부 링크 Block Kit."""
    blocks = build_gov_briefing_blocks(answer)
    blocks.extend(make_gov_file_blocks(gov_attachments))
    return clean_blocks(blocks)


async def build_rag_response_blocks(
    user_id,
    answer,
    docs,
    source_doc_numbers: list[int] | None = None,
    links_used_by_doc: dict[int, list[int]] | None = None,
    attachments_used_by_doc: dict[int, list[int]] | None = None,
):
    """
    RAG 답변 전용 Block Kit 빌더.

    - 본문: <answer>만 표시 (출처 번호 없음)
    - 하단: Turn2 <sources_used>·<attachments_used>·<links_used>로 출처·첨부·링크 블록 생성
    """
    nums = source_doc_numbers or []
    display_answer = prepare_rag_slack_display(answer, docs, nums)

    blocks: list[dict[str, Any] | None] = []

    blocks.extend(make_answer_blocks(display_answer, docs=None))
    blocks.extend(
        make_related_resources_blocks(
            docs,
            source_doc_numbers=nums,
            links_used_by_doc=links_used_by_doc,
            attachments_used_by_doc=attachments_used_by_doc,
        )
    )
    blocks.extend(make_inline_source_blocks(docs, source_doc_numbers=nums))

    return clean_blocks(blocks)