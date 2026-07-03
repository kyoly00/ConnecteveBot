# -*- coding: utf-8 -*-
"""Slack 첨부 ingest · 이미지 I/O · DB 메타 · Turn1/Turn2 프롬프트."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Literal

import httpx
from PIL import Image, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pillow_heif = None  # type: ignore[assignment]

_CONN_BOT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _CONN_BOT_DIR.parent
for _p in (_CONN_BOT_DIR, _REPO_ROOT):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

from app.core.config import (
    CHAT_ATTACHMENT_CHUNK_OVERLAP,
    CHAT_ATTACHMENT_CHUNK_SIZE,
    CHAT_ATTACHMENT_MANIFEST_MODEL,
    CHAT_ATTACHMENT_VISION_MODEL,
    CHAT_ATTACHMENT_MAX_CHARS,
    CHAT_ATTACHMENT_PREVIEW_CHARS,
    CHAT_ATTACHMENT_TURN2_TOP_CHUNKS,
    CHAT_ATTACHMENT_TURN2_SUMMARY_CHARS,
    CHAT_ATTACHMENT_VISION_MAX_DIMENSION,
    CHAT_ATTACHMENTS_DIR,
    DATA_DIR,
)
from parsers.base import get_parser
from app.rag.vectordb import chunk_text

logger = logging.getLogger(__name__)

# =============================================================================
# Image I/O (정규화 · data URL · ingest 저장)
# =============================================================================

_MAX_IMAGE_BYTES = int(os.getenv("CHAT_IMAGE_MAX_BYTES", str(4 * 1024 * 1024)))
_MAX_IMAGE_DIMENSION = int(os.getenv("CHAT_IMAGE_MAX_DIMENSION", "2048"))
_JPEG_QUALITY = int(os.getenv("CHAT_IMAGE_JPEG_QUALITY", "85"))
_SAFE_MIMES = frozenset({"image/jpeg", "image/png"})
_CONVERT_MIMES = frozenset({
    "image/heic", "image/heif", "image/tiff", "image/x-tiff",
    "image/bmp", "image/x-ms-bmp", "image/webp", "image/gif",
})
_CONVERT_EXTENSIONS = frozenset({
    ".heic", ".heif", ".tif", ".tiff", ".bmp", ".webp", ".gif",
})
_IMAGE_EXTENSIONS = _CONVERT_EXTENSIONS | {".jpg", ".jpeg", ".png"}


def is_image_file(file_obj: dict[str, Any]) -> bool:
    mime = str(file_obj.get("mimetype") or "").lower()
    if mime.startswith("image/"):
        return True
    name = str(file_obj.get("name") or file_obj.get("title") or "")
    return Path(name).suffix.lower() in _IMAGE_EXTENSIONS


def _img_resize(img: Image.Image, *, max_dim: int) -> Image.Image:
    w, h = img.size
    if max(w, h) <= max_dim:
        return img
    scale = max_dim / max(w, h)
    return img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)


def _img_has_alpha(img: Image.Image) -> bool:
    if img.mode in ("RGBA", "LA"):
        return True
    if img.mode == "P" and "transparency" in img.info:
        return True
    return False


def normalize_image_bytes(
    data: bytes,
    mimetype: str = "",
    *,
    filename: str = "",
    max_dim: int | None = None,
) -> tuple[bytes, str]:
    limit = max_dim or _MAX_IMAGE_DIMENSION
    mime = (mimetype or "").lower().split(";")[0].strip()
    ext = Path(filename).suffix.lower() if filename else ""
    must_convert = (
        mime in _CONVERT_MIMES
        or ext in _CONVERT_EXTENSIONS
        or mime not in _SAFE_MIMES
        or len(data) > _MAX_IMAGE_BYTES
    )
    if not must_convert and mime in _SAFE_MIMES:
        return data, mime

    img = Image.open(BytesIO(data))
    img = ImageOps.exif_transpose(img)
    img = _img_resize(img, max_dim=limit)

    if _img_has_alpha(img):
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        out = BytesIO()
        img.save(out, format="PNG", optimize=True)
        normalized, out_mime = out.getvalue(), "image/png"
    else:
        img = img.convert("RGB")
        out = BytesIO()
        img.save(out, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
        normalized, out_mime = out.getvalue(), "image/jpeg"

    if len(normalized) > _MAX_IMAGE_BYTES and out_mime == "image/jpeg":
        img = Image.open(BytesIO(normalized)).convert("RGB")
        for quality in (75, 65, 55, 45):
            out = BytesIO()
            img.save(out, format="JPEG", quality=quality, optimize=True)
            normalized = out.getvalue()
            if len(normalized) <= _MAX_IMAGE_BYTES:
                break
    return normalized, out_mime


def _encode_bytes(data: bytes, mimetype: str = "image/jpeg") -> str:
    b64 = base64.b64encode(data).decode("utf-8")
    return f"data:{mimetype};base64,{b64}"


def path_to_data_url(path: Path, *, max_dim: int | None = None) -> str:
    if not path.is_file():
        return ""
    try:
        raw = path.read_bytes()
        if max_dim:
            img = Image.open(BytesIO(raw))
            img = ImageOps.exif_transpose(img)
            img = _img_resize(img, max_dim=max_dim)
            buf = BytesIO()
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGB")
            img.save(buf, format="JPEG", quality=82)
            return _encode_bytes(buf.getvalue(), "image/jpeg")
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        return _encode_bytes(raw, mime)
    except Exception as exc:
        logger.warning("path_to_data_url 실패 (%s): %s", path, exc)
        return ""


def save_image_assets(
    storage_dir: Path,
    raw: bytes,
    *,
    filename: str,
    mimetype: str,
) -> Path:
    normalized, out_mime = normalize_image_bytes(raw, mimetype=mimetype, filename=filename)
    ext = ".png" if out_mime == "image/png" else ".jpg"
    (storage_dir / f"original{ext}").write_bytes(normalized)
    display_path = storage_dir / f"display{ext}"
    display_path.write_bytes(normalized)
    try:
        img = Image.open(BytesIO(normalized))
        img.thumbnail((512, 512))
        img.convert("RGB").save(storage_dir / "thumb.jpg", format="JPEG", quality=80)
    except Exception:
        pass
    return display_path


# =============================================================================
# Attachment models & policy
# =============================================================================

AttachmentMode = Literal["general", "doc_based", "image_based", "hybrid", "uncertain"]

_DOCUMENT_EXTENSIONS = frozenset({
    ".pdf", ".csv", ".xlsx", ".xls", ".xlsm", ".hwp", ".docx", ".doc",
})

_IMAGE_HINTS = re.compile(
    r"사진|이미지|스크린|캡처|screenshot|photo|picture|그림|명함",
    re.IGNORECASE,
)
_DOC_HINTS = re.compile(
    r"pdf|엑셀|excel|csv|문서|파일|첨부|시트|표|invoice|영수|계약|공문|hwp|word",
    re.IGNORECASE,
)
_DEICTIC_HINTS = re.compile(
    r"이거|이것|이건|이게|이렇게|위에|방금|올린|첨부한|그거|그것|그림|스샷|캡처|하라던|하라고",
    re.IGNORECASE,
)


@dataclass
class SubmitterInfo:
    slack_user_id: str = ""
    name: str = ""
    email: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "slack_user_id": self.slack_user_id,
            "name": self.name,
            "email": self.email,
        }


@dataclass
class AttachmentItem:
    attachment_id: str
    filename: str
    kind: Literal["document", "image"]
    storage_dir: Path
    storage_relpath: str = ""
    slack_file_id: str = ""
    manifest: dict[str, Any] = field(default_factory=dict)

    @property
    def text_full_path(self) -> Path:
        return self.storage_dir / "text_full.txt"

    @property
    def chunks_path(self) -> Path:
        return self.storage_dir / "text_chunks.jsonl"

    @property
    def caption_path(self) -> Path:
        return self.storage_dir / "caption.txt"

    @property
    def thumb_path(self) -> Path:
        return self.storage_dir / "thumb.jpg"

    @property
    def display_image_path(self) -> Path:
        p = self.storage_dir / "display.jpg"
        if p.is_file():
            return p
        return self.storage_dir / "display.png"

    def primary_file_path(self) -> Path | None:
        """OneDrive 업로드용 로컬 파일 (이미지=display, 문서=original)."""
        if self.kind == "image":
            for name in ("display.jpg", "display.png", "original.jpg", "original.png"):
                p = self.storage_dir / name
                if p.is_file():
                    return p
            return None
        for p in sorted(self.storage_dir.glob("original*")):
            if p.is_file():
                return p
        if self.text_full_path.is_file():
            return self.text_full_path
        return None


@dataclass
class UserAttachmentBundle:
    session_id: str = ""
    submitter: SubmitterInfo = field(default_factory=SubmitterInfo)
    items: list[AttachmentItem] = field(default_factory=list)

    @property
    def has_content(self) -> bool:
        return bool(self.items)

    @property
    def filenames(self) -> list[str]:
        return [item.filename for item in self.items]

    def get_item(self, attachment_id: str) -> AttachmentItem | None:
        for item in self.items:
            if item.attachment_id == attachment_id:
                return item
        return None

    def item_ids(self) -> list[str]:
        return [item.attachment_id for item in self.items]

    def to_db_records(self, user_text: str = "") -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in self.items:
            summary = str(
                item.manifest.get("one_line")
                or item.manifest.get("caption")
                or ""
            ).strip()
            rows.append({
                "attachment_path": item.storage_relpath,
                "attachment_title": item.filename,
                "attachment_summary": summary or None,
                "attachment_kind": item.kind,
                "slack_file_id": item.slack_file_id or None,
                "metadata": {
                    "attachment_id": item.attachment_id,
                    "session_id": self.session_id,
                    "submitter": self.submitter.to_dict(),
                },
            })
        return rows


@dataclass
class AttachmentPolicy:
    mode: AttachmentMode = "general"
    include_attachment_ids: list[str] = field(default_factory=list)
    include_doc_chunks: bool = False
    include_images: bool = False
    reason: str = ""

    def applies_to_turn2(self) -> bool:
        return self.mode != "general"


# 하위 호환 alias
AttachmentPlan = AttachmentPolicy


@dataclass
class AttachmentMeta:
    """DB·ingest 공통 첨부 메타 (프롬프트용)."""
    attachment_id: str
    title: str
    kind: Literal["document", "image"]
    summary: str
    storage_relpath: str

    def short_summary(self, limit: int = CHAT_ATTACHMENT_TURN2_SUMMARY_CHARS) -> str:
        return _truncate(self.summary, limit)


@dataclass
class AttachmentContext:
    """logical attachment_id → 메타. 파일 I/O는 storage_relpath로 resolve."""
    items: dict[str, AttachmentMeta] = field(default_factory=dict)

    @classmethod
    def from_bundle(cls, bundle: UserAttachmentBundle | None) -> AttachmentContext:
        ctx = cls()
        if not bundle:
            return ctx
        for item in bundle.items:
            summary = str(
                item.manifest.get("one_line")
                or item.manifest.get("caption")
                or item.filename
            ).strip()
            ctx.items[item.attachment_id] = AttachmentMeta(
                attachment_id=item.attachment_id,
                title=item.filename,
                kind=item.kind,
                summary=summary,
                storage_relpath=item.storage_relpath,
            )
        return ctx

    @classmethod
    def from_db_rows(cls, rows: list[Any]) -> AttachmentContext:
        ctx = cls()
        for row in rows:
            meta = getattr(row, "metadata_", None) or {}
            if not isinstance(meta, dict):
                meta = {}
            logical_id = str(meta.get("attachment_id") or "").strip()
            if not logical_id:
                continue
            kind_raw = str(getattr(row, "attachment_kind", "") or "document")
            kind: Literal["document", "image"] = (
                "image" if kind_raw == "image" else "document"
            )
            ctx.items[logical_id] = AttachmentMeta(
                attachment_id=logical_id,
                title=str(getattr(row, "attachment_title", "") or ""),
                kind=kind,
                summary=str(getattr(row, "attachment_summary", "") or "").strip(),
                storage_relpath=str(getattr(row, "attachment_path", "") or ""),
            )
        return ctx

    def merge(self, other: AttachmentContext | None) -> AttachmentContext:
        if not other:
            return self
        merged = dict(self.items)
        merged.update(other.items)
        return AttachmentContext(items=merged)

    def has(self, attachment_id: str) -> bool:
        return attachment_id in self.items

    def get_meta(self, attachment_id: str) -> AttachmentMeta | None:
        return self.items.get(attachment_id)

    def ids(self) -> list[str]:
        return list(self.items.keys())

    def to_item(self, attachment_id: str) -> AttachmentItem | None:
        meta = self.get_meta(attachment_id)
        if not meta or not meta.storage_relpath:
            return None
        abs_path = (_REPO_ROOT / meta.storage_relpath).resolve()
        if not abs_path.is_file():
            return None
        return AttachmentItem(
            attachment_id=meta.attachment_id,
            filename=meta.title,
            kind=meta.kind if meta.kind in ("document", "image") else "document",
            storage_dir=abs_path.parent,
            storage_relpath=meta.storage_relpath,
            manifest={"one_line": meta.summary, "caption": meta.summary},
        )


@dataclass
class Turn2UserContent:
    """Turn2 user 메시지 — 로그용 text와 API용 multimodal 분리."""
    text: str
    vision_paths: list[Path] = field(default_factory=list)

    def to_api_content(self) -> str | list[dict[str, Any]]:
        if not self.vision_paths:
            return self.text
        parts: list[dict[str, Any]] = [{"type": "text", "text": self.text}]
        for path in self.vision_paths:
            url = path_to_data_url(path, max_dim=CHAT_ATTACHMENT_VISION_MAX_DIMENSION)
            if url:
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": url, "detail": "low"},
                })
        return parts if len(parts) > 1 else self.text


async def resolve_attachment_context(
    policy: AttachmentPolicy | None,
    *,
    bundle: UserAttachmentBundle | None = None,
    db_session_id: Any = None,
) -> AttachmentContext | None:
    """in-memory bundle + DB(metadata.attachment_id) 병합."""
    ctx = AttachmentContext.from_bundle(bundle)
    if not policy:
        return ctx if ctx.items else None

    want_ids = policy.include_attachment_ids or ctx.ids()
    if not want_ids and bundle:
        want_ids = bundle.item_ids()

    missing = [aid for aid in want_ids if not ctx.has(aid)]
    if missing and db_session_id is not None:
        try:
            from app.services.chat import chat_service

            rows = await chat_service.get_attachments_by_logical_ids(
                missing,
                session_id=db_session_id,
            )
            ctx = ctx.merge(AttachmentContext.from_db_rows(rows))
        except Exception as exc:
            logger.warning("[attachment] DB resolve failed: %s", exc)

    return ctx if ctx.items else None


def _stable_attachment_id(session_id: str, slack_file_id: str, filename: str) -> str:
    raw = f"{session_id}|{slack_file_id}|{filename}"
    return "att_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…(이하 생략)"


def _parsed_text_to_str(parsed: Any) -> str:
    parts: list[str] = []
    body = (getattr(parsed, "text", None) or "").strip()
    if body:
        parts.append(body)
    for table in getattr(parsed, "tables", None) or []:
        t = str(table or "").strip()
        if t:
            parts.append(t)
    return "\n\n".join(parts)


def _is_document_file(file_obj: dict[str, Any]) -> bool:
    if is_image_file(file_obj):
        return False
    name = str(file_obj.get("name") or file_obj.get("title") or "")
    ext = Path(name).suffix.lower()
    if ext in _DOCUMENT_EXTENSIONS:
        return True
    mime = str(file_obj.get("mimetype") or "").lower()
    return any(
        key in mime
        for key in ("pdf", "spreadsheet", "csv", "msword", "wordprocessingml", "hwp", "haansoft")
    )


def _write_chunks(text: str, chunks_path: Path) -> list[dict[str, Any]]:
    pieces = chunk_text(
        text,
        chunk_size=CHAT_ATTACHMENT_CHUNK_SIZE,
        chunk_overlap=CHAT_ATTACHMENT_CHUNK_OVERLAP,
    )
    rows: list[dict[str, Any]] = []
    with chunks_path.open("w", encoding="utf-8") as f:
        for idx, piece in enumerate(pieces):
            row = {"chunk_id": idx, "text": piece}
            rows.append(row)
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return rows


def _load_chunks(item: AttachmentItem) -> list[dict[str, Any]]:
    if not item.chunks_path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with item.chunks_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _fallback_manifest(
    *,
    filename: str,
    kind: str,
    text_preview: str = "",
    caption: str = "",
) -> dict[str, Any]:
    preview = _truncate(text_preview or caption, 200)
    one_line = preview or f"{kind} 파일: {filename}"
    return {
        "filename": filename,
        "kind": kind,
        "one_line": one_line,
        "topics": [],
        "language": "ko",
        "needs_vision": kind == "image",
        "needs_full_text": kind == "document" and len(text_preview) > CHAT_ATTACHMENT_PREVIEW_CHARS,
        "char_count": len(text_preview),
        "caption": caption,
    }


def _rel_storage_path(path: Path) -> str:
    try:
        return path.relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def _cheap_llm_json(prompt: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        kwargs: dict[str, Any] = {"api_key": api_key}
        if os.getenv("OPENAI_BASE_URL"):
            kwargs["base_url"] = os.getenv("OPENAI_BASE_URL")
            client = OpenAI(**kwargs)
        resp = client.chat.completions.create(
            model=CHAT_ATTACHMENT_MANIFEST_MODEL,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": "JSON만 출력. 한국어 one_line·caption 작성.",
                },
                {
                    "role": "user",
                    "content": prompt + "\n\nINPUT:\n" + json.dumps(payload, ensure_ascii=False),
                },
            ],
        )
        content = resp.choices[0].message.content or "{}"
        data = json.loads(content)
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.warning("[attachment] cheap LLM failed: %s", exc)
        return None


def _vision_llm_json(
    image_path: Path,
    *,
    prompt: str,
    max_dim: int,
    detail: str = "low",
) -> dict[str, Any] | None:
    """실제 이미지를 vision API로 분석 (ingest summary / DB 저장용)."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or not image_path.is_file():
        logger.warning("[attachment] vision skip: api_key=%s path=%s", bool(api_key), image_path)
        return None
    data_url = path_to_data_url(image_path, max_dim=max_dim)
    if not data_url:
        return None
    try:
        from openai import OpenAI

        kwargs: dict[str, Any] = {"api_key": api_key}
        if os.getenv("OPENAI_BASE_URL"):
            kwargs["base_url"] = os.getenv("OPENAI_BASE_URL")
        client = OpenAI(**kwargs)
        resp = client.chat.completions.create(
            model=CHAT_ATTACHMENT_VISION_MODEL,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "JSON만 출력. 한국어 one_line·caption. "
                        "이미지에 보이는 텍스트·UI·표·조직도를 구체적으로 설명."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url, "detail": detail},
                        },
                    ],
                },
            ],
        )
        content = resp.choices[0].message.content or "{}"
        data = json.loads(content)
        if isinstance(data, dict):
            logger.info(
                "[attachment] vision ok model=%s file=%s one_line=%s",
                CHAT_ATTACHMENT_VISION_MODEL,
                image_path.name,
                str(data.get("one_line") or "")[:80],
            )
            return data
        return None
    except Exception as exc:
        logger.warning("[attachment] vision LLM failed model=%s: %s", CHAT_ATTACHMENT_VISION_MODEL, exc)
        return None


