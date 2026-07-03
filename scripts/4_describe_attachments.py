"""
metadata 첨부파일·페이지 링크 설명 생성 → Data/attachment_descriptions/result_{page_id}.json

- 첨부: file_name + extension (기존). refined 있으면 API 스킵.
- 링크: url + anchor_text. metadata content.urls 기준, 페이지당 LINK_REFINE_MAX_PER_PAGE 상한.
"""
import os
import json
import base64
import logging
import traceback
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from bs4 import BeautifulSoup
from openai import OpenAI
from dotenv import load_dotenv

import scripts._bootstrap  # noqa: F401
from parsers.base import get_parser
from app.core.config import PROJECT_DIR, SKIP_SCRAPING_DOMAINS

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_debug_logger(log_path: Path) -> Callable[[dict], None]:
    def _write(record: dict) -> None:
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"[DebugLog] 기록 실패: {e}")

    return _write


GPT_MODEL = "gpt-4.1-nano"
GPT_MAX_OUTPUT_TOKENS = 1200
GPT_LINK_MAX_OUTPUT_TOKENS = 256
GPT_TEXT_CHAR_LIMIT = 3000
LINK_REFINE_MAX_PER_PAGE = int(os.getenv("LINK_REFINE_MAX_PER_PAGE", "40"))

EXCLUDE_LINK_URL_PREFIXES = (
    "https://connecteve-prod.atlassian.net/wiki/people",
)
SKIP_LINK_URL_SUBSTRINGS = (
    "resumedraft.action",
    "draftId=",
)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY 가 .env 파일에 없습니다.")

openai_client = OpenAI(api_key=OPENAI_API_KEY)


SYSTEM_INSTRUCTION = (
    "당신은 RAG(Vector DB) 구축을 위한 문서/이미지 의미 요약 전문가입니다.\n"
    "목표는 첨부파일을 그대로 상세 재작성하는 것이 아니라, "
    "해당 파일이 원문 문서의 이 위치에 삽입되었을 때 어떤 의미를 갖는지 "
    "검색 가능한 짧은 마크다운 설명으로 정리하는 것입니다.\n\n"
    "반드시 지켜야 할 규칙:\n"
    "1. 원문 전체를 그대로 옮기지 마세요.\n"
    "2. 이미지의 픽셀/레이아웃/색상/배치만 길게 설명하지 마세요.\n"
    "3. 문서의 파싱 텍스트를 그대로 재출력하지 마세요.\n"
    "4. 문서 맥락과 첨부파일 내용을 함께 보고, 핵심 의미만 요약하세요.\n"
    "5. 최종 출력은 600~800자 이내의 마크다운이어야 합니다.\n"
    "6. URL, 파일 경로, page_id 등 정확 출처 정보는 생성하지 마세요.\n"
    "7. 모르면 추측하지 말고, 확인 가능한 내용만 쓰세요.\n"
)

LINK_SYSTEM_INSTRUCTION = (
    "당신은 사내 위키 링크를 Slack에서 바로 이해할 수 있는 짧은 라벨로 요약하는 전문가입니다.\n"
    "목표는 URL 전체를 나열하는 것이 아니라, 동료가 클릭 전에 무엇을 볼지 알 수 있게 "
    "한 줄(10~20자) 한국어 라벨을 만드는 것입니다.\n\n"
    "규칙:\n"
    "1. 마크다운 제목(##) 없이 한 줄 또는 한 문장만 출력하세요.\n"
    "2. 앵커 텍스트·페이지 맥락에 있는 제목·기간·주제를 우선 사용하세요.\n"
    "3. URL 경로만으로 추측하지 말고, 제공된 정보만 사용하세요.\n"
    "4. 예: archived 수행 정부과제 현황 (2022~2024.07)\n"
    "5. 불확실하면 앵커 텍스트를 다듬어 라벨로 쓰세요.\n"
)


