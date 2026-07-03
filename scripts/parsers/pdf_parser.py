"""
parsers/pdf_parser.py

PyMuPDF(fitz)를 사용하여 PDF 문서의 텍스트와 테이블을 추출한다.

주요 기능:
1. Layout-aware 텍스트 추출
   - page.get_text("blocks") 기반
   - 좌표(y, x) 정렬을 통한 읽기 순서 복원
   - 멀티 컬럼 PDF 대응

2. 테이블 추출
   - page.find_tables() 사용
   - pandas DataFrame -> markdown 변환

3. OCR fallback 감지
   - 텍스트가 거의 없는 스캔 PDF 감지 가능

참고:
- opendataloader-pdf 스타일의 layout-aware extraction 방식 참고
"""

from __future__ import annotations

import logging
from pathlib import Path
import fitz

from parsers.base import BaseParser, ParsedContent

logger = logging.getLogger(__name__)

fitz.TOOLS.mupdf_display_errors(False) # <--- 핵심: MuPDF 내부 에러 출력을 끔

class PDFParser(BaseParser):

    def supported_extensions(self) -> list[str]:
        return [".pdf"]

    def parse(self, file_path: Path, **kwargs) -> ParsedContent:

        try:
            import fitz
        except ImportError:
            logger.error("PyMuPDF 설치 필요: pip install PyMuPDF")
            return ParsedContent(
                text="",
                parse_method="pymupdf_unavailable",
                confidence=0.0,
            )

        file_path = Path(file_path)

        if not file_path.exists():
            return ParsedContent(
                text="",
                parse_method="file_not_found",
                confidence=0.0,
            )

        try:
            doc = fitz.open(str(file_path))

        except Exception as e:
            logger.exception(f"PDF 열기 실패: {file_path}")

            return ParsedContent(
                text="",
                parse_method="open_failed",
                confidence=0.0,
                metadata={
                    "error": str(e),
                },
            )

        all_text_parts: list[str] = []
        all_tables: list[str] = []

        page_count = len(doc)

        for page_index, page in enumerate(doc):

            # -------------------------------------------------
            # BLOCK EXTRACTION
            # -------------------------------------------------
            # blocks format:
            # (
            #   x0, y0, x1, y1,
            #   text,
            #   block_no,
            #   block_type
            # )
            # block_type:
            #   0 = text
            #   1 = image
            # -------------------------------------------------

            try:
                blocks = page.get_text("blocks")

            except Exception:
                logger.exception(
                    f"텍스트 블록 추출 실패: page={page_index + 1}"
                )
                continue

            # -------------------------------------------------
            # LAYOUT-AWARE SORT
            # -------------------------------------------------
            # y 우선 -> x 정렬
            # 사람이 읽는 순서와 유사하게 복원
            # 예: 좌 -> 우, 상 -> 하
            # 멀티 컬럼 PDF 대응
            # -------------------------------------------------
            blocks.sort(
                key=lambda b: (
                    round(b[1], 1),
                    round(b[0], 1),
                )
            )

            page_text_blocks: list[str] = []

            for block in blocks:

                try:
                    block_type = block[6]

                    # text block만 사용
                    if block_type != 0:
                        continue

                    text = block[4].strip()

                    if not text:
                        continue

                    normalized = normalize_text(text)

                    if normalized:
                        page_text_blocks.append(normalized)

                except Exception:
                    continue

            if page_text_blocks:

                combined_text = "\n".join(page_text_blocks)

                all_text_parts.append(
                    f"[페이지 {page_index + 1}]\n{combined_text}"
                )

            # 테이블 추출
            try:
                tables = page.find_tables()

                for table in tables:

                    try:
                        df = table.to_pandas()

                        if df is None or df.empty:
                            continue

                        markdown_table = dataframe_to_markdown(df)

                        if markdown_table:

                            all_tables.append(
                                f"[페이지 {page_index + 1} 테이블]\n"
                                f"{markdown_table}"
                            )

                    except Exception:
                        continue

            except Exception:
                logger.debug(
                    f"테이블 추출 실패: page={page_index + 1}"
                )

        doc.close()

        full_text = "\n\n".join(all_text_parts).strip()

        confidence = 1.0

        # OCR 필요 가능성
        if len(full_text) < 50 and page_count > 0:
            confidence = 0.3

            logger.info(
                f"OCR 필요 가능성 높은 스캔 PDF 감지: "
                f"{file_path.name}"
            )

        return ParsedContent(
            text=full_text,
            tables=all_tables,
            metadata={
                "filename": file_path.name,
                "page_count": page_count,
                "file_size": file_path.stat().st_size,
                "parser_strategy": "layout_aware_pymupdf",
            },
            page_count=page_count,
            parse_method="pymupdf_layout_optimized",
            confidence=confidence,
        )


def normalize_text(text: str) -> str:
    """
    PDF 추출 시 발생하는 불필요한 공백 정리
    """

    import re

    text = text.replace("\xa0", " ")

    text = re.sub(r"[ \t]+", " ", text)

    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def dataframe_to_markdown(df) -> str:
    """
    pandas DataFrame -> markdown table
    """

    try:
        headers = [str(h).strip() for h in df.columns]

        header_line = "| " + " | ".join(headers) + " |"

        separator_line = "|" + "|".join(["---"] * len(headers)) + "|"

        rows = []

        for _, row in df.iterrows():

            cells = []

            for value in row:

                cell = str(value)

                cell = cell.replace("\n", " ")
                cell = cell.replace("|", "\\|")

                cells.append(cell.strip())

            rows.append("| " + " | ".join(cells) + " |")

        return "\n".join(
            [header_line, separator_line] + rows
        )

    except Exception:
        logger.exception("DataFrame markdown 변환 실패")
        return ""