def _enrich_manifest_image(
    filename: str,
    display_path: Path,
    *,
    user_text: str = "",
) -> dict[str, Any]:
    user_ctx = _truncate((user_text or "").strip(), 500) or "(없음)"
    prompt = (
        f"파일명: {filename}\n"
        f"사용자 메시지(맥락): {user_ctx}\n"
        "첨부 이미지를 직접 보고 JSON: "
        "{one_line, caption, topics[], language}\n"
        "- one_line: 한 줄 요약\n"
        "- caption: 화면에 보이는 내용·텍스트·UI 요소 설명 (2~4문장)\n"
        "- placeholder·'설명 필요'·파일명 반복 금지"
    )
    llm = _vision_llm_json(
        display_path,
        prompt=prompt,
        max_dim=CHAT_ATTACHMENT_VISION_MAX_DIMENSION,
        detail="high",
    )
    if not llm:
        llm = _vision_llm_json(
            display_path,
            prompt=prompt,
            max_dim=CHAT_ATTACHMENT_VISION_MAX_DIMENSION,
            detail="low",
        )
    base = _fallback_manifest(filename=filename, kind="image", caption=f"이미지 첨부: {filename}")
    if llm:
        cap = str(llm.get("caption") or "").strip()
        one = str(llm.get("one_line") or "").strip()
        if cap and "설명이 필요" not in cap and "캡션입니다" not in cap:
            base["caption"] = cap
        if one and "캡션입니다" not in one:
            base["one_line"] = one
        elif cap:
            base["one_line"] = cap
        if llm.get("topics"):
            base["topics"] = llm["topics"]
    return base


