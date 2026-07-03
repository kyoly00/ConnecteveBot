# -*- coding: utf-8 -*-
"""
경비 증빙 첨부 → OneDrive 분류 업로드 (Microsoft Graph).

Path addressing: /users/{upn}/drive/root:/{folder}/{date}/{user}/{title}:/content
https://learn.microsoft.com/graph/onedrive-addressing-driveitems
"""

from __future__ import annotations

import logging
import mimetypes
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

_CONN_BOT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _CONN_BOT_DIR.parent
for _p in (_CONN_BOT_DIR, _REPO_ROOT):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

from app.core.config import (
    EXPENSE_ARCHIVE_FOLDERS,
    EXPENSE_ONEDRIVE_BASE_FOLDER,
    EXPENSE_ONEDRIVE_USER,
)
from app.services.outlook_room.ms_graph_room import get_valid_app_token
from app.services.slack_attachments import AttachmentItem, UserAttachmentBundle

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_KST = ZoneInfo("Asia/Seoul")
_ONEDRIVE_RESERVED_RE = re.compile(r'[/\\*<>?:|"#%]')


def encode_drive_path(relative_path: str) -> str:
    """Graph path segment별 percent-encoding (전체 URL 한 번에 인코딩 금지)."""
    parts = relative_path.replace("\\", "/").strip("/").split("/")
    return "/".join(quote(part, safe="") for part in parts if part)


def resolve_folder_dir(category: str) -> str:
    """category 코드 → OneDrive 상대 폴더 경로."""
    key = (category or "").strip()
    entry = EXPENSE_ARCHIVE_FOLDERS.get(key)
    if not entry:
        raise ValueError(f"지원하지 않는 category: {category}")
    folder_name = entry[0]
    if EXPENSE_ONEDRIVE_BASE_FOLDER:
        return f"{EXPENSE_ONEDRIVE_BASE_FOLDER}/{folder_name}"
    return folder_name


def _sanitize_path_segment(name: str, *, max_len: int = 80, fallback: str = "untitled") -> str:
    s = (name or "").strip()
    s = _ONEDRIVE_RESERVED_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip().strip(".")
    if not s or s.lower() in {"untitled", "attachment", "unknown"}:
        s = fallback
    if len(s) > max_len:
        s = s[:max_len].rstrip()
    return s or fallback


def _document_title(item: AttachmentItem) -> str:
    manifest = item.manifest or {}
    for key in ("one_line", "title", "caption"):
        raw = str(manifest.get(key) or "").strip()
        if not raw:
            continue
        if raw == item.filename:
            continue
        cleaned = _sanitize_path_segment(raw, max_len=120, fallback="")
        if cleaned:
            return cleaned
    stem = Path(item.filename).stem or "attachment"
    return _sanitize_path_segment(stem, fallback="attachment")


def _build_upload_relative_path(
    folder_dir: str,
    item: AttachmentItem,
    bundle: UserAttachmentBundle,
) -> str:
    """
    {category}/{YYYY-MM-DD}/{submitter_name}/{document_title}{ext}
    예: 01_Invoices/2026-06-25/OOO/Gemini 구독 Invoice.pdf
    """
    date_seg = datetime.now(_KST).strftime("%Y-%m-%d")
    user_seg = _sanitize_path_segment(
        bundle.submitter.name or "unknown",
        max_len=40,
        fallback="unknown",
    )
    title_stem = _document_title(item)
    ext = Path(item.filename).suffix
    if not ext:
        path = item.primary_file_path()
        if path:
            ext = path.suffix
    file_name = f"{title_stem}{ext}" if ext else title_stem
    return f"{folder_dir}/{date_seg}/{user_seg}/{file_name}"


def upload_bytes_to_onedrive(
    *,
    user_upn: str,
    relative_path: str,
    content: bytes,
    content_type: str = "application/octet-stream",
) -> dict[str, Any]:
    """PUT .../drive/root:/{path}:/content"""
    upn = quote(user_upn.strip(), safe="@.")
    encoded_path = encode_drive_path(relative_path)
    url = f"{GRAPH_BASE}/users/{upn}/drive/root:/{encoded_path}:/content"
    token = get_valid_app_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": content_type,
    }
    res = requests.put(url, headers=headers, data=content, timeout=120)
    if res.status_code >= 400:
        logger.error(
            "OneDrive upload failed status=%s body=%s",
            res.status_code,
            res.text[:500],
        )
        res.raise_for_status()
    return res.json() if res.content else {"ok": True}


def _guess_mime(path: Path, filename: str) -> str:
    mime, _ = mimetypes.guess_type(filename)
    if mime:
        return mime
    if path.suffix.lower() in (".jpg", ".jpeg"):
        return "image/jpeg"
    if path.suffix.lower() == ".png":
        return "image/png"
    if path.suffix.lower() == ".pdf":
        return "application/pdf"
    return "application/octet-stream"


def archive_attachments_to_onedrive(
    *,
    category: str,
    attachment_ids: list[str],
    bundle: UserAttachmentBundle | None,
    reason: str = "",
) -> tuple[str, list[dict[str, Any]]]:
    """
    첨부 파일을 OneDrive 분류 폴더에 업로드.

    Returns:
        (user_facing_message, upload_results)
    """
    if not EXPENSE_ONEDRIVE_USER:
        return (
            "OneDrive 업로드가 설정되지 않았습니다. EXPENSE_ONEDRIVE_USER 환경변수를 확인해 주세요.",
            [],
        )
    if not bundle or not bundle.has_content:
        return ("업로드할 Slack 첨부가 없습니다.", [])

    try:
        folder_dir = resolve_folder_dir(category)
    except ValueError as exc:
        return (str(exc), [])

    ids = [str(x).strip() for x in attachment_ids if str(x).strip()] or bundle.item_ids()
    results: list[dict[str, Any]] = []
    errors: list[str] = []

    for att_id in ids:
        item = bundle.get_item(att_id)
        if not item:
            errors.append(f"{att_id}: 세션에서 찾을 수 없음")
            continue
        path = item.primary_file_path()
        if not path or not path.is_file():
            errors.append(f"{att_id}: 로컬 파일 없음")
            continue
        rel_path = _build_upload_relative_path(folder_dir, item, bundle)
        try:
            raw = path.read_bytes()
            meta = upload_bytes_to_onedrive(
                user_upn=EXPENSE_ONEDRIVE_USER,
                relative_path=rel_path,
                content=raw,
                content_type=_guess_mime(path, item.filename),
            )
            web_url = str(meta.get("webUrl") or "")
            results.append({
                "attachment_id": att_id,
                "filename": item.filename,
                "upload_path": rel_path,
                "folder": folder_dir,
                "category": category,
                "web_url": web_url,
            })
            logger.info(
                "[expense] uploaded %s → %s (%s bytes)",
                att_id,
                rel_path,
                len(raw),
            )
        except Exception as exc:
            logger.warning("[expense] upload failed %s: %s", att_id, exc)
            errors.append(f"{att_id}: {exc}")

    if not results and errors:
        return ("OneDrive 업로드에 실패했습니다.\n" + "\n".join(f"- {e}" for e in errors), [])

    lines = [
        f"OneDrive `{folder_dir}`에 {len(results)}건 업로드했습니다.",
    ]
    if reason.strip():
        lines.append(f"분류 근거: {reason.strip()}")
    for row in results:
        url_part = f" ({row['web_url']})" if row.get("web_url") else ""
        lines.append(f"- {row['filename']} → {row['upload_path']}{url_part}")
    if errors:
        lines.append("실패:")
        lines.extend(f"- {e}" for e in errors)
    return ("\n".join(lines), results)