def is_excluded_link_url(url: str) -> bool:
    u = (url or "").strip()
    if not u.startswith(("http://", "https://")):
        return True
    if any(u.startswith(p) for p in EXCLUDE_LINK_URL_PREFIXES):
        return True
    if any(s in u for s in SKIP_LINK_URL_SUBSTRINGS):
        return True
    try:
        from urllib.parse import urlparse

        host = urlparse(u).netloc.lower()
        if any(skip in host for skip in SKIP_SCRAPING_DOMAINS):
            return True
    except Exception:
        pass
    return False


def collect_link_candidates(meta_data: dict[str, Any]) -> list[dict[str, str]]:
    """metadata content.urls → 중복 제거·제외 URL 필터."""
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for item in meta_data.get("content", {}).get("urls") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url or url in seen or is_excluded_link_url(url):
            continue
        seen.add(url)
        anchor = str(item.get("text") or "").strip()
        if anchor == url or anchor.startswith(("http://", "https://")):
            anchor = ""
        out.append({"url": url, "anchor_text": anchor})
        if len(out) >= LINK_REFINE_MAX_PER_PAGE:
            break
    return out


def load_cached_results(save_path: Path) -> tuple[list[dict], dict[tuple[str, str], dict], dict[str, dict]]:
    """
    result_{page_id}.json 캐시 로드.

    Returns:
        (results, cached_files by (file_name, ext), cached_urls by url)
    """
    results: list[dict] = []
    cached_files: dict[tuple[str, str], dict] = {}
    cached_urls: dict[str, dict] = {}
    if not save_path.exists():
        return results, cached_files, cached_urls
    try:
        with open(save_path, "r", encoding="utf-8") as f:
            cached_data = json.load(f)
        for item in cached_data.get("results", []) or []:
            if not isinstance(item, dict) or not item.get("refined"):
                continue
            url = str(item.get("url") or "").strip()
            if url.startswith(("http://", "https://")):
                cached_urls[url] = item
                results.append(item)
                continue
            fn = str(item.get("file_name") or "")
            ext = str(item.get("extension") or "").lower()
            if fn:
                cached_files[(fn, ext)] = item
                results.append(item)
    except Exception as e:
        logger.warning("캐시 로드 실패: %s — %s", save_path.name, e)
    return results, cached_files, cached_urls


def persist_results(save_path: Path, page_title: str, page_id: str, results: list[dict]) -> None:
    with open(save_path, "w", encoding="utf-8") as f:
        json.dump(
            {"page_title": page_title, "page_id": page_id, "results": results},
            f,
            ensure_ascii=False,
            indent=2,
        )