def _enrich_manifest_document(filename: str, text_preview: str) -> dict[str, Any]:
    llm = _cheap_llm_json(
        "문서 첨부 manifest JSON: {one_line, topics[], language, needs_full_text(bool)}",
        {"filename": filename, "preview": _truncate(text_preview, 1500)},
    )
    base = _fallback_manifest(filename=filename, kind="document", text_preview=text_preview)
    if llm:
        base.update({k: v for k, v in llm.items() if v is not None})
    return base


async def _download_slack_file(
    client: httpx.AsyncClient,
    file_obj: dict[str, Any],
    bot_token: str,
) -> tuple[str, bytes] | None:
    download_url = file_obj.get("url_private_download") or file_obj.get("url_private") or ""
    if not download_url:
        return None
    filename = str(file_obj.get("name") or file_obj.get("title") or "attachment")
    try:
        res = await client.get(
            download_url,
            headers={"Authorization": f"Bearer {bot_token}"},
        )
        res.raise_for_status()
        return filename, res.content
    except Exception as exc:
        logger.warning("Slack 파일 다운로드 실패 (%s): %s", filename, exc)
        return None


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def build_turn1_user_content(
    query_stripped: str,
    bundle: UserAttachmentBundle | None,
    *,
    memory_context: str = "",
) -> str:
    """Turn1 — 첨부 id·종류·파일명만 (메타 상세는 DB)."""
    parts: list[str] = []
    mem = (memory_context or "").strip()
    if mem:
        parts.append(mem)

    if bundle and bundle.has_content:
        lines = ["<attachments>"]
        for item in bundle.items:
            lines.append(f"{item.attachment_id} {item.kind} {item.filename}")
        lines.append("</attachments>")
        parts.append("\n".join(lines))

    parts.append(f"<user_question>\n{query_stripped}\n</user_question>")
    return "\n\n".join(parts)


