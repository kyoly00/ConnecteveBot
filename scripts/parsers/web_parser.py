"""
parsers/web_parser.py — 외부 링크 웹 스크래핑

도메인별 차별 처리:
- forms.office.com → skip (양식)
- connecteve-my.sharepoint.com → Data/attachments/ 로컬 우선 탐색 → fallback
- visitkorea.or.kr, hanaromf.com 등 → trafilatura 본문 추출
- youtu.be/youtube.com → 제목/설명만
- 일반 URL → trafilatura + BeautifulSoup fallback
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.core.config import (
    ATTACHMENTS_DIR,
    SHAREPOINT_DOMAINS,
    SKIP_SCRAPING_DOMAINS,
    WEB_SCRAPING_MAX_RETRIES,
    WEB_SCRAPING_RATE_LIMIT,
    WEB_SCRAPING_TIMEOUT,
)

logger = logging.getLogger(__name__)


@dataclass
class WebParseResult:
    """웹 스크래핑 결과."""
    text: str                        # 추출된 본문
    title: str = ""                  # 페이지 제목
    status: str = "success"          # "success", "skipped", "fallback", "failed"
    url: str = ""
    source_method: str = ""          # "trafilatura", "beautifulsoup", "local_file", "metadata_only"


class WebParser:
    """외부 링크 스크래핑 엔진."""

    def __init__(self):
        self.timeout = WEB_SCRAPING_TIMEOUT
        self.max_retries = WEB_SCRAPING_MAX_RETRIES
        self.rate_limit = WEB_SCRAPING_RATE_LIMIT
        self.skip_domains = SKIP_SCRAPING_DOMAINS
        self.sharepoint_domains = SHAREPOINT_DOMAINS
        self.attachments_dir = ATTACHMENTS_DIR
        self._last_request_time = 0.0

    def parse_url(
        self,
        url: str,
        display_text: str = "",
        context_summary: str = "",
        attachment_metadata: Optional[dict] = None,
    ) -> WebParseResult:
        """
        URL을 분석하고 콘텐츠를 추출합니다.

        Args:
            url: 스크래핑할 URL
            display_text: <a> 태그의 텍스트
            context_summary: 주변 문맥 (fallback 용)
            attachment_metadata: 첨부파일 metadata (로컬 파일 탐색 용)
        """
        parsed = urlparse(url)
        domain = parsed.netloc.lower()

        # ── 1. Skip 도메인 ──
        if self._should_skip(domain):
            return WebParseResult(
                text=f"[양식/신청 링크] {display_text or url}",
                title=display_text or "외부 양식",
                status="skipped",
                url=url,
                source_method="skip_domain",
            )

        # ── 2. SharePoint → 로컬 파일 우선 탐색 ──
        if self._is_sharepoint(domain):
            local_result = self._try_local_attachment(url, display_text, attachment_metadata)
            if local_result:
                return local_result
            # 로컬 파일 없음 → context 기반 fallback
            return WebParseResult(
                text=context_summary or f"[SharePoint 링크] {display_text or url}",
                title=display_text or "SharePoint 파일",
                status="fallback",
                url=url,
                source_method="metadata_only",
            )

        # ── 3. YouTube → 제목/설명만 ──
        if self._is_youtube(domain):
            return self._parse_youtube(url, display_text)

        # ── 4. Confluence 내부 링크 → skip (이미 page JSON으로 처리) ──
        if "atlassian.net" in domain and "/wiki/" in url:
            return WebParseResult(
                text=f"[Confluence 내부 링크] {display_text or url}",
                title=display_text or "내부 페이지 참조",
                status="skipped",
                url=url,
                source_method="internal_link",
            )

        # ── 5. 일반 웹 페이지 → trafilatura 스크래핑 ──
        return self._scrape_general(url, display_text, context_summary)

    def _should_skip(self, domain: str) -> bool:
        return any(skip in domain for skip in self.skip_domains)

    def _is_sharepoint(self, domain: str) -> bool:
        return any(sp in domain for sp in self.sharepoint_domains)

    def _is_youtube(self, domain: str) -> bool:
        return any(yt in domain for yt in ("youtube.com", "youtu.be"))

    def _try_local_attachment(
        self, url: str, display_text: str, attachment_metadata: Optional[dict]
    ) -> Optional[WebParseResult]:
        """Data/attachments에서 매칭 파일 탐색."""
        if not self.attachments_dir.exists():
            return None

        # URL에서 파일명 추출 시도
        parsed = urlparse(url)
        path_parts = parsed.path.split("/")
        possible_filenames = [p for p in path_parts if "." in p]

        # display_text에서도 파일명 추출 시도
        if display_text and "." in display_text:
            possible_filenames.append(display_text.strip())

        for fname in possible_filenames:
            # attachments 디렉토리 내 재귀 탐색
            for match in self.attachments_dir.rglob(fname):
                if match.is_file():
                    logger.info("SharePoint 링크 → 로컬 파일 발견: %s", match)
                    from parsers.base import get_parser
                    parser = get_parser(match.suffix)
                    if parser:
                        parsed_content = parser.parse(match)
                        return WebParseResult(
                            text=parsed_content.text,
                            title=display_text or fname,
                            status="success",
                            url=url,
                            source_method=f"local_file:{parsed_content.parse_method}",
                        )
                    else:
                        return WebParseResult(
                            text=f"[로컬 파일 존재하나 파서 없음] {match.name}",
                            title=display_text or fname,
                            status="fallback",
                            url=url,
                            source_method="local_file:no_parser",
                        )

        return None

    def _scrape_general(
        self, url: str, display_text: str, context_summary: str
    ) -> WebParseResult:
        """trafilatura + httpx 기반 일반 웹 스크래핑."""
        self._rate_limit_wait()

        # trafilatura 시도
        try:
            import trafilatura

            downloaded = trafilatura.fetch_url(url)
            if downloaded:
                text = trafilatura.extract(
                    downloaded,
                    include_tables=True,
                    include_links=False,
                    favor_recall=True,
                )
                if text and len(text.strip()) > 30:
                    # 제목 추출 시도
                    title = ""
                    try:
                        metadata = trafilatura.extract_metadata(downloaded)
                        if metadata and metadata.title:
                            title = metadata.title
                    except Exception:
                        pass

                    return WebParseResult(
                        text=text.strip(),
                        title=title or display_text or urlparse(url).netloc,
                        status="success",
                        url=url,
                        source_method="trafilatura",
                    )
        except ImportError:
            logger.debug("trafilatura 미설치")
        except Exception as e:
            logger.debug("trafilatura 실패: %s — %s", url, e)

        # httpx + BeautifulSoup fallback
        try:
            import httpx
            from bs4 import BeautifulSoup

            resp = httpx.get(url, timeout=self.timeout, follow_redirects=True)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")

                title = ""
                title_tag = soup.find("title")
                if title_tag:
                    title = title_tag.get_text(strip=True)

                # 본문 추출 (간단한 heuristic)
                for tag in soup(["script", "style", "nav", "footer", "header"]):
                    tag.decompose()
                body_text = soup.get_text(separator="\n", strip=True)

                if body_text and len(body_text.strip()) > 30:
                    return WebParseResult(
                        text=body_text[:5000].strip(),
                        title=title or display_text or urlparse(url).netloc,
                        status="success",
                        url=url,
                        source_method="beautifulsoup",
                    )
        except ImportError:
            logger.debug("httpx 미설치")
        except Exception as e:
            logger.debug("httpx/BS4 실패: %s — %s", url, e)

        # 완전 실패 → context fallback
        return WebParseResult(
            text=context_summary or f"[접근 불가] {display_text or url}",
            title=display_text or "외부 링크",
            status="failed",
            url=url,
            source_method="fallback",
        )

    def _parse_youtube(self, url: str, display_text: str) -> WebParseResult:
        """YouTube 영상 제목/설명 추출."""
        self._rate_limit_wait()

        try:
            import httpx
            from bs4 import BeautifulSoup

            resp = httpx.get(url, timeout=self.timeout, follow_redirects=True)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                title = ""
                title_tag = soup.find("title")
                if title_tag:
                    title = title_tag.get_text(strip=True).replace(" - YouTube", "")

                # og:description
                desc = ""
                meta_desc = soup.find("meta", property="og:description")
                if meta_desc:
                    desc = meta_desc.get("content", "")

                return WebParseResult(
                    text=f"[YouTube 영상] {title}\n{desc}".strip(),
                    title=title or display_text or "YouTube 영상",
                    status="success",
                    url=url,
                    source_method="youtube_meta",
                )
        except Exception as e:
            logger.debug("YouTube 메타 추출 실패: %s", e)

        return WebParseResult(
            text=f"[YouTube 영상] {display_text or url}",
            title=display_text or "YouTube 영상",
            status="fallback",
            url=url,
            source_method="youtube_fallback",
        )

    def _rate_limit_wait(self):
        """요청 간 최소 간격 대기."""
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < self.rate_limit:
            time.sleep(self.rate_limit - elapsed)
        self._last_request_time = time.time()