class GPTRefiner:
    """GPT-4.1 nano를 사용해 첨부파일을 문서 맥락 기반 semantic summary로 정제."""

    def __init__(self, model: str = GPT_MODEL):
        self.model = model
        logger.info(f"🚀 GPT Refiner 초기화: {self.model}")

    def _truncate_text(self, text: Optional[str], max_chars: int = GPT_TEXT_CHAR_LIMIT) -> str:
        if not text:
            return ""
        if len(text) > max_chars:
            logger.warning(f"⚠️ 텍스트가 너무 길어 {max_chars}자로 제한합니다.")
            return text[:max_chars]
        return text

    def _encode_image(self, image_path: Path) -> str:
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    def _image_media_type(self, image_path: Path) -> str:
        ext = image_path.suffix.lower()
        mapping = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".heic": "image/heic",
        }
        return mapping.get(ext, "image/jpeg")

    def _extract_page_context_from_metadata(self, metadata: dict, max_chars: int = 1200) -> str:
        """
        metadata 안의 원문 문서 내용에서 첨부파일 주변 판단에 쓸 문서 맥락을 추출한다.

        우선순위:
        1. page.body_view_html
        2. page.body.storage.value
        3. page.body
        4. body_view_html
        5. body
        6. raw_metadata.page.body_view_html
        """

        raw_metadata = metadata.get("raw_metadata", {}) if isinstance(metadata.get("raw_metadata"), dict) else {}

        candidates = [
            metadata.get("page", {}).get("body_view_html") if isinstance(metadata.get("page"), dict) else None,
            (
                metadata.get("page", {}).get("body", {}).get("storage", {}).get("value")
                if isinstance(metadata.get("page", {}).get("body"), dict)
                else None
            )
            if isinstance(metadata.get("page"), dict)
            else None,
            metadata.get("page", {}).get("body") if isinstance(metadata.get("page"), dict) else None,
            metadata.get("body_view_html"),
            metadata.get("body"),
            raw_metadata.get("page", {}).get("body_view_html") if isinstance(raw_metadata.get("page"), dict) else None,
            raw_metadata.get("body_view_html"),
        ]

        raw = ""
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                raw = candidate
                break

        if not raw:
            return ""

        try:
            soup = BeautifulSoup(raw, "html.parser")
            for tag in soup(["script", "style"]):
                tag.decompose()
            text = soup.get_text("\n")
        except Exception:
            text = raw

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        text = "\n".join(lines)

        return text[:max_chars]

    def _build_meta_block(self, metadata: dict) -> str:
        page_context = self._extract_page_context_from_metadata(metadata)

        section_path = metadata.get("section_path", [])
        if isinstance(section_path, list) and section_path:
            section_str = " > ".join(str(x) for x in section_path)
        else:
            section_str = "최상위 또는 알 수 없음"

        return (
            "[문서 메타데이터]\n"
            f"- 페이지 제목: {metadata.get('page_title')}\n"
            f"- 파일명: {metadata.get('file_name')}\n"
            f"- 첨부파일 확장자: {metadata.get('attachment_extension')}\n"
            f"- 문서 내 섹션 위치: {section_str}\n\n"
            "[첨부파일이 포함된 원문 문서 맥락]\n"
            f"{page_context if page_context else '(문서 본문 맥락 없음)'}\n\n"
        )

    def refine(
        self,
        metadata: dict,
        image_path: Optional[Path] = None,
        extracted_text: Optional[str] = None,
        log_fn: Optional[Callable[[dict], None]] = None,
    ) -> str:
        """
        메타데이터와 첨부파일 내용을 결합해 GPT로 semantic summary 생성.

        - image_path 전달 시: vision 요청
        - extracted_text 전달 시: 텍스트 기반 요청
        - log_fn: 디버그 로그 콜백
        """

        meta_block = self._build_meta_block(metadata)

        if image_path:
            b64 = self._encode_image(image_path)
            media_type = self._image_media_type(image_path)

            text_prompt = (
                meta_block
                + "[작업]\n"
                "이미지를 보고, 이 이미지가 현재 문서 맥락에서 어떤 정보를 제공하는지 파악하세요.\n"
                "이미지의 시각적 요소가 아닌, **이 이미지의 목적(예: 정책, 장비 가이드, 공지 등)**과 핵심 내용을 3줄 이내로 정리하세요.\n\n"
                "[출력 형식]\n"
                "## 요약\n"
                "- 핵심 내용 1~3개 (총 200자 이내)\n\n"
                "## 검색 키워드\n"
                "- 키워드 3~5개\n\n"
                "[제한]\n"
                "- 시각적 묘사 및 OCR식 텍스트 나열 금지\n"
                "- 문서 맥락과 무관한 추측 금지\n"
                "- 전체 200자 이내 준수"
            )

            user_content = [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{media_type};base64,{b64}",
                        "detail": "high",
                    },
                },
                {"type": "text", "text": text_prompt},
            ]

        else:
            truncated = self._truncate_text(extracted_text)

            text_prompt = (
                meta_block
                + f"[첨부 문서]\n{truncated}\n\n"
                "[작업]\n"
                "위 첨부 문서를 보고, 이 문서가 원문 페이지에서 **무엇을 증빙하거나 보충하는지** 핵심 가치만 요약하세요.\n"
                "불필요한 서술은 제거하고, 실제 데이터(수치, 정책 이름, 절차명) 위주로 짧게 정리합니다.\n\n"
                "[출력 형식]\n"
                "## 요약\n"
                "- 핵심 내용 2~3줄 (총 200자 이내)\n\n"
                "## 검색 키워드\n"
                "- 키워드 3~5개\n\n"
                "[제한]\n"
                "- 원문 복사 및 나열 금지\n"
                "- 전체 200자 이내 준수"
            )
            
            user_content = text_prompt

            if log_fn:
                log_fn({
                    "ts": _ts(),
                    "step": "refine_input",
                    "mode": "text",
                    "model": self.model,
                    "page_title": metadata.get("page_title"),
                    "file_name": metadata.get("file_name"),
                    "raw_text_chars": len(extracted_text or ""),
                    "truncated_text_chars": len(truncated),
                    "truncated": len(extracted_text or "") > GPT_TEXT_CHAR_LIMIT,
                    "system_prompt_chars": len(SYSTEM_INSTRUCTION),
                    "user_prompt_chars": len(user_content),
                    "page_context_chars": len(self._extract_page_context_from_metadata(metadata)),
                    "user_prompt_preview": user_content[:700],
                })

        response = openai_client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_INSTRUCTION},
                {"role": "user", "content": user_content},
            ],
            max_tokens=GPT_MAX_OUTPUT_TOKENS,
            temperature=0.1,
        )

        answer = response.choices[0].message.content or ""
        usage = response.usage

        logger.info(
            f"[GPTRefiner] 완료 — 입력 {usage.prompt_tokens}tok / "
            f"출력 {usage.completion_tokens}tok"
        )

        if log_fn:
            log_fn({
                "ts": _ts(),
                "step": "refine_output",
                "model": self.model,
                "page_title": metadata.get("page_title"),
                "file_name": metadata.get("file_name"),
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "output_chars": len(answer),
                "output_preview": answer[:500],
                "output_full": answer,
            })

        return answer

    def refine_link(
        self,
        metadata: dict,
        log_fn: Optional[Callable[[dict], None]] = None,
    ) -> str:
        """위키/외부 링크용 Slack 한 줄 라벨 생성."""
        page_context = self._extract_page_context_from_metadata(metadata, max_chars=800)
        anchor = str(metadata.get("anchor_text") or "").strip()
        url = str(metadata.get("url") or "").strip()

        user_content = (
            "[문서 메타데이터]\n"
            f"- 페이지 제목: {metadata.get('page_title')}\n"
            f"- 링크 앵커 텍스트: {anchor or '(없음)'}\n"
            f"- 링크 URL: {url}\n\n"
            "[원문 페이지 맥락 일부]\n"
            f"{page_context or '(맥락 없음)'}\n\n"
            "[작업]\n"
            "위 정보를 바탕으로, 동료가 Slack에서 링크를 클릭하기 전에 알 수 있는 "
            "한 줄 한국어 라벨을 작성하세요. 마크다운 제목 없이 라벨만 출력하세요.\n"
        )

        if log_fn:
            log_fn({
                "ts": _ts(),
                "step": "refine_link_input",
                "model": self.model,
                "page_id": metadata.get("page_id"),
                "url": url,
                "anchor_text": anchor,
            })

        response = openai_client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": LINK_SYSTEM_INSTRUCTION},
                {"role": "user", "content": user_content},
            ],
            max_tokens=GPT_LINK_MAX_OUTPUT_TOKENS,
            temperature=0.1,
        )

        answer = (response.choices[0].message.content or "").strip()
        answer = " ".join(answer.split())
        if len(answer) > 120:
            answer = answer[:117].rstrip() + "..."

        if log_fn:
            log_fn({
                "ts": _ts(),
                "step": "refine_link_output",
                "url": url,
                "output": answer,
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
            })

        return answer