def _select_chunk_ids(query: str, chunks: list[dict[str, Any]], top_k: int) -> list[int]:
    if not chunks:
        return []
    q = (query or "").casefold()
    tokens = [t for t in re.split(r"\W+", q) if len(t) >= 2]
    if not tokens:
        return [int(c["chunk_id"]) for c in chunks[:top_k]]

    scored: list[tuple[int, int]] = []
    for row in chunks:
        text = str(row.get("text") or "").casefold()
        score = sum(1 for t in tokens if t in text)
        scored.append((score, int(row["chunk_id"])))
    scored.sort(key=lambda x: (-x[0], x[1]))
    picked = [cid for score, cid in scored if score > 0][:top_k]
    if picked:
        return picked
    return [int(c["chunk_id"]) for c in chunks[:top_k]]


def build_turn2_attachment_content(
    policy: AttachmentPolicy | None,
    ctx: AttachmentContext | None,
    query_stripped: str,
) -> Turn2UserContent:
    """Turn2 — compact 텍스트(DB 메타) + vision은 API 경계에서만."""
    empty = Turn2UserContent(text="")
    if not policy or not ctx or not policy.applies_to_turn2():
        return empty

    mode = policy.mode
    ids = policy.include_attachment_ids or ctx.ids()
    want_docs = policy.include_doc_chunks
    want_images = policy.include_images
    meta_lines: list[str] = []
    excerpt_lines: list[str] = []
    vision_paths: list[Path] = []

    for att_id in ids:
        meta = ctx.get_meta(att_id)
        if not meta:
            continue
        meta_lines.append(
            f"{att_id} | {meta.kind} | {meta.title} | {meta.short_summary()}"
        )

    if want_docs and mode in ("doc_based", "hybrid", "uncertain"):
        for att_id in ids:
            item = ctx.to_item(att_id)
            if not item or item.kind != "document":
                continue
            if mode == "uncertain":
                preview = _truncate(_read_text(item.text_full_path), CHAT_ATTACHMENT_PREVIEW_CHARS)
                if preview:
                    excerpt_lines.append(f"[{att_id}] {preview}")
                continue
            chunks = _load_chunks(item)
            for cid in _select_chunk_ids(query_stripped, chunks, CHAT_ATTACHMENT_TURN2_TOP_CHUNKS):
                row = next((c for c in chunks if int(c["chunk_id"]) == cid), None)
                if not row:
                    continue
                excerpt_lines.append(
                    f"[{att_id}#{cid}] {_truncate(str(row.get('text') or ''), CHAT_ATTACHMENT_PREVIEW_CHARS)}"
                )

    if want_images and mode in ("image_based", "hybrid"):
        for att_id in ids:
            item = ctx.to_item(att_id)
            if not item or item.kind != "image":
                continue
            path = item.display_image_path
            if path.is_file():
                vision_paths.append(path)

    blocks: list[str] = []
    if meta_lines:
        blocks.append("<attachments>\n" + "\n".join(meta_lines) + "\n</attachments>")
    if excerpt_lines:
        blocks.append(
            "<attachment_excerpts>\n" + "\n".join(excerpt_lines) + "\n</attachment_excerpts>"
        )

    return Turn2UserContent(text="\n\n".join(blocks), vision_paths=vision_paths)


