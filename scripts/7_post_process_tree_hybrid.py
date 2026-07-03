"""
RAG Tree Hybrid Registry 후처리 (rag_tree_hybrid_*)

수행 작업:
  parent_store (page_body):
    1. urls        - 본문에 등장·refined 있는 URL만 urls[] + text [링크: refined | URL]
    2. attachments - [첨부: ...] / [이미지: ...] → metadata attachments/extracted_links + description 매칭
    3. text 교정   - [이미지: *.pdf/.docx/.xlsx 등] → [첨부: 파일명 | 핵심 요약: ...] 로 변환
       - [링크: … | URL](중복 URL/경로) 접미사 제거
    4. lexical_boost는 parent_store에는 추가하지 않음

  vector_payload (child_evidence):
    5. urls / attachments 동일 enrichment
    6. lexical_boost는 child/vector payload에만 추가

  검증 리포트:
    7. parent_doc_id 미등록 child 목록
    8. 처리 통계 리포트

주의:
  - [PAYCO]공문_...pdf 같이 파일명 자체에 ']'가 포함되어도 확장자 뒤의 ']'까지 파일명으로 인식한다.
  - 이미지도 attachments 필드에 {id,title,download_url,saved_path}만 넣는다.
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import scripts._bootstrap  # noqa: F401

# =============================================================================
# Config
# =============================================================================

from app.core.config import METADATA_DIR, DESC_DIR, PROJECT_ROOT, TREE_REGISTRY_DIR

REGISTRY_DIR = Path(
    os.getenv("TREE_HYBRID_REGISTRY_DIR", str(TREE_REGISTRY_DIR))
).resolve()

PARENT_STORE_JSONL = REGISTRY_DIR / "rag_tree_hybrid_parent_store.jsonl"
VECTOR_PAYLOAD_JSONL = REGISTRY_DIR / "rag_tree_hybrid_vector_payload.jsonl"
REPORT_JSON = REGISTRY_DIR / "post_process_tree_hybrid_report.json"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".heic", ".gif", ".tif", ".tiff", ".svg"}
DOC_EXTS = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".hwp", ".hwpx", ".txt", ".md", ".csv"}
ALL_FILE_EXTS = sorted(IMAGE_EXTS | DOC_EXTS, key=len, reverse=True)
EXT_PATTERN = "|".join(re.escape(e.lstrip(".")) for e in ALL_FILE_EXTS)


# =============================================================================
# Patterns
# =============================================================================

# https:// URL
URL_IN_TEXT_RE = re.compile(r"https?://[^\s\]()<>'\"]+")

# Confluence 사용자 프로필 — 본문 (url) 블록 제거 대상
WIKI_PEOPLE_URL_PREFIX = "https://connecteve-prod.atlassian.net/wiki/people"
PEOPLE_WIKI_PARENS_RE = re.compile(
    r"\(\s*" + re.escape(WIKI_PEOPLE_URL_PREFIX) + r"[^)]*\)",
    re.IGNORECASE,
)

# 최종/중간 링크 마커
FINAL_LINK_MARKER_RE = re.compile(
    r"\[링크:\s*([^\]|]+?)\s*\|\s*(https?://[^\]\s]+)\s*\]",
    re.IGNORECASE,
)
INTERMEDIATE_LINK_RE = re.compile(
    r"\[(?!링크:|첨부:|이미지:)(?P<anchor>[^\]|]+?)\s*\|\s*(?P<url>https?://[^\]\s]+)\s*\]",
    re.IGNORECASE,
)
NESTED_LINK_INNER_RE = re.compile(
    r"\[링크:\s*(?P<label>[^\]|]+?)\s*\|\s*(?:\[+\s*링크:[^\]]*?\|\s*)*(?P<url>https?://[^\]\s]+)\s*\]+",
    re.IGNORECASE,
)
# [링크: … | https://…](동일·포함 URL 또는 /wiki/… 경로) — 마크다운 잔여 (…) 제거
LINK_BRACKET_HTTP_THEN_PAREN_RE = re.compile(
    r"(\[(?:링크:\s*)?[^\]]*\|\s*https?://[^\]]+\])\(([^)]+)\)",
    re.IGNORECASE,
)

# 파일 marker는 label이 아니라 확장자 기준으로 찾는다.
# 지원 예:
#   [이미지 image-20240703.png]
#   [이미지: image-20240703.png]
#   [첨부: [PAYCO]공문_복지포인트 시스템 개편 안내의 건.pdf | 설명]
#   [[PAYCO]공문_복지포인트 시스템 개편 안내의 건.pdf]
#   [파일명.xlsx | 핵심 요약: ...]
FILE_MARKER_RE = re.compile(
    rf"\["
    rf"(?:(?P<label>이미지|첨부|파일)\s*:?\s*)?"
    rf"(?P<filename>[^\n\r]*?\.(?:{EXT_PATTERN}))"
    rf"(?:\s*\|\s*(?P<desc>[^\]]*))?"
    rf"\]",
    re.IGNORECASE,
)

# =============================================================================
# Basic helpers
# =============================================================================

def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def one_line(text: str) -> str:
    return re.sub(r"\s+", " ", normalize_text(text)).strip()


def file_ext(name: str) -> str:
    return Path(str(name or "").split("?")[0]).suffix.lower()


def clean_filename(name: str) -> str:
    name = one_line(str(name or ""))
    if "|" in name:
        name = name.split("|", 1)[0].strip()
    return Path(name).name


def is_image_file(name: str) -> bool:
    return file_ext(name) in IMAGE_EXTS


def is_doc_file(name: str) -> bool:
    return file_ext(name) in DOC_EXTS


def strip_empty(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def dedupe_by_key(rows: list[dict[str, Any]], key_fields: tuple[str, ...]) -> list[dict[str, Any]]:
    seen: set[tuple[str, ...]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = tuple(str(row.get(k, "")).strip().lower() for k in key_fields)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


# =============================================================================
# Metadata / description loader
# =============================================================================

def load_all_metadata(meta_dir: Path) -> dict[str, dict[str, Any]]:
    """page_id → metadata dict."""
    index: dict[str, dict[str, Any]] = {}
    if not meta_dir.is_dir():
        return index
    for path in meta_dir.glob("*.metadata.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        pid = str((data.get("page") or {}).get("id") or data.get("page_id") or "").strip()
        if pid:
            index[pid] = data
    return index


def _iter_desc_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("results", "items", "descriptions", "attachments", "data"):
        val = data.get(key)
        if isinstance(val, list):
            return [x for x in val if isinstance(x, dict)]
    return []


def load_page_descriptions(page_id: str) -> dict[str, dict[str, str]]:
    """
    Data/attachment_descriptions/result_{page_id}.json에서 results[].refined를 읽는다.

    반환:
      {
        "by_filename": {filename_lower: refined},
        "by_url": {url: refined},
      }
    """
    path = DESC_DIR / f"result_{page_id}.json"
    out = {"by_filename": {}, "by_url": {}}
    if not path.exists():
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return out

    for item in _iter_desc_items(data):
        refined = normalize_text(
            item.get("refined")
            or item.get("description")
            or item.get("summary")
            or item.get("text")
            or ""
        )
        if not refined:
            continue
        names = [
            item.get("file_name"),
            item.get("filename"),
            item.get("title"),
            item.get("name"),
        ]
        for name in names:
            fname = clean_filename(str(name or ""))
            if fname:
                out["by_filename"][fname.lower()] = refined
        url = str(item.get("url") or item.get("download_url") or item.get("link_url") or "").strip()
        if url:
            out["by_url"][url] = refined
    return out


def get_desc_for_file(desc_index: dict[str, dict[str, str]], filename: str) -> str:
    fname = clean_filename(filename)
    if not fname:
        return ""
    return desc_index.get("by_filename", {}).get(fname.lower(), "")


def get_desc_for_url(desc_index: dict[str, dict[str, str]], url: str) -> str:
    url = str(url or "").strip()
    if not url:
        return ""
    if url in desc_index.get("by_url", {}):
        return desc_index["by_url"][url]
    # URL path basename fallback
    fname = clean_filename(url.split("?", 1)[0])
    return get_desc_for_file(desc_index, fname)


def get_meta_urls(meta: dict[str, Any]) -> list[dict[str, str]]:
    """metadata content.urls → [{text, url}]"""
    return list(meta.get("content", {}).get("urls", []) or [])


def get_meta_attachments(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """metadata attachments → 최소 attachment meta."""
    raw = meta.get("attachments") or []
    result: list[dict[str, Any]] = []
    for a in raw:
        if not isinstance(a, dict):
            continue
        result.append(strip_empty({
            "id": a.get("id") or a.get("attachment_id") or a.get("content_id"),
            "title": a.get("title") or a.get("filename") or "",
            "download_url": a.get("download_url") or a.get("url") or "",
            "saved_path": a.get("saved_path") or a.get("local_path") or "",
        }))
    return result


def get_meta_extracted_links(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """metadata extracted_links → [{tag, filename, extension, url, ...}]"""
    return list(meta.get("extracted_links") or [])



# =============================================================================
# Enrichment helpers
# =============================================================================

def extract_urls_from_text(text: str) -> list[str]:
    return URL_IN_TEXT_RE.findall(text or "")


def strip_people_wiki_parentheses(text: str) -> tuple[str, int]:
    """
    마크다운 등 본문에서 (https://.../wiki/people/...) 괄호 블록 전체를 제거한다.
    """
    if not text:
        return text, 0
    new_text, n = PEOPLE_WIKI_PARENS_RE.subn("", text)
    return new_text, n


def _url_fragment_key(s: str) -> str:
    """URL·경로 비교용 정규화 (소문자, unquote, trailing slash 제거)."""
    raw = unquote(str(s or "").strip())
    if not raw:
        return ""
    if raw.startswith(("http://", "https://")):
        parsed = urlparse(raw)
        frag = parsed.path or ""
        if parsed.query:
            frag += "?" + parsed.query
        return frag.rstrip("/").lower()
    return raw.rstrip("/").lower()


def _paren_overlaps_bracket_urls(paren: str, bracket: str) -> bool:
    """괄호 안 URL/경로가 앞 […| https://…] 블록 URL과 동일·포함 관계인지."""
    p = unquote(str(paren or "").strip())
    if not p:
        return False
    bracket_u = unquote(bracket or "")
    if p in bracket_u:
        return True
    p_key = _url_fragment_key(p)
    if not p_key:
        return False
    for u in URL_IN_TEXT_RE.findall(bracket_u):
        u_key = _url_fragment_key(u)
        if not u_key:
            continue
        if p_key in u_key or u_key in p_key:
            return True
        if p.startswith("/") and (p_key in u_key or u_key.endswith(p_key)):
            return True
        if u in p or p in u:
            return True
    p_base = Path(p.split("?")[0].split("#")[0]).name.lower()
    if len(p_base) >= 4:
        for u in URL_IN_TEXT_RE.findall(bracket_u):
            u_base = Path(urlparse(u).path).name.lower()
            if u_base and u_base == p_base:
                return True
    return False


def strip_duplicate_link_paren_suffix(text: str) -> tuple[str, int]:
    """
    [링크: … | https://full…](/wiki/… 또는 동일·포함 URL) 형태에서
    뒤 (…) 마크다운 접미사 전체를 제거한다.
    """
    if not text:
        return text, 0
    total = 0
    out = text
    while True:
        changed = False

        def _repl(m: re.Match[str]) -> str:
            nonlocal changed, total
            bracket, paren = m.group(1), m.group(2)
            if _paren_overlaps_bracket_urls(paren, bracket):
                changed = True
                total += 1
                return bracket
            return m.group(0)

        out, _ = LINK_BRACKET_HTTP_THEN_PAREN_RE.subn(_repl, out)
        if not changed:
            break
    return out, total


def match_url_meta(url: str, meta_urls: list[dict[str, str]], desc_index: dict[str, dict[str, str]]) -> dict[str, str] | None:
    """refined가 있는 URL만 entry 반환. text는 Slack/LLM용 한 줄 요약."""
    desc = get_desc_for_url(desc_index, url)
    if not desc:
        return None

    summary = one_line(desc)
    entry: dict[str, str] = {
        "text": summary,
        "url": url,
        "description": summary,
    }
    for mu in meta_urls:
        if str(mu.get("url", "")) == url:
            anchor = str(mu.get("text") or "").strip()
            if anchor and anchor != url and not anchor.startswith(("http://", "https://")):
                entry["anchor_text"] = anchor
            break
    return entry


def _meta_anchor_for_url(url: str, meta_urls: list[dict[str, str]]) -> str:
    for mu in meta_urls:
        if str(mu.get("url") or "").strip() == url:
            anc = str(mu.get("text") or "").strip()
            if anc and anc != url and not anc.startswith(("http://", "https://")):
                return anc
    return ""


def _link_relevant_in_body(body: str, url: str, meta_urls: list[dict[str, str]]) -> bool:
    """본문에 raw URL 또는 해당 링크의 metadata 앵커 텍스트가 있는지."""
    if url and url in body:
        return True
    anchor = _meta_anchor_for_url(url, meta_urls)
    return bool(anchor and anchor in body)


def iter_url_candidates(
    text: str,
    meta_urls: list[dict[str, str]],
    desc_index: dict[str, dict[str, str]],
) -> list[str]:
    """본문에 URL·평문 앵커가 실제로 등장하는 링크만 수집."""
    seen: set[str] = set()
    out: list[str] = []
    body = text or ""

    def add(u: str) -> None:
        u = str(u or "").strip()
        if not u.startswith(("http://", "https://")) or u in seen:
            return
        if not _link_relevant_in_body(body, u, meta_urls):
            return
        seen.add(u)
        out.append(u)

    for u in extract_urls_from_text(body):
        add(u)
    for mu in meta_urls:
        add(str(mu.get("url") or ""))
    return out


def _final_link_marker_exists(text: str, url: str) -> bool:
    """본문에 [링크: … | url] 최종 마커가 이미 있는지."""
    url = str(url or "").strip()
    if not url:
        return False
    for m in FINAL_LINK_MARKER_RE.finditer(text or ""):
        if str(m.group(2) or "").strip() == url:
            return True
    return False


def _url_inside_link_marker(text: str, url: str) -> bool:
    """URL이 [링크: … | …] 또는 [앵커 | url] 블록 안에 있는지."""
    url = str(url or "").strip()
    if not url or url not in (text or ""):
        return False
    if _final_link_marker_exists(text, url):
        return True
    for m in INTERMEDIATE_LINK_RE.finditer(text or ""):
        if str(m.group("url") or "").strip() == url:
            return True
    return False


def repair_nested_link_markers(text: str) -> tuple[str, int]:
    """[링크: a | [링크: a | [링크: a | url]]] → [링크: a | url] 평탄화."""
    out = text or ""
    fixed = 0
    while "| [링크:" in out or "| [[링크:" in out or "| [[[링크:" in out:
        m = NESTED_LINK_INNER_RE.search(out)
        if not m:
            break
        label = one_line(m.group("label") or "링크")
        url = str(m.group("url") or "").strip()
        out = out[: m.start()] + link_marker_text(label, url) + out[m.end() :]
        fixed += 1
    return out, fixed


def _is_http_link_file_match(m: re.Match[str]) -> bool:
    """[…xlsx | https://…] 등 http 링크 블록은 첨부 파일 마커가 아님."""
    return bool(re.search(r"https?://", m.group(0) or "", re.IGNORECASE))


def build_urls_with_description(
    text: str,
    meta_urls: list[dict[str, str]],
    desc_index: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    """refined(description)가 attachment_descriptions에 있는 URL만 urls 필드용으로 반환."""
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for u in iter_url_candidates(text, meta_urls, desc_index):
        entry = match_url_meta(u, meta_urls, desc_index)
        if not entry or entry["url"] in seen:
            continue
        seen.add(entry["url"])
        entries.append(entry)
    return entries


def link_marker_text(summary: str, url: str) -> str:
    """Slack/LLM용 인라인 링크 마커: [링크: refined | url]."""
    label = one_line(summary or "링크")
    return f"[링크: {label} | {url}]"


def _plain_anchor_marked(text: str, anchor: str) -> bool:
    if not anchor:
        return True
    if f"[{anchor}]" in text or f"[{anchor} |" in text:
        return True
    return bool(re.search(rf"\[링크:[^\]]*{re.escape(anchor)}", text or "", re.IGNORECASE))


def inject_link_markers_into_text(
    text: str,
    url_entries: list[dict[str, str]],
) -> tuple[str, int]:
    """
    평문 앵커·[앵커 | URL]·raw URL → [링크: refined | URL] (멱등).
    """
    if not text or not url_entries:
        return text, 0

    out, nested_fixed = repair_nested_link_markers(text)
    replaced = nested_fixed
    ordered = sorted(
        url_entries,
        key=lambda e: len(str(e.get("anchor_text") or e.get("text") or e.get("url") or "")),
        reverse=True,
    )

    for entry in ordered:
        url = str(entry.get("url") or "").strip()
        if not url:
            continue
        anchor = str(entry.get("anchor_text") or "").strip()
        if url not in out and not (anchor and anchor in out):
            continue
        if _final_link_marker_exists(out, url):
            continue

        summary = str(entry.get("text") or entry.get("description") or "").strip()
        marker = link_marker_text(summary, url)

        block = re.search(
            rf"\[(?!링크:|첨부:|이미지:)(?P<anchor>[^\]|]+?)\s*\|\s*{re.escape(url)}\s*\]",
            out,
            re.IGNORECASE,
        )
        if block:
            out = out[: block.start()] + marker + out[block.end() :]
            replaced += 1
            continue

        if anchor and anchor != url:
            for pattern in (f"[{anchor}]", f"[{anchor} |"):
                if pattern in out and not _url_inside_link_marker(out, url):
                    out = out.replace(pattern, marker, 1)
                    replaced += 1
                    break
            if _final_link_marker_exists(out, url):
                continue
            if anchor in out and not _plain_anchor_marked(out, anchor):
                out = out.replace(anchor, marker, 1)
                replaced += 1
                continue

        if _url_inside_link_marker(out, url):
            continue
        idx = out.find(url)
        if idx >= 0:
            out = out[:idx] + marker + out[idx + len(url) :]
            replaced += 1

    return out, replaced


def _attachment_minimal(row: dict[str, Any], fallback_title: str = "") -> dict[str, Any]:
    """attachments 필드에는 id/title/download_url/saved_path만 유지."""
    return strip_empty({
        "id": row.get("id") or row.get("attachment_id") or row.get("content_id"),
        "title": row.get("title") or row.get("filename") or fallback_title,
        "download_url": row.get("download_url") or row.get("url") or "",
        "saved_path": row.get("saved_path") or row.get("local_path") or "",
    })


def match_attachment_meta(
    filename: str,
    meta_atts: list[dict[str, Any]],
    extracted_links: list[dict[str, Any]],
) -> dict[str, Any]:
    """파일명으로 attachment/extracted_links 매칭. 반환은 id/title/download_url/saved_path 최소 필드."""
    fname = clean_filename(filename)
    for a in meta_atts:
        title = clean_filename(str(a.get("title", "")))
        if title and title.lower() == fname.lower():
            return _attachment_minimal(a, fname)
    for el in extracted_links:
        title = clean_filename(str(el.get("filename") or el.get("title") or ""))
        if title and title.lower() == fname.lower():
            return _attachment_minimal({
                "id": el.get("id") or el.get("content_id") or el.get("data-linked-resource-id"),
                "title": fname,
                "download_url": el.get("download_url") or el.get("url") or "",
                "saved_path": el.get("saved_path") or "",
            }, fname)
    return {"title": fname, "download_url": "", "saved_path": ""}

def iter_file_markers(text: str) -> list[dict[str, str]]:
    """
    본문에서 [파일명.ext] 계열 marker를 확장자 기준으로 추출한다.

    label이 없어도 확장자만 있으면 추출한다.
    예:
      [이미지 image.png]
      [image.png]
      [첨부: [PAYCO]공문.pdf | 설명]
    """
    out: list[dict[str, str]] = []

    for m in FILE_MARKER_RE.finditer(text or ""):
        raw_label = (m.group("label") or "").strip()
        filename = clean_filename(m.group("filename") or "")
        desc = (m.group("desc") or "").strip()

        if not filename:
            continue

        ext = file_ext(filename)

        if ext in IMAGE_EXTS:
            kind = "image"
        elif ext in DOC_EXTS:
            kind = "attachment"
        else:
            kind = "unknown"

        out.append({
            "label": raw_label,
            "filename": filename,
            "desc": desc,
            "kind": kind,
        })

    return out


def marker_label_for_file(filename: str, raw_label: str = "") -> str:
    """파일 확장자 기준으로 본문 marker label을 결정한다."""
    if is_image_file(filename):
        return "이미지"
    if is_doc_file(filename):
        return "첨부"
    return raw_label or "파일"


def marker_with_description(label: str, filename: str, desc: str) -> str:
    """본문 marker를 description 포함 형태로 통일."""
    fname = clean_filename(filename)
    if desc:
        return f"[{label}: {fname} | 핵심 요약: {one_line(desc)}]"
    return f"[{label}: {fname}]"


def normalize_markers_with_descriptions(text: str, desc_index: dict[str, dict[str, str]]) -> tuple[str, dict[str, int]]:
    """
    본문 marker를 확장자 기준으로 정규화한다.

    - 이미지 확장자(.png/.jpg/...) → [이미지: 파일명 | 핵심 요약: ...]
    - 문서 확장자(.pdf/.xlsx/...) → [첨부: 파일명 | 핵심 요약: ...]
    - label이 없어도 확장자만 있으면 처리한다.
    """
    stats = defaultdict(int)

    def repl(m: re.Match[str]) -> str:
        if _is_http_link_file_match(m):
            stats["file_marker_skipped_http_link"] += 1
            return m.group(0)

        raw_label = (m.group("label") or "").strip()
        fname = clean_filename(m.group("filename") or "")
        if not fname:
            return m.group(0)

        desc = get_desc_for_file(desc_index, fname) or (m.group("desc") or "").strip()
        out_label = marker_label_for_file(fname, raw_label)

        if raw_label == "이미지" and is_doc_file(fname):
            stats["image_doc_marker_fixed_to_attachment"] += 1
        if raw_label and raw_label != out_label:
            stats["marker_label_normalized_by_extension"] += 1
        if not raw_label:
            stats["marker_label_inferred_by_extension"] += 1
        if desc:
            stats["markers_description_added"] += 1

        return marker_with_description(out_label, fname, desc)

    return FILE_MARKER_RE.sub(repl, text or ""), dict(stats)


def extract_marked_files(text: str) -> tuple[list[str], list[str]]:
    """
    본문 marker에서 (doc filenames, image filenames)를 확장자 기준으로 분류한다.

    label이 [이미지 ...]인지 [첨부 ...]인지에 의존하지 않는다.
    """
    docs: list[str] = []
    images: list[str] = []
    seen_doc: set[str] = set()
    seen_img: set[str] = set()

    for item in iter_file_markers(text or ""):
        raw = str(item.get("filename") or "")
        if re.search(r"https?://", raw, re.IGNORECASE):
            continue
        fname = clean_filename(raw)
        if not fname:
            continue

        if is_image_file(fname):
            if fname.lower() not in seen_img:
                seen_img.add(fname.lower())
                images.append(fname)
        elif is_doc_file(fname):
            if fname.lower() not in seen_doc:
                seen_doc.add(fname.lower())
                docs.append(fname)

    return docs, images


def enrich_doc(doc: dict[str, Any], meta: dict[str, Any] | None, desc_index: dict[str, dict[str, str]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    단일 doc에 urls / attachments 필드를 추가하고 text marker에 refined description을 붙인다.
    lexical_boost는 여기서 parent에 추가하지 않는다.
    """
    stats: dict[str, Any] = {}
    text: str = doc.get("text", "") or ""
    if text:
        text, n_dup = strip_duplicate_link_paren_suffix(text)
        if n_dup:
            doc = dict(doc)
            doc["text"] = text
            stats["duplicate_link_parens_stripped"] = n_dup

    if meta is None or doc.get("source_kind") not in ("page_body",):
        return doc, stats

    doc = dict(doc)
    text = doc.get("text", "") or ""
    meta_urls = get_meta_urls(meta)
    meta_atts = get_meta_attachments(meta)
    extracted_links = get_meta_extracted_links(meta)

    # 0) Confluence people 프로필 URL — (…wiki/people…) 괄호 블록 제거
    text, n_people = strip_people_wiki_parentheses(text)
    if n_people:
        doc["text"] = text
        stats["people_wiki_parens_stripped"] = n_people

    # 1) 중첩 [링크: … | [링크: …]] 평탄화
    text, n_nested = repair_nested_link_markers(text)
    if n_nested:
        doc["text"] = text
        stats["nested_link_markers_repaired"] = n_nested

    # 2) 본문 marker description 추가 + [이미지: *.pdf] → [첨부: *.pdf] 교정 (http 링크 블록 제외)
    new_text, marker_stats = normalize_markers_with_descriptions(text, desc_index)
    if new_text != text:
        doc["text"] = new_text
        text = new_text
    for k, v in marker_stats.items():
        stats[k] = stats.get(k, 0) + v

    # 3) URLs — refined 있는 항목만 urls[] + 본문 [링크: 요약 | URL] 치환
    url_entries = build_urls_with_description(text, meta_urls, desc_index)
    if url_entries:
        doc["urls"] = url_entries
        stats["urls_added"] = len(url_entries)
        stats["url_descriptions_added"] = len(url_entries)
        marked_text, n_mark = inject_link_markers_into_text(text, url_entries)
        if marked_text != text:
            doc["text"] = marked_text
            text = marked_text
            stats["link_markers_in_text"] = n_mark
    else:
        stats["urls_skipped_no_description"] = len(iter_url_candidates(text, meta_urls, desc_index))

    # 4) Attachments: 첨부 + 이미지 모두 attachments 필드에 넣는다.
    doc_filenames, image_filenames = extract_marked_files(text)
    attachment_entries: list[dict[str, Any]] = []
    for fname in doc_filenames + image_filenames:
        info = match_attachment_meta(fname, meta_atts, extracted_links)
        # attachments 필드에는 이미지도 포함하되 최소 키만 유지
        entry = _attachment_minimal(info, fname)
        attachment_entries.append(entry)
    attachment_entries = dedupe_by_key(attachment_entries, ("title", "download_url", "saved_path"))
    if attachment_entries:
        doc["attachments"] = attachment_entries
        stats["attachments_added"] = len(attachment_entries)
        stats["image_attachments_added"] = sum(1 for a in attachment_entries if is_image_file(str(a.get("title", ""))))

    return doc, stats


# =============================================================================
# IO
# =============================================================================

def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _count_field(docs: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for d in docs:
        counts[str(d.get(field, ""))] += 1
    return dict(sorted(counts.items()))


def _merge_stats(dst: defaultdict[str, int], stats: dict[str, Any]) -> None:
    for k, v in stats.items():
        if isinstance(v, int):
            dst[k] += v
        elif isinstance(v, list):
            dst[k] += len(v)


# =============================================================================
# Main
# =============================================================================

def post_process_tree_hybrid() -> None:
    print(f"[post_process] Loading metadata from {METADATA_DIR}")
    meta_index = load_all_metadata(METADATA_DIR)
    print(f"  metadata pages: {len(meta_index)}")
    print(f"[post_process] Description dir: {DESC_DIR}")

    # ── parent_store ──────────────────────────────────────────────────────────
    print(f"\n[post_process] Processing parent_store: {PARENT_STORE_JSONL}")
    parents = read_jsonl(PARENT_STORE_JSONL)
    print(f"  total parents: {len(parents)}")

    new_parents: list[dict[str, Any]] = []
    parent_stats: defaultdict[str, int] = defaultdict(int)
    parent_ids: set[str] = set()

    for doc in parents:
        parent_ids.add(str(doc.get("id", "")))
        pid = str(doc.get("page_id", ""))
        meta = meta_index.get(pid)
        desc_index = load_page_descriptions(pid)

        enriched, stats = enrich_doc(doc, meta, desc_index)
        new_parents.append(enriched)
        _merge_stats(parent_stats, stats)

    write_jsonl(PARENT_STORE_JSONL, new_parents)
    print(f"  saved {len(new_parents)} parents")
    print(f"  stats: {dict(parent_stats)}")

    # ── vector_payload ────────────────────────────────────────────────────────
    print(f"\n[post_process] Processing vector_payload: {VECTOR_PAYLOAD_JSONL}")
    payload = read_jsonl(VECTOR_PAYLOAD_JSONL)
    print(f"  total payload: {len(payload)}")

    new_payload: list[dict[str, Any]] = []
    payload_stats: defaultdict[str, int] = defaultdict(int)
    orphan_children: list[dict[str, Any]] = []

    for doc in payload:
        pid = str(doc.get("page_id", ""))
        meta = meta_index.get(pid)
        desc_index = load_page_descriptions(pid)

        enriched, stats = enrich_doc(doc, meta, desc_index)

        if enriched.get("answer_expand_parent"):
            parent_doc_id = str(enriched.get("parent_doc_id", "")).strip()
            if parent_doc_id and parent_doc_id not in parent_ids:
                orphan_children.append({
                    "id": enriched.get("id"),
                    "page_id": pid,
                    "parent_doc_id": parent_doc_id,
                    "chunk_type": enriched.get("chunk_type"),
                })

        new_payload.append(enriched)
        _merge_stats(payload_stats, stats)

    write_jsonl(VECTOR_PAYLOAD_JSONL, new_payload)
    print(f"  saved {len(new_payload)} payload items")
    print(f"  stats: {dict(payload_stats)}")

    if orphan_children:
        print(f"  WARNING: orphan children (parent_doc_id not found): {len(orphan_children)}")
    else:
        print("  all parent_doc_ids valid")

    report = {
        "parent_store": {
            "total": len(new_parents),
            "source_kind_counts": _count_field(new_parents, "source_kind"),
            "enrichment_stats": dict(parent_stats),
            "lexical_boost_policy": "removed_from_parent_store",
        },
        "vector_payload": {
            "total": len(new_payload),
            "source_kind_counts": _count_field(new_payload, "source_kind"),
            "chunk_type_counts": _count_field(new_payload, "chunk_type"),
            "enrichment_stats": dict(payload_stats),
            "orphan_children_count": len(orphan_children),
            "orphan_children_sample": orphan_children[:10],
            "lexical_boost_policy": "child_vector_payload_only",
        },
    }
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[post_process] Report saved: {REPORT_JSON}")
    print("Done")


if __name__ == "__main__":
    post_process_tree_hybrid()
