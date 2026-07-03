"""
Wiki RAG builder — metadata + attachments → vector_payload + parent_store.

원칙: chunk text는 원문 substring. parent/child 경계·메타는 LLM 1회, 실패 시 rule fallback.
HTML: body.storage 우선·markdownify 변환. GFM 표는 [표 시작/끝] 마커로 감쌈.
metadata content.urls·attachments에 있는 항목만 본문 [앵커 | URL] / 첨부 마커로 enrich.
LLM 실패 시 VectorDB/rag_tree_registry/llm_debug/{page_id}/ 에 입력·응답·apply 진단 저장 (RAG_LLM_DEBUG=1).
경로 메타: title_path 단일 필드.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag
from dotenv import load_dotenv
from openai import OpenAI
from tqdm import tqdm
import logging
logger = logging.getLogger(__name__)

from parsers.confluence_html import html_to_markdown, wrap_markdown_tables

try:
    from parsers.base import get_parser
except Exception:
    get_parser = None

load_dotenv()

import scripts._bootstrap  # noqa: F401

# =============================================================================
# Config
# =============================================================================


from app.core.config import (
    PROJECT_ROOT,
    DATA_DIR,
    METADATA_DIR,
    POLICY_PAGE_DIR,
    ATTACHMENTS_DIR,
    ADDED_ATTACHMENTS_DIR,
    DESC_DIR,
    TREE_REGISTRY_DIR,
    page_title_excluded
)

ATTACHMENT_DIRS = [ATTACHMENTS_DIR, ADDED_ATTACHMENTS_DIR]
OUT_DIR = TREE_REGISTRY_DIR

VECTOR_JSONL = OUT_DIR / "rag_tree_hybrid_vector_payload.jsonl"
PARENT_JSONL = OUT_DIR / "rag_tree_hybrid_parent_store.jsonl"
CHECKPOINT_JSON = OUT_DIR / "rag_tree_hybrid_registry_checkpoint.json"
OPENAI_CACHE_JSON = OUT_DIR / "openai_cache.json"
LLM_DEBUG_DIR = OUT_DIR / "llm_debug"

BASE_URL = os.getenv("CONFLUENCE_BASE_URL", "https://connecteve-prod.atlassian.net/wiki")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_REGISTRY_MODEL", "gpt-5-mini")
OPENAI_ENABLED = os.getenv("OPENAI_ENABLED", "true").lower() == "true"
LLM_INPUT_MAX = int(os.getenv("OPENAI_PAGE_FULL_TEXT_MAX_CHARS", "15000"))
LLM_MAX_OUTPUT = int(os.getenv("OPENAI_UNIFIED_PAGE_CHUNK_MAX_OUTPUT_TOKENS", "60000"))
RAG_LLM_DEBUG = os.getenv("RAG_LLM_DEBUG", "1").lower() in ("1", "true", "yes")
RAG_LLM_DEBUG_ALWAYS = os.getenv("RAG_LLM_DEBUG_ALWAYS", "0").lower() in ("1", "true", "yes")
LLM_DEBUG_PREVIEW_CHARS = int(os.getenv("RAG_LLM_DEBUG_PREVIEW_CHARS", "2500"))

PARENT_TARGET = int(os.getenv("PARENT_SECTION_TARGET_MAX_CHARS", "1000")) 
PARENT_MAX = int(os.getenv("MAX_PARENT_SECTION_CHARS", "1500"))           
PARENT_MIN = int(os.getenv("PARENT_MIN_CHARS", "300"))
PARENT_OVERLAP = int(os.getenv("PARENT_SECTION_CHUNK_OVERLAP", "150"))    

CHILD_TARGET = int(os.getenv("TARGET_CHILD_EVIDENCE_CHARS", "450"))
CHILD_MIN = int(os.getenv("CHILD_MIN_CHARS", "200"))
CHILD_MAX = int(os.getenv("CHILD_MAX_CHARS", "700"))
CHILD_DEDUPE_RATIO = float(os.getenv("CHILD_DEDUPE_CONTAIN_RATIO", "0.92"))

ATTACHMENT_CHUNK_SIZE = int(os.getenv("ATTACHMENT_BODY_CHUNK_SIZE", "2000"))
ATTACHMENT_CHUNK_OVERLAP = int(os.getenv("ATTACHMENT_BODY_CHUNK_OVERLAP", "200"))

# RAG 첨부 파이프라인 전체 스킵 (파싱·청킹·registry 미생성)
ATTACHMENT_SKIP_BASENAMES = frozenset({
    "생일24_전체 상품리스트(이미지링크포함).xlsx",
})

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg", ".heic", ".tif", ".tiff"}
DOC_EXTS = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".hwp", ".hwpx", ".txt", ".md", ".csv"}

TABLE_OPEN, TABLE_CLOSE = "[표 시작]", "[표 끝]"
TABLE_BLOCK_RE = re.compile(r"\[표 시작\][\s\S]*?\[표 끝\]", re.MULTILINE)
REF_ATTACHMENT_RE = re.compile(
    r"\[(?:이미지|첨부|첨부참조)[^:\]]*:\s*([^\]]+)\]",
    re.IGNORECASE,
)


WIKI_SEP = " > "

# =============================================================================
# Utils
# =============================================================================


def now_iso() -> str:
    """UTC ISO 타임스탬프."""
    return datetime.now(timezone.utc).isoformat()


def chash(text: str) -> str:
    """정규화 텍스트 SHA256."""
    return hashlib.sha256(normalize_text(text).encode()).hexdigest()


def short_hash(text: str, n: int = 10) -> str:
    """짧은 content hash."""
    return chash(text)[:n]


def slugify(text: str, n: int = 90) -> str:
    """section_id용 slug."""
    s = re.sub(r"[^\w가-힣]+", "_", (text or "").strip(), flags=re.UNICODE).strip("_")
    return (s or "item")[:n]


def normalize_text(text: str) -> str:
    """공백·줄바꿈 정규화."""
    if not text:
        return ""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def safe_join(*parts: str) -> str:
    """비어 있지 않은 문자열을 \\n\\n으로 연결."""
    return "\n\n".join(p.strip() for p in parts if p and str(p).strip())


def clean_title(v: Any) -> str:
    """제목/라벨 정리."""
    return normalize_text(str(v or ""))


def page_url(page_id: str) -> str:
    """Confluence 페이지 URL."""
    return f"{BASE_URL}/spaces/CW/pages/{page_id}" if page_id else ""


def read_json(path: Path) -> Any:
    """JSON 파일 읽기."""
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    """JSON 파일 쓰기."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """JSONL 한 줄씩 저장."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def is_image_file(name: str) -> bool:
    """이미지 확장자 여부."""
    return Path(str(name or "").split("?", 1)[0]).suffix.lower() in IMAGE_EXTS


def is_doc_file(name: str) -> bool:
    """문서형 확장자 여부."""
    return Path(str(name or "").split("?", 1)[0]).suffix.lower() in DOC_EXTS


_WS_COLLAPSE_RE = re.compile(r"\s+")


def _ws_collapse(text: str) -> str:
    """줄바꿈·공백 차이 무시 비교용."""
    return _WS_COLLAPSE_RE.sub(" ", text).strip()


def find_span_range(parent_text: str, candidate: str) -> tuple[int, int] | None:
    """parent_text에서 candidate 구간 (start, end) — 공백·줄바꿈 차이 허용."""
    if not parent_text or not candidate:
        return None
    cand = normalize_text(candidate)
    if not cand:
        return None
    idx = parent_text.find(cand)
    if idx >= 0:
        return idx, idx + len(cand)
    cand_compact = _ws_collapse(cand)
    if cand_compact:
        parent_compact = _ws_collapse(parent_text)
        cidx = parent_compact.find(cand_compact)
        if cidx >= 0:
            anchor_len = min(40, len(cand_compact))
            anchor = cand_compact[:anchor_len]
            tail_len = min(40, len(cand_compact))
            tail = cand_compact[-tail_len:]
            for head in (cand[:60].strip(), anchor):
                if not head:
                    continue
                hidx = parent_text.find(head)
                if hidx >= 0:
                    tidx = parent_text.rfind(tail, hidx)
                    if tidx > hidx:
                        return hidx, tidx + len(tail)
                    end = min(len(parent_text), hidx + len(cand) + 200)
                    return hidx, end
    anchor = cand[:80].strip()
    if anchor:
        hidx = parent_text.find(anchor)
        if hidx >= 0:
            end = min(len(parent_text), hidx + len(cand))
            return hidx, end
    return None


def find_substring(haystack: str, needle: str) -> Optional[str]:
    """needle에 대응하는 haystack 원문 slice (공백·줄바꿈 차이 허용)."""
    haystack = normalize_text(haystack)
    span = find_span_range(haystack, needle)
    if not span:
        return None
    start, end = span
    return haystack[start:end].strip()


# =============================================================================
# Page meta (title_path only)
# =============================================================================


@dataclass
class PageMeta:
    """페이지 식별·경로 — title_path만 계층 표현."""
    page_id: str
    page_title: str
    page_url: str
    title_path: str

    def as_dict(self) -> dict[str, str]:
        return {
            "page_id": self.page_id,
            "page_title": self.page_title,
            "page_url": self.page_url,
            "title_path": self.title_path,
        }


def build_page_meta(page_id: str, page_title: str, ancestors: list[str]) -> PageMeta:
    """ancestor + page_title → title_path."""
    titles = []
    for t in ancestors:
        t = clean_title(t)
        if t and (not titles or titles[-1] != t):
            titles.append(t)
    pt = clean_title(page_title)
    if pt and (not titles or titles[-1] != pt):
        titles.append(pt)
    if not titles:
        titles = [page_id]
    return PageMeta(
        page_id=page_id,
        page_title=pt or page_id,
        page_url=page_url(page_id),
        title_path=WIKI_SEP.join(titles),
    )