def build_turn2_user_message_content(
    query_stripped: str,
    *,
    memory_context: str = "",
    prefix_blocks: list[str] | None = None,
    suffix_blocks: list[str] | None = None,
    attachment_policy: AttachmentPolicy | None = None,
    attachment_context: AttachmentContext | None = None,
    attachment_bundle: UserAttachmentBundle | None = None,
) -> Turn2UserContent:
    """Turn2 user 블록 조립 (로그 text / API multimodal 분리)."""
    ctx = attachment_context or AttachmentContext.from_bundle(attachment_bundle)
    att = build_turn2_attachment_content(attachment_policy, ctx, query_stripped)

    parts: list[str] = []
    mem = (memory_context or "").strip()
    if mem:
        parts.append(mem)
    for block in prefix_blocks or []:
        b = (block or "").strip()
        if b:
            parts.append(b)
    parts.append(f"<user_question>\n{query_stripped}\n</user_question>")
    if att.text.strip():
        parts.append(att.text.strip())
    for block in suffix_blocks or []:
        b = (block or "").strip()
        if b:
            parts.append(b)

    return Turn2UserContent(text="\n\n".join(parts), vision_paths=att.vision_paths)


def parse_attachment_policy_dict(data: dict[str, Any]) -> AttachmentPolicy:
    mode = str(data.get("mode") or data.get("attachment_mode") or "general").strip().lower()
    if mode not in ("general", "doc_based", "image_based", "hybrid", "uncertain"):
        mode = "uncertain"
    ids = data.get("include_attachment_ids") or []
    if isinstance(ids, str):
        ids = [x.strip() for x in ids.split(",") if x.strip()]

    include_doc_chunks = data.get("include_doc_chunks")
    include_images = data.get("include_images")
    if include_doc_chunks is None and "include_full_text" in data:
        include_doc_chunks = bool(data.get("include_full_text"))
    if include_doc_chunks is None:
        include_doc_chunks = mode in ("doc_based", "hybrid", "uncertain")
    if include_images is None:
        include_images = mode in ("image_based", "hybrid", "uncertain")

    return AttachmentPolicy(
        mode=mode,  # type: ignore[arg-type]
        include_attachment_ids=[str(x) for x in ids if x],
        include_doc_chunks=bool(include_doc_chunks),
        include_images=bool(include_images),
        reason=str(data.get("reason") or "")[:200],
    )