def process_metadata_and_attachments(metadata_dir: Path, project_root: Path):
    refiner = GPTRefiner() if globals().get("VLM_ENABLED", True) else None

    metadata_files = list(metadata_dir.glob("*.metadata.json"))
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    save_dir = project_root / "Data" / "attachment_descriptions"
    save_dir.mkdir(parents=True, exist_ok=True)

    debug_log_path = save_dir / "debug.jsonl"
    log = make_debug_logger(debug_log_path)

    logger.info(f"📝 디버그 로그 경로: {debug_log_path}")
    log({
        "ts": _ts(),
        "step": "run_start",
        "run_id": run_id,
        "model": GPT_MODEL,
        "metadata_files": len(metadata_files),
    })

    for meta_path in metadata_files:
        logger.info(f"\n{'=' * 50}\n▶ 처리 시작: {meta_path.name}\n{'=' * 50}")

        with open(meta_path, "r", encoding="utf-8") as f:
            meta_data = json.load(f)

        page = meta_data.get("page", {})
        page_title = page.get("title")
        page_id = page.get("id")
        attachments = meta_data.get("attachments", [])

        save_path = save_dir / f"result_{page_id}.json"

        results, cached_files, cached_urls = load_cached_results(save_path)
        if cached_files or cached_urls:
            logger.info(
                "🧠 캐시 로드: %s (첨부 %d, 링크 %d)",
                save_path.name,
                len(cached_files),
                len(cached_urls),
            )

        links_new = 0
        links_skipped = 0
        files_new = 0

        for att in attachments:
            ext = str(att.get("extension", "")).lower()
            title = att.get("title", "unknown")
            saved_path = att.get("saved_path", "")
            file_path = project_root / saved_path.replace("\\", "/")

            cache_key = (str(title or ""), str(ext or "").lower())
            cached_entry = cached_files.get(cache_key)
            if cached_entry and cached_entry.get("refined"):
                log({
                    "ts": _ts(),
                    "step": "cache_hit",
                    "page_id": page_id,
                    "file_name": title,
                    "ext": ext,
                    "reason": "already_refined",
                })
                continue

            if not file_path.exists():
                log({
                    "ts": _ts(),
                    "step": "file_skip",
                    "page_id": page_id,
                    "file_name": title,
                    "reason": "file_not_found",
                    "path": str(file_path),
                })
                continue

            logger.info(f"📄 파일 분석 중: {title}")

            log({
                "ts": _ts(),
                "step": "file_start",
                "page_id": page_id,
                "page_title": page_title,
                "file_name": title,
                "ext": ext,
                "file_size_bytes": file_path.stat().st_size,
            })

            file_res = {
                "file_name": title,
                "extension": ext,
                "raw": None,
                "refined": None,
            }

            refiner_meta = {
                "page_title": page_title,
                "page_id": page_id,
                "file_name": title,
                "attachment_title": title,
                "attachment_extension": ext,
                "attachment_saved_path": saved_path,
                "page": page,
                "body_view_html": page.get("body_view_html") or meta_data.get("body_view_html", ""),
                "section_path": att.get("section_path", []),
                "raw_metadata": meta_data,
            }

            try:
                is_image = ext in [".png", ".jpg", ".jpeg", ".heic", ".webp"]

                if not is_image:
                    parser = get_parser(ext)

                    if parser:
                        parsed_result = parser.parse(file_path)
                        parsed_text = parsed_result.text or ""
                        file_res["raw"] = parsed_text

                        log({
                            "ts": _ts(),
                            "step": "parse_done",
                            "page_id": page_id,
                            "file_name": title,
                            "raw_chars": len(parsed_text),
                        })

                        if refiner and parsed_text.strip():
                            file_res["refined"] = refiner.refine(
                                refiner_meta,
                                extracted_text=parsed_text,
                                log_fn=log,
                            )
                        else:
                            log({
                                "ts": _ts(),
                                "step": "refine_skip",
                                "page_id": page_id,
                                "file_name": title,
                                "reason": "empty_parsed_text_or_refiner_disabled",
                            })
                    else:
                        log({
                            "ts": _ts(),
                            "step": "file_skip",
                            "page_id": page_id,
                            "file_name": title,
                            "reason": "no_parser_for_ext",
                            "ext": ext,
                        })
                        continue

                else:
                    if refiner:
                        file_res["refined"] = refiner.refine(
                            refiner_meta,
                            image_path=file_path,
                            log_fn=log,
                        )
                    else:
                        log({
                            "ts": _ts(),
                            "step": "refine_skip",
                            "page_id": page_id,
                            "file_name": title,
                            "reason": "refiner_disabled",
                        })

                results.append(file_res)
                files_new += 1
                persist_results(save_path, page_title, page_id, results)
                logger.info(f"💾 중간 저장 완료: {save_path.name} (현재 {len(results)}개 누적)")

                log({
                    "ts": _ts(),
                    "step": "file_done",
                    "page_id": page_id,
                    "file_name": title,
                    "accumulated": len(results),
                })

            except Exception as e:
                logger.error(f"❌ 처리 실패 ({title}): {e}")

                log({
                    "ts": _ts(),
                    "step": "file_error",
                    "page_id": page_id,
                    "file_name": title,
                    "error": str(e),
                    "traceback": traceback.format_exc(),
                })

        link_candidates = collect_link_candidates(meta_data)
        if link_candidates:
            logger.info(
                "🔗 링크 설명 대상: %d개 (상한 %d)",
                len(link_candidates),
                LINK_REFINE_MAX_PER_PAGE,
            )

        for link in link_candidates:
            url = link["url"]
            if cached_urls.get(url, {}).get("refined"):
                links_skipped += 1
                log({
                    "ts": _ts(),
                    "step": "link_cache_hit",
                    "page_id": page_id,
                    "url": url,
                })
                continue

            if not refiner:
                continue

            logger.info("🔗 링크 분석: %s", url[:80])

            link_meta = {
                "page_title": page_title,
                "page_id": page_id,
                "url": url,
                "anchor_text": link.get("anchor_text") or "",
                "page": page,
                "raw_metadata": meta_data,
            }

            try:
                refined = refiner.refine_link(link_meta, log_fn=log)
                if not refined:
                    continue

                link_res = {
                    "kind": "link",
                    "url": url,
                    "anchor_text": link.get("anchor_text") or "",
                    "refined": refined,
                }
                results.append(link_res)
                cached_urls[url] = link_res
                links_new += 1
                persist_results(save_path, page_title, page_id, results)

                log({
                    "ts": _ts(),
                    "step": "link_done",
                    "page_id": page_id,
                    "url": url,
                    "refined_preview": refined[:120],
                })
            except Exception as e:
                logger.error("❌ 링크 처리 실패 (%s): %s", url[:60], e)
                log({
                    "ts": _ts(),
                    "step": "link_error",
                    "page_id": page_id,
                    "url": url,
                    "error": str(e),
                })

        file_count = sum(1 for r in results if r.get("file_name"))
        link_count = sum(1 for r in results if str(r.get("url") or "").startswith(("http://", "https://")))
        logger.info(
            "✅ 페이지 완료: %s (첨부 %d, 링크 %d / 신규 파일 %d, 신규 링크 %d)",
            page_title,
            file_count,
            link_count,
            files_new,
            links_new,
        )

        log({
            "ts": _ts(),
            "step": "page_done",
            "page_id": page_id,
            "page_title": page_title,
            "total_results": len(results),
            "file_results": file_count,
            "link_results": link_count,
            "files_new": files_new,
            "links_new": links_new,
            "links_skipped_cache": links_skipped,
        })


if __name__ == "__main__":
    VLM_ENABLED = True

    process_metadata_and_attachments(
        PROJECT_DIR / "Data" / "metadata",
        PROJECT_DIR,
    )