def extract_ancestors(raw: dict[str, Any]) -> list[str]:
    """metadata JSON에서 ancestor title 목록."""
    page = raw.get("page") if isinstance(raw.get("page"), dict) else {}
    cands = page.get("ancestors") or raw.get("ancestors") or []
    out: list[str] = []
    if isinstance(cands, list):
        for a in cands:
            if isinstance(a, dict):
                t = clean_title(a.get("title") or a.get("name"))
            else:
                t = clean_title(a)
            if t:
                out.append(t)
    return out


@dataclass
class PolicyPageRecord:
    """PolicyPage JSON 1건 (트리 + HTML)."""
    ancestors: list[str]
    body_view_html: str


def load_policy_page_index() -> dict[str, PolicyPageRecord]:
    """PolicyPage JSON → page_id별 ancestor·body_view_html."""
    index: dict[str, PolicyPageRecord] = {}
    if not POLICY_PAGE_DIR.is_dir():
        return index
    for path in sorted(POLICY_PAGE_DIR.glob("*.json")):
        try:
            raw = read_json(path)
            page_obj = raw.get("page") if isinstance(raw.get("page"), dict) else {}
            pid = str(page_obj.get("id") or raw.get("page_id") or "")
            if not pid:
                m = re.search(r"(\d{5,})", path.stem)
                pid = m.group(1) if m else ""
            if not pid:
                continue
            ancestors: list[str] = []
            for anc in page_obj.get("ancestors") or raw.get("ancestors") or []:
                t = clean_title(anc.get("title") or anc.get("name")) if isinstance(anc, dict) else clean_title(anc)
                if t:
                    ancestors.append(t)
            html = ""
            v = page_obj.get("body_view_html")
            if isinstance(v, str) and v.strip() and "<" in v:
                html = v
            index[pid] = PolicyPageRecord(ancestors=ancestors, body_view_html=html)
        except Exception:
            continue
    return index


def extract_page_id(raw: dict[str, Any], path: Path) -> str:
    """metadata 파일에서 page_id."""
    page = raw.get("page") if isinstance(raw.get("page"), dict) else {}
    for key in ("id", "page_id"):
        val = page.get(key) or raw.get(key)
        if val:
            return str(val)
    m = re.search(r"(\d{5,})", path.stem)
    return m.group(1) if m else path.stem


def extract_body_html(raw: dict[str, Any]) -> str:
    """metadata 내 HTML 본문 (PolicyPage HTML 없을 때만 보조)."""
    content = raw.get("content") if isinstance(raw.get("content"), dict) else {}
    page = raw.get("page") if isinstance(raw.get("page"), dict) else {}
    for key in (
        "body_storage_html", "body_view_html", "body_export_view_html",
        "body_html",
    ):
        for src in (content, page, raw):
            v = src.get(key) if isinstance(src, dict) else None
            if isinstance(v, str) and v.strip() and "<" in v:
                return v
    body = raw.get("body")
    if isinstance(body, str) and body.strip() and "<" in body:
        return body
    return ""


def strip_html_to_text(html: str) -> str:
    """HTML → 마크다운 (body_text 후보가 HTML일 때)."""
    if not html.strip():
        return ""
    return normalize_text(wrap_markdown_tables(html_to_markdown(html)))


def extract_body_text(raw: dict[str, Any]) -> str:
    """metadata content.body_text 등 평문 본문."""
    content = raw.get("content") if isinstance(raw.get("content"), dict) else {}
    page = raw.get("page") if isinstance(raw.get("page"), dict) else {}
    for key in ("body_text",):
        for src in (content, page, raw):
            v = src.get(key) if isinstance(src, dict) else None
            if isinstance(v, str) and v.strip():
                return strip_html_to_text(v) if "<" in v else normalize_text(v)
    return ""


def resolve_page_body_html(
    page_id: str,
    policy: PolicyPageRecord | None,
    meta_html: str,
) -> tuple[str, str]:
    """본문 HTML: PolicyPage body_view_html 우선, metadata는 body.storage 우선."""
    if policy and policy.body_view_html.strip():
        return policy.body_view_html, "policy_body_view_html"
    if meta_html.strip():
        return meta_html, "metadata_html"
    return "", "empty"


def resolve_attachment_path(page_id: str, att: dict[str, Any]) -> Optional[Path]:
    """첨부 메타·디스크 경로 해석."""
    title = str(att.get("title") or att.get("filename") or "")
    saved = str(att.get("saved_path") or att.get("path") or att.get("local_path") or "")
    cands: list[Path] = []
    if saved:
        cands += [Path(saved), PROJECT_ROOT / saved, DATA_DIR / saved]
        tail = saved.replace("\\", "/")
        for pre in ("Data/attachments/", "Data/added_attachments/"):
            if tail.startswith(pre):
                tail = tail[len(pre):]
        for root in ATTACHMENT_DIRS:
            cands.append(root / tail)
            cands.append(root / page_id / Path(tail).name)
    if title:
        for root in ATTACHMENT_DIRS:
            cands.append(root / page_id / title)
            pdir = root / page_id
            if pdir.is_dir():
                cands.extend(pdir.rglob(title))
    for p in cands:
        try:
            if p.is_file():
                return p.resolve()
        except Exception:
            pass
    return None