def parse_attachment_policy_from_content(content: str | None) -> AttachmentPolicy | None:
    text = (content or "").strip()
    if not text:
        return None
    for tag in ("attachment_policy", "attachment_routing"):
        match = re.search(
            rf"<{tag}>\s*(\{{.*?\}})\s*</{tag}>",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        if match:
            try:
                data = json.loads(match.group(1))
                if isinstance(data, dict):
                    return parse_attachment_policy_dict(data)
            except json.JSONDecodeError:
                continue
    if text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict) and (data.get("mode") or data.get("attachment_mode")):
                return parse_attachment_policy_dict(data)
        except json.JSONDecodeError:
            pass
    return None


parse_attachment_plan_from_tool_args = parse_attachment_policy_dict
parse_attachment_plan_from_content = parse_attachment_policy_from_content


def infer_attachment_policy(
    query: str,
    bundle: UserAttachmentBundle | None,
) -> AttachmentPolicy:
    if not bundle or not bundle.has_content:
        return AttachmentPolicy(mode="general")

    has_doc = any(i.kind == "document" for i in bundle.items)
    has_img = any(i.kind == "image" for i in bundle.items)
    q = query or ""

    if not q.strip() or q.strip() == "첨부 파일 내용을 참고해 주세요.":
        mode: AttachmentMode = (
            "hybrid" if (has_doc and has_img)
            else ("image_based" if has_img else "doc_based")
        )
    elif (_DEICTIC_HINTS.search(q) or re.search(r"관련|페이지|어디", q)) and (has_img or has_doc):
        mode = (
            "hybrid" if (has_img and has_doc)
            else ("image_based" if has_img else "doc_based")
        )
    elif _IMAGE_HINTS.search(q) and has_img:
        mode = "image_based"
    elif _DOC_HINTS.search(q) and has_doc:
        mode = "doc_based"
    elif has_doc and has_img:
        mode = "hybrid"
    elif has_img:
        mode = "image_based"
    elif has_doc:
        mode = "doc_based"
    elif not _IMAGE_HINTS.search(q) and not _DOC_HINTS.search(q):
        return AttachmentPolicy(mode="general", reason="질문이 첨부 내용과 무관")
    else:
        mode = "uncertain"

    return AttachmentPolicy(
        mode=mode,
        include_attachment_ids=bundle.item_ids(),
        include_doc_chunks=mode in ("doc_based", "hybrid", "uncertain"),
        include_images=mode in ("image_based", "hybrid", "uncertain"),
        reason="fallback inference",
    )


