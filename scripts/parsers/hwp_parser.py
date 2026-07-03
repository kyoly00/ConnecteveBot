"""
parsers/hwp_parser.py — HWP 파일 파서

https://github.com/HariFatherKR/hwp-parser/blob/main/hwpparser/reader.py의 핵심 로직을 standalone으로 재구성합니다.
pyhwp CLI(hwp5txt, hwp5html)를 사용하여 텍스트/HTML을 추출하고,
테이블을 마크다운으로 변환합니다.
"""

import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import List, Tuple
from bs4 import BeautifulSoup, NavigableString

from parsers.base import BaseParser, ParsedContent

logger = logging.getLogger(__name__)

class HWPParser(BaseParser):
    """제공된 HWPReader 로직을 적용한 고도화된 HWP 파서."""

    def supported_extensions(self) -> List[str]:
        return [".hwp"]

    def parse(self, file_path: Path, **kwargs) -> ParsedContent:
        file_path = Path(file_path)
        if not file_path.exists():
            return ParsedContent(text="", parse_method="file_not_found", confidence=0.0)

        logger.info(f"🚀 HWP 리치 텍스트 추출 시작: {file_path.name}")

        try:
            # 1. HTML로 변환 (표 구조 보존을 위해)
            html_content = self._hwp_to_html(file_path)
            
            if html_content:
                # 2. HTML 내 표를 마크다운으로 변환하며 텍스트 추출
                rich_text = self._html_to_rich_text(html_content)
                
                return ParsedContent(
                    text=rich_text,
                    metadata={
                        "filename": file_path.name,
                        "file_size": file_path.stat().st_size,
                    },
                    parse_method="pyhwp_rich_text",
                    confidence=1.0
                )
        except Exception as e:
            logger.error(f"HWP 리치 텍스트 추출 실패: {e}")

        # HTML 변환 실패 시 기본 텍스트 추출 시도 (Fallback)
        return self._fallback_to_plain_text(file_path)

    def _hwp_to_html(self, file_path: Path) -> str:
        """hwp5html CLI를 호출하여 임시 HTML 생성"""
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                output_target = Path(tmp_dir) / "hwp_output"
                
                # pyhwp 명령어 실행
                subprocess.run(
                    ["hwp5html", str(file_path), "--output", str(output_target)],
                    check=True, capture_output=True, text=True, timeout=60
                )

                if output_target.is_dir():
                    for name in ("index.xhtml", "index.html"):
                        html_file = output_target / name
                        if html_file.exists():
                            return html_file.read_text(encoding="utf-8")
                elif output_target.exists():
                    return output_target.read_text(encoding="utf-8")
        except Exception as e:
            logger.debug(f"hwp5html 실행 중 오류: {e}")
        return ""

    def _html_to_rich_text(self, html_content: str) -> str:
        """HTML을 분석하여 표를 마크다운으로 포함한 텍스트 반환"""
        soup = BeautifulSoup(html_content, 'lxml')
        
        # 표(Table) 처리
        tables = soup.find_all('table')
        table_markdowns = []
        for i, table in enumerate(tables):
            md = self._parse_table_to_markdown(table)
            if md:
                table_markdowns.append(md)
                placeholder = soup.new_tag('div')
                placeholder.string = f"__TABLE_{i}__"
                table.replace_with(placeholder)

        # 텍스트 추출 로직
        text_parts = []
        root = soup.body if soup.body else soup
        for elem in root.descendants:
            if isinstance(elem, NavigableString):
                text = str(elem).strip()
                if text:
                    text_parts.append(text)

        full_text = '\n'.join(text_parts)

        # 플레이스홀더를 실제 마크다운 표로 교체
        for i, md in enumerate(table_markdowns):
            full_text = full_text.replace(f"__TABLE_{i}__", f"\n\n{md}\n\n")

        return re.sub(r'\n{3,}', '\n\n', full_text).strip()

    def _parse_table_to_markdown(self, table) -> str:
        """HTML 표 구조를 마크다운으로 변환"""
        rows = table.find_all('tr')
        if not rows: return ""

        lines = []
        for i, row in enumerate(rows):
            cells = row.find_all(['td', 'th'])
            cell_texts = []
            for cell in cells:
                # 셀 텍스트 추출 (제공된 _extract_cell_text 로직 적용)
                txt = ' '.join([str(e).strip() for e in cell.descendants if isinstance(e, NavigableString)]).strip()
                cell_texts.append(txt.replace('\n', ' ') if txt else " ")
                
                # colspan 대응 (빈 셀 추가)
                colspan = int(cell.get('colspan', 1))
                for _ in range(colspan - 1):
                    cell_texts.append(" ")
            
            lines.append("| " + " | ".join(cell_texts) + " |")
            if i == 0: # 헤더 구분선
                lines.append("|" + "|".join(["---" for _ in cell_texts]) + "|")
        
        return "\n".join(lines)

    def _fallback_to_plain_text(self, file_path: Path) -> ParsedContent:
        """HTML 변환 실패 시 hwp5txt로 텍스트만 추출"""
        try:
            res = subprocess.run(["hwp5txt", str(file_path)], capture_output=True, text=True, encoding="utf-8")
            if res.returncode == 0:
                return ParsedContent(text=res.stdout, parse_method="pyhwp_plain_text", confidence=0.7)
        except: pass
        return ParsedContent(text="", parse_method="failed", confidence=0.0)