def merge_disk_attachments(page_id: str, metadata_atts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """metadata 첨부 + attachments/{page_id}/ 디스크 파일 병합."""
    merged: dict[str, dict[str, Any]] = {}
    for att in metadata_atts:
        title = clean_title(att.get("title") or att.get("filename") or "")
        if title:
            merged[Path(title).name.lower()] = dict(att)
    for root in ATTACHMENT_DIRS:
        pdir = root / page_id
        if not pdir.is_dir():
            continue
        for fp in sorted(pdir.iterdir()):
            if not fp.is_file():
                continue
            k = fp.name.lower()
            row = merged.get(k) or {"title": fp.name}
            row["title"] = fp.name
            row["local_path"] = str(fp)
            merged[k] = row
    return list(merged.values())


@dataclass
class WikiPage:
    page_id: str
    page_title: str
    meta: PageMeta
    body_html: str
    body_text: str
    body_html_source: str
    attachments: list[dict[str, Any]]
    meta_urls: list[dict[str, str]] = field(default_factory=list)
    meta_attachment_titles: list[str] = field(default_factory=list)


def load_pages() -> list[WikiPage]:
    """METADATA_DIR *.metadata.json → WikiPage 목록."""
    if not METADATA_DIR.is_dir():
        raise FileNotFoundError(f"METADATA_DIR not found: {METADATA_DIR}")
    policy_index = load_policy_page_index()
    pages: list[WikiPage] = []
    for path in sorted(METADATA_DIR.glob("*.metadata.json")):
        try:
            raw = read_json(path)
            pid = extract_page_id(raw, path)
            page_obj = raw.get("page") if isinstance(raw.get("page"), dict) else {}
            title = clean_title(page_obj.get("title") or raw.get("title") or pid)
            if page_title_excluded(title):
                logger.info("RAG 제외(페이지 제목): %s (%s)", title, pid)
                continue
            ancestors = extract_ancestors(raw)
            policy = policy_index.get(pid)
            if not ancestors and policy and policy.ancestors:
                ancestors = policy.ancestors
            body_html, html_src = resolve_page_body_html(pid, policy, extract_body_html(raw))
            atts = merge_disk_attachments(pid, list(raw.get("attachments") or []))
            for att in atts:
                if not att.get("local_path"):
                    lp = resolve_attachment_path(pid, att)
                    if lp:
                        att["local_path"] = str(lp)
            pages.append(WikiPage(
                page_id=pid,
                page_title=title,
                meta=build_page_meta(pid, title, ancestors),
                body_html=body_html,
                body_text=extract_body_text(raw),
                body_html_source=html_src,
                attachments=atts,
                meta_urls=extract_meta_urls(raw),
                meta_attachment_titles=extract_meta_attachment_titles(raw),
            ))
        except Exception as e:
            print(f"⚠️ skip {path.name}: {e}")
    return pages


# =============================================================================
# HTML → plain (table marker only)
# =============================================================================


def load_attachment_descriptions(page_id: str) -> dict[str, str]:
    """attachment_descriptions/result_{page_id}.json → 파일명→설명."""
    path = DESC_DIR / f"result_{page_id}.json"
    if not path.exists():
        return {}
    try:
        data = read_json(path)
    except Exception:
        return {}
    items = data if isinstance(data, list) else []
    if isinstance(data, dict):
        for k in ("items", "descriptions", "attachments", "results"):
            if isinstance(data.get(k), list):
                items = data[k]
                break
    out: dict[str, str] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        name = Path(clean_title(it.get("title") or it.get("filename") or it.get("name") or "")).name
        desc = normalize_text(it.get("description") or it.get("summary") or it.get("text") or "")
        if name:
            out[name] = desc
            out[name.lower()] = desc
    return out


def _img_filename(img: Tag) -> str:
    """img 태그에서 파일명 추출."""
    for key in ("data-linked-resource-default-alias", "data-filename", "alt", "title"):
        v = str(img.get(key) or "").strip()
        if v:
            return Path(unquote(v)).name
    src = str(img.get("src") or "")
    if src:
        return Path(unquote(urlparse(src).path)).name
    return ""


def _lookup_desc(desc_index: dict[str, str], fname: str) -> str:
    """파일명 대소문자 무시 description 조회."""
    if not fname:
        return ""
    return desc_index.get(fname) or desc_index.get(fname.lower()) or ""


def _attachment_marker(fname: str, desc_index: dict[str, str]) -> str:
    """파일명·description → 본문 마커 문자열."""
    desc = _lookup_desc(desc_index, fname)
    if fname and is_doc_file(fname):
        return f"[첨부: {fname} | {desc}]" if desc else f"[첨부: {fname}]"
    if fname:
        return f"[{fname} | {desc}]" if desc else f"[{fname}]"
    return "[image]"


def _replace_images(soup: BeautifulSoup, desc_index: dict[str, str]) -> None:
    """img·embedded-file → [파일명 | 설명] 또는 [첨부: 파일명 | 설명]."""
    for img in list(soup.find_all("img")):
        fname = _img_filename(img)
        img.replace_with(NavigableString(_attachment_marker(fname, desc_index)))
    for node in list(soup.find_all(attrs={"data-linked-resource-default-alias": True})):
        alias = str(node.get("data-linked-resource-default-alias") or "").strip()
        fname = Path(unquote(alias)).name if alias else ""
        if fname:
            node.replace_with(NavigableString(_attachment_marker(fname, desc_index)))


def html_to_marked_text(html: str, desc_index: dict[str, str]) -> str:
    """HTML→마크다운(markdownify). 이미지·첨부는 마커로 치환 후 표는 [표 시작/끝] 감쌈."""
    if not html.strip():
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    _replace_images(soup, desc_index)
    inner = soup.body.decode_contents() if soup.body else str(soup)
    md = html_to_markdown(inner, already_cleaned=True)
    return normalize_text(wrap_markdown_tables(md))


def enrich_plain_attachment_refs(text: str, page_id: str) -> str:
    """평문 [이미지: file.pdf] → description 있으면 [첨부: file | desc]."""
    desc_index = load_attachment_descriptions(page_id)

    def repl(m: re.Match[str]) -> str:
        raw = m.group(1).strip()
        fname = Path(raw.split("|")[0].strip()).name
        if not fname:
            return m.group(0)
        return _attachment_marker(fname, desc_index)

    return REF_ATTACHMENT_RE.sub(repl, text or "")


def extract_meta_urls(raw: dict[str, Any]) -> list[dict[str, str]]:
    """metadata content.urls → [{text, url}]."""
    content = raw.get("content") if isinstance(raw.get("content"), dict) else {}
    raw_urls = content.get("urls") or raw.get("urls") or raw.get("links") or []
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for u in raw_urls:
        if not isinstance(u, dict):
            continue
        text = str(u.get("text") or u.get("title") or u.get("label") or "").strip()
        url = str(u.get("url") or u.get("href") or "").strip()
        if not text and not url:
            continue
        key = (text, url)
        if key in seen:
            continue
        seen.add(key)
        out.append({"text": text, "url": url})
    return out


def extract_meta_attachment_titles(raw: dict[str, Any]) -> list[str]:
    """metadata attachments·extracted_links → 본문 enrich용 파일명 목록."""
    seen: set[str] = set()
    out: list[str] = []

    def add(name: str) -> None:
        fname = Path(unquote(str(name or "").strip())).name
        if not fname:
            return
        key = fname.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(fname)

    for att in raw.get("attachments") or []:
        if isinstance(att, dict):
            add(str(att.get("title") or att.get("filename") or att.get("file_name") or ""))
    for el in raw.get("extracted_links") or []:
        if isinstance(el, dict):
            add(str(el.get("filename") or el.get("title") or ""))
    return out


def _metadata_link_marker(anchor: str, url: str) -> str:
    """post-process inject가 인식하는 [앵커 | URL] 형태."""
    anchor = clean_title(anchor)
    url = str(url or "").strip()
    if url.startswith(("http://", "https://")):
        if anchor and anchor != url and not anchor.startswith(("http://", "https://")):
            return f"[{anchor} | {url}]"
        return f"[{url}]"
    if anchor:
        return f"[{anchor}]"
    return ""


def _already_link_marked(text: str, anchor: str, url: str) -> bool:
    if anchor and (f"[{anchor}]" in text or f"[{anchor} |" in text):
        return True
    if url and url in text:
        marker = _metadata_link_marker(anchor, url)
        if marker and marker in text:
            return True
    return False


def enrich_metadata_links(text: str, meta_urls: list[dict[str, str]]) -> tuple[str, int]:
    """metadata urls — 본문에 URL이 있고 [링크:…]가 아닐 때만 [앵커 | URL]."""
    if not text or not meta_urls:
        return text, 0

    body = text
    entries: list[tuple[str, str]] = []
    for u in meta_urls:
        anchor = str(u.get("text") or "").strip()
        url = str(u.get("url") or "").strip()
        if anchor == url or anchor.startswith(("http://", "https://")):
            anchor = ""
        if not url:
            continue
        if url not in body and not (anchor and anchor in body):
            continue
        entries.append((anchor, url))

    entries.sort(key=lambda e: len(e[0] or e[1]), reverse=True)
    out = body
    replaced = 0
    for anchor, url in entries:
        marker = _metadata_link_marker(anchor, url)
        if not marker or _already_link_marked(out, anchor, url):
            continue
        if anchor and anchor in out:
            out = out.replace(anchor, marker, 1)
            replaced += 1
            continue
        if url in out:
            idx = out.find(url)
            if idx >= 0:
                out = out[:idx] + marker + out[idx + len(url) :]
                replaced += 1
    return out, replaced


def _already_attachment_marked(text: str, fname: str) -> bool:
    if not fname:
        return True
    if f"[첨부: {fname}" in text or f"[{fname} |" in text or f"[{fname}]" in text:
        return True
    return False


def enrich_metadata_attachment_titles(
    text: str,
    titles: list[str],
    page_id: str,
    link_anchors: list[str] | None = None,
) -> tuple[str, int]:
    """metadata 첨부 파일명이 본문 평문으로 있으면 첨부 마커로 치환 (URL 링크 앵커 제외)."""
    if not text or not titles:
        return text, 0

    skip = {clean_title(a).lower() for a in (link_anchors or []) if clean_title(a)}
    desc_index = load_attachment_descriptions(page_id)
    ordered = sorted({Path(t).name for t in titles if t}, key=len, reverse=True)
    out = text
    replaced = 0
    for fname in ordered:
        if not fname or fname.lower() in skip:
            continue
        if not fname or fname not in out or _already_attachment_marked(out, fname):
            continue
        if re.search(rf"\[{re.escape(fname)}[^\]]*\|\s*https?://", out, re.IGNORECASE):
            continue
        marker = _attachment_marker(fname, desc_index)
        out = out.replace(fname, marker, 1)
        replaced += 1
    return out, replaced


def build_page_full_text(page: WikiPage) -> tuple[str, dict[str, Any]]:
    """청킹 입력 본문: PolicyPage HTML(marked) 우선, 없으면 metadata body_text."""
    audit: dict[str, Any] = {
        "body_html_source": page.body_html_source,
        "html_chars": len(page.body_html or ""),
        "plain_chars": len(page.body_text or ""),
    }
    text = ""
    if page.body_html.strip():
        text = html_to_marked_text(page.body_html, load_attachment_descriptions(page.page_id))
        audit["body_source"] = page.body_html_source
    elif page.body_text.strip():
        text = enrich_plain_attachment_refs(page.body_text, page.page_id)
        audit["body_source"] = "metadata_body_text"
    else:
        audit["body_source"] = "empty"
        audit["full_text_chars"] = 0
        return "", audit

    text, n_links = enrich_metadata_links(text, page.meta_urls)
    audit["metadata_links_enriched"] = n_links
    link_anchors = [str(u.get("text") or "") for u in page.meta_urls if u.get("text")]
    text, n_atts = enrich_metadata_attachment_titles(
        text, page.meta_attachment_titles, page.page_id, link_anchors=link_anchors,
    )
    audit["metadata_attachments_enriched"] = n_atts
    text = normalize_text(text)
    audit["full_text_chars"] = len(text)
    return text, audit


def extract_table_hints(text: str) -> list[dict[str, Any]]:
    """LLM 힌트용 table 블록 목록 (목록 마커 없음)."""
    hints = []
    for i, m in enumerate(TABLE_BLOCK_RE.finditer(text), 1):
        block = m.group(0).strip()
        hints.append({"index": i, "kind": "table", "char_count": len(block), "text": block[:2000]})
    return hints


# =============================================================================
# Attachments
# =============================================================================


@dataclass
class AttachmentInput:
    title: str
    parsed_text: str
    extension: str = ""


def filenames_from_body(text: str) -> list[str]:
    """본문 [첨부:|이미지: ...] 에서 파일명 수집."""
    seen: set[str] = set()
    names: list[str] = []
    for m in REF_ATTACHMENT_RE.finditer(text or ""):
        raw = m.group(1).strip()
        name = raw.split("|")[0].strip() if "|" in raw else Path(raw).name
        name = Path(name).name
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return names


def parse_attachment_file(path: Path) -> tuple[str, str, float]:
    """로컬 문서 파싱 (get_parser는 확장자만 전달). 전체 텍스트 반환 — 절단 없음."""
    if get_parser is None:
        return "", "parser_unavailable", 0.0
    try:
        parser = get_parser(path.suffix.lower())
        if parser is None:
            return "", "parser_not_found", 0.0
        parsed = parser.parse(path)
        text = normalize_text(getattr(parsed, "text", "") or "")
        return text, str(getattr(parsed, "parse_method", "")), float(getattr(parsed, "confidence", 0.7) or 0.7)
    except Exception as e:
        return "", f"parse_failed:{type(e).__name__}", 0.0


def attachment_should_skip(title: str) -> bool:
    """True면 collect_attachment_inputs·청킹 대상에서 제외."""
    name = Path(clean_title(title)).name.lower()
    return name in {n.lower() for n in ATTACHMENT_SKIP_BASENAMES}


def collect_attachment_inputs(page: WikiPage, body_marked: str) -> list[AttachmentInput]:
    """문서형 첨부 파싱 (메타 + 본문 참조 + 디스크)."""
    out: list[AttachmentInput] = []
    seen: set[str] = set()

    def add(title: str, path: Optional[Path]) -> None:
        title = clean_title(title)
        if attachment_should_skip(title):
            return
        if not title or is_image_file(title):
            return
        key = Path(title).name.lower()
        if key in seen:
            return
        if not (Path(title).suffix.lower() in DOC_EXTS or (path and is_doc_file(str(path)))):
            return
        text = ""
        if path:
            text, _, _ = parse_attachment_file(path)
        if text:
            seen.add(key)
            out.append(AttachmentInput(title=title, parsed_text=text, extension=Path(title).suffix.lower()))

    for att in page.attachments:
        add(clean_title(att.get("title") or att.get("filename") or ""), resolve_attachment_path(page.page_id, att))
    for fname in filenames_from_body(body_marked):
        if fname.lower() in seen:
            continue
        for root in ATTACHMENT_DIRS:
            direct = root / page.page_id / fname
            if direct.is_file():
                add(fname, direct)
                break
            pdir = root / page.page_id
            if pdir.is_dir():
                for fp in pdir.rglob(fname):
                    if fp.is_file():
                        add(fname, fp)
                        break
    return out


# =============================================================================
# Rule fallback chunking (no heading peel — LLM이 의미 경계 담당)
# =============================================================================


@dataclass(frozen=True)
class TextChunkPolicy:
    target: int
    hard: int
    overlap: int = 0


PARENT_POLICY = TextChunkPolicy(PARENT_TARGET, PARENT_MAX, PARENT_OVERLAP)
CHILD_POLICY = TextChunkPolicy(CHILD_TARGET, CHILD_MAX, 0)


def _split_by_policy(text: str, policy: TextChunkPolicy) -> list[str]:
    """단락/길이 기준 분할. 표 블록은 분할하지 않고 한 덩어리 유지."""
    text = normalize_text(text)
    if not text:
        return []
    if "[표 시작]" not in text:
        return _split_plain(text, policy)
    units: list[str] = []
    pos = 0
    for m in TABLE_BLOCK_RE.finditer(text):
        before = text[pos : m.start()].strip()
        if before:
            units.extend(_split_plain(before, policy))
        units.append(m.group(0).strip())
        pos = m.end()
    tail = text[pos:].strip()
    if tail:
        units.extend(_split_plain(tail, policy))
    return [u for u in units if u]


def _split_plain(text: str, policy: TextChunkPolicy) -> list[str]:
    """표 외 일반 텍스트 길이 분할."""
    text = normalize_text(text)
    if not text:
        return []
    if len(text) <= policy.hard:
        return [text]
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0

    def flush() -> None:
        nonlocal buf, buf_len
        if buf:
            chunks.append("\n\n".join(buf))
        buf, buf_len = [], 0

    for p in paras:
        if buf and buf_len + len(p) + 2 > policy.target:
            flush()
        if len(p) > policy.hard:
            flush()
            start = 0
            while start < len(p):
                end = min(len(p), start + policy.target)
                if end < len(p):
                    for sep in ("\n\n", "\n", " "):
                        cut = p.rfind(sep, start, end)
                        if cut > start:
                            end = cut
                            break
                part = p[start:end].strip()
                if part:
                    chunks.append(part)
                if end >= len(p):
                    break
                start = max(end - policy.overlap, start + 1) if policy.overlap else end
            continue
        buf.append(p)
        buf_len += len(p) + 2
    flush()
    return chunks or [text]


def _overlap_ratio(a: str, b: str) -> float:
    """child 중복 판정용."""
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return 0.0
    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    if short in long:
        return len(short) / max(len(long), 1)
    ta, tb = set(short.split()), set(long.split())
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def dedupe_children(children: list[dict[str, Any]], parent_text: str = "") -> list[dict[str, Any]]:
    """포함·유사도 기반 child 중복 제거 + parent span 비겹침."""
    out: list[dict[str, Any]] = []
    for c in children:
        ct = normalize_text(c.get("text") or "")
        if not ct or any(_overlap_ratio(ct, normalize_text(k.get("text") or "")) >= CHILD_DEDUPE_RATIO for k in out):
            continue
        pruned = [
            e for e in out
            if not (len(ct) > len(normalize_text(e.get("text") or "")) and normalize_text(e.get("text") or "") in ct)
        ]
        out = pruned
        if not any(_overlap_ratio(ct, normalize_text(k.get("text") or "")) >= CHILD_DEDUPE_RATIO for k in out):
            out.append(c)
    if not parent_text.strip():
        return out
    located: list[tuple[int, int, dict[str, Any]]] = []
    for c in out:
        t = c.get("text") or ""
        idx = parent_text.find(t[: min(80, len(t))]) if t else -1
        located.append((idx if idx >= 0 else 10**9, idx + len(t), c))
    located.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    kept: list[dict[str, Any]] = []
    last_end = -1
    for s, e, c in located:
        if s >= 10**9:
            kept.append(c)
            continue
        if s >= last_end:
            kept.append(c)
            last_end = e
    return kept


@dataclass
class ParentChunk:
    title: str
    text: str
    section_type: str = "guide"
    source_kind: str = "page_body"
    attachment_title: str = ""
    page_summary: str = ""
    children: list[dict[str, Any]] = field(default_factory=list)


def rule_fallback_parents(full: str, page_title: str) -> list[ParentChunk]:
    """LLM 실패 시 parent size split."""
    parts = _split_by_policy(full, PARENT_POLICY)
    if not parts:
        return []
    if len(parts) == 1:
        return [ParentChunk(title=page_title, text=parts[0])]
    return [ParentChunk(title=f"{page_title} · {i}", text=t) for i, t in enumerate(parts, 1)]


def rule_fallback_children(parent: ParentChunk) -> list[dict[str, Any]]:
    """LLM 실패·child 없음 시 단락 단위 분할 (fallback 전용)."""
    return [
        {"title": parent.title, "text": sp, "keywords": [], "section_type": parent.section_type}
        for sp in _split_by_policy(parent.text, CHILD_POLICY)
        if sp.strip()
    ]


def _split_attachment_text(text: str) -> list[str]:
    """첨부 전체 텍스트 → ATTACHMENT_CHUNK_SIZE 단위 분할.
    split_page_text_for_llm과 같은 \\n\\n·\\n·표 블록 존중 방식.
    overlap은 ATTACHMENT_CHUNK_OVERLAP만큼 앞 구간 끝에서 이어붙임.
    """
    text = normalize_text(text)
    if not text:
        return []
    if len(text) <= ATTACHMENT_CHUNK_SIZE:
        return [text]

    # 표 블록 보존하며 단위 목록 생성 (split_page_text_for_llm 방식)
    units: list[str] = []
    if "[표 시작]" in text:
        pos = 0
        for m in TABLE_BLOCK_RE.finditer(text):
            before = text[pos : m.start()].strip()
            if before:
                units.extend(u for u in re.split(r"\n\s*\n", before) if u.strip())
            units.append(m.group(0).strip())
            pos = m.end()
        tail = text[pos:].strip()
        if tail:
            units.extend(u for u in re.split(r"\n\s*\n", tail) if u.strip())
    else:
        units = [u.strip() for u in re.split(r"\n\s*\n", text) if u.strip()]

    parts: list[str] = []
    buf: list[str] = []
    buf_len = 0
    joiner = "\n\n"

    def flush() -> None:
        nonlocal buf, buf_len
        if buf:
            parts.append(joiner.join(buf))
        buf.clear()
        buf_len = 0

    for unit in units:
        if len(unit) > ATTACHMENT_CHUNK_SIZE:
            flush()
            # 표 블록이면 통째로, 아니면 강제 분할
            if unit.startswith("[표 시작]"):
                parts.append(unit)
            else:
                start = 0
                while start < len(unit):
                    end = min(len(unit), start + ATTACHMENT_CHUNK_SIZE)
                    if end < len(unit):
                        for sep in ("\n\n", "\n", " "):
                            cut = unit.rfind(sep, start, end)
                            if cut > start:
                                end = cut
                                break
                    piece = unit[start:end].strip()
                    if piece:
                        parts.append(piece)
                    if end >= len(unit):
                        break
                    start = max(end - ATTACHMENT_CHUNK_OVERLAP, start + 1)
            continue
        add = len(unit) + (len(joiner) if buf else 0)
        if buf and buf_len + add > ATTACHMENT_CHUNK_SIZE:
            flush()
        buf.append(unit)
        buf_len += add
    flush()

    # overlap 적용: 다음 part 앞에 이전 part 끝 N자를 붙임
    if ATTACHMENT_CHUNK_OVERLAP <= 0 or len(parts) <= 1:
        return parts
    overlapped: list[str] = [parts[0]]
    for i in range(1, len(parts)):
        prev_tail = normalize_text(parts[i - 1])[-ATTACHMENT_CHUNK_OVERLAP:]
        # 이미 겹치는 내용이 없을 때만 붙임
        if not parts[i].startswith(prev_tail[:40]):
            overlapped.append(normalize_text(prev_tail + "\n\n" + parts[i]))
        else:
            overlapped.append(parts[i])
    return overlapped


def _rule_attachment_parents(att: AttachmentInput) -> list[ParentChunk]:
    """첨부 rule 분할(ATTACHMENT_CHUNK_SIZE + child rule)."""
    parents: list[ParentChunk] = []
    parts = _split_attachment_text(att.parsed_text)
    base = Path(att.title).name
    for i, part in enumerate(parts, 1):
        title = f"첨부: {base}" if len(parts) == 1 else f"첨부: {base} ({i}/{len(parts)})"
        p = ParentChunk(
            title=title,
            text=part,
            section_type="attachment",
            source_kind="attachment_doc",
            attachment_title=att.title,
        )
        p.children = dedupe_children(rule_fallback_children(p), p.text)
        parents.append(p)
    return parents


def _finalize_attachment_parents(
    parents: list[ParentChunk], att: AttachmentInput, *, from_llm: bool
) -> list[ParentChunk]:
    """LLM 청킹 결과에 첨부 메타·제목 접두사 적용."""
    base = Path(att.title).name
    multi = len(parents) > 1
    for i, p in enumerate(parents, 1):
        if from_llm:
            inner = clean_title(p.title)
            if multi and inner and inner != base:
                p.title = f"첨부: {base} — {inner}"
            elif multi:
                p.title = f"첨부: {base} ({i}/{len(parents)})"
            else:
                p.title = f"첨부: {base}"
        p.source_kind = "attachment_doc"
        p.attachment_title = att.title
        p.section_type = "attachment"
        for ch in p.children:
            ch["section_type"] = "attachment"
    return parents


def build_one_attachment_parents(
    page: WikiPage, llm: LLMClient, att: AttachmentInput
) -> tuple[list[ParentChunk], dict[str, Any]]:
    """첨부 1건 — LLM 우선, 실패 시 rule."""
    audit: dict[str, Any] = {
        "attachment_title": att.title,
        "canonical_chars": len(att.parsed_text),
        "used_llm": False,
        "llm_enabled": False,
        "llm_response": False,
    }
    if attachment_should_skip(att.title):
        audit["skipped"] = "attachment_skip_list"
        return [], audit
    if not att.parsed_text.strip():
        return [], audit

    full_text = att.parsed_text
    att_label = Path(att.title).name
    hints = extract_table_hints(full_text)
    llm_parts = split_page_text_for_llm(full_text, LLM_INPUT_MAX)
    audit["llm_input_parts"] = len(llm_parts)
    llm_obj: Optional[dict[str, Any]] = None
    apply_diag: Optional[dict[str, Any]] = None

    if llm.ok:
        audit["llm_enabled"] = True
        llm_obj = llm.chunk_page(page, full_text, hints, attachment_title=att_label)
        if llm_obj:
            audit["llm_response"] = True
            raw_secs = llm_obj.get("parent_sections")
            audit["llm_sections_raw"] = len(raw_secs) if isinstance(raw_secs, list) else 0
            if llm.run_debug.get("failure"):
                audit["llm_run_failure"] = llm.run_debug.get("failure")
            apply_diag = diagnose_llm_apply(page, full_text, llm_obj)
            audit["llm_sections_matched"] = apply_diag.get("sections_parent_ok", 0)
            parents = apply_llm_chunking(page, full_text, llm_obj)
            if parents:
                audit["used_llm"] = True
                audit["llm_parent_sections"] = len(parents)
                audit["llm_child_evidence"] = sum(len(p.children) for p in parents)
                parents = _finalize_attachment_parents(parents, att, from_llm=True)
                write_llm_debug_files(
                    page, full_text, audit,
                    llm=llm, llm_obj=llm_obj, apply_diag=apply_diag,
                    debug_label=f"att_{slugify(att_label)}",
                )
                return parents, audit
            audit["llm_apply_empty"] = True
        elif llm.run_debug.get("failure"):
            audit["llm_run_failure"] = llm.run_debug.get("failure")

    parents = _rule_attachment_parents(att)
    audit["fallback"] = "rule_size_split"
    write_llm_debug_files(
        page, full_text, audit,
        llm=llm, llm_obj=llm_obj, apply_diag=apply_diag,
        debug_label=f"att_{slugify(att_label)}",
    )
    return parents, audit


def build_attachment_parents(
    page: WikiPage, body_marked: str, llm: LLMClient
) -> tuple[list[ParentChunk], dict[str, Any]]:
    """파싱된 첨부 → attachment_doc parent (LLM 우선, 실패 시 rule)."""
    all_parents: list[ParentChunk] = []
    per_att: list[dict[str, Any]] = []
    for att in collect_attachment_inputs(page, body_marked):
        parents, a = build_one_attachment_parents(page, llm, att)
        all_parents.extend(parents)
        per_att.append(a)
    audit: dict[str, Any] = {
        "attachment_chunking": per_att,
        "attachment_llm_count": sum(1 for a in per_att if a.get("used_llm")),
        "attachment_rule_excluded": sum(1 for a in per_att if a.get("chunking") == "rule_excluded"),
        "attachment_rule_fallback": sum(1 for a in per_att if a.get("fallback")),
    }
    return all_parents, audit


# =============================================================================
# LLM unified chunking
# =============================================================================


def _hard_split_chars(text: str, max_chars: int) -> list[str]:
    """단위가 max_chars를 넘을 때 \\n\\n → \\n → 공백 순으로 잘라 분할."""
    text = normalize_text(text)
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    out: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            cut = -1
            for sep in ("\n\n", "\n", " "):
                pos = text.rfind(sep, start, end)
                if pos > start:
                    cut = pos
                    break
            if cut > start:
                end = cut
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        if end >= len(text):
            break
        start = end if end > start else start + 1
    return out or [text[:max_chars]]


def _llm_split_units_plain(text: str) -> list[str]:
    """\\n\\n 단락 → 필요 시 \\n 줄 단위."""
    text = normalize_text(text)
    if not text:
        return []
    units: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        lines = [ln.strip() for ln in para.split("\n") if ln.strip()]
        if len(lines) > 1 and len(para) > 400:
            units.extend(lines)
        else:
            units.append(para)
    return units


def _llm_split_units(text: str) -> list[str]:
    """표 블록은 유지, 나머지는 \\n\\n / \\n 경계로 분해."""
    text = normalize_text(text)
    if not text:
        return []
    if "[표 시작]" not in text:
        return _llm_split_units_plain(text)
    units: list[str] = []
    pos = 0
    for m in TABLE_BLOCK_RE.finditer(text):
        before = text[pos : m.start()].strip()
        if before:
            units.extend(_llm_split_units_plain(before))
        units.append(m.group(0).strip())
        pos = m.end()
    tail = text[pos:].strip()
    if tail:
        units.extend(_llm_split_units_plain(tail))
    return [u for u in units if u]


def split_page_text_for_llm(text: str, max_chars: int = LLM_INPUT_MAX) -> list[str]:
    """LLM 입력 한도 초과 시 \\n\\n·\\n·표 경계로 나눈 연속 구간 목록."""
    text = normalize_text(text)
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    units = _llm_split_units(text)
    if not units:
        return _hard_split_chars(text, max_chars)
    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0
    joiner = "\n\n"

    def flush() -> None:
        nonlocal buf, buf_len
        if buf:
            chunks.append(joiner.join(buf))
        buf, buf_len = [], 0

    for u in units:
        u = u.strip()
        if not u:
            continue
        if len(u) > max_chars:
            flush()
            chunks.extend(_hard_split_chars(u, max_chars))
            continue
        add = len(u) + (len(joiner) if buf else 0)
        if buf and buf_len + add > max_chars:
            flush()
        buf.append(u)
        buf_len += add
    flush()
    return chunks or _hard_split_chars(text, max_chars)


def merge_llm_chunk_objects(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """구간별 LLM JSON → 단일 page_summary + parent_sections."""
    if not parts:
        return {}
    if len(parts) == 1:
        return parts[0]
    summary = ""
    sections: list[Any] = []
    for obj in parts:
        if not summary:
            summary = clean_title(obj.get("page_summary") or "")
        secs = obj.get("parent_sections")
        if isinstance(secs, list):
            sections.extend(secs)
    return {"page_summary": summary, "parent_sections": sections}


def _preview(text: str, n: int = LLM_DEBUG_PREVIEW_CHARS) -> str:
    """디버그 JSON용 텍스트 미리보기."""
    t = text or ""
    if len(t) <= n:
        return t
    return t[:n] + f"\n... [{len(t) - n} chars truncated]"


def diagnose_llm_apply(page: WikiPage, full_text: str, obj: dict[str, Any]) -> dict[str, Any]:
    """LLM JSON → ParentChunk 적용 실패 원인 (parent/child별 substring 매칭)."""
    full_text = normalize_text(full_text)
    sections = obj.get("parent_sections")
    if not isinstance(sections, list):
        return {"error": "parent_sections missing or not a list", "sections": []}

    rows: list[dict[str, Any]] = []
    matched_parents = 0
    for i, sec in enumerate(sections):
        if not isinstance(sec, dict):
            rows.append({"index": i, "skip_reason": "not_a_dict"})
            continue
        raw_ptext = str(sec.get("text") or "")
        ptext = find_substring(full_text, raw_ptext)
        parent_reason = "ok"
        if not raw_ptext.strip():
            parent_reason = "empty_parent_text"
        elif not ptext:
            parent_reason = "substring_not_found"
        elif len(ptext) < PARENT_MIN:
            parent_reason = f"parent_too_short:{len(ptext)}"

        child_rows: list[dict[str, Any]] = []
        children_matched = 0
        for j, ch in enumerate(sec.get("child_evidence") or []):
            if not isinstance(ch, dict):
                child_rows.append({"index": j, "skip_reason": "not_a_dict"})
                continue
            raw_ctext = str(ch.get("text") or "")
            ctext = (
                find_substring(ptext or "", raw_ctext)
                or find_substring(full_text, raw_ctext)
            )
            reason = "ok"
            if not raw_ctext.strip():
                reason = "empty_child_text"
            elif not ctext:
                reason = "substring_not_found"
            elif len(ctext) < CHILD_MIN:
                reason = f"child_too_short:{len(ctext)}"
            else:
                children_matched += 1
            child_rows.append({
                "index": j,
                "title": ch.get("title"),
                "raw_len": len(raw_ctext),
                "matched_len": len(ctext) if ctext else 0,
                "skip_reason": reason,
                "raw_preview": _preview(raw_ctext, 500),
                "matched_preview": _preview(ctext or "", 500),
                "anchor_head": raw_ctext[:80],
                "anchor_in_parent": bool(ptext and raw_ctext[:80] in (ptext or "")),
                "anchor_in_full": raw_ctext[:80] in full_text if raw_ctext else False,
            })

        if parent_reason == "ok":
            matched_parents += 1
        rows.append({
            "index": i,
            "title": sec.get("title"),
            "section_type": sec.get("section_type"),
            "raw_parent_len": len(raw_ptext),
            "matched_parent_len": len(ptext) if ptext else 0,
            "skip_reason": parent_reason,
            "raw_preview": _preview(raw_ptext, 800),
            "matched_preview": _preview(ptext or "", 800),
            "anchor_head": raw_ptext[:80],
            "anchor_in_full": raw_ptext[:80] in full_text if raw_ptext else False,
            "children_raw": len(sec.get("child_evidence") or []),
            "children_matched": children_matched,
            "children": child_rows,
        })

    return {
        "page_id": page.page_id,
        "full_text_chars": len(full_text),
        "sections_raw": len(sections),
        "sections_parent_ok": matched_parents,
        "sections": rows,
    }


def _llm_debug_failure_reason(audit: dict[str, Any]) -> str:
    """audit → 실패 유형 코드."""
    if audit.get("canonical_chars", 0) == 0:
        return "empty_body"
    if not audit.get("llm_enabled"):
        return "llm_disabled"
    if not audit.get("llm_response"):
        return "llm_no_response"
    if audit.get("llm_apply_empty"):
        return "llm_sections_empty"
    if audit.get("fallback"):
        return str(audit.get("fallback"))
    return "unknown"


def should_write_llm_debug(audit: dict[str, Any], *, force: bool = False) -> bool:
    """디버그 파일 기록 여부."""
    if not RAG_LLM_DEBUG:
        return False
    if force or RAG_LLM_DEBUG_ALWAYS:
        return True
    if audit.get("canonical_chars", 0) == 0:
        return True
    if not audit.get("llm_enabled"):
        return True
    if not audit.get("used_llm"):
        return True
    return False


def write_llm_debug_files(
    page: WikiPage,
    full_text: str,
    audit: dict[str, Any],
    *,
    llm: Optional["LLMClient"] = None,
    llm_obj: Optional[dict[str, Any]] = None,
    apply_diag: Optional[dict[str, Any]] = None,
    force: bool = False,
    debug_label: str = "",
) -> Optional[Path]:
    """실패·fallback 시 llm_debug/{page_id}/ 에 입력·출력·원인 저장."""
    if not should_write_llm_debug(audit, force=force):
        return None

    reason = _llm_debug_failure_reason(audit)
    page_dir = LLM_DEBUG_DIR / page.page_id
    if debug_label:
        page_dir = page_dir / debug_label
    page_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    llm_parts = split_page_text_for_llm(normalize_text(full_text), LLM_INPUT_MAX)

    payload: dict[str, Any] = {
        "written_at": now_iso(),
        "failure_reason": reason,
        "page_id": page.page_id,
        "page_title": page.page_title,
        "title_path": page.meta.title_path,
        "body_html_source": page.body_html_source,
        "html_chars": len(page.body_html or ""),
        "plain_chars": len(page.body_text or ""),
        "audit": audit,
        "input": {
            "full_text_chars": len(full_text),
            "full_text_hash": short_hash(full_text),
            "llm_input_parts": len(llm_parts),
            "part_char_sizes": [len(p) for p in llm_parts],
            "table_hints": len(extract_table_hints(full_text)),
            "model": OPENAI_MODEL,
            "llm_input_max": LLM_INPUT_MAX,
            "llm_max_output": LLM_MAX_OUTPUT,
            "parent_min": PARENT_MIN,
            "child_min": CHILD_MIN,
            "child_max": CHILD_MAX,
        },
        "llm_run": llm.run_debug if llm else {},
        "llm_merged_response": llm_obj,
        "apply_diagnosis": apply_diag,
    }

    # 본문·프롬프트·raw 응답 (별도 파일 — JSON 크기 제한)
    (page_dir / "full_text.txt").write_text(full_text or "", encoding="utf-8")
    for i, part in enumerate(llm_parts):
        (page_dir / f"input_part_{i + 1:02d}_of_{len(llm_parts):02d}.txt").write_text(part, encoding="utf-8")

    if llm and llm.run_debug.get("parts"):
        for part_dbg in llm.run_debug["parts"]:
            idx = int(part_dbg.get("part_idx", 0)) + 1
            prompt = part_dbg.get("prompt") or ""
            if prompt:
                (page_dir / f"prompt_part_{idx:02d}.txt").write_text(prompt, encoding="utf-8")
            raw = part_dbg.get("raw_response") or ""
            if raw:
                (page_dir / f"response_raw_part_{idx:02d}.txt").write_text(raw, encoding="utf-8")

    if llm_obj is not None:
        write_json(page_dir / "llm_response_parsed.json", llm_obj)

    stamp_path = page_dir / f"{reason}_{ts}.json"
    write_json(stamp_path, payload)
    write_json(page_dir / "latest.json", payload)

    index_path = LLM_DEBUG_DIR / "index.jsonl"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with index_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "written_at": payload["written_at"],
            "page_id": page.page_id,
            "page_title": page.page_title,
            "failure_reason": reason,
            "used_llm": audit.get("used_llm"),
            "llm_response": audit.get("llm_response"),
            "llm_apply_failed": audit.get("llm_apply_failed"),
            "fallback": audit.get("fallback"),
            "full_text_chars": len(full_text),
            "llm_sections_raw": audit.get("llm_sections_raw"),
            "sections_parent_ok": (apply_diag or {}).get("sections_parent_ok"),
            "debug_dir": str(page_dir),
        }, ensure_ascii=False) + "\n")

    print(f"📝 LLM debug → {page_dir} ({reason})")
    return page_dir


