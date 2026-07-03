"""
parsers/base.py — 파서 기본 인터페이스 및 팩토리

모든 파일 파서가 구현해야 할 인터페이스를 정의합니다.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

@dataclass
class ParsedContent:
    """파서가 반환하는 파싱 결과."""
    text: str
    tables: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    page_count: Optional[int] = None
    images_text: list[str] = field(default_factory=list)
    parse_method: str = ""
    confidence: float = 1.0

logger = logging.getLogger(__name__)


class BaseParser(ABC):
    """파일 파서 기본 클래스."""

    @abstractmethod
    def parse(self, file_path: Path, **kwargs) -> ParsedContent:
        """
        파일에서 텍스트/테이블/메타데이터를 추출합니다.

        Args:
            file_path: 파싱할 파일 경로
            **kwargs: 파서별 추가 옵션

        Returns:
            ParsedContent 인스턴스
        """
        ...

    @abstractmethod
    def supported_extensions(self) -> list[str]:
        """지원하는 파일 확장자 목록."""
        ...


def get_parser(extension: str) -> Optional[BaseParser]:
    """
    확장자 기반 파서 팩토리.

    Args:
        extension: 파일 확장자 (예: ".pdf")

    Returns:
        적절한 파서 인스턴스, 지원하지 않으면 None
    """
    ext = extension.lower()

    if ext == ".pdf":
        from parsers.pdf_parser import PDFParser
        return PDFParser()

    if ext in (".pptx", ".ppt"):
        from parsers.ppt_parser import PPTXParser
        return PPTXParser()

    if ext in (".xlsx", ".xls", ".csv"):
        from parsers.excel_parser import ExcelParser
        return ExcelParser()

    if ext in (".docx", ".doc"):
        from parsers.docs_parser import DOCXParser
        return DOCXParser()

    if ext == ".hwp":
        from parsers.hwp_parser import HWPParser
        return HWPParser()

    # 이미지는 바로 VLM으로 처리하기 때문에 여기서는 pass
    # if ext in (".png", ".jpg", ".jpeg", ".heic", ".webp"):
    #     from parsers.image_parser import ImageParser
    #     return ImageParser()

    logger.warning("지원하지 않는 확장자: %s", ext)
    return None