infer_attachment_plan = infer_attachment_policy


def resolve_attachment_policy(
    content: str | None,
    query: str,
    bundle: UserAttachmentBundle | None,
) -> AttachmentPolicy:
    parsed = parse_attachment_policy_from_content(content)
    if parsed:
        if not parsed.include_attachment_ids and bundle:
            parsed.include_attachment_ids = bundle.item_ids()
        return parsed
    return infer_attachment_policy(query, bundle)


resolve_attachment_plan = resolve_attachment_policy

BUSINESS_TOOL_NAMES = frozenset({
    "search_company_wiki",
    "query_gov_projects",
    "search_worker_schedule",
    "manage_room_schedule",
    "archive_expense_attachment",
    "respond_general",
})


def filter_business_tool_calls(tool_calls: list[Any]) -> list[Any]:
    """attachment_policy용 레거시 tool 호출 제외."""
    filtered = []
    for tc in tool_calls or []:
        name = getattr(getattr(tc, "function", None), "name", "") or ""
        if name == "route_user_attachments":
            continue
        if name in BUSINESS_TOOL_NAMES or not name:
            filtered.append(tc)
    return filtered


async def ingest_slack_files(
    files: list[dict[str, Any]] | None,
    bot_token: str,
    *,
    session_id: str = "",
    submitter: SubmitterInfo | None = None,
    user_text: str = "",
) -> UserAttachmentBundle:
    bundle = UserAttachmentBundle(
        session_id=session_id,
        submitter=submitter or SubmitterInfo(),
    )
    if not files or not bot_token:
        return bundle

    CHAT_ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    session_dir = CHAT_ATTACHMENTS_DIR / (session_id or "unknown")
    session_dir.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(timeout=120.0) as client:
        for file_obj in files:
            slack_file_id = str(file_obj.get("id") or file_obj.get("name") or "")
            downloaded = await _download_slack_file(client, file_obj, bot_token)
            if not downloaded:
                continue
            filename, raw = downloaded
            att_id = _stable_attachment_id(session_id, slack_file_id, filename)
            storage_dir = session_dir / att_id
            storage_dir.mkdir(parents=True, exist_ok=True)

            if is_image_file(file_obj):
                mime = str(file_obj.get("mimetype") or "")
                display_path = save_image_assets(
                    storage_dir, raw, filename=filename, mimetype=mime,
                )
                manifest = await asyncio.to_thread(
                    _enrich_manifest_image,
                    filename,
                    display_path,
                    user_text=user_text,
                )
                (storage_dir / "caption.txt").write_text(
                    str(manifest.get("caption") or manifest.get("one_line") or filename),
                    encoding="utf-8",
                )
                kind: Literal["document", "image"] = "image"
                storage_relpath = _rel_storage_path(display_path)
            elif _is_document_file(file_obj):
                ext = Path(filename).suffix.lower()
                original_path = storage_dir / f"original{ext or '.bin'}"
                original_path.write_bytes(raw)
                parser = get_parser(ext)
                if parser is None:
                    continue
                try:
                    parsed = parser.parse(original_path)
                    text = _parsed_text_to_str(parsed)
                except Exception as exc:
                    logger.warning("첨부 파싱 실패 (%s): %s", filename, exc)
                    continue
                if not text.strip():
                    continue
                full_text = _truncate(text, CHAT_ATTACHMENT_MAX_CHARS)
                (storage_dir / "text_full.txt").write_text(full_text, encoding="utf-8")
                _write_chunks(full_text, storage_dir / "text_chunks.jsonl")
                manifest = _enrich_manifest_document(filename, full_text)
                kind = "document"
                storage_relpath = _rel_storage_path(original_path)
            else:
                logger.info("지원하지 않는 Slack 첨부: %s", filename)
                continue

            manifest.update({
                "attachment_id": att_id,
                "filename": filename,
                "kind": kind,
                "session_id": session_id,
            })
            (storage_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            bundle.items.append(
                AttachmentItem(
                    attachment_id=att_id,
                    filename=filename,
                    kind=kind,
                    storage_dir=storage_dir,
                    storage_relpath=storage_relpath,
                    slack_file_id=slack_file_id,
                    manifest=manifest,
                )
            )

    return bundle


# 하위 호환
UserAttachmentContext = UserAttachmentBundle
process_slack_files = ingest_slack_files