class LLMClient:
    """페이지당 1회 parent+child chunking."""

    def __init__(self) -> None:
        self.ok = bool(OPENAI_ENABLED and OPENAI_API_KEY)
        self.client = OpenAI(api_key=OPENAI_API_KEY) if self.ok else None
        self.cache: dict[str, Any] = {}
        self.run_debug: dict[str, Any] = {}
        if OPENAI_CACHE_JSON.exists():
            try:
                self.cache = read_json(OPENAI_CACHE_JSON)
            except Exception:
                pass

    def save_cache(self) -> None:
        """openai_cache.json 저장."""
        write_json(OPENAI_CACHE_JSON, self.cache)

    def _parse_json(self, raw: str) -> Optional[dict[str, Any]]:
        """응답에서 JSON object 추출."""
        text = (raw or "").strip()
        if "```" in text:
            for part in text.split("```"):
                p = part.strip()
                if p.startswith("json"):
                    p = p[4:].strip()
                if p.startswith("{"):
                    text = p
                    break
        s, e = text.find("{"), text.rfind("}")
        if s < 0 or e <= s:
            return None
        try:
            obj = json.loads(text[s : e + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None

    def _chunk_page_part(
        self,
        page: WikiPage,
        part_text: str,
        table_hints: list[dict[str, Any]],
        *,
        full_text: str,
        part_idx: int,
        part_total: int,
        attachment_title: str = "",
    ) -> Optional[dict[str, Any]]:
        """원문 한 구간 LLM chunking (캐시 키: 전체 해시 + 구간 인덱스)."""
        part_text = normalize_text(part_text)
        att_seg = f":att:{short_hash(attachment_title)}" if attachment_title else ""
        key = (
            f"rb:chunk:v8:{page.page_id}{att_seg}:{short_hash(full_text)}:"
            f"{part_total}:{part_idx}:{short_hash(part_text)}:{len(table_hints)}"
        )
        part_dbg: dict[str, Any] = {
            "part_idx": part_idx,
            "part_total": part_total,
            "part_chars": len(part_text),
            "cache_key": key,
            "table_hints": len(table_hints),
        }
        if key in self.cache:
            part_dbg.update({
                "cache_hit": True,
                "parse_ok": True,
                "from_cache": True,
                "parsed_sections": len((self.cache[key].get("parent_sections") or [])),
            })
            self.run_debug.setdefault("parts", []).append(part_dbg)
            return self.cache[key]

        if attachment_title:
            scope = (
                f"첨부 문서 원문 일부 ({part_idx + 1}/{part_total}). 아래 page_full_text 구간만 chunking 하세요."
                if part_total > 1
                else "아래 첨부 문서 원문을 RAG registry용으로 chunking 하세요."
            )
        else:
            scope = (
                f"원문 일부 ({part_idx + 1}/{part_total}). 아래 page_full_text 구간만 chunking 하세요."
                if part_total > 1
                else "아래 사내 위키 페이지 원문을 RAG registry용으로 chunking 하세요."
            )
        parent_count_hint = max(1, round(len(part_text) / max(PARENT_TARGET, 1)))
        att_line = f"첨부 파일: {attachment_title}\n" if attachment_title else ""
        prompt = f"""{scope}

규칙:
- JSON object만 출력.
- text 필드는 page_full_text의 내용을 원문 그대로 사용 (요약·재작성·생략 금지).
- parent_section: 주제·절차·정책이 달라지는 문맥 경계마다 분리. 각 parent는 {PARENT_TARGET}자 전후(최소 {PARENT_MIN}, 최대 {PARENT_MAX}). 예상 {parent_count_hint}~{parent_count_hint + 2}개.
- child_evidence: parent 안 검색용 핵심 단락. 각 {CHILD_TARGET}자 전후(최소 {CHILD_MIN}, 최대 {CHILD_MAX}). 짧은 항목·FAQ는 한 child로 묶기.
- [표 시작]...[표 끝] 블록은 분리하지 말 것.
- [파일명 | 설명], [첨부: 파일명 | 설명] 줄은 관련 절차 맥락에 포함.
- FAQ는 여러 Q/A를 하나의 child에 묶어도 됨.

출력 스키마:
{{
  "page_summary": "1~2문장",
  "parent_sections": [
    {{
      "title": "의미 단위 제목",
      "section_type": "guide|procedure|policy|faq|other",
      "text": "원문 내용",
      "keywords": ["검색 키워드 3~8개"],
      "child_evidence": [
        {{
          "title": "짧은 제목",
          "text": "원문 내용 (parent 내 핵심 단락)",
          "keywords": ["키워드"]
        }}
      ]
    }}
  ]
}}

문서 제목: {page.page_title}
{att_line}title_path: {page.meta.title_path}

page_full_text:
{part_text}
"""
        part_dbg["prompt"] = prompt
        if attachment_title:
            part_dbg["attachment_title"] = attachment_title
        part_dbg["cache_hit"] = False
        try:
            resp = self.client.chat.completions.create(
                model=OPENAI_MODEL,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": "사내 위키 RAG chunking. JSON만. 문맥 경계로 parent/child를 나누고 글자 수 가이드를 지켜라. 원문 전체를 빠짐없이 커버한다."},
                    {"role": "user", "content": prompt},
                ],
            )
            raw_content = resp.choices[0].message.content or ""
            part_dbg["raw_response"] = raw_content
            part_dbg["raw_response_chars"] = len(raw_content)
            obj = self._parse_json(raw_content)
            part_dbg["parse_ok"] = obj is not None
            if obj is None:
                part_dbg["error"] = "json_parse_failed"
                part_dbg["raw_response_preview"] = _preview(raw_content, 1500)
            else:
                secs = obj.get("parent_sections")
                part_dbg["parsed_sections"] = len(secs) if isinstance(secs, list) else 0
                self.cache[key] = obj
            self.run_debug.setdefault("parts", []).append(part_dbg)
            return obj
        except Exception as e:
            part_dbg["error"] = "api_error"
            part_dbg["error_message"] = str(e)
            part_dbg["error_type"] = type(e).__name__
            part_dbg["traceback"] = traceback.format_exc()
            self.run_debug.setdefault("parts", []).append(part_dbg)
            print(f"⚠️ LLM chunk_page {page.page_id} part {part_idx + 1}/{part_total}: {e}")
            return None

    def chunk_page(
        self,
        page: WikiPage,
        page_full_text: str,
        table_hints: list[dict[str, Any]],
        *,
        attachment_title: str = "",
    ) -> Optional[dict[str, Any]]:
        """전체 원문 → (필요 시 구간 분할) parent_sections + nested child_evidence JSON."""
        full_text = normalize_text(page_full_text)
        self.run_debug = {
            "page_id": page.page_id,
            "llm_ok": self.ok,
            "full_text_chars": len(full_text),
            "input_parts": 0,
            "parts": [],
            "merged_from_cache": False,
            "partial_parts_ok": 0,
            "failure": None,
        }
        if attachment_title:
            self.run_debug["attachment_title"] = attachment_title
        if not self.ok:
            self.run_debug["failure"] = "llm_disabled"
            return None
        parts = split_page_text_for_llm(full_text, LLM_INPUT_MAX)
        self.run_debug["input_parts"] = len(parts)
        if not parts:
            self.run_debug["failure"] = "empty_input_parts"
            return None

        att_seg = f":att:{short_hash(attachment_title)}" if attachment_title else ""
        merged_key = (
            f"rb:chunk:v8:{page.page_id}{att_seg}:{short_hash(full_text)}:"
            f"merged:{len(parts)}:{len(table_hints)}"
        )
        if merged_key in self.cache:
            self.run_debug["merged_from_cache"] = True
            return self.cache[merged_key]

        objs: list[dict[str, Any]] = []
        for idx, part in enumerate(parts):
            part_hints = [
                h for h in table_hints
                if isinstance(h, dict) and (h.get("text") or "")[:500] in part
            ]
            logger.info(
                "[RAG-LLM] 호출 %d/%d page_id=%s attachment=%s part_chars=%d",
                idx + 1, len(parts), page.page_id, attachment_title or "-", len(part),
            )
            obj = self._chunk_page_part(
                page,
                part,
                part_hints or table_hints,
                full_text=full_text,
                part_idx=idx,
                part_total=len(parts),
                attachment_title=attachment_title,
            )
            if obj:
                objs.append(obj)
        self.run_debug["partial_parts_ok"] = len(objs)
        if not objs:
            self.run_debug["failure"] = "all_parts_failed"
            return None
        if len(objs) < len(parts):
            self.run_debug["failure"] = "partial_parts_failed"
        merged = merge_llm_chunk_objects(objs)
        if merged:
            self.cache[merged_key] = merged
        else:
            self.run_debug["failure"] = "merge_empty"
        return merged


def apply_llm_chunking(page: WikiPage, full_text: str, obj: dict[str, Any]) -> list[ParentChunk]:
    """LLM JSON → ParentChunk (크기·문맥은 프롬프트에 위임)."""
    parents: list[ParentChunk] = []
    summary = clean_title(obj.get("page_summary") or "")
    sections = obj.get("parent_sections")
    if not isinstance(sections, list):
        return []

    for sec in sections:
        if not isinstance(sec, dict):
            continue
        title = clean_title(sec.get("title") or page.page_title)
        ptext = normalize_text(str(sec.get("text") or ""))
        if not ptext:
            continue
        stype = clean_title(sec.get("section_type") or "guide") or "guide"

        children: list[dict[str, Any]] = []
        for ch in sec.get("child_evidence") or []:
            if not isinstance(ch, dict):
                continue
            ctext = normalize_text(str(ch.get("text") or ""))
            if not ctext:
                continue
            ckw = ch.get("keywords") if isinstance(ch.get("keywords"), list) else []
            children.append({
                "title": clean_title(ch.get("title") or title),
                "text": ctext,
                "keywords": [clean_title(k) for k in ckw if clean_title(k)][:10],
                "section_type": stype,
            })

        children = dedupe_children(children, ptext)
        pc = ParentChunk(
            title=title,
            text=ptext,
            section_type=stype,
            page_summary=summary,
            children=children,
        )
        if not pc.children:
            pc.children = dedupe_children(rule_fallback_children(pc), ptext)
        parents.append(pc)

    return parents


def build_body_parents(page: WikiPage, llm: LLMClient, full_text: str) -> tuple[list[ParentChunk], dict[str, Any]]:
    """본문 parent/child — LLM 우선, 실패 시 rule."""
    audit: dict[str, Any] = {
        "canonical_chars": len(full_text),
        "used_llm": False,
        "llm_enabled": False,
        "llm_response": False,
    }
    if not full_text.strip():
        return [], audit

    hints = extract_table_hints(full_text)
    llm_parts = split_page_text_for_llm(full_text, LLM_INPUT_MAX)
    audit["llm_input_parts"] = len(llm_parts)
    audit["llm_input_chars"] = len(full_text)
    parents: list[ParentChunk] = []
    llm_obj: Optional[dict[str, Any]] = None
    apply_diag: Optional[dict[str, Any]] = None
    if llm.ok:
        audit["llm_enabled"] = True
        llm_obj = llm.chunk_page(page, full_text, hints)
        if llm_obj:
            audit["llm_response"] = True
            raw_secs = llm_obj.get("parent_sections")
            audit["llm_sections_raw"] = len(raw_secs) if isinstance(raw_secs, list) else 0
            if llm.run_debug.get("failure"):
                audit["llm_run_failure"] = llm.run_debug.get("failure")
            # 원문 매칭 진단은 audit/log 전용 (적용 여부 결정에 쓰지 않음)
            apply_diag = diagnose_llm_apply(page, full_text, llm_obj)
            audit["llm_sections_matched"] = apply_diag.get("sections_parent_ok", 0)
            parents = apply_llm_chunking(page, full_text, llm_obj)
            if parents:
                audit["used_llm"] = True
                audit["llm_parent_sections"] = len(parents)
                audit["llm_child_evidence"] = sum(len(p.children) for p in parents)
                write_llm_debug_files(
                    page, full_text, audit,
                    llm=llm, llm_obj=llm_obj, apply_diag=apply_diag,
                )
                return parents, audit
            # section 목록이 비었을 때만 fallback
            audit["llm_apply_empty"] = True
        elif llm.run_debug.get("failure"):
            audit["llm_run_failure"] = llm.run_debug.get("failure")

    parents = rule_fallback_parents(full_text, page.page_title)
    for p in parents:
        p.children = dedupe_children(rule_fallback_children(p), p.text)
    audit["fallback"] = "rule_size_split"
    write_llm_debug_files(
        page, full_text, audit,
        llm=llm, llm_obj=llm_obj, apply_diag=apply_diag,
    )
    return parents, audit


def build_page_chunks(page: WikiPage, llm: LLMClient) -> tuple[list[ParentChunk], dict[str, Any]]:
    """페이지 1건: 본문 청킹 → 첨부 parent (본문 직후)."""
    body_marked, body_meta = build_page_full_text(page)
    audit: dict[str, Any] = {"attachment_inputs": 0, **body_meta}
    body_parents, body_audit = build_body_parents(page, llm, body_marked)
    audit.update(body_audit)
    att_parents, att_audit = build_attachment_parents(page, body_marked, llm)
    audit.update(att_audit)
    audit["attachment_inputs"] = len(collect_attachment_inputs(page, body_marked))
    audit["body_parent_sections"] = len(body_parents)
    audit["attachment_parent_sections"] = len(att_parents)
    audit["child_evidence"] = sum(len(p.children) for p in body_parents + att_parents)
    return body_parents + att_parents, audit


# =============================================================================
# Payload rows
# =============================================================================


def make_parent_row(page: WikiPage, parent: ParentChunk, order: int) -> dict[str, Any]:
    """parent_store JSONL 1행."""
    section_id = f"parent__{order:03d}__{slugify(parent.title)}"
    doc_id = f"{page.page_id}:parent_section:{section_id}:{order:05d}:{short_hash(parent.text)}"
    row: dict[str, Any] = {
        "id": doc_id,
        "group_id": page.page_id,
        "chunk_type": "parent_section",
        "source_kind": parent.source_kind,
        "section_id": section_id,
        "section_title": parent.title,
        "section_type": parent.section_type,
        "text": parent.text,
        "index_for_embedding": False,
        **page.meta.as_dict(),
    }
    if parent.attachment_title:
        row["attachment_title"] = parent.attachment_title
    if parent.page_summary:
        row["page_summary"] = parent.page_summary
    return row


def make_child_row(
    page: WikiPage,
    parent: ParentChunk,
    parent_doc_id: str,
    parent_section_id: str,
    child: dict[str, Any],
    order: int,
    cidx: int,
) -> dict[str, Any]:
    """vector_payload JSONL 1행."""
    title = clean_title(child.get("title") or parent.title)
    text = child.get("text") or ""
    keywords = child.get("keywords") or []
    kw_str = " ".join(keywords[:12]) if keywords else ""
    section_id = f"child__{order:03d}__{slugify(title)}__{cidx:03d}"
    doc_id = f"{page.page_id}:child_evidence:{section_id}:{order:05d}:{short_hash(text)}"
    dense = f"{page.page_title} / {title} / {kw_str} / {text[:400]}".strip(" /")
    return {
        "id": doc_id,
        "group_id": page.page_id,
        "parent_doc_id": parent_doc_id,
        "parent_id": parent_section_id,
        "chunk_type": "child_evidence",
        "source_kind": parent.source_kind,
        "section_title": title,
        "parent_section_title": parent.title,
        "section_type": child.get("section_type") or parent.section_type,
        "text": text,
        "content_dense": dense,
        "lexical_boost": f"{page.page_title} {parent.title} {title} {kw_str}".strip(),
        "keywords": keywords,
        "answer_expand_parent": True,
        **page.meta.as_dict(),
    }


def page_to_rows(page: WikiPage, parents: list[ParentChunk]) -> tuple[list[dict], list[dict]]:
    """ParentChunk 목록 → (vector rows, parent rows)."""
    vectors: list[dict] = []
    parent_rows: list[dict] = []
    order = 0
    for parent in parents:
        order += 1
        prow = make_parent_row(page, parent, order)
        parent_rows.append(prow)
        pid, sid = prow["id"], prow["section_id"]
        for cidx, child in enumerate(parent.children, 1):
            order += 1
            vectors.append(make_child_row(page, parent, pid, sid, child, order, cidx))
    return vectors, parent_rows


# =============================================================================
# Build loop
# =============================================================================


def read_jsonl(path: Path) -> list[dict]:
    """JSONL 읽기."""
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


def _existing_group_ids(*rows_lists: list[dict]) -> set[str]:
    """
    기존 registry JSONL에 이미 존재하는 group_id(page_id) 목록.
    checkpoint가 없거나 깨졌을 때도 '이미 진행된 페이지'를 생략하기 위함.
    """
    ids: set[str] = set()
    for rows in rows_lists:
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            gid = r.get("group_id")
            if gid:
                ids.add(str(gid))
    return ids


def build_rag() -> None:
    """전체 페이지 빌드 + checkpoint."""
    pages = load_pages()
    llm = LLMClient()
    cp = read_json(CHECKPOINT_JSON) if CHECKPOINT_JSON.exists() else {}
    completed = set(str(x) for x in cp.get("completed_page_ids", []))
    failed = set(str(x) for x in cp.get("failed_page_ids", []))
    errors: list[dict] = list(cp.get("errors", []))
    page_stats: dict[str, Any] = dict(cp.get("page_stats", {}))
    all_vector = read_jsonl(VECTOR_JSONL)
    all_parent = read_jsonl(PARENT_JSONL)

    # registry(JSONL)에 이미 들어간 page_id도 completed로 간주해 생략한다.
    completed |= _existing_group_ids(all_vector, all_parent)

    print(f"▶️ pages={len(pages)} done={len(completed)} vector={len(all_vector)} llm={llm.ok}")

    for page in tqdm(pages, desc="RAG"):
        pid = page.page_id
        print(f"▶️ pid={pid}, v={len(all_vector)}, p={len(all_parent)}")
        if pid in completed:
            continue
        try:
            parents, audit = build_page_chunks(page, llm)
            v_new, p_new = page_to_rows(page, parents)
            all_vector = [r for r in all_vector if r.get("group_id") != pid] + v_new
            all_parent = [r for r in all_parent if r.get("group_id") != pid] + p_new
            write_jsonl(VECTOR_JSONL, all_vector)
            write_jsonl(PARENT_JSONL, all_parent)
            page_stats[pid] = {
                "page_id": pid,
                "page_title": page.page_title,
                "title_path": page.meta.title_path,
                **audit,
                "vector_rows": len(v_new),
                "parent_rows": len(p_new),
            }
            completed.add(pid)
            failed.discard(pid)
            write_json(CHECKPOINT_JSON, {
                "completed_page_ids": sorted(completed),
                "failed_page_ids": sorted(failed),
                "page_stats": page_stats,
                "errors": errors,
                "updated_at": now_iso(),
            })
            llm.save_cache()
            llm_flag = audit.get("used_llm")
            if not llm_flag and audit.get("llm_response"):
                llm_flag = "empty_sections"
            print(
                f"✅ {pid} v={len(v_new)} p={len(p_new)} "
                f"llm={llm_flag} att={audit.get('attachment_inputs', 0)}"
            )
        except Exception as e:
            failed.add(pid)
            errors.append({"page_id": pid, "error": str(e), "traceback": traceback.format_exc()})
            write_json(CHECKPOINT_JSON, {
                "completed_page_ids": sorted(completed),
                "failed_page_ids": sorted(failed),
                "page_stats": page_stats,
                "errors": errors,
                "updated_at": now_iso(),
            })
            llm.save_cache()
            print(f"❌ {pid}: {e}")
            raise

    print(f"🎉 vector={len(all_vector)} parent={len(all_parent)}")


def debug_page(page_id: str) -> None:
    """단일 페이지: HTML 소스·full_text·parent/child 샘플 + llm_debug 저장."""
    pages = {p.page_id: p for p in load_pages()}
    page = pages.get(page_id)
    if not page:
        print(f"❌ page not found: {page_id}")
        return
    full_text, meta = build_page_full_text(page)
    llm = LLMClient()
    parents, audit = build_page_chunks(page, llm)
    llm_obj: Optional[dict[str, Any]] = None
    apply_diag: Optional[dict[str, Any]] = None
    if llm.ok:
        full_n = normalize_text(full_text)
        hints = extract_table_hints(full_n)
        parts = split_page_text_for_llm(full_n, LLM_INPUT_MAX)
        merged_key = (
            f"rb:chunk:v6:{page.page_id}:{short_hash(full_n)}:"
            f"merged:{len(parts)}:{len(hints)}"
        )
        llm_obj = llm.cache.get(merged_key)
        if llm_obj:
            apply_diag = diagnose_llm_apply(page, full_text, llm_obj)
    dbg_dir = write_llm_debug_files(
        page, full_text, audit,
        llm=llm, llm_obj=llm_obj, apply_diag=apply_diag, force=True,
    )
    print(json.dumps({
        "page_id": page_id,
        "page_title": page.page_title,
        "title_path": page.meta.title_path,
        **meta,
        "full_text_preview": full_text[:1200],
        "full_text_chars": len(full_text),
        **audit,
        "parents": [
            {
                "title": p.title,
                "text_len": len(p.text),
                "text_preview": p.text[:300],
                "children": [
                    {"title": c.get("title"), "text_len": len(c.get("text") or ""), "preview": (c.get("text") or "")[:200]}
                    for c in p.children[:5]
                ],
            }
            for p in parents[:4]
        ],
        "llm_debug_dir": str(dbg_dir) if dbg_dir else None,
        "apply_diagnosis_summary": {
            "sections_raw": (apply_diag or {}).get("sections_raw"),
            "sections_parent_ok": (apply_diag or {}).get("sections_parent_ok"),
        } if apply_diag else None,
    }, ensure_ascii=False, indent=2))


def main() -> None:
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "--debug":
        debug_page(sys.argv[2])
        return
    build_rag()


if __name__ == "__main__":
    main()
