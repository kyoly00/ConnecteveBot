"""
Confluence 웹훅 → PolicyPage/첨부/RAG registry/Elasticsearch 증분 동기화.

파이프라인 (page_created / page_updated):
  1_Confluence_RestAPI_crawlingAll → 2_filtering_attachment → 3_attachment_download
  → 4_describe_attachments → 6_rag_builder → 7_post_process_tree_hybrid → 8_build_ES_tree_hybrid

웹훅: page_created(version=1 추론), 디바운스, 첨부필터(storage/id)
  - 첨부 단계 실패 시 본문 RAG/ES 계속 (CONFLUENCE_WEBHOOK_CONTINUE_ON_ATTACHMENT_ERROR, 기본 true)
  - post_process 생략: CONFLUENCE_WEBHOOK_SKIP_POST_PROCESS=true
삭제: 웹훅 미사용 — Playwright viewtrash 폴링
  - 증분: CONFLUENCE_TRASH_POLL_INTERVAL_SEC(기본 3600s), 상위 TOP_N, diff 후 검증·삭제
  - 전체: CONFLUENCE_TRASH_POLL_FULL_SCAN_INTERVAL_SEC(기본 86400s), registry∩trash, 검증·삭제
  - 검증: get_page_content → 404 또는 status=trashed 만 remove_page_artifacts (current 는 스킵)

웹훅(생성/수정): body.updateTrigger(edit_page→page_updated) 또는 body.event.
CONFLUENCE_WEBHOOK_SPACE_KEYS(기본 CW) 스페이스만 동기화. 삭제 이벤트는 ignored.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import shutil
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from app.core.config import (
    ATTACHMENTS_DIR,
    ADDED_ATTACHMENTS_DIR,
    CONFLUENCE_SPACE_KEY,
    DATA_DIR,
    DEBUG_DIR,
    DESC_DIR,
    ES_INDEX_NAME,
    METADATA_DIR,
    POLICY_PAGE_DIR,
    PROJECT_ROOT,
    TREE_REGISTRY_DIR,
    page_title_excluded
)

logger = logging.getLogger(__name__)

PROJECT_DIR = PROJECT_ROOT  # 하위 호환
REGISTRY_DIR = TREE_REGISTRY_DIR
VECTOR_JSONL = REGISTRY_DIR / "rag_tree_hybrid_vector_payload.jsonl"
PARENT_JSONL = REGISTRY_DIR / "rag_tree_hybrid_parent_store.jsonl"
CHECKPOINT_JSON = REGISTRY_DIR / "rag_tree_hybrid_registry_checkpoint.json"
LOCAL_PARSE_DEBUG_DIR = DEBUG_DIR / "added_attachment_parse"
TRASH_POLL_STATE_JSON = DATA_DIR / ".trash_poll_state.json"

# 웹훅으로 삭제 처리하지 않음 (버그/오인 방지 → Trash 폴링만 사용)
WEBHOOK_HANDLES_PAGE_REMOVED = os.getenv(
    "CONFLUENCE_WEBHOOK_HANDLE_REMOVED", "false"
).lower() in ("1", "true", "yes")

WEBHOOK_CONTINUE_ON_ATTACHMENT_ERROR = os.getenv(
    "CONFLUENCE_WEBHOOK_CONTINUE_ON_ATTACHMENT_ERROR", "true"
).lower() in ("1", "true", "yes")
WEBHOOK_SKIP_POST_PROCESS = os.getenv(
    "CONFLUENCE_WEBHOOK_SKIP_POST_PROCESS", "false"
).lower() in ("1", "true", "yes")

_sync_lock = threading.Lock()
_module_cache: dict[str, Any] = {}

# 웹훅 디바운스: 동일 page_id 연속 수신 시 마지막 version만 1회 동기화
WEBHOOK_DEBOUNCE_SEC = int(os.getenv("CONFLUENCE_WEBHOOK_DEBOUNCE_SEC", "45") or "45")
_webhook_debounce_lock = threading.Lock()
_pending_webhook_sync: dict[str, dict[str, Any]] = {}


def _load_script_module(module_name: str, filename: str) -> Any:
    """숫자 접두 스크립트(1_*.py 등)를 importlib로 로드."""
    if module_name in _module_cache:
        return _module_cache[module_name]

    path = PROJECT_DIR / "scripts" / filename
    if not path.is_file():
        raise FileNotFoundError(f"파이프라인 스크립트 없음: {path}")

    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"모듈 spec 생성 실패: {path}")

    mod = importlib.util.module_from_spec(spec)
    # dataclass/typing 초기화 중 sys.modules 조회가 필요하므로 먼저 등록
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    _module_cache[module_name] = mod
    return mod


def _safe_title_slug(title: str) -> str:
    title_context = (title or "untitled").replace(" ", "_").replace("/", "_")
    return "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in title_context)


def find_policy_json(page_id: str) -> Optional[Path]:
    """page_id에 해당하는 PolicyPage JSON 경로."""
    pid = str(page_id)
    for path in POLICY_PAGE_DIR.glob("confluence_page_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(data.get("page", {}).get("id")) == pid:
            return path
    return None


def find_all_metadata_json(page_id: str) -> list[Path]:
    """page_id에 해당하는 metadata JSON 경로 목록."""
    pid = str(page_id)
    matches: list[Path] = []
    for path in METADATA_DIR.glob("*.metadata.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(data.get("page", {}).get("id")) == pid:
            matches.append(path)
    return matches


def find_metadata_json(page_id: str) -> Optional[Path]:
    """page_id에 해당하는 metadata JSON (동일 id 여러 개면 최신 수정본)."""
    matches = find_all_metadata_json(page_id)
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def cleanup_metadata_by_page_id(page_id: str, keep: Optional[Path] = None) -> int:
    """
    page_id에 속한 metadata JSON 중 keep을 제외하고 삭제.
    제목 변경 시 {page_id}_{구제목}.metadata.json 등 중복 파일 정리용.
    keep 미지정 시 해당 page_id metadata 전부 삭제(page_removed 등).
    """
    pid = str(page_id)
    removed = 0
    METADATA_DIR.mkdir(parents=True, exist_ok=True)

    for path in METADATA_DIR.glob("*.metadata.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(data.get("page", {}).get("id")) != pid:
            continue
        if keep and path.resolve() == keep.resolve():
            continue
        path.unlink(missing_ok=True)
        removed += 1
        logger.info("구 metadata 삭제: %s", path.name)

    if removed:
        logger.info("metadata 정리 완료: page_id=%s removed=%d keep=%s", pid, removed, keep)
    return removed

# ── 1. Confluence 크롤 (단일 페이지) ─────────────────────────────────────────


def crawl_single_page(page_id: str) -> Path:
    """웹훅 대상 페이지만 Confluence API로 fetch 후 PolicyPage JSON 저장."""
    crawl = _load_script_module("confluence_crawl", "1_Confluence_RestAPI_crawlingAll.py")
    from urllib.parse import urljoin

    pid = str(page_id)
    POLICY_PAGE_DIR.mkdir(parents=True, exist_ok=True)

    page = crawl.get_page_content(pid)
    children = crawl.get_child_pages(pid)
    attachments = crawl.get_attachments(pid)

    result = {
        "page": {
            "id": page.get("id"),
            "title": page.get("title"),
            "type": page.get("type"),
            "status": page.get("status"),
            "space": page.get("space", {}),
            "version": page.get("version", {}),
            "ancestors": [
                {"id": a.get("id"), "title": a.get("title"), "type": a.get("type")}
                for a in page.get("ancestors", [])
            ],
            "labels": page.get("metadata", {}).get("labels", {}).get("results", []),
            "body_storage_html": page.get("body", {}).get("storage", {}).get("value"),
            "body_view_html": page.get("body", {}).get("view", {}).get("value"),
            "body_export_view_html": page.get("body", {}).get("export_view", {}).get("value"),
        },
        "children": [
            {
                "id": c.get("id"),
                "title": c.get("title"),
                "type": c.get("type"),
                "status": c.get("status"),
                "space": c.get("space", {}),
                "version": c.get("version", {}),
                "webui": urljoin(crawl.BASE_URL, c.get("_links", {}).get("webui", "")),
            }
            for c in children
        ],
        "attachments": [
            norm
            for att in attachments
            if (norm := crawl.normalize_attachment(att)) is not None
        ],
    }

    existing = find_policy_json(pid)  # page_id 기준으로 기존 파일 찾기
    safe_title = _safe_title_slug(page.get("title", "untitled"))
    new_path = POLICY_PAGE_DIR / f"confluence_page_{safe_title}.json"

    if existing and existing.exists() and existing.resolve() != new_path.resolve():
        existing.unlink()
        logger.info("구 PolicyPage 삭제(제목 변경): %s", existing.name)

    new_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info(
        "PolicyPage 저장: page_id=%s title=%s children=%d attachments=%d → %s",
        pid,
        page.get("title"),
        len(children),
        len(result["attachments"]),
        new_path.name,
    )
    return new_path


# ── 2. 첨부 필터 (단일 파일) ─────────────────────────────────────────────────


def _page_body_text_blobs(page_data: dict[str, Any]) -> list[str]:
    """PolicyPage page 필드에서 본문 HTML/storage 텍스트 수집."""
    blobs: list[str] = []
    for key in ("body_view_html", "body_storage_html", "body_export_view_html"):
        raw = page_data.get(key)
        if raw:
            blobs.append(str(raw))
    return blobs


def _attachment_referenced_in_body(att: dict[str, Any], body_blobs: list[str]) -> bool:
    """
    본문(storage/view)에 첨부가 참조됐는지 판별.
    - 파일명 문자열
    - attachment id (ri:attachment-id, data-linked-resource-id 등)
    - URL 인코딩 파일명
    """
    if not body_blobs:
        return False
    combined = "\n".join(body_blobs)
    title = str(att.get("title") or "").strip()
    att_id = str(att.get("id") or "").strip()

    if title and title in combined:
        return True
    if att_id and att_id in combined:
        return True
    if title:
        encoded = quote(title, safe="")
        if encoded != title and encoded in combined:
            return True
        stem = Path(title).stem
        if len(stem) >= 5 and stem in combined:
            return True
        if f'ri:filename="{title}"' in combined or f"ri:filename='{title}'" in combined:
            return True
    download_url = str(att.get("download_url") or "")
    if download_url and download_url in combined:
        return True
    return False


def filter_attachments_for_page(policy_path: Path) -> None:
    """본문에 참조되지 않은 첨부 노이즈 제거 (파일명 + storage 매크로/id)."""
    try:
        data = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("첨부 필터 스킵 (JSON 읽기 실패): %s", e)
        return

    page_data = data.get("page", {})
    attachments = data.get("attachments", [])
    body_blobs = _page_body_text_blobs(page_data)

    if not attachments:
        return
    if not body_blobs:
        logger.warning("첨부 필터 스킵 (본문 HTML 없음): %s", policy_path.name)
        return

    filtered = []
    removed = 0
    for att in attachments:
        if _attachment_referenced_in_body(att, body_blobs):
            filtered.append(att)
        else:
            removed += 1

    if removed > 0:
        data["attachments"] = filtered
        policy_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("첨부 필터: %s — %d개 제거, %d개 유지", policy_path.name, removed, len(filtered))


# ── 3. 첨부 다운로드 + metadata ──────────────────────────────────────────────


def sync_attachments_for_page(policy_path: Path) -> None:
    """3_attachment_download: metadata 생성 및 Playwright 일괄 다운로드."""
    dl = _load_script_module("attachment_download", "3_attachment_download.py")
    dl.global_download_jobs.clear()
    dl.process_policy_page_metadata(str(policy_path))
    if dl.global_download_jobs:
        dl.download_attachment_with_playwright(list(dl.global_download_jobs))


# ── 4. 첨부 VLM/GPT 설명 ─────────────────────────────────────────────────────


def describe_attachments_for_page(page_id: str) -> None:
    """4_describe_attachments: 해당 page metadata 1건만 처리."""
    meta_path = find_metadata_json(page_id)
    if not meta_path:
        logger.info("metadata 없음 — 첨부 설명 스킵: page_id=%s", page_id)
        return

    desc = _load_script_module("describe_attachments", "4_describe_attachments.py")
    from parsers.base import get_parser

    refiner = desc.GPTRefiner()
    save_dir = DESC_DIR
    save_dir.mkdir(parents=True, exist_ok=True)
    debug_log_path = save_dir / "debug.jsonl"
    log = desc.make_debug_logger(debug_log_path)

    log({
        "ts": desc._ts(),
        "step": "webhook_single_page",
        "page_id": str(page_id),
        "metadata_file": meta_path.name,
    })

    with open(meta_path, "r", encoding="utf-8") as f:
        meta_data = json.load(f)

    page = meta_data.get("page", {})
    page_title = page.get("title")
    pid = str(page.get("id") or page_id)
    attachments = meta_data.get("attachments", [])
    save_path = save_dir / f"result_{pid}.json"

    cached_map: dict[tuple[str, str], dict] = {}
    results: list[dict] = []
    if save_path.exists():
        try:
            cached_data = json.loads(save_path.read_text(encoding="utf-8"))
            for item in cached_data.get("results", []) or []:
                if not isinstance(item, dict):
                    continue
                key = (str(item.get("file_name") or ""), str(item.get("extension") or "").lower())
                if key != ("", ""):
                    cached_map[key] = item
            results = [v for v in cached_map.values() if v.get("refined")]
        except Exception as e:
            logger.warning("첨부 설명 캐시 로드 실패: %s", e)

    for att in attachments:
        ext = str(att.get("extension", "")).lower()
        title = att.get("title", "unknown")
        saved_path = att.get("saved_path", "")
        file_path = PROJECT_DIR / saved_path.replace("\\", "/")

        cache_key = (str(title or ""), ext)
        cached_entry = cached_map.get(cache_key)
        if cached_entry and cached_entry.get("refined"):
            continue
        if not file_path.exists():
            continue

        file_res = {"file_name": title, "extension": ext, "raw": None, "refined": None}
        refiner_meta = {
            "page_title": page_title,
            "page_id": pid,
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
                if not parser:
                    continue
                parsed_result = parser.parse(file_path)
                parsed_text = parsed_result.text or ""
                file_res["raw"] = parsed_text
                if refiner and parsed_text.strip():
                    file_res["refined"] = refiner.refine(
                        refiner_meta,
                        extracted_text=parsed_text,
                        log_fn=log,
                    )
            elif refiner:
                file_res["refined"] = refiner.refine(
                    refiner_meta,
                    image_path=file_path,
                    log_fn=log,
                )

            results.append(file_res)
            save_path.write_text(
                json.dumps(
                    {"page_title": page_title, "page_id": pid, "results": results},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error("첨부 설명 실패 (%s): %s", title, e)


# ── 5. RAG registry (단일 page) ──────────────────────────────────────────────


def purge_rag_registry_for_page(page_id: str) -> None:
    """제외 페이지 registry JSONL에서 group_id 제거."""
    rag = _load_script_module("rag_builder", "6_rag_builder.py")
    pid = str(page_id)
    all_vector = rag.read_jsonl(rag.VECTOR_JSONL)
    all_parent = rag.read_jsonl(rag.PARENT_JSONL)
    rag.write_jsonl(rag.VECTOR_JSONL, [r for r in all_vector if str(r.get("group_id")) != pid])
    rag.write_jsonl(rag.PARENT_JSONL, [r for r in all_parent if str(r.get("group_id")) != pid])


def _title_from_policy_json(policy_path: Path) -> str:
    try:
        data = json.loads(policy_path.read_text(encoding="utf-8"))
        page = data.get("page") if isinstance(data.get("page"), dict) else {}
        return str(page.get("title") or "").strip()
    except Exception:
        return ""


def build_rag_for_page(page_id: str) -> None:
    """6_rag_builder: 해당 page_id만 registry JSONL 갱신."""
    rag = _load_script_module("rag_builder", "6_rag_builder.py")
    pid = str(page_id)
    pages = {p.page_id: p for p in rag.load_pages()}
    page = pages.get(pid)
    if not page:
        raise ValueError(f"RAG 대상 페이지를 찾을 수 없음 (PolicyPage/metadata 확인): {pid}")

    llm = rag.LLMClient()
    parents, audit = rag.build_page_chunks(page, llm)
    v_new, p_new = rag.page_to_rows(page, parents)

    all_vector = rag.read_jsonl(rag.VECTOR_JSONL)
    all_parent = rag.read_jsonl(rag.PARENT_JSONL)
    all_vector = [r for r in all_vector if str(r.get("group_id")) != pid] + v_new
    all_parent = [r for r in all_parent if str(r.get("group_id")) != pid] + p_new
    rag.write_jsonl(rag.VECTOR_JSONL, all_vector)
    rag.write_jsonl(rag.PARENT_JSONL, all_parent)

    cp = rag.read_json(rag.CHECKPOINT_JSON) if rag.CHECKPOINT_JSON.exists() else {}
    completed = set(str(x) for x in cp.get("completed_page_ids", []))
    failed = set(str(x) for x in cp.get("failed_page_ids", []))
    page_stats: dict[str, Any] = dict(cp.get("page_stats", {}))
    errors: list[dict] = list(cp.get("errors", []))

    completed.add(pid)
    failed.discard(pid)
    page_stats[pid] = {
        "page_id": pid,
        "page_title": page.page_title,
        "title_path": page.meta.title_path,
        **audit,
        "vector_rows": len(v_new),
        "parent_rows": len(p_new),
        "source": "confluence_webhook",
    }
    rag.write_json(
        rag.CHECKPOINT_JSON,
        {
            "completed_page_ids": sorted(completed),
            "failed_page_ids": sorted(failed),
            "page_stats": page_stats,
            "errors": errors,
            "updated_at": rag.now_iso(),
        },
    )
    llm.save_cache()
    logger.info("RAG registry 갱신: page_id=%s vector=%d parent=%d", pid, len(v_new), len(p_new))


# ── 6. Elasticsearch (단일 page) ─────────────────────────────────────────────


def delete_es_docs_by_page_id(page_id: str) -> int:
    """ES에서 page_id에 해당하는 문서 삭제."""
    es_mod = _load_script_module("es_tree_hybrid", "8_build_ES_tree_hybrid.py")
    es = es_mod.es
    pid = str(page_id)

    if not es.indices.exists(index=ES_INDEX_NAME):
        return 0

    resp = es.delete_by_query(
        index=ES_INDEX_NAME,
        body={"query": {"term": {"page_id": pid}}},
        conflicts="proceed",
        refresh=True,
    )
    deleted = int(resp.get("deleted", 0))
    logger.info("ES 삭제: page_id=%s deleted=%d", pid, deleted)
    return deleted


def upload_page_to_es(page_id: str) -> int:
    """registry JSONL에서 해당 page 청크만 임베딩 후 ES 업로드."""
    import numpy as np
    from elasticsearch import helpers

    es_mod = _load_script_module("es_tree_hybrid", "8_build_ES_tree_hybrid.py")
    es = es_mod.es
    pid = str(page_id)

    vector_raws = [
        r for r in es_mod.load_jsonl(es_mod.VECTOR_JSONL_PATH)
        if str(r.get("group_id")) == pid
    ]
    parent_raws = [
        r for r in es_mod.load_jsonl(es_mod.PARENT_STORE_JSONL_PATH)
        if str(r.get("group_id")) == pid
    ]

    if not vector_raws and not parent_raws:
        logger.warning("ES 업로드 스킵 — registry에 청크 없음: page_id=%s", pid)
        return 0

    embedder = es_mod.get_embedder()
    vector_dim = embedder.get_sentence_embedding_dimension()
    es_mod.create_es_index(es, ES_INDEX_NAME, vector_dim=vector_dim, force_rebuild=False)

    embed_docs: list[dict[str, Any]] = []
    store_docs: list[dict[str, Any]] = []

    for raw in vector_raws:
        embed_docs.append(es_mod.prepare_doc(raw))
    for raw in parent_raws:
        doc = es_mod.prepare_doc(raw)
        doc["index_for_embedding"] = False
        store_docs.append(doc)

    all_docs = embed_docs + store_docs
    batch_size = es_mod.EMBEDDING_BATCH_SIZE

    for start in range(0, len(all_docs), batch_size):
        batch = all_docs[start : start + batch_size]
        to_embed: list[tuple[int, dict[str, Any]]] = []
        for i, d in enumerate(batch):
            ct = str(d.get("chunk_type") or "")
            is_store = ct in es_mod._STORE_ONLY_CHUNK_TYPES or d.get("index_for_embedding") is False
            if not is_store and d.get("embedding_text"):
                to_embed.append((i, d))

        if to_embed:
            texts = [d["embedding_text"] for _, d in to_embed]
            vectors = embedder.encode(
                texts,
                batch_size=batch_size,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            vectors = np.asarray(vectors, dtype=np.float32)
            for (_, d), vec in zip(to_embed, vectors):
                d["embedding"] = vec.tolist()

        actions = []
        for d in batch:
            action: dict[str, Any] = {
                "_op_type": "index",
                "_index": ES_INDEX_NAME,
                "_source": d,
            }
            if d.get("id"):
                action["_id"] = d["id"]
            actions.append(action)

        helpers.bulk(es, actions, raise_on_error=False)

    es.indices.refresh(index=ES_INDEX_NAME)
    logger.info("ES 업로드 완료: page_id=%s docs=%d", pid, len(all_docs))
    return len(all_docs)


# ── page_removed 정리 ────────────────────────────────────────────────────────


def remove_page_artifacts(page_id: str) -> None:
    """로컬 PolicyPage/metadata/첨부/설명/registry/ES에서 page 제거."""
    pid = str(page_id)

    policy_path = find_policy_json(pid)
    if policy_path and policy_path.exists():
        policy_path.unlink()
        logger.info("삭제: %s", policy_path)

    cleanup_metadata_by_page_id(pid)

    att_dir = ATTACHMENTS_DIR / pid
    if att_dir.is_dir():
        shutil.rmtree(att_dir, ignore_errors=True)
        logger.info("삭제: %s", att_dir)

    added_att_dir = ADDED_ATTACHMENTS_DIR / pid
    if added_att_dir.is_dir():
        shutil.rmtree(added_att_dir, ignore_errors=True)
        logger.info("삭제: %s", added_att_dir)

    desc_path = DESC_DIR / f"result_{pid}.json"
    if desc_path.exists():
        desc_path.unlink()
        logger.info("삭제: %s", desc_path)

    rag = _load_script_module("rag_builder", "6_rag_builder.py")
    all_vector = [r for r in rag.read_jsonl(VECTOR_JSONL) if str(r.get("group_id")) != pid]
    all_parent = [r for r in rag.read_jsonl(PARENT_JSONL) if str(r.get("group_id")) != pid]
    rag.write_jsonl(VECTOR_JSONL, all_vector)
    rag.write_jsonl(PARENT_JSONL, all_parent)

    if CHECKPOINT_JSON.exists():
        cp = rag.read_json(CHECKPOINT_JSON)
        completed = set(str(x) for x in cp.get("completed_page_ids", []))
        failed = set(str(x) for x in cp.get("failed_page_ids", []))
        page_stats = dict(cp.get("page_stats", {}))
        completed.discard(pid)
        failed.discard(pid)
        page_stats.pop(pid, None)
        rag.write_json(
            CHECKPOINT_JSON,
            {
                **cp,
                "completed_page_ids": sorted(completed),
                "failed_page_ids": sorted(failed),
                "page_stats": page_stats,
                "updated_at": rag.now_iso(),
            },
        )

    delete_es_docs_by_page_id(pid)


def _local_registry_page_ids() -> set[str]:
    """registry JSONL에 등록된 group_id(page_id) 집합."""
    rag = _load_script_module("rag_builder", "6_rag_builder.py")
    ids: set[str] = set()
    for path in (VECTOR_JSONL, PARENT_JSONL):
        for row in rag.read_jsonl(path):
            gid = row.get("group_id")
            if gid:
                ids.add(str(gid))
    return ids


def page_has_local_artifacts(page_id: str, *, registry_ids: set[str] | None = None) -> bool:
    """PolicyPage / metadata / registry / 첨부 중 하나라도 있으면 True."""
    pid = str(page_id)
    if find_policy_json(pid):
        return True
    if find_metadata_json(pid):
        return True
    if (ATTACHMENTS_DIR / pid).is_dir():
        return True
    if (ADDED_ATTACHMENTS_DIR / pid).is_dir():
        return True
    if (DESC_DIR / f"result_{pid}.json").exists():
        return True
    if registry_ids is None:
        registry_ids = _local_registry_page_ids()
    return pid in registry_ids


def _load_trash_poll_state() -> dict[str, Any]:
    if not TRASH_POLL_STATE_JSON.is_file():
        return {}
    try:
        return json.loads(TRASH_POLL_STATE_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _parse_iso_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _should_run_periodic(last_at: str | None, interval_sec: int) -> bool:
    if interval_sec <= 0:
        return False
    last = _parse_iso_utc(last_at)
    if last is None:
        return True
    elapsed = (datetime.now(timezone.utc) - last).total_seconds()
    return elapsed >= interval_sec


def verify_page_removal_candidate(page_id: str, crawl: Any) -> tuple[bool, str]:
    """
    REST 본문 조회로 삭제 여부 확인.
    True: remove_page_artifacts 수행 (404 또는 status=trashed/deleted)
    False: 스킵 (current 등)
    """
    pid = str(page_id).strip()
    try:
        page = crawl.get_page_content(pid)
    except Exception as e:
        resp = getattr(e, "response", None)
        status_code = getattr(resp, "status_code", None) if resp is not None else None
        err = str(e).lower()
        if status_code == 404 or "404" in err or "not found" in err:
            return True, "not_found"
        return False, f"api_error:{e}"

    status = str(page.get("status") or "").strip().lower()
    if status in ("trashed", "deleted"):
        return True, status
    if status in ("current", "archived", "draft"):
        return False, f"status_{status}"
    return False, f"status_{status or 'unknown'}"


def _process_removal_candidates(
    candidates: list[dict[str, Any]],
    crawl: Any,
    registry_ids: set[str],
    bucket: dict[str, Any],
    *,
    mode: str,
) -> None:
    for item in candidates:
        pid = str(item.get("id") or "").strip()
        if not pid:
            continue
        title = str(item.get("title") or "")
        try:
            if not page_has_local_artifacts(pid, registry_ids=registry_ids):
                bucket["skipped_no_local"].append(pid)
                continue
            should_remove, reason = verify_page_removal_candidate(pid, crawl)
            if not should_remove:
                bucket.setdefault("skipped_verify", []).append(
                    {"page_id": pid, "reason": reason, "mode": mode}
                )
                continue
            logger.info(
                "휴지통 삭제 (%s): page_id=%s title=%s verify=%s",
                mode,
                pid,
                title,
                reason,
            )
            remove_page_artifacts(pid)
            bucket["removed_locally"].append(
                {"page_id": pid, "title": title, "verify_reason": reason, "mode": mode}
            )
        except Exception as e:
            logger.error("휴지통 삭제 실패 (%s) page_id=%s: %s", mode, pid, e)
            bucket["errors"].append({"page_id": pid, "mode": mode, "error": str(e)})


def _poll_trash_incremental(
    space: str,
    dl: Any,
    crawl: Any,
    state: dict[str, Any],
    registry_ids: set[str],
) -> dict[str, Any]:
    """상위 TOP_N 휴지통 diff → 새 id 만 검증 후 삭제."""
    top_n = int(os.getenv("CONFLUENCE_TRASH_POLL_TOP_N", "10") or "10")
    section: dict[str, Any] = {
        "mode": "incremental",
        "top_n": top_n,
        "trashed_remote": 0,
        "new_ids": [],
        "removed_locally": [],
        "skipped_no_local": [],
        "skipped_verify": [],
        "errors": [],
    }
    try:
        trashed = dl.get_trashed_pages_via_playwright(space, top_n=top_n)
    except Exception as e:
        logger.error("휴지통 Playwright(증분) 실패: %s", e)
        section["errors"].append({"stage": "fetch_trash_playwright", "error": str(e)})
        return section

    section["trashed_remote"] = len(trashed)
    current_ids = [str(x.get("id") or "") for x in trashed if x.get("id")]
    prev_ids = set(str(x) for x in (state.get("last_top10_ids") or []))
    new_ids = [i for i in current_ids if i not in prev_ids]
    section["new_ids"] = new_ids
    by_id = {str(x.get("id")): x for x in trashed if x.get("id")}
    candidates = [by_id[i] for i in new_ids if i in by_id]
    _process_removal_candidates(candidates, crawl, registry_ids, section, mode="incremental")
    section["current_top_ids"] = current_ids
    return section


def _poll_trash_full_scan(
    space: str,
    dl: Any,
    crawl: Any,
    registry_ids: set[str],
) -> dict[str, Any]:
    """휴지통 전체 Playwright → registry_ids ∩ trashed_ids 검증 후 삭제."""
    section: dict[str, Any] = {
        "mode": "full_scan",
        "trashed_remote": 0,
        "candidates": 0,
        "removed_locally": [],
        "skipped_no_local": [],
        "skipped_verify": [],
        "errors": [],
    }
    try:
        trashed = dl.get_trashed_pages_via_playwright(space, top_n=None)
    except Exception as e:
        logger.error("휴지통 Playwright(전체) 실패: %s", e)
        section["errors"].append({"stage": "fetch_trash_playwright", "error": str(e)})
        return section

    section["trashed_remote"] = len(trashed)
    trash_ids = {str(x.get("id") or "") for x in trashed if x.get("id")}
    candidates = [x for x in trashed if str(x.get("id") or "") in registry_ids]
    section["candidates"] = len(candidates)
    _process_removal_candidates(candidates, crawl, registry_ids, section, mode="full_scan")
    section["trash_ids_sample"] = sorted(trash_ids)[:20]
    return section


def poll_trashed_pages_and_remove() -> dict[str, Any]:
    """
    Playwright viewtrash + REST get_page_content 검증 후 로컬 제거.
    증분(상위 TOP_N diff) + 주기적 전체 스캔(registry ∩ trash).
    """
    space = os.getenv("CONFLUENCE_TRASH_POLL_SPACE_KEY", CONFLUENCE_SPACE_KEY).strip()
    full_interval = int(
        os.getenv("CONFLUENCE_TRASH_POLL_FULL_SCAN_INTERVAL_SEC", "86400") or "86400"
    )
    state = _load_trash_poll_state()
    run_full = _should_run_periodic(state.get("last_full_scan_at"), full_interval)

    dl = _load_script_module("attachment_download_trash", "3_attachment_download.py")
    crawl = _load_script_module("confluence_crawl", "1_Confluence_RestAPI_crawlingAll.py")

    report: dict[str, Any] = {
        "space_key": space,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "incremental": {},
        "full_scan": None,
        "run_full_scan": run_full,
    }

    with _sync_lock:
        registry_ids = _local_registry_page_ids()
        try:
            inc = _poll_trash_incremental(space, dl, crawl, state, registry_ids)
            report["incremental"] = inc
            if inc.get("current_top_ids") is not None:
                state["last_top10_ids"] = inc["current_top_ids"]
            state["last_incremental_at"] = report["updated_at"]

            if run_full:
                full = _poll_trash_full_scan(space, dl, crawl, registry_ids)
                report["full_scan"] = full
                state["last_full_scan_at"] = report["updated_at"]
        except Exception as e:
            logger.error("휴지통 폴링 실패: %s", e)
            report["errors"] = [{"stage": "poll", "error": str(e)}]

    try:
        TRASH_POLL_STATE_JSON.parent.mkdir(parents=True, exist_ok=True)
        TRASH_POLL_STATE_JSON.write_text(
            json.dumps({**state, **report}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass

    inc = report.get("incremental") or {}
    full = report.get("full_scan") or {}
    removed = len(inc.get("removed_locally") or []) + len(full.get("removed_locally") or [])
    logger.info(
        "휴지통 폴링 완료 space=%s inc_remote=%s new=%s full=%s removed=%d",
        space,
        inc.get("trashed_remote"),
        len(inc.get("new_ids") or []),
        report.get("run_full_scan"),
        removed,
    )
    return report


# ── 로컬 added_attachments (웹훅·Confluence 크롤 없음) ─────────────────────────


def discover_added_attachment_page_ids() -> list[str]:
    """Data/added_attachments/{page_id}/ 하위 디렉터리에서 page_id 목록."""
    if not ADDED_ATTACHMENTS_DIR.is_dir():
        return []
    out: list[str] = []
    for p in sorted(ADDED_ATTACHMENTS_DIR.iterdir()):
        if not p.is_dir():
            continue
        if any(p.glob("*")):
            out.append(p.name)
    return out


def _relative_data_path(path: Path) -> str:
    try:
        rel = path.resolve().relative_to(PROJECT_DIR.resolve())
        return str(rel).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def build_metadata_from_added_attachments(
    page_id: str,
    extra: Optional[dict[str, Any]] = None,
) -> Path:
    """
    added_attachments/{page_id}/ 파일로 최소 metadata JSON 생성·갱신.
    extra: title, body_text, page_url, urls 등 (Confluence 없을 때).
    """
    extra = dict(extra or {})
    pid = str(page_id)
    att_dir = ADDED_ATTACHMENTS_DIR / pid
    if not att_dir.is_dir():
        raise FileNotFoundError(f"added_attachments 디렉터리 없음: {att_dir}")

    files = [fp for fp in sorted(att_dir.iterdir()) if fp.is_file()]
    if not files:
        raise FileNotFoundError(f"added_attachments에 파일 없음: {att_dir}")

    title = str(extra.get("title") or extra.get("page_title") or f"로컬 첨부 page {pid}")
    page_url = str(
        extra.get("page_url")
        or extra.get("url")
        or f"local://added_attachments/{pid}"
    )
    version_num = int(extra.get("version") or 1)

    attachments: list[dict[str, Any]] = []
    for fp in files:
        ext = fp.suffix.lower()
        attachments.append({
            "title": fp.name,
            "filename": fp.name,
            "extension": ext,
            "media_type": "image" if ext in {".png", ".jpg", ".jpeg", ".webp", ".gif"} else "file",
            "saved_path": _relative_data_path(fp),
            "disk_source": "added_attachments",
        })

    body_text = str(extra.get("body_text") or extra.get("body") or "").strip()
    if not body_text:
        names = ", ".join(a["title"] for a in attachments[:8])
        body_text = f"로컬 추가 첨부 ({len(attachments)}개): {names}"

    meta: dict[str, Any] = {
        "page": {
            "id": pid,
            "title": title,
            "type": "page",
            "body_text": body_text,
            "body_view_html": extra.get("body_view_html") or "",
            "version": {"number": version_num, "when": extra.get("updated_at") or ""},
            "_links": {"webui": page_url},
        },
        "content": {
            "body_text": body_text,
            "urls": list(extra.get("urls") or []),
        },
        "attachments": attachments,
        "source": "local_added_attachments",
        "extra": {
            k: v for k, v in extra.items()
            if k not in ("title", "page_title", "body_text", "body")
        },
    }

    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    safe = _safe_title_slug(title)
    meta_path = METADATA_DIR / f"{pid}_{safe}.metadata.json"
    cleanup_metadata_by_page_id(pid, keep=meta_path)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "로컬 metadata 생성: page_id=%s attachments=%d → %s",
        pid,
        len(attachments),
        meta_path.name,
    )
    return meta_path


def debug_parse_added_attachments_for_page(page_id: str, preview_chars: int = 1200) -> dict[str, Any]:
    """로컬 added_attachments 파일 파서 결과를 로그/디버그 파일로 남긴다."""
    pid = str(page_id)
    att_dir = ADDED_ATTACHMENTS_DIR / pid
    files = [fp for fp in sorted(att_dir.iterdir()) if fp.is_file()] if att_dir.is_dir() else []

    report: dict[str, Any] = {"page_id": pid, "file_count": len(files), "items": []}
    if not files:
        logger.info("파서 디버그 스킵: added 파일 없음 page_id=%s", pid)
        return report

    try:
        from parsers.base import get_parser
    except Exception as e:
        logger.warning("파서 디버그 스킵: parsers.base import 실패: %s", e)
        report["error"] = f"import_failed:{type(e).__name__}"
        return report

    LOCAL_PARSE_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    out_dir = LOCAL_PARSE_DEBUG_DIR / pid
    out_dir.mkdir(parents=True, exist_ok=True)

    for fp in files:
        ext = fp.suffix.lower()
        item: dict[str, Any] = {"file": fp.name, "ext": ext}
        parser = None
        try:
            parser = get_parser(ext)
        except Exception as e:
            item["status"] = "parser_error"
            item["error"] = f"get_parser:{type(e).__name__}"
            report["items"].append(item)
            continue

        if parser is None:
            item["status"] = "parser_not_found"
            report["items"].append(item)
            continue

        try:
            parsed = parser.parse(fp)
            text = str(getattr(parsed, "text", "") or "")
            parse_method = str(getattr(parsed, "parse_method", "") or "")
            confidence = getattr(parsed, "confidence", None)
            preview = text[:preview_chars]

            item["status"] = "ok"
            item["parser"] = type(parser).__name__
            item["parse_method"] = parse_method
            item["confidence"] = confidence
            item["text_chars"] = len(text)
            item["preview_chars"] = len(preview)

            preview_path = out_dir / f"{fp.stem}.preview.txt"
            preview_path.write_text(preview, encoding="utf-8")
            item["preview_file"] = str(preview_path.relative_to(PROJECT_DIR)).replace("\\", "/")

            logger.info(
                "파서 디버그: page_id=%s file=%s parser=%s method=%s chars=%d",
                pid,
                fp.name,
                type(parser).__name__,
                parse_method or "-",
                len(text),
            )
        except Exception as e:
            item["status"] = "parse_failed"
            item["error"] = f"{type(e).__name__}: {e}"
        report["items"].append(item)

    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("파서 디버그 리포트 저장: %s", report_path)
    return report


def run_post_process_hybrid_for_page(page_id: str) -> dict[str, Any]:
    """7_post_process_tree_hybrid를 page_id 단위로만 적용."""
    pp = _load_script_module("post_process_hybrid", "7_post_process_tree_hybrid.py")
    pid = str(page_id)

    meta_index = pp.load_all_metadata(pp.METADATA_DIR)
    meta = meta_index.get(pid)
    if not meta:
        logger.info("post_process page 스킵 (metadata 없음): page_id=%s", pid)
        return {"page_id": pid, "status": "skipped_no_metadata"}

    desc_index = pp.load_page_descriptions(pid)
    report: dict[str, Any] = {"page_id": pid}

    parents = pp.read_jsonl(pp.PARENT_STORE_JSONL)
    new_parents: list[dict[str, Any]] = []
    parent_target = 0
    parent_updated = 0
    parent_stats: dict[str, int] = {}
    for row in parents:
        if str(row.get("group_id")) == pid:
            parent_target += 1
            enriched, stats = pp.enrich_doc(row, meta, desc_index)
            if enriched != row:
                parent_updated += 1
            for k, v in (stats or {}).items():
                if isinstance(v, int):
                    parent_stats[k] = parent_stats.get(k, 0) + v
            new_parents.append(enriched)
        else:
            new_parents.append(row)
    pp.write_jsonl(pp.PARENT_STORE_JSONL, new_parents)
    report["parent_store"] = {
        "target_rows": parent_target,
        "updated_rows": parent_updated,
        "stats": parent_stats,
    }

    payload = pp.read_jsonl(pp.VECTOR_PAYLOAD_JSONL)
    new_payload: list[dict[str, Any]] = []
    vector_target = 0
    vector_updated = 0
    vector_stats: dict[str, int] = {}
    for row in payload:
        if str(row.get("group_id")) == pid:
            vector_target += 1
            enriched, stats = pp.enrich_doc(row, meta, desc_index)
            if enriched != row:
                vector_updated += 1
            for k, v in (stats or {}).items():
                if isinstance(v, int):
                    vector_stats[k] = vector_stats.get(k, 0) + v
            new_payload.append(enriched)
        else:
            new_payload.append(row)
    pp.write_jsonl(pp.VECTOR_PAYLOAD_JSONL, new_payload)
    report["vector_payload"] = {
        "target_rows": vector_target,
        "updated_rows": vector_updated,
        "stats": vector_stats,
    }
    report["status"] = "success"
    logger.info(
        "post_process page 적용: page_id=%s parent(%d/%d) vector(%d/%d)",
        pid,
        parent_updated,
        parent_target,
        vector_updated,
        vector_target,
    )
    return report


def sync_local_added_attachments(
    page_id: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
    *,
    post_process: bool = True,
    upload_es: bool = True,
) -> dict[str, Any]:
    """
    웹훅·Confluence API 없이 Data/added_attachments 만으로 RAG registry + ES 갱신.

    page_id 미지정 시 파일이 있는 하위 폴더를 모두 처리.
    """
    extra = dict(extra or {})
    ids = [str(page_id)] if page_id else discover_added_attachment_page_ids()
    if not ids:
        raise FileNotFoundError(
            f"처리할 page_id 없음 — {ADDED_ATTACHMENTS_DIR} 아래에 "
            "{page_id}/파일 구조로 넣어 주세요."
        )

    summary: dict[str, Any] = {
        "mode": "local_added_attachments",
        "page_ids": ids,
        "results": [],
    }

    for pid in ids:
        row: dict[str, Any] = {"page_id": pid, "steps": []}
        try:
            meta_path = build_metadata_from_added_attachments(pid, extra)
            row["metadata"] = meta_path.name
            row["steps"].append("metadata_from_added")

            parse_dbg = debug_parse_added_attachments_for_page(pid)
            row["parse_debug"] = parse_dbg
            row["steps"].append("parse_debug")

            describe_attachments_for_page(pid)
            row["steps"].append("describe_attachments")

            build_rag_for_page(pid)
            row["steps"].append("rag_registry")

            if post_process:
                pp_report = run_post_process_hybrid_for_page(pid)
                row["post_process"] = pp_report
                row["steps"].append("post_process_hybrid")

            if upload_es:
                delete_es_docs_by_page_id(pid)
                uploaded = upload_page_to_es(pid)
                row["steps"].append("elasticsearch")
                row["es_docs_uploaded"] = uploaded

            row["status"] = "success"
        except Exception as e:
            logger.error("로컬 added 처리 실패 page_id=%s: %s", pid, e)
            row["status"] = "error"
            row["error"] = str(e)
            row["traceback"] = traceback.format_exc()

        summary["results"].append(row)

    summary["status"] = (
        "success"
        if all(r.get("status") == "success" for r in summary["results"])
        else "partial"
    )
    return summary


# ── 웹훅 오케스트레이션 ──────────────────────────────────────────────────────

_UPDATE_TRIGGER_TO_EVENT: dict[str, str] = {
    "edit_page": "page_updated",
    "create_page": "page_created",
    "remove_page": "page_removed",
    "trash_page": "page_removed",
    "restore_page": "page_updated",
    "move_page": "page_updated",
    "publish_page": "page_updated",
}

_BODY_EVENT_TO_NORMALIZED: dict[str, str] = {
    "page_created": "page_created",
    "page_updated": "page_updated",
    "page_removed": "page_removed",
    "page_trashed": "page_removed",
    "page_restored": "page_updated",
    "page_moved": "page_updated",
    "page_published": "page_updated",
    "page_unarchived": "page_updated",
    "blueprint_page_created": "page_created",
    "content_updated": "page_updated",
    "content_restored": "page_updated",
    "content_trashed": "page_removed",
}


def allowed_webhook_space_keys() -> set[str]:
    """처리 대상 Confluence spaceKey (기본: config CONFLUENCE_SPACE_KEY=CW)."""
    raw = os.getenv("CONFLUENCE_WEBHOOK_SPACE_KEYS", CONFLUENCE_SPACE_KEY)
    return {s.strip().upper() for s in raw.split(",") if s.strip()}


def extract_webhook_page_info(payload: dict[str, Any]) -> tuple[str, Optional[str], str]:
    """page.id / idAsString, title, spaceKey."""
    page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
    pid = page.get("id")
    if pid is None:
        pid = page.get("idAsString")
    title = page.get("title")
    space_key = str(page.get("spaceKey") or "").strip()
    if pid is None:
        return "", title if isinstance(title, str) else None, space_key
    return str(pid).strip(), title if isinstance(title, str) else None, space_key


def resolve_webhook_event(header_event: str, payload: dict[str, Any]) -> tuple[str, str]:
    """정규 이벤트 + 출처(header | body.event | body.updateTrigger | …)."""
    h = (header_event or "").strip().lower()
    if h in ("page_created", "page_updated", "page_removed"):
        return h, "header"

    raw = str(payload.get("event") or "").strip().lower()
    if raw in _BODY_EVENT_TO_NORMALIZED:
        return _BODY_EVENT_TO_NORMALIZED[raw], "body.event"

    for key in ("updateTrigger", "update_trigger", "trigger", "webhookEvent", "eventType"):
        trigger = str(payload.get(key) or "").strip().lower()
        if trigger in _UPDATE_TRIGGER_TO_EVENT:
            return _UPDATE_TRIGGER_TO_EVENT[trigger], f"body.{key}"
        if trigger in _BODY_EVENT_TO_NORMALIZED:
            return _BODY_EVENT_TO_NORMALIZED[trigger], f"body.{key}"

    # Confluence Cloud: 페이지 생성 웹훅은 updateTrigger 없이 version=1만 오는 경우가 많음
    page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
    try:
        if int(page.get("version") or 0) == 1:
            return "page_created", "body.page.version"
    except (TypeError, ValueError):
        pass

    return "", ""


def parse_confluence_webhook(
    payload: dict[str, Any],
    *,
    header_event: str = "",
) -> dict[str, Any]:
    """Confluence Cloud 웹훅 1건 파싱."""
    page_id, title, space_key = extract_webhook_page_info(payload)
    event, event_source = resolve_webhook_event(header_event, payload)
    allowed = allowed_webhook_space_keys()
    sk_upper = space_key.upper()
    page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
    return {
        "page_id": page_id,
        "title": title,
        "space_key": space_key,
        "space_allowed": bool(sk_upper and sk_upper in allowed),
        "allowed_space_keys": sorted(allowed),
        "event": event,
        "event_source": event_source,
        "update_trigger": payload.get("updateTrigger"),
        "page_version": page.get("version"),
        "payload_top_keys": sorted(payload.keys()) if isinstance(payload, dict) else [],
        "raw_header_event": (header_event or "").strip() or None,
        "raw_body_event": payload.get("event"),
    }


def sync_page_create_or_update(page_id: str, title: Optional[str] = None) -> dict[str, Any]:
    """page_created / page_updated 공통 파이프라인."""
    pid = str(page_id)
    result: dict[str, Any] = {
        "page_id": pid,
        "title": title,
        "steps": [],
        "partial_errors": [],
    }

    policy_path = crawl_single_page(pid)
    result["steps"].append("crawl")

    page_title = (title or _title_from_policy_json(policy_path)).strip()
    if page_title_excluded(page_title):
        purge_rag_registry_for_page(pid)
        deleted = delete_es_docs_by_page_id(pid)
        result["excluded_by_title"] = page_title
        result["es_docs_deleted"] = deleted
        result["steps"].append("excluded_by_page_title")
        return result

    filter_attachments_for_page(policy_path)
    result["steps"].append("filter_attachments")

    try:
        sync_attachments_for_page(policy_path)
        result["steps"].append("attachments")
    except Exception as e:
        err = {"step": "attachments", "error": str(e)}
        result["partial_errors"].append(err)
        logger.error(
            "첨부 다운로드 실패 page_id=%s (본문 RAG/ES 계속=%s): %s",
            pid,
            WEBHOOK_CONTINUE_ON_ATTACHMENT_ERROR,
            e,
            exc_info=True,
        )
        if not WEBHOOK_CONTINUE_ON_ATTACHMENT_ERROR:
            raise

    keep_meta = find_metadata_json(pid)
    removed_meta = cleanup_metadata_by_page_id(pid, keep=keep_meta)
    if removed_meta:
        result["metadata_cleaned"] = removed_meta

    try:
        describe_attachments_for_page(pid)
        result["steps"].append("describe_attachments")
    except Exception as e:
        err = {"step": "describe_attachments", "error": str(e)}
        result["partial_errors"].append(err)
        logger.error(
            "첨부 설명 실패 page_id=%s (계속 진행=%s): %s",
            pid,
            WEBHOOK_CONTINUE_ON_ATTACHMENT_ERROR,
            e,
            exc_info=True,
        )
        if not WEBHOOK_CONTINUE_ON_ATTACHMENT_ERROR:
            raise

    build_rag_for_page(pid)
    result["steps"].append("rag_registry")

    if not WEBHOOK_SKIP_POST_PROCESS:
        result["post_process"] = run_post_process_hybrid_for_page(pid)
        result["steps"].append("post_process_hybrid")
    else:
        logger.info("post_process 스킵 (CONFLUENCE_WEBHOOK_SKIP_POST_PROCESS): page_id=%s", pid)

    delete_es_docs_by_page_id(pid)
    uploaded = upload_page_to_es(pid)
    result["steps"].append("elasticsearch")
    result["es_docs_uploaded"] = uploaded

    result["status"] = "partial" if result["partial_errors"] else "success"
    return result


def _run_debounced_webhook_sync(page_id: str) -> None:
    """디바운스 타이머 만료 시 대기 중인 최신 웹훅 1건 실행."""
    pid = str(page_id)
    with _webhook_debounce_lock:
        pending = _pending_webhook_sync.pop(pid, None)
    if not pending:
        return
    logger.info(
        "웹훅 디바운스 실행 page_id=%s event=%s version=%s (대기 %ds)",
        pid,
        pending.get("event"),
        pending.get("page_version"),
        WEBHOOK_DEBOUNCE_SEC,
    )
    out = process_webhook_payload(
        str(pending.get("event") or ""),
        pid,
        pending.get("title"),
        webhook_meta=pending.get("webhook_meta"),
    )
    logger.info("웹훅 디바운스 처리 결과: %s", out)


def schedule_webhook_sync(
    event: str,
    page_id: str,
    title: Optional[str] = None,
    *,
    webhook_meta: Optional[dict[str, Any]] = None,
) -> None:
    """
    동일 page_id 연속 웹훅을 WEBHOOK_DEBOUNCE_SEC 동안 묶어 마지막 1회만 sync.
    (즉시 반환 — BackgroundTasks에서 호출)
    """
    pid = str(page_id)
    if WEBHOOK_DEBOUNCE_SEC <= 0:
        process_webhook_payload(event, pid, title, webhook_meta=webhook_meta)
        return

    page_version = None
    if webhook_meta:
        page_version = webhook_meta.get("page_version")

    with _webhook_debounce_lock:
        old = _pending_webhook_sync.get(pid)
        if old and old.get("timer"):
            old["timer"].cancel()

        timer = threading.Timer(
            WEBHOOK_DEBOUNCE_SEC,
            lambda: _run_debounced_webhook_sync(pid),
        )
        timer.daemon = True
        _pending_webhook_sync[pid] = {
            "event": event,
            "title": title,
            "webhook_meta": webhook_meta,
            "page_version": page_version,
            "timer": timer,
        }
        timer.start()

    logger.info(
        "웹훅 디바운스 예약 page_id=%s event=%s version=%s (%ds 후 실행)",
        pid,
        event,
        page_version,
        WEBHOOK_DEBOUNCE_SEC,
    )


def process_webhook_payload(
    event: str,
    page_id: str,
    title: Optional[str] = None,
    *,
    webhook_meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    Confluence 웹훅 백그라운드 처리.
    event: page_created | page_updated | page_removed
    """
    event = (event or "").strip().lower()
    pid = str(page_id)
    if webhook_meta:
        logger.info(
            "웹훅 동기화 시작 page_id=%s event=%s space=%s source=%s updateTrigger=%s",
            pid,
            event,
            webhook_meta.get("space_key"),
            webhook_meta.get("event_source"),
            webhook_meta.get("update_trigger"),
        )

    with _sync_lock:
        try:
            if event == "page_removed":
                if WEBHOOK_HANDLES_PAGE_REMOVED:
                    remove_page_artifacts(pid)
                    return {"status": "success", "event": event, "page_id": pid, "action": "removed"}
                logger.info(
                    "삭제 웹훅 무시 (Trash API 폴링 사용): page_id=%s", pid
                )
                return {
                    "status": "ignored",
                    "event": event,
                    "page_id": pid,
                    "message": "delete_handled_by_trash_poll_only",
                }

            if event in ("page_created", "page_updated"):
                out = sync_page_create_or_update(pid, title)
                out["event"] = event
                return out

            logger.warning("알 수 없는 웹훅 이벤트: %s (page_id=%s)", event, pid)
            return {
                "status": "ignored",
                "event": event,
                "page_id": pid,
                "message": f"지원하지 않는 이벤트: {event}",
            }
        except Exception as e:
            logger.error(
                "웹훅 동기화 실패 event=%s page_id=%s: %s\n%s",
                event,
                pid,
                e,
                traceback.format_exc(),
            )
            return {
                "status": "error",
                "event": event,
                "page_id": pid,
                "error": str(e),
            }


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 2 and sys.argv[1] == "poll-trash":
        print(json.dumps(poll_trashed_pages_and_remove(), ensure_ascii=False, indent=2))
    else:
        print("Usage: python confluence_webhook_sync.py poll-trash")
