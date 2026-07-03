"""
parsers/excel_parser.py

Excel / CSV parser

지원 포맷:
- .xlsx
- .xlsm
- .csv

목적:
1. 시트별 텍스트 추출
2. 테이블 구조 유지
3. RAG/VLM metadata 생성용

"""

from __future__ import annotations

import logging
import warnings
from pathlib import Path

import pandas as pd

from parsers.base import BaseParser, ParsedContent

logger = logging.getLogger(__name__)


class ExcelParser(BaseParser):

    def supported_extensions(self) -> list[str]:
        return [
            ".xlsx",
            ".xlsm",
            ".csv",
        ]

    def parse(self, file_path: Path, **kwargs) -> ParsedContent:

        file_path = Path(file_path)

        if not file_path.exists():
            return ParsedContent(
                text="",
                parse_method="file_not_found",
                confidence=0.0,
            )

        ext = file_path.suffix.lower()

        try:

            if ext == ".csv":
                parsed = self._parse_csv(file_path)

            else:
                parsed = self._parse_excel(file_path)

            return parsed

        except Exception as e:

            logger.exception(
                f"Excel parsing 실패: {file_path}"
            )

            return ParsedContent(
                text="",
                parse_method="excel_parse_failed",
                confidence=0.0,
                metadata={
                    "error": str(e),
                    "filename": file_path.name,
                },
            )

    # =====================================================
    # CSV
    # =====================================================

    def _parse_csv(
        self,
        file_path: Path,
    ) -> ParsedContent:

        encodings = [
            "utf-8",
            "euc-kr",
            "utf-8-sig",
            "cp949",
        ]

        df = None

        for encoding in encodings:

            try:
                df = pd.read_csv(
                    file_path,
                    encoding=encoding,
                )
                break

            except Exception:
                continue

        if df is None:
            raise ValueError(
                f"CSV encoding 해석 실패: {file_path.name}"
            )

        markdown_table = dataframe_to_markdown(df)

        text = (
            f"[CSV 파일]\n"
            f"파일명: {file_path.name}\n\n"
            f"{markdown_table}"
        )

        return ParsedContent(
            text=text,
            tables=[markdown_table],
            metadata={
                "filename": file_path.name,
                "sheet_count": 1,
                "row_count": len(df),
                "column_count": len(df.columns),
                "file_size": file_path.stat().st_size,
            },
            page_count=1,
            parse_method="pandas_csv",
            confidence=1.0,
        )

    # =====================================================
    # XLSX / XLSM (pandas — openpyxl 엔진 미사용, calamine 우선)
    # =====================================================

    def _parse_excel(
        self,
        file_path: Path,
    ) -> ParsedContent:

        sheets, engine_used = _read_excel_all_sheets(file_path)

        all_text_parts: list[str] = []
        all_tables: list[str] = []
        sheet_metadata: list[dict] = []

        for sheet_name, df in sheets.items():

            try:
                if df is None or df.empty:
                    continue

                df = df.fillna("")

                markdown_table = dataframe_to_markdown(df)
                if not markdown_table.strip():
                    continue

                sheet_text = (
                    f"[시트: {sheet_name}]\n"
                    f"{markdown_table}"
                )

                all_text_parts.append(sheet_text)
                all_tables.append(markdown_table)
                sheet_metadata.append({
                    "sheet_name": sheet_name,
                    "row_count": len(df),
                    "column_count": len(df.columns),
                })

            except Exception:
                logger.exception(
                    "시트 파싱 실패: %s / %s",
                    file_path.name,
                    sheet_name,
                )
                continue

        full_text = "\n\n".join(all_text_parts)

        return ParsedContent(
            text=full_text,
            tables=all_tables,
            metadata={
                "filename": file_path.name,
                "sheet_count": len(sheet_metadata),
                "sheets": sheet_metadata,
                "file_size": file_path.stat().st_size,
                "excel_engine": engine_used,
            },
            page_count=len(sheet_metadata),
            parse_method=f"pandas_excel_{engine_used}",
            confidence=1.0,
        )


def _read_excel_all_sheets(file_path: Path) -> tuple[dict[str, pd.DataFrame], str]:
    """
    pandas.read_excel 로 전 시트 로드.
    engine: calamine(python-calamine) 우선 — openpyxl 확장 경고 회피.
    """
    errors: list[str] = []
    for engine in ("calamine", None):
        label = engine or "auto"
        try:
            kwargs: dict = {"sheet_name": None, "dtype": str}
            if engine:
                kwargs["engine"] = engine
            with warnings.catch_warnings():
                if engine is None:
                    warnings.filterwarnings(
                        "ignore",
                        message=".*extension.*",
                        category=UserWarning,
                    )
                raw = pd.read_excel(file_path, **kwargs)
            if isinstance(raw, pd.DataFrame):
                return {"Sheet1": raw}, label
            return {str(name): raw[name] for name in raw}, label
        except ImportError as e:
            errors.append(f"{label}: {e}")
            continue
        except Exception as e:
            errors.append(f"{label}: {e}")
            continue

    raise ValueError(
        f"Excel 읽기 실패 ({file_path.name}): " + "; ".join(errors)
    )


# =========================================================
# DATAFRAME -> MARKDOWN
# =========================================================

def dataframe_to_markdown(df) -> str:

    try:

        headers = [
            str(h).strip()
            for h in df.columns
        ]

        header_line = (
            "| "
            + " | ".join(headers)
            + " |"
        )

        separator_line = (
            "|"
            + "|".join(["---"] * len(headers))
            + "|"
        )

        rows = []

        for _, row in df.iterrows():

            cells = []

            for value in row:

                cell = str(value)

                cell = cell.replace("\n", " ")
                cell = cell.replace("|", "\\|")

                cells.append(cell.strip())

            rows.append(
                "| "
                + " | ".join(cells)
                + " |"
            )

        return "\n".join(
            [header_line, separator_line] + rows
        )

    except Exception:

        logger.exception(
            "DataFrame markdown 변환 실패"
        )

        return ""