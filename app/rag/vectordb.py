# =============================================================================
# ConnBot — Elasticsearch Vector DB + Hybrid Search + Reranker Module
# =============================================================================
'''
noVLM_ES_vectordb.py를 모듈화한 파일.
main.py에서 import하여 사용한다.

사용법:
    from app.rag.vectordb import init_vectordb, async_generate_rag_answer

    # 앱 시작 시 1회 호출
    init_vectordb(force_rebuild=False)

    # 질문에 대한 RAG 답변 생성 (async)
    answer, docs = await async_generate_rag_answer(query)
'''

from __future__ import annotations

import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pytz import timezone

from pathlib import Path
from typing import Any, Sequence

import numpy as np
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from elasticsearch import Elasticsearch, helpers
from sentence_transformers import SentenceTransformer
from tqdm import tqdm

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.core import rag_debug_logger

load_dotenv()

from app.core.config import (
    now_iso, PROJECT_DIR, DATA_DIR, POLICY_PAGE_DIR, VECTOR_DB_DIR, ES_REPORT_DIR,
    BASE_CONFLUENCE_URL, PAGE_JSON_GLOB, CHUNK_SIZE, CHUNK_OVERLAP,
    EMBEDDING_MODEL_NAME,
    RERANKER_MODEL_NAME, RERANKER_LOCAL_DIR, RERANKER_ONNX_SUBDIR, RERANKER_ONNX_FILENAME,
    RERANKER_MAX_LENGTH, RERANKER_BATCH_SIZE, RERANK_QUERY_MAX_CHARS, RERANK_DOC_MAX_CHARS,
    ES_INDEX_NAME, ELASTICSEARCH_URL,
    CHUNK_TYPE_RETRIEVAL_WEIGHTS,
    resolve_attachment_url,
    retrieval_weight_for_chunk,
    get_excluded_page_ids,
    LLM_MODEL_NAME,
    RAG_ANSWER_MODEL_NAME,
    HIGH_RISK_FALLBACK_MODEL_NAME,
    RAG_MAX_PARENTS,
    RAG_RESEARCH_ENABLED,
    ROUTER_MODEL_NAME,
    get_router_instruction,
    RAG_KEYWORD_LIMIT,
    RAG_MAX_PAGES,
    RAG_MAX_PARENTS_PER_PAGE,
    RAG_MIN_PAGES,
    RAG_PAGE_GAP_MIN_KEEP,
    RAG_PAGE_RERANK_RELATIVE_GAP,
    RAG_RERANK_POOL,
    RAG_SEMANTIC_LIMIT,
)

ELASTICSEARCH_API_KEY = os.getenv("ELASTICSEARCH_API_KEY", "")


# =============================================================================
# 1. Elasticsearch / Embedding Client
# =============================================================================

def create_es_client() -> Elasticsearch:
    '''
    Elasticsearch client를 생성한다.
    '''
    if ELASTICSEARCH_API_KEY:
        return Elasticsearch(
            ELASTICSEARCH_URL,
            api_key=ELASTICSEARCH_API_KEY,
            verify_certs=False,
            request_timeout=120,
        )

    return Elasticsearch(
        ELASTICSEARCH_URL,
        request_timeout=120,
    )


es = create_es_client()

_embedder: SentenceTransformer | None = None
_ort_reranker: "OnnxReranker | None" = None


def get_embedder() -> SentenceTransformer:
    """임베딩 전용 — sentence-transformers (reranker와 분리)."""
    global _embedder

    if _embedder is None:
        _embedder = SentenceTransformer(EMBEDDING_MODEL_NAME)
        _embedder.max_seq_length = 512

    return _embedder


class OnnxReranker:
    """
    Reranker 전용 스택: 로컬 AutoTokenizer + ONNXRuntime.
    sentence-transformers / PyTorch 모델 로딩과 분리한다.
    """

    def __init__(
        self,
        repo_id: str,
        local_dir: Path,
        *,
        onnx_subdir: str = RERANKER_ONNX_SUBDIR,
        onnx_filename: str = RERANKER_ONNX_FILENAME,
        max_length: int = RERANKER_MAX_LENGTH,
        batch_size: int = RERANKER_BATCH_SIZE,
    ) -> None:
        self.repo_id = repo_id
        self.local_dir = Path(local_dir)
        self.onnx_path = self.local_dir / onnx_subdir / onnx_filename
        self.max_length = max_length
        self.batch_size = batch_size
        self._session = None
        self._tokenizer = None
        self._input_names: list[str] | None = None

    def _ensure_local_assets(self) -> None:
        """HF → 로컬 캐시 (tokenizer + onnx). 이후 local_files_only."""
        if self.onnx_path.is_file() and (self.local_dir / "tokenizer_config.json").is_file():
            return

        from huggingface_hub import snapshot_download

        self.local_dir.mkdir(parents=True, exist_ok=True)
        print(f"Reranker 자산 다운로드: {self.repo_id} → {self.local_dir}")
        snapshot_download(
            repo_id=self.repo_id,
            local_dir=str(self.local_dir),
            allow_patterns=[
                f"{RERANKER_ONNX_SUBDIR}/*",
                "tokenizer*",
                "*.json",
                "*.model",
                "*.py",
            ],
        )

    def _ensure_loaded(self) -> None:
        if self._session is not None:
            return

        from onnxruntime import InferenceSession, SessionOptions
        from transformers import AutoTokenizer

        self._ensure_local_assets()
        if not self.onnx_path.is_file():
            raise FileNotFoundError(f"ONNX 모델 없음: {self.onnx_path}")

        opts = SessionOptions()
        self._session = InferenceSession(
            str(self.onnx_path),
            opts,
            providers=["CPUExecutionProvider"],
        )
        self._input_names = [inp.name for inp in self._session.get_inputs()]
        self._tokenizer = AutoTokenizer.from_pretrained(
            str(self.local_dir),
            local_files_only=True,
            trust_remote_code=True,
            fix_mistral_regex=True,
        )
        print(f"Reranker 로드 완료 (ORT): {self.onnx_path}")

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        """(query, document) relevance — sigmoid(onnx logit)."""
        if not pairs:
            return []

        self._ensure_loaded()
        assert self._session is not None
        assert self._tokenizer is not None
        assert self._input_names is not None

        scores: list[float] = []
        for start in range(0, len(pairs), self.batch_size):
            batch = pairs[start : start + self.batch_size]
            queries = [q for q, _ in batch]
            docs = [d for _, d in batch]
            encoded = self._tokenizer(
                queries,
                docs,
                padding=True,
                truncation=True,
                max_length=self.max_length,
                return_tensors="np",
            )
            ort_inputs = {
                name: encoded[name]
                for name in self._input_names
                if name in encoded
            }
            logits = np.asarray(self._session.run(None, ort_inputs)[0]).reshape(-1)
            batch_scores = 1.0 / (1.0 + np.exp(-logits))
            scores.extend(float(s) for s in batch_scores)

        return scores


def get_ort_reranker() -> OnnxReranker:
    """RRF 후보 재순위화 — ONNXRuntime + 로컬 tokenizer."""
    global _ort_reranker

    if _ort_reranker is None:
        _ort_reranker = OnnxReranker(
            RERANKER_MODEL_NAME,
            RERANKER_LOCAL_DIR,
        )

    return _ort_reranker


def _reranker_compute_scores(pairs: list[tuple[str, str]]) -> list[float]:
    """리랭킹 — ProcessPool 워커(ml_inference)에 위임."""
    from app.ml.inference import rerank_pairs_sync

    return rerank_pairs_sync(pairs)


# =============================================================================
# 2. Data Model
# =============================================================================

@dataclass
class PageChunk:
    id: str
    text: str
    embedding_text: str

    page_id: str
    page_title: str
    page_url: str
    title_path: str

    version: int | None = None
    updated_at: str | None = None
    indexed_at: str = field(default_factory=now_iso)

    source_type: str = "page_document"
    chunk_index: int = 0
    chunk_count: int = 1
    content_hash: str = ""


@dataclass
class SearchHit:
    doc_id: str
    payload: dict[str, Any]
    score: float
    semantic_score: float = 0.0
    keyword_score: float = 0.0


@dataclass
class _PageStats:
    """page 단위 집계 — ranking 입력."""
    page_id: str
    hits: list[SearchHit]
    chunk_count: int
    semantic_count: int
    bm25_count: int
    subquery_coverage: int
    top_rank: int
    avg_rank: float
    page_score: float = 0.0


# =============================================================================
# 3. Basic Helpers
# =============================================================================

def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def safe_join_text(parts: list[str]) -> str:
    return "\n\n".join(str(p).strip() for p in parts if p and str(p).strip())


def html_to_text(html: str) -> str:
    '''
    Confluence body HTML을 plain text로 변환한다.
    '''
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style"]):
        tag.decompose()

    text = soup.get_text("\n", strip=True)

    lines = []
    prev = ""

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line == prev:
            continue
        lines.append(line)
        prev = line

    return "\n".join(lines)


def chunk_text(
    text: str,
    chunk_size: int = CHUNK_SIZE,
    chunk_overlap: int = CHUNK_OVERLAP,
) -> list[str]:
    '''
    page-level document를 chunk로 분할한다.
    '''
    text = (text or "").strip()

    if not text:
        return []

    if len(text) <= chunk_size:
        return [text]

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks = []
    current = ""

    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph

        if len(candidate) <= chunk_size:
            current = candidate
            continue

        if current:
            chunks.append(current)

        if len(paragraph) <= chunk_size:
            current = paragraph
            continue

        step = max(1, chunk_size - chunk_overlap)

        for start in range(0, len(paragraph), step):
            piece = paragraph[start:start + chunk_size].strip()
            if piece:
                chunks.append(piece)

        current = ""

    if current:
        chunks.append(current)

    return chunks


# =============================================================================
# 4. Page JSON → PageChunk
# =============================================================================

def extract_page_metadata(data: dict[str, Any]) -> dict[str, Any]:
    '''
    Confluence page JSON에서 deterministic metadata를 추출한다.
    '''
    page = data.get("page", {}) or {}

    page_id = str(page.get("id", ""))
    page_title = page.get("title", "")

    ancestors = page.get("ancestors", []) or []
    title_path = " / ".join(
        [a.get("title", "") for a in ancestors if a.get("title")] + [page_title]
    )

    page_url = page.get("webui") or f"{BASE_CONFLUENCE_URL}/spaces/CW/pages/{page_id}"

    version_info = page.get("version", {}) or {}
    version = version_info.get("number")
    updated_at = version_info.get("when")

    body_html = (
        page.get("body_view_html", "")
        or page.get("body_export_view_html", "")
        or page.get("body_storage_html", "")
        or ""
    )

    return {
        "page_id": page_id,
        "page_title": page_title,
        "title_path": title_path,
        "page_url": page_url,
        "version": version,
        "updated_at": updated_at,
        "body_html": body_html,
    }


def synthesize_page_document(page_meta: dict[str, Any], body_text: str) -> str:
    '''
    Elasticsearch에 저장할 page-level document를 만든다.
    '''
    return safe_join_text(
        [
            f"# {page_meta['page_title']}",
            f"문서 경로: {page_meta['title_path']}",
            f"page_id: {page_meta['page_id']}",
            f"version: {page_meta['version']}",
            f"updated_at: {page_meta['updated_at']}",
            "[본문]",
            body_text,
        ]
    )


def process_page(page_file: Path) -> list[PageChunk]:
    '''
    단일 page JSON을 PageChunk 목록으로 변환한다.
    '''
    with page_file.open("r", encoding="utf-8") as f:
        data = json.load(f)

    page_meta = extract_page_metadata(data)
    body_text = html_to_text(page_meta["body_html"])

    final_document = synthesize_page_document(
        page_meta=page_meta,
        body_text=body_text,
    )

    pieces = chunk_text(final_document)
    total = len(pieces)

    chunks = []

    for idx, piece in enumerate(pieces):
        chunk_id = (
            f"{page_meta['page_id']}:page_document:"
            f"{idx:04d}:{content_hash(piece)[:10]}"
        )

        embedding_text = safe_join_text(
            [
                f"문서 제목: {page_meta['page_title']}",
                f"문서 경로: {page_meta['title_path']}",
                piece,
            ]
        )

        chunks.append(
            PageChunk(
                id=chunk_id,
                text=piece,
                embedding_text=embedding_text,
                page_id=page_meta["page_id"],
                page_title=page_meta["page_title"],
                page_url=page_meta["page_url"],
                title_path=page_meta["title_path"],
                version=page_meta["version"],
                updated_at=page_meta["updated_at"],
                chunk_index=idx,
                chunk_count=total,
                content_hash=content_hash(final_document),
            )
        )

    return chunks


# =============================================================================
# 5. Elasticsearch Index Build
# =============================================================================

def es_index_exists_with_docs(index_name: str = ES_INDEX_NAME) -> bool:
    '''
    Elasticsearch index가 존재하고 문서가 1개 이상 있는지 확인한다.
    '''
    if not es.indices.exists(index=index_name):
        return False

    count = es.count(index=index_name).get("count", 0)
    return int(count) > 0


def create_es_index(
    vector_dim: int,
    index_name: str = ES_INDEX_NAME,
    force_rebuild: bool = False,
) -> None:
    '''
    Elasticsearch index를 생성한다.

    force_rebuild=True이면 기존 index를 삭제하고 새로 만든다.
    '''
    exists = es.indices.exists(index=index_name)

    if exists and force_rebuild:
        es.indices.delete(index=index_name)
        exists = False

    if exists:
        return

    mappings = {
        "properties": {
            "id": {"type": "keyword"},
            "source_type": {"type": "keyword"},

            "page_id": {"type": "keyword"},
            "page_title": {
                "type": "text",
                "fields": {
                    "keyword": {"type": "keyword"}
                },
            },
            "page_url": {"type": "keyword"},
            "title_path": {
                "type": "text",
                "fields": {
                    "keyword": {"type": "keyword"}
                },
            },

            "version": {"type": "integer"},
            "updated_at": {"type": "text"},
            "indexed_at": {"type": "text"},

            "chunk_index": {"type": "integer"},
            "chunk_count": {"type": "integer"},
            "content_hash": {"type": "keyword"},

            "text": {"type": "text"},
            "embedding_text": {"type": "text"},

            "embedding": {
                "type": "dense_vector",
                "dims": vector_dim,
                "index": True,
                "similarity": "cosine",
            },
        }
    }

    settings = {
        "number_of_shards": 1,
        "number_of_replicas": 0,
    }

    es.indices.create(
        index=index_name,
        settings=settings,
        mappings=mappings,
    )

    print(f"Elasticsearch index 생성 완료: {index_name}")


def build_elasticsearch_db(
    force_rebuild: bool = False,
    index_name: str = ES_INDEX_NAME,
) -> None:
    '''
    Elasticsearch Vector DB를 생성한다.
    기존 index에 문서가 있으면 force_rebuild=False일 때 생성 과정을 건너뛴다.
    '''
    if not force_rebuild:
        if es_index_exists_with_docs(index_name):
            count = es.count(index=index_name).get("count", 0)
            print(f"기존 Elasticsearch index 사용: {index_name}, docs={count}")
            return
        else:
            print(f"⚠️ Elasticsearch index '{index_name}'가 존재하지 않거나 비어 있습니다.")
            print(f"   5_build_ES.py를 실행하여 인덱스를 먼저 빌드해 주세요.")
            return

    page_files = sorted(POLICY_PAGE_DIR.glob(PAGE_JSON_GLOB))
    print(f"처리할 page 수: {len(page_files)}")

    all_chunks: list[PageChunk] = []
    errors = []

    for page_file in tqdm(page_files, desc="페이지 처리"):
        try:
            all_chunks.extend(process_page(page_file))
        except Exception as exc:
            errors.append(
                {
                    "file": page_file.name,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    print(f"생성된 chunk 수: {len(all_chunks)}")

    if not all_chunks:
        raise RuntimeError("생성된 chunk가 없습니다. POLICY_PAGE_DIR와 JSON 구조를 확인하세요.")

    embedder = get_embedder()
    vector_dim = embedder.get_sentence_embedding_dimension()

    create_es_index(
        vector_dim=vector_dim,
        index_name=index_name,
        force_rebuild=force_rebuild,
    )

    actions = []
    batch_size = 8

    for start in tqdm(range(0, len(all_chunks), batch_size), desc="Embedding + ES indexing"):
        batch_chunks = all_chunks[start:start + batch_size]
        texts = [c.embedding_text[:6000] for c in batch_chunks]

        vectors = embedder.encode(
            texts,
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )

        vectors = np.asarray(vectors, dtype="float32")

        for chunk, vector in zip(batch_chunks, vectors):
            doc = asdict(chunk)
            doc["embedding"] = vector.tolist()
            # version이 None이면 ES integer 매핑에서 에러 발생 → 0으로 대체
            if doc.get("version") is None:
                doc["version"] = 0

            actions.append(
                {
                    "_op_type": "index",
                    "_index": index_name,
                    "_id": chunk.id,
                    "_source": doc,
                }
            )

        if len(actions) >= 128:
            success, errors = helpers.bulk(es, actions, raise_on_error=False)
            if errors:
                print(f"⚠️ Bulk indexing 부분 실패: {len(errors)}건")
                for err in errors[:3]:
                    print(f"  - {err}")
            actions.clear()

    if actions:
        success, errors = helpers.bulk(es, actions, raise_on_error=False)
        if errors:
            print(f"⚠️ Bulk indexing 부분 실패: {len(errors)}건")
            for err in errors[:3]:
                print(f"  - {err}")

    es.indices.refresh(index=index_name)

    count = es.count(index=index_name).get("count", 0)

    report = {
        "built_at": now_iso(),
        "index_name": index_name,
        "page_files": len(page_files),
        "chunks_total": len(all_chunks),
        "docs_count": count,
        "vector_dim": vector_dim,
        "errors": errors,
    }

    build_report_path = ES_REPORT_DIR / "build_report.json"
    with build_report_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("Elasticsearch Vector DB 생성 완료")
    print(f"index: {index_name}")
    print(f"docs: {count}")


# =============================================================================
# 6. Hybrid Search
# =============================================================================

def normalize_score_map(score_map: dict[str, float]) -> dict[str, float]:
    '''
    score dict를 0~1로 정규화한다.
    '''
    if not score_map:
        return {}

    values = list(score_map.values())
    min_v = min(values)
    max_v = max(values)

    if math.isclose(min_v, max_v):
        return {k: 1.0 for k in score_map}

    return {
        k: (v - min_v) / (max_v - min_v)
        for k, v in score_map.items()
    }


def _search_chunk_filter() -> dict[str, Any]:
    """본문 parent/child/faq/문서형 첨부를 검색 대상으로 제한 + 제외 page_id."""
    chunk_filter: dict[str, Any] = {"terms": {"chunk_type": list(_SEARCH_CHUNK_TYPES)}}
    excluded_ids = list(get_excluded_page_ids())
    if not excluded_ids:
        return chunk_filter
    return {
        "bool": {
            "filter": [chunk_filter],
            "must_not": [{"terms": {"page_id": excluded_ids}}],
        }
    }


def keyword_search(
    query: str,
    top_k: int = 30,
    index_name: str = ES_INDEX_NAME,
) -> list[SearchHit]:
    '''
    Elasticsearch BM25 keyword search.
    best_fields + cross_fields를 bool should로 결합하여 recall을 높인다.
    검색은 child_evidence / faq_evidence만 대상으로 한다.
    '''
    body = {
        "size": top_k,
        "query": {
            "bool": {
                "filter": [_search_chunk_filter()],
                "should": [
                    {
                        "multi_match": {
                            "query": query,
                            "fields": [
                                "page_title^5",
                                "keywords^4",
                                "lexical_boost^4",
                                "section_title^3",
                                "parent_section_title^3",
                                "title_path^3",
                                "text^2",
                                "content_dense",
                            ],
                            "type": "best_fields",
                        }
                    },
                    {
                        "multi_match": {
                            "query": query,
                            "fields": [
                                "page_title^3",
                                "keywords^3",
                                "lexical_boost^3",
                                "title_path^2",
                                "section_title^2",
                                "content_dense^2",
                                "text",
                            ],
                            "type": "cross_fields",
                        }
                    },
                ],
                "minimum_should_match": 1,
            }
        },
    }

    res = es.search(index=index_name, body=body)

    hits = []

    for h in res["hits"]["hits"]:
        source = h["_source"]
        source.pop("embedding", None)

        hits.append(
            SearchHit(
                doc_id=h["_id"],
                payload=source,
                score=float(h["_score"]),
                semantic_score=0.0,
                keyword_score=float(h["_score"]),
            )
        )

    _log_search_step(
        "keyword_search",
        query=query,
        top_k=top_k,
        hits_count=len(hits),
        hits=_debug_hits(
            hits,
            extra_fn=lambda h, _r: {
                "es_bm25_score": round(float(h.keyword_score or h.score), 4),
            },
        ),
        truncated=len(hits) > _SEARCH_DEBUG_MAX_HITS,
    )

    return hits


_SEARCH_DEBUG_FILE = "06_search_pipeline.jsonl"
_SEARCH_DEBUG_MAX_HITS = 50


def _log_search(filename: str, record: dict) -> None:
    """검색 파이프라인 디버그 로그를 기록한다."""
    if not rag_debug_logger.is_enabled():
        return
    record.setdefault("ts", rag_debug_logger._ts())
    rag_debug_logger._write(filename, record)


def _log_search_step(step: str, **fields: Any) -> None:
    """06_search_pipeline.jsonl — 단계별 RAG 검색 디버그."""
    merged = {**rag_debug_logger.get_search_log_fields(), **fields}
    _log_search(_SEARCH_DEBUG_FILE, {"step": step, **merged})


def _debug_hit_row(
    hit: SearchHit,
    *,
    rank: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """검색 디버그용 hit 요약 (doc/페이지/섹션/parent/점수/미리보기)."""
    p = hit.payload or {}
    row: dict[str, Any] = {
        "rank": rank,
        "doc_id": hit.doc_id,
        "score": round(float(hit.score), 6),
        "semantic_score": round(float(hit.semantic_score), 6) if hit.semantic_score else None,
        "keyword_score": round(float(hit.keyword_score), 6) if hit.keyword_score else None,
        "page_id": p.get("page_id"),
        "page_title": p.get("page_title"),
        "section_title": p.get("section_title") or p.get("parent_section_title"),
        "parent_doc_id": p.get("parent_doc_id"),
        "chunk_type": p.get("chunk_type"),
        "title_path": str(p.get("title_path") or "")[:160],
        "text_preview": str(p.get("content_dense") or p.get("text") or "")[:240],
    }
    if extra:
        row.update(extra)
    return row


def _debug_hits(
    hits: list[SearchHit],
    *,
    max_items: int | None = None,
    extra_fn: Any = None,
) -> list[dict[str, Any]]:
    """hit 목록을 디버그 row 리스트로 변환 (상한 적용)."""
    limit = _SEARCH_DEBUG_MAX_HITS if max_items is None else max_items
    rows: list[dict[str, Any]] = []
    for i, hit in enumerate(hits[:limit]):
        extra = extra_fn(hit, i + 1) if extra_fn else None
        rows.append(_debug_hit_row(hit, rank=i + 1, extra=extra))
    return rows


def _page_id_from_hit(hit: SearchHit) -> str:
    payload = hit.payload or {}
    return str(payload.get("page_id") or payload.get("page_title") or hit.doc_id)


def select_diverse_parents(
    parents: list[SearchHit],
    *,
    max_total: int,
    min_pages: int,
    max_per_page: int,
) -> list[SearchHit]:
    """score 순 유지: 최소 min_pages·page당 max_per_page·총 max_total."""
    if not parents or max_total <= 0:
        return []
    min_pages = max(1, min_pages)
    max_per_page = max(1, max_per_page)

    by_page: dict[str, list[SearchHit]] = {}
    for h in parents:
        pid = _page_id_from_hit(h) or "__unknown__"
        by_page.setdefault(pid, []).append(h)
    for hits in by_page.values():
        hits.sort(key=lambda x: x.score, reverse=True)

    selected: list[SearchHit] = []
    seen: set[str] = set()
    per_page: dict[str, int] = {}

    def can_take(h: SearchHit, pid: str) -> bool:
        return (
            h.doc_id not in seen
            and len(selected) < max_total
            and per_page.get(pid, 0) < max_per_page
        )

    def take(h: SearchHit, pid: str) -> None:
        selected.append(h)
        seen.add(h.doc_id)
        per_page[pid] = per_page.get(pid, 0) + 1

    page_order = sorted(by_page, key=lambda p: by_page[p][0].score, reverse=True)

    # 1) page별 best — 상위 page부터 min_pages 확보
    for pid in page_order[:min_pages]:
        if len(selected) >= max_total:
            break
        h = by_page[pid][0]
        if can_take(h, pid):
            take(h, pid)

    # 2) 남은 슬롯 — 전역 score 순, page당 상한 유지
    for h in sorted(parents, key=lambda x: x.score, reverse=True):
        if len(selected) >= max_total:
            break
        pid = _page_id_from_hit(h) or "__unknown__"
        if can_take(h, pid):
            take(h, pid)

    # 3) min_pages 미달이면 아직 없는 page에서 best 추가
    if len(per_page) < min_pages:
        for pid in page_order:
            if len(per_page) >= min_pages or len(selected) >= max_total:
                break
            if pid in per_page:
                continue
            h = by_page[pid][0]
            if can_take(h, pid):
                take(h, pid)

    return selected


def _mark_payload_flag(hit: SearchHit, key: str, value: bool = True) -> SearchHit:
    payload = dict(hit.payload or {})
    payload[key] = value
    hit.payload = payload
    return hit


def _hit_evidence_key(hit: SearchHit) -> str:
    """child chunk 단위 키 (doc_id)."""
    return hit.doc_id


def _merge_hit_into_list(
    hits: list[SearchHit],
    incoming: SearchHit,
    *,
    seen_keys: set[str] | None = None,
) -> list[SearchHit]:
    """evidence key 기준 중복 없이 hit 추가."""
    if seen_keys is None:
        seen_keys = {_hit_evidence_key(h) for h in hits}

    key = _hit_evidence_key(incoming)
    if key in seen_keys:
        return hits

    seen_keys.add(key)
    return hits + [incoming]


def semantic_search(
    query: str,
    top_k: int = 30,
    index_name: str = ES_INDEX_NAME,
) -> list[SearchHit]:
    '''
    Elasticsearch dense_vector kNN semantic search.
    num_candidates를 넉넉하게 설정하여 recall을 높인다.
    '''
    # rag_tree_hybrid 인덱스: 문서는 content_dense(컨텍스트 포함 원문)로 임베딩됨.
    # 쿼리도 plain text로 인코딩해야 임베딩 공간이 일치한다.
    query_embedding_text = query

    from app.ml.inference import embed_query_sync

    query_vector = embed_query_sync(query_embedding_text)

    res = es.search(
        index=index_name,
        size=top_k,
        knn={
            "field": "embedding",
            "query_vector": query_vector,
            "k": top_k,
            "num_candidates": max(top_k * 8, 120),
            "filter": _search_chunk_filter(),
        },
        source_excludes=["embedding"],
    )

    hits = []

    for h in res["hits"]["hits"]:
        hits.append(
            SearchHit(
                doc_id=h["_id"],
                payload=h["_source"],
                score=float(h["_score"]),
                semantic_score=float(h["_score"]),
                keyword_score=0.0,
            )
        )

    _log_search_step(
        "semantic_search",
        query=query,
        query_embedding_text=query_embedding_text,
        embedding_model=EMBEDDING_MODEL_NAME,
        top_k=top_k,
        hits_count=len(hits),
        hits=_debug_hits(
            hits,
            extra_fn=lambda h, _r: {
                "es_knn_score": round(float(h.semantic_score or h.score), 4),
            },
        ),
        truncated=len(hits) > _SEARCH_DEBUG_MAX_HITS,
    )

    return hits


def _rrf_score(rank: int, k: int = 60) -> float:
    '''
    Reciprocal Rank Fusion score.
    k=60은 논문 기본값으로, rank-based fusion에서 안정적인 결과를 보인다.
    '''
    return 1.0 / (k + rank)


def _rrf_fuse_child_hits(
    semantic_hits: list[SearchHit],
    keyword_hits: list[SearchHit],
) -> list[SearchHit]:
    """단일 subquery: child doc_id 기준 RRF merge (rough ordering only)."""
    keyword_sorted = sorted(keyword_hits, key=lambda h: h.score, reverse=True)

    semantic_rrf = {hit.doc_id: _rrf_score(i + 1) for i, hit in enumerate(semantic_hits)}
    keyword_rrf = {hit.doc_id: _rrf_score(i + 1) for i, hit in enumerate(keyword_sorted)}

    hits_by_doc_id: dict[str, SearchHit] = {}
    for hit in semantic_hits + keyword_sorted:
        if hit.doc_id not in hits_by_doc_id:
            hits_by_doc_id[hit.doc_id] = hit

    fused: list[SearchHit] = []
    for rank, (doc_id, hit) in enumerate(
        sorted(
            hits_by_doc_id.items(),
            key=lambda item: (
                semantic_rrf.get(item[0], 0.0) + keyword_rrf.get(item[0], 0.0)
            ) * retrieval_weight_for_chunk(item[1].payload or {}),
            reverse=True,
        ),
        start=1,
    ):
        s_rank = next(
            (i + 1 for i, h in enumerate(semantic_hits) if h.doc_id == doc_id),
            None,
        )
        k_rank = next(
            (i + 1 for i, h in enumerate(keyword_sorted) if h.doc_id == doc_id),
            None,
        )
        s_rrf = semantic_rrf.get(doc_id, 0.0)
        k_rrf = keyword_rrf.get(doc_id, 0.0)
        weight = retrieval_weight_for_chunk(hit.payload or {})
        combined = (s_rrf + k_rrf) * weight

        payload = dict(hit.payload or {})
        payload["_rrf_rank"] = rank
        payload["_semantic_rank"] = s_rank
        payload["_keyword_rank"] = k_rank
        payload["_from_semantic"] = s_rank is not None
        payload["_from_keyword"] = k_rank is not None

        fused.append(
            SearchHit(
                doc_id=doc_id,
                payload=payload,
                score=combined,
                semantic_score=s_rrf,
                keyword_score=k_rrf,
            )
        )

    return fused


def _merge_fused_child(
    store: dict[str, SearchHit],
    incoming: SearchHit,
    *,
    subquery_idx: int,
    page_subqueries: dict[str, set[int]],
) -> None:
    """다중 subquery RRF 결과를 doc_id 기준으로 병합 (최고 RRF 점수 유지)."""
    page_id = _page_id_from_hit(incoming)
    page_subqueries.setdefault(page_id, set()).add(subquery_idx)

    prev = store.get(incoming.doc_id)
    if prev is None:
        store[incoming.doc_id] = incoming
        return

    in_payload = incoming.payload or {}
    prev_payload = dict(prev.payload or {})

    for key in ("_semantic_rank", "_keyword_rank"):
        new_r = in_payload.get(key)
        old_r = prev_payload.get(key)
        if new_r is not None and (old_r is None or new_r < old_r):
            prev_payload[key] = new_r

    prev_payload["_from_semantic"] = prev_payload.get("_from_semantic") or in_payload.get(
        "_from_semantic"
    )
    prev_payload["_from_keyword"] = prev_payload.get("_from_keyword") or in_payload.get(
        "_from_keyword"
    )

    if incoming.score > prev.score:
        prev.doc_id = incoming.doc_id
        prev.payload = {**in_payload, **prev_payload}
        prev.score = incoming.score
        prev.semantic_score = incoming.semantic_score
        prev.keyword_score = incoming.keyword_score
    else:
        prev.payload = {**prev_payload, **in_payload}
        prev.semantic_score = max(prev.semantic_score, incoming.semantic_score)
        prev.keyword_score = max(prev.keyword_score, incoming.keyword_score)
        prev.score = max(prev.score, incoming.score)

    store[incoming.doc_id] = prev


def _aggregate_pages_from_children(
    children: list[SearchHit],
    page_subqueries: dict[str, set[int]],
) -> list[_PageStats]:
    """child hit을 page_id 기준으로 묶고 ranking용 통계를 계산한다."""
    by_page: dict[str, list[SearchHit]] = {}
    for hit in children:
        by_page.setdefault(_page_id_from_hit(hit), []).append(hit)

    stats_list: list[_PageStats] = []
    for page_id, hits in by_page.items():
        semantic_count = sum(
            1 for h in hits if (h.payload or {}).get("_from_semantic")
        )
        bm25_count = sum(1 for h in hits if (h.payload or {}).get("_from_keyword"))

        ranks: list[int] = []
        for h in hits:
            p = h.payload or {}
            for key in ("_semantic_rank", "_keyword_rank"):
                r = p.get(key)
                if r is not None:
                    ranks.append(int(r))

        top_rank = min(ranks) if ranks else 9999
        avg_rank = sum(ranks) / len(ranks) if ranks else 9999.0

        stats_list.append(
            _PageStats(
                page_id=page_id,
                hits=hits,
                chunk_count=len(hits),
                semantic_count=semantic_count,
                bm25_count=bm25_count,
                subquery_coverage=len(page_subqueries.get(page_id, set())),
                top_rank=top_rank,
                avg_rank=avg_rank,
            )
        )

    return stats_list


def _rank_pages(
    page_stats: list[_PageStats],
    *,
    n_queries: int,
) -> list[_PageStats]:
    """
    page relevance — rerank 절대 점수 + 청크 밀도·hybrid·subquery 보너스.
    """
    if not page_stats:
        return []

    n_q = max(n_queries, 1)

    for stats in page_stats:
        max_rerank = max(h.score for h in stats.hits)
        density_bonus = 1.0 + (min(stats.chunk_count, 5) * 0.05)
        hybrid_bonus = (
            1.1 if stats.semantic_count > 0 and stats.bm25_count > 0 else 1.0
        )
        coverage_bonus = 1.0 + (0.05 * (stats.subquery_coverage / n_q))
        stats.page_score = min(
            max_rerank * density_bonus * hybrid_bonus * coverage_bonus,
            1.0,
        )

    page_stats.sort(key=lambda s: s.page_score, reverse=True)
    return page_stats


_CURRENT_YEAR_HINTS: tuple[str, ...] = (
    "올해",
    "금년",
    "이번 연도",
    "당해년",
)


def _extract_reference_year_from_queries(queries: list[str]) -> int | None:
    """검색 쿼리 묶음에서 기준 연도 추출."""
    blob = " ".join((q or "").strip() for q in queries if (q or "").strip())
    if not blob:
        return None

    explicit = [int(y) for y in re.findall(r"(20\d{2})", blob)]
    if explicit:
        return max(explicit)

    if any(hint in blob for hint in _CURRENT_YEAR_HINTS):
        return datetime.now(timezone("Asia/Seoul")).year

    return None


def _temporal_page_score_multiplier(
    page_title: str,
    reference_year: int | None,
) -> float:
    """기준 연도 질문 시 구·마감 문서 page_score 보정."""
    if reference_year is None:
        return 1.0

    title = (page_title or "").strip()
    if not title:
        return 1.0

    if "(마감)" in title or "마감)" in title:
        return 0.35

    title_years = [int(y) for y in re.findall(r"(20\d{2})", title)]
    if title_years:
        doc_year = max(title_years)
        if doc_year < reference_year:
            return 0.45
        if doc_year == reference_year:
            return 1.15

    return 1.0


def _apply_temporal_page_ranking(
    ranked_pages: list[_PageStats],
    *,
    reference_year: int | None,
) -> list[_PageStats]:
    if reference_year is None or not ranked_pages:
        return ranked_pages

    for stats in ranked_pages:
        page_title = ""
        if stats.hits:
            page_title = str((stats.hits[0].payload or {}).get("page_title") or "")
        mult = _temporal_page_score_multiplier(page_title, reference_year)
        stats.page_score = min(float(stats.page_score or 0.0) * mult, 1.0)

    ranked_pages.sort(key=lambda s: s.page_score, reverse=True)
    return ranked_pages


def _rerank_score_from_hit(hit: SearchHit) -> float:
    """리랭커 원본 점수 (_rerank_score 우선)."""
    payload = hit.payload or {}
    raw = payload.get("_rerank_score")
    if raw is not None:
        return float(raw)
    return float(hit.score or 0.0)


def _filter_hits_by_rerank_score(
    hits: list[SearchHit],
    *,
    relative_gap: float,
    min_keep: int = 1,
) -> list[SearchHit]:
    """
    rerank 점수 순 유지 — 최상위 대비 relative_gap 초과 항목 제거.
    min_keep: 전부 탈락 시 상위 N개만 유지.
    """
    if not hits or relative_gap <= 0:
        return hits

    ordered = sorted(hits, key=_rerank_score_from_hit, reverse=True)
    top = _rerank_score_from_hit(ordered[0])
    kept = [h for h in ordered if (top - _rerank_score_from_hit(h)) <= relative_gap]
    if kept:
        return kept
    return ordered[: max(1, min_keep)]


def _page_max_rerank_score(stats: _PageStats) -> float:
    if not stats.hits:
        return 0.0
    return max(_rerank_score_from_hit(h) for h in stats.hits)


def _filter_ranked_pages_by_rerank_gap(
    ranked_pages: list[_PageStats],
    *,
    relative_gap: float,
    min_keep: int = 1,
) -> tuple[list[_PageStats], list[dict[str, Any]]]:
    """
    page 애그리게이션·ranking 직후 — page별 max rerank로 gap 필터.
    상위 min_keep page는 gap과 무관하게 유지, 나머지는 gap 이내만 추가.
    page_score 정렬 순서를 유지한다.
    """
    if not ranked_pages:
        return [], []
    if relative_gap <= 0:
        return ranked_pages, []

    top_rerank = max(_page_max_rerank_score(s) for s in ranked_pages)
    floor = max(1, int(min_keep or 1))
    mandatory = ranked_pages[: min(floor, len(ranked_pages))]
    mandatory_ids = {s.page_id for s in mandatory}

    kept: list[_PageStats] = list(mandatory)
    dropped_rows: list[dict[str, Any]] = []

    for stats in ranked_pages:
        if stats.page_id in mandatory_ids:
            continue
        max_rr = _page_max_rerank_score(stats)
        gap = round(top_rerank - max_rr, 6)
        if gap <= relative_gap:
            kept.append(stats)
        else:
            dropped_rows.append(
                {
                    "page_id": stats.page_id,
                    "max_rerank_score": round(max_rr, 6),
                    "gap_from_top": gap,
                    "page_score": round(stats.page_score, 6),
                    "page_title": (stats.hits[0].payload or {}).get("page_title")
                    if stats.hits
                    else None,
                }
            )

    if not kept:
        kept = ranked_pages[:floor]
        dropped_rows = [
            {
                "page_id": s.page_id,
                "max_rerank_score": round(_page_max_rerank_score(s), 6),
                "gap_from_top": round(top_rerank - _page_max_rerank_score(s), 6),
                "reason": "min_keep_fallback",
            }
            for s in ranked_pages[floor:]
        ]

    return kept, dropped_rows


def _filter_children_by_pages(
    children: list[SearchHit],
    selected_page_ids: set[str],
) -> list[SearchHit]:
    """선택된 page에 속한 child만 유지 (rerank 점수 순)."""
    filtered = [
        h for h in children if _page_id_from_hit(h) in selected_page_ids
    ]
    filtered.sort(key=lambda h: h.score, reverse=True)
    return filtered


# 검색(리트리벌) 전용 청크 — child/faq/attachment만 (parent는 mget으로 답변 확장)
_SEARCH_CHUNK_TYPES = frozenset({
    "child_evidence",
    "faq_evidence",
    "attachment_evidence",
})

_RESOURCE_CHUNK_TYPES = frozenset({
    "attachment_summary",
    "attachment_collection",
    "reference_link",
})

_PARENT_FETCH_SOURCE = [
    "id", "chunk_type", "section_title", "section_type",
    "text",
    "page_title", "title_path", "page_url", "page_summary",
    # post_process_tree_hybrid 필드 — 리소스 블록 생성에 사용
    "urls", "attachments", "images",
]


def _normalize_url(url: str) -> str:
    u = (url or "").strip()
    if u and not u.startswith(("http://", "https://")):
        return ""
    return u


def _attachment_summary_from_payload(payload: dict[str, Any]) -> str:
    """첨부 요약만 (summary_context 또는 content_dense '요약:' — 토큰 절약)."""
    p = payload or {}
    summary = str(p.get("summary_context") or "").strip()
    if summary:
        return summary[:200]
    dense = str(p.get("content_dense") or "")
    if "요약:" in dense:
        part = dense.split("요약:", 1)[1].strip()
        for sep in ("\n\n근거:", "\n\n맥락:", "\n\n청크유형:"):
            if sep in part:
                part = part.split(sep, 1)[0]
        if part.strip():
            return part.strip()[:200]
    return ""


def _extract_resources_from_payload(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    단일 ES payload에서 첨부·링크 메타 추출.

    rag_tree_hybrid 신규 형식:
      - attachments: [{title, download_url, file_size, saved_path, webui}]
      - urls:        [{text, url}]
      - images:      [{filename, url, description}]

    레거시 형식 (fallback):
      - attachment_title / attachment_url / attachment_saved_path
      - link_url / link_text / link_purpose
    """
    attachments: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    p = payload or {}
    chunk_type = str(p.get("chunk_type") or "")

    # ── 신규: attachments list ────────────────────────────────────────────────
    for att in (p.get("attachments") or []):
        if not isinstance(att, dict):
            continue
        title = str(att.get("title") or "").strip()
        url = str(att.get("download_url") or att.get("url") or "").strip()
        saved = str(att.get("saved_path") or "").strip()
        if title or url or saved:
            attachments.append({
                "title": title or "첨부파일",
                "url": url,
                "extension": str(Path(title).suffix).lower() if title else "",
                "saved_path": saved,
            })

    # ── 신규: urls list → links ───────────────────────────────────────────────
    for u in (p.get("urls") or []):
        if not isinstance(u, dict):
            continue
        url = _normalize_url(str(u.get("url") or ""))
        if url:
            links.append({
                "text": str(u.get("text") or url).strip(),
                "url": url,
                "purpose": "",
            })

    # ── 레거시 fallback (old format) ──────────────────────────────────────────
    if not attachments:
        saved_path = str(p.get("attachment_saved_path") or "").strip()
        att_url = resolve_attachment_url(
            saved_path=saved_path,
            fallback_url=str(p.get("attachment_url") or ""),
        )
        att_title = str(p.get("attachment_title") or "").strip()
        att_summary = _attachment_summary_from_payload(p) if chunk_type.startswith("attachment") else ""
        if att_url or saved_path or att_title:
            entry: dict[str, Any] = {
                "title": att_title or "첨부파일",
                "url": att_url,
                "extension": str(p.get("attachment_extension") or "").strip().lower(),
                "saved_path": saved_path,
            }
            if att_summary:
                entry["summary"] = att_summary
            attachments.append(entry)

    if not links:
        link_url = _normalize_url(str(p.get("link_url") or ""))
        if link_url:
            links.append({
                "text": str(p.get("link_text") or p.get("link_purpose") or link_url).strip(),
                "url": link_url,
                "purpose": str(p.get("link_purpose") or "").strip(),
            })

    return attachments, links


def _merge_resource_lists(
    base_att: list[dict[str, Any]],
    base_links: list[dict[str, Any]],
    extra_att: list[dict[str, Any]],
    extra_links: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """파일명 기준 첨부 병합(URL·요약 합침), 링크는 URL 기준 중복 제거."""
    merged_att: dict[str, dict[str, Any]] = {}
    for item in base_att + extra_att:
        key = (
            str(item.get("title") or "").strip().lower()
            or str(item.get("saved_path") or "").strip().lower()
            or str(item.get("url") or "").strip()
        )
        if not key:
            continue
        if key not in merged_att:
            merged_att[key] = dict(item)
            continue
        cur = merged_att[key]
        for field in ("url", "saved_path", "extension", "title"):
            if not cur.get(field) and item.get(field):
                cur[field] = item[field]
        if len(str(item.get("summary") or "")) > len(str(cur.get("summary") or "")):
            cur["summary"] = item.get("summary")

    seen_link: set[str] = set()
    out_att = list(merged_att.values())
    out_links: list[dict[str, Any]] = []

    for item in base_links + extra_links:
        key = item.get("url") or ""
        if not key or key in seen_link:
            continue
        seen_link.add(key)
        out_links.append(item)

    return out_att, out_links


def _collect_page_resources(group: list[SearchHit]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """한 page에 속한 모든 청크에서 첨부·링크 메타를 모은다."""
    attachments: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []

    for hit in group:
        att, lk = _extract_resources_from_payload(hit.payload or {})
        attachments, links = _merge_resource_lists(attachments, links, att, lk)

    return attachments, links


def fetch_page_resources_from_es(
    page_id: str,
    index_name: str = ES_INDEX_NAME,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    검색 top-k 히트에 리소스가 없을 때, ES에서 동일 page_id의 첨부·링크를 보강한다.

    rag_tree_hybrid 인덱스:
      - resources(urls, attachments)는 parent_section 청크에 저장되므로
        parent_section 청크를 조회한 뒤 _extract_resources_from_payload로 추출한다.
    레거시 인덱스 fallback:
      - attachment_summary / attachment_collection / reference_link 청크를 조회한다.
    """
    if not page_id or not es.indices.exists(index=index_name):
        return [], []

    resource_source = [
        "chunk_type",
        "urls", "attachments", "images",
        "attachment_title", "attachment_url", "attachment_extension",
        "attachment_saved_path", "link_text", "link_url", "link_purpose",
    ]

    # ── 1) rag_tree_hybrid: parent_section 청크에서 urls/attachments 추출 ───────
    parent_body = {
        "size": 30,
        "_source": resource_source,
        "query": {
            "bool": {
                "filter": [
                    {"term": {"page_id": page_id}},
                    {"term": {"chunk_type": "parent_section"}},
                ],
                "should": [
                    {"exists": {"field": "urls"}},
                    {"exists": {"field": "attachments"}},
                ],
                "minimum_should_match": 1,
            }
        },
    }

    attachments: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []

    try:
        res = es.search(index=index_name, body=parent_body)
        for h in res.get("hits", {}).get("hits", []):
            att, lk = _extract_resources_from_payload(h.get("_source") or {})
            attachments, links = _merge_resource_lists(attachments, links, att, lk)
    except Exception:
        pass

    # ── 2) 레거시 fallback: attachment/link 전용 청크 조회 ─────────────────────
    if not attachments and not links:
        legacy_body = {
            "size": 25,
            "_source": resource_source,
            "query": {
                "bool": {
                    "filter": [{"term": {"page_id": page_id}}],
                    "should": [
                        {"terms": {"chunk_type": list(_RESOURCE_CHUNK_TYPES)}},
                        {"exists": {"field": "attachment_url"}},
                        {"exists": {"field": "link_url"}},
                    ],
                    "minimum_should_match": 1,
                }
            },
        }
        try:
            res = es.search(index=index_name, body=legacy_body)
            for h in res.get("hits", {}).get("hits", []):
                att, lk = _extract_resources_from_payload(h.get("_source") or {})
                attachments, links = _merge_resource_lists(attachments, links, att, lk)
        except Exception:
            pass

    return attachments, links


_PARENT_CATALOG_SOURCE = [
    "id",
    "page_id",
    "page_title",
    "page_summary",
    "section_title",
    "section_type",
    "section_id",
]


def page_ids_from_search_hits(hits: Sequence[SearchHit]) -> list[str]:
    """검색 hit에서 page_id 목록 (중복 제거, 순서 유지)."""
    seen: list[str] = []
    for hit in hits or []:
        payload = hit.payload or {}
        page_id = str(payload.get("page_id") or "").strip()
        if not page_id:
            page_id = _page_id_from_hit(hit)
        if not page_id or page_id == "__unknown__" or page_id in seen:
            continue
        seen.append(page_id)
    return seen


def fetch_page_parent_catalog(
    page_ids: list[str],
    *,
    index_name: str = ES_INDEX_NAME,
) -> list[dict[str, Any]]:
    """page별 parent_section 메타데이터만 조회 (본문 text 제외)."""
    unique = [p for p in dict.fromkeys(page_ids) if p and p != "__unknown__"]
    if not unique or not es.indices.exists(index=index_name):
        return []

    body = {
        "size": max(50, len(unique) * 25),
        "_source": _PARENT_CATALOG_SOURCE,
        "query": {
            "bool": {
                "filter": [
                    {"terms": {"page_id": unique}},
                    {"term": {"chunk_type": "parent_section"}},
                ],
            },
        },
    }
    try:
        res = es.search(index=index_name, body=body)
    except Exception:
        return []

    catalog: list[dict[str, Any]] = []
    for hit in res.get("hits", {}).get("hits", []):
        src = dict(hit.get("_source") or {})
        doc_id = str(hit.get("_id") or src.get("id") or "").strip()
        if not doc_id:
            continue
        src["id"] = doc_id
        catalog.append(src)

    catalog.sort(
        key=lambda x: (
            str(x.get("page_id") or ""),
            str(x.get("section_id") or ""),
            str(x.get("section_title") or ""),
        )
    )
    return catalog


def expand_parent_ids_by_page_neighbors(
    seed_parent_ids: list[str],
    catalog: list[dict[str, Any]],
    *,
    neighbor_radius: int = 2,
    max_total: int = 12,
    max_per_page: int = 6,
) -> list[str]:
    """seed parent_id 기준 같은 page catalog에서 section_id 순 ±radius 이웃 확장."""
    seeds = [p for p in dict.fromkeys(seed_parent_ids) if p]
    if not seeds or not catalog or neighbor_radius < 0:
        return seeds

    by_page: dict[str, list[dict[str, Any]]] = {}
    for item in catalog:
        page_id = str(item.get("page_id") or "").strip()
        by_page.setdefault(page_id, []).append(item)

    page_order: dict[str, list[str]] = {}
    id_to_page: dict[str, str] = {}
    id_to_index: dict[str, int] = {}
    for page_id, items in by_page.items():
        sorted_items = sorted(
            items,
            key=lambda x: (
                str(x.get("section_id") or ""),
                str(x.get("section_title") or ""),
            ),
        )
        ids = [
            str(x.get("id") or "").strip()
            for x in sorted_items
            if str(x.get("id") or "").strip()
        ]
        page_order[page_id] = ids
        for idx, pid in enumerate(ids):
            id_to_page[pid] = page_id
            id_to_index[pid] = idx

    expanded: list[str] = []
    seen: set[str] = set()

    def _add(pid: str) -> None:
        if pid and pid not in seen:
            seen.add(pid)
            expanded.append(pid)

    for pid in seeds:
        _add(pid)
        page = id_to_page.get(pid)
        if not page:
            continue
        order = page_order.get(page) or []
        idx = id_to_index.get(pid, -1)
        if idx < 0:
            continue
        lo = max(0, idx - neighbor_radius)
        hi = min(len(order), idx + neighbor_radius + 1)
        for j in range(lo, hi):
            _add(order[j])

    if max_per_page > 0:
        seed_set = set(seeds)
        per_page: dict[str, int] = {}
        capped: list[str] = []
        for pid in expanded:
            page = id_to_page.get(pid, "__unknown__")
            if pid in seed_set or per_page.get(page, 0) < max_per_page:
                capped.append(pid)
                per_page[page] = per_page.get(page, 0) + 1
        expanded = capped

    if max_total > 0:
        expanded = expanded[:max_total]
    return expanded


def _search_hit_from_parent_payload(
    parent_id: str,
    parent: dict[str, Any],
    *,
    score: float = 0.5,
) -> SearchHit:
    """parent_section payload → Turn2용 SearchHit."""
    section_title = str(parent.get("section_title") or "").strip()
    parent_body = _parent_section_body(parent)
    chunk_parts: list[str] = []
    if parent_body:
        main_block = (
            f"▸ {section_title}\n{parent_body}" if section_title else parent_body
        )
        chunk_parts = [main_block]

    payload = dict(parent)
    payload["merged_chunks"] = chunk_parts
    payload["_parent_id"] = parent_id
    payload["_chunk_count"] = 1
    payload["_evidence_count"] = len(chunk_parts)
    payload["_source_chunk_ids"] = [parent_id]
    payload.setdefault("chunk_type", "parent_section")

    att, links = _extract_resources_from_payload(parent)
    payload["_attachments"] = att
    payload["_links"] = links
    citation_url = _resolve_citation_url(payload)
    if citation_url:
        payload["_citation_url"] = citation_url
        payload["page_url"] = citation_url

    return SearchHit(
        doc_id=parent_id,
        payload=payload,
        score=score,
        semantic_score=score,
        keyword_score=0.0,
    )


def search_hits_from_parent_ids(
    parent_ids: list[str],
    *,
    score_by_id: dict[str, float] | None = None,
    index_name: str = ES_INDEX_NAME,
) -> list[SearchHit]:
    """선택된 parent_id 목록 → mget 후 SearchHit 리스트."""
    unique = [p for p in dict.fromkeys(parent_ids) if p]
    if not unique:
        return []

    payloads = fetch_parent_payloads_by_ids(unique, index_name=index_name)
    scores = score_by_id or {}
    hits: list[SearchHit] = []
    for pid in unique:
        parent = payloads.get(pid)
        if not parent:
            continue
        hits.append(
            _search_hit_from_parent_payload(
                pid,
                parent,
                score=float(scores.get(pid, 0.5)),
            )
        )
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits


def _format_resources_context_block(
    attachments: list[dict[str, Any]],
    links: list[dict[str, Any]],
) -> str:
    """문서별 첨부 목록 — 번호·파일명·요약 (URL은 attachments_used로 선택 후 Slack에서만 노출)."""
    lines: list[str] = []
    if attachments:
        lines.append("관련 첨부 (attachments_used에서 '첨부자료N' 번호로 참조):")
        for i, a in enumerate(attachments, start=1):
            title = str(a.get("title") or "첨부").strip()
            summary = str(a.get("summary") or "").strip()
            parts = [f"첨부자료{i}", f"파일명: {title}"]
            if summary:
                parts.append(f"요약: {summary}")
            lines.append("  - " + " | ".join(parts))
    if links:
        lines.append("관련 링크 (답변·links_used에서 '링크N' 번호로 참조):")
        for i, link in enumerate(links, start=1):
            label = str(
                link.get("description") or link.get("text") or "링크"
            ).strip()
            url = str(link.get("url") or "").strip()
            if label.startswith(("http://", "https://")):
                label = "링크"
            lines.append(f"  - 링크{i}: {label} — {url}")
    return "\n".join(lines)


def _llm_context_hash(payload: dict[str, Any]) -> str:
    """llm_context 본문 기준 중복 판별용 해시."""
    ctx = (payload.get("llm_context") or payload.get("text") or "").strip()
    normalized = " ".join(ctx.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _evidence_dedupe_key(payload: dict[str, Any]) -> str:
    """
    동일 parent/동일 llm_context 창은 하나의 evidence로 취급.
    RRF·rerank pool이 chunk id 단위로 부풀려지지 않도록 한다.
    """
    parent_doc_id = str(payload.get("parent_doc_id") or "").strip()
    if parent_doc_id:
        return f"parent:{parent_doc_id}"
    return f"ctx:{_llm_context_hash(payload)}"


def _resolve_citation_url(payload: dict[str, Any]) -> str:
    """
    인용 링크 우선순위: page_url → attachment_url → link_url
    (slack_ui.get_doc_url이 page_url을 읽도록 page_url에도 반영)
    """
    page_url = _normalize_url(str(payload.get("page_url") or ""))
    if page_url:
        return page_url

    att_url = resolve_attachment_url(
        saved_path=str(payload.get("attachment_saved_path") or ""),
        fallback_url=str(payload.get("attachment_url") or ""),
    )
    if att_url:
        return att_url

    return _normalize_url(str(payload.get("link_url") or ""))


def _fetch_semantic_and_keyword_parallel(
    query: str,
    *,
    semantic_limit: int,
    keyword_limit: int,
    index_name: str,
) -> tuple[list[SearchHit], list[SearchHit]]:
    """단일 subquery — semantic·BM25 ES 호출 병렬."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_sem = pool.submit(
            semantic_search,
            query=query,
            top_k=semantic_limit,
            index_name=index_name,
        )
        f_kw = pool.submit(
            keyword_search,
            query=query,
            top_k=keyword_limit,
            index_name=index_name,
        )
        return f_sem.result(), f_kw.result()


def _tag_hits_with_retrieval_role(
    hits: list[SearchHit],
    *,
    role: str,
    query: str,
) -> list[SearchHit]:
    """ES hit에 retrieval role/direct|policy 메타 부착."""
    tagged: list[SearchHit] = []
    for hit in hits:
        payload = dict(hit.payload or {})
        payload["_retrieval_role"] = role
        payload["_retrieval_query"] = query
        hit.payload = payload
        tagged.append(hit)
    return tagged


def _build_retrieval_specs(
    query_or_queries: str | list[str] | None = None,
    *,
    direct_query: str | None = None,
    policy_query: str | None = None,
) -> list[tuple[str, str]]:
    """
    (role, query) 목록 — direct·policy 각각 semantic+BM25 수행 후 RRF.
    role: direct | policy | legacy | q0 …
    """
    specs: list[tuple[str, str]] = []
    d = (direct_query or "").strip()
    p = (policy_query or "").strip()
    if d:
        specs.append(("direct", d))
    if p:
        specs.append(("policy", p))
    if specs:
        return specs

    if query_or_queries is None:
        return []
    if isinstance(query_or_queries, str):
        q = query_or_queries.strip()
        return [("legacy", q)] if q else []
    out: list[tuple[str, str]] = []
    for i, raw in enumerate(query_or_queries):
        q = str(raw).strip()
        if q:
            out.append((f"q{i}", q))
    return out


def _retrieve_queries_parallel(
    retrieval_specs: list[tuple[str, str]],
    *,
    semantic_limit: int,
    keyword_limit: int,
    index_name: str,
) -> list[tuple[int, str, str, list[SearchHit], list[SearchHit]]]:
    """(role, query)별 ES retrieval — 쿼리 단위 병렬 (semantic + BM25 each)."""
    if not retrieval_specs:
        return []

    max_workers = min(max(len(retrieval_specs), 1), 6)
    results: list[tuple[int, str, str, list[SearchHit], list[SearchHit]]] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {
            pool.submit(
                _fetch_semantic_and_keyword_parallel,
                q,
                semantic_limit=semantic_limit,
                keyword_limit=keyword_limit,
                index_name=index_name,
            ): (qi, role, q)
            for qi, (role, q) in enumerate(retrieval_specs)
        }
        for future in as_completed(future_map):
            qi, role, q = future_map[future]
            sem_hits, kw_hits = future.result()
            sem_hits = _tag_hits_with_retrieval_role(sem_hits, role=role, query=q)
            kw_hits = _tag_hits_with_retrieval_role(kw_hits, role=role, query=q)
            results.append((qi, role, q, sem_hits, kw_hits))

    results.sort(key=lambda item: item[0])
    return results


def _rerank_child_candidates(
    hits: list[SearchHit],
    *,
    query_text: str,
    pool_size: int,
) -> list[SearchHit]:
    """RRF rough pool → reranker 재순위화 → score 갱신 후 재정렬."""
    if not hits or pool_size <= 0:
        return hits

    rerank_query = (query_text or "").strip()
    if not rerank_query:
        return hits

    pool_source = deduplicate_evidence_hits(hits, sort_by_score=True)
    pool = pool_source[:pool_size]
    if not pool:
        return hits

    pairs = [
        (rerank_query, _rerank_document_text(h.payload or {}))
        for h in pool
    ]

    try:
        rerank_scores = _reranker_compute_scores(pairs)
    except Exception as exc:
        _log_search_step("rerank_failed", error=f"{type(exc).__name__}: {exc}")
        return hits

    score_by_evidence: dict[str, float] = {}
    for hit, rerank_score in zip(pool, rerank_scores):
        score_by_evidence[_evidence_dedupe_key(hit.payload or {})] = rerank_score

    reranked_rows: list[dict[str, Any]] = []
    updated: list[SearchHit] = []
    for hit in hits:
        ev_key = _evidence_dedupe_key(hit.payload or {})
        if ev_key not in score_by_evidence:
            updated.append(hit)
            continue

        payload = dict(hit.payload or {})
        payload["_rrf_score"] = hit.score
        payload["_rerank_score"] = score_by_evidence[ev_key]
        hit.payload = payload
        hit.score = score_by_evidence[ev_key]
        updated.append(hit)
        reranked_rows.append(
            {
                "doc_id": hit.doc_id,
                "page_id": _page_id_from_hit(hit),
                "rrf_score": payload.get("_rrf_score"),
                "rerank_score": payload.get("_rerank_score"),
            }
        )

    updated.sort(key=lambda h: h.score, reverse=True)
    _log_search_step(
        "rerank",
        pool_size=pool_size,
        reranked_count=len(reranked_rows),
        rerank_query=rerank_query[:240],
        rerank_max_length=RERANKER_MAX_LENGTH,
        rerank_doc_max_chars=RERANK_DOC_MAX_CHARS,
        top_reranked=reranked_rows[:_SEARCH_DEBUG_MAX_HITS],
    )
    return updated


def _rerank_query_text(user_query: str, retrieval_queries: list[str]) -> str:
    """
    Reranker용 질의문.
    긴 대화형 원문만 넣으면 점수가 0 근처로 뭉개지므로,
    ES용 보조 구문(정책명·제도명) + 사용자 질문 앞부분을 짧게 결합한다.
    """
    user = (user_query or "").strip()
    user_cf = user.casefold()
    focused_parts: list[str] = []
    for q in retrieval_queries:
        q = q.strip()
        if not q or q.casefold() == user_cf:
            continue
        if q not in focused_parts:
            focused_parts.append(q)

    if focused_parts and user:
        combined = f"{' '.join(focused_parts[:2])} | {user[:RERANK_QUERY_MAX_CHARS]}"
    elif user:
        combined = user[:RERANK_QUERY_MAX_CHARS]
    else:
        combined = " ".join(focused_parts[:2])

    return combined[:RERANK_QUERY_MAX_CHARS].strip() or "검색"


def _rerank_document_text(
    payload: dict[str, Any],
    parent_payloads: dict[str, dict[str, Any]] | None = None,
) -> str:
    """
    리랭커 document — 섹션 제목 + child 근거만 (title_path 제외).

    RERANKER_MAX_LENGTH 토큰 예산에서 경로가 앞을 차지하면 근거 본문이 잘려
    sigmoid가 낮게 나오므로, query(_rerank_query_text)는 유지하고 document만 압축한다.
    """
    p = payload or {}
    section = (
        str(p.get("section_title") or "")
        or str(p.get("parent_section_title") or "")
        or ""
    ).strip()

    if not section and parent_payloads:
        parent_id = str(p.get("parent_doc_id") or "").strip()
        parent = parent_payloads.get(parent_id) if parent_id else None
        if parent:
            section = str(parent.get("section_title") or "").strip()

    body = str(
        p.get("content_dense") or p.get("text") or p.get("llm_context") or ""
    ).strip()
    if "근거:" in body:
        body = body.split("근거:", 1)[-1].strip()
    body = body[:RERANK_DOC_MAX_CHARS]

    if section and body:
        return f"[{section}]\n{body}"[:RERANK_DOC_MAX_CHARS]
    if body:
        return body[:RERANK_DOC_MAX_CHARS]
    if section:
        return f"[{section}]"[:RERANK_DOC_MAX_CHARS]
    return "문서"


def deduplicate_evidence_hits(
    hits: list[SearchHit],
    *,
    sort_by_score: bool = True,
) -> list[SearchHit]:
    """
    child 검색 결과 중복 제거 (parent_doc_id → llm_context 해시 순).
    동일 evidence 키는 score가 가장 높은 chunk 1건만 남긴다.
    """
    if not hits:
        return []

    best_by_key: dict[str, SearchHit] = {}

    for hit in hits:
        key = _evidence_dedupe_key(hit.payload or {})
        prev = best_by_key.get(key)
        if prev is None:
            best_by_key[key] = hit
            continue

        winner, loser = (hit, prev) if hit.score >= prev.score else (prev, hit)
        winner.semantic_score = max(winner.semantic_score, loser.semantic_score)
        winner.keyword_score = max(winner.keyword_score, loser.keyword_score)
        winner.score = max(winner.score, loser.score)
        best_by_key[key] = winner

    out = list(best_by_key.values())
    if sort_by_score:
        out.sort(key=lambda h: h.score, reverse=True)
    return out


def fetch_parent_payloads_by_ids(
    parent_ids: list[str],
    index_name: str = ES_INDEX_NAME,
) -> dict[str, dict[str, Any]]:
    """parent_doc_id → parent_section payload (mget, 실패 id 무시)."""
    unique_ids = [pid for pid in dict.fromkeys(parent_ids) if pid]
    if not unique_ids or not es.indices.exists(index=index_name):
        return {}

    try:
        res = es.mget(index=index_name, ids=unique_ids, source_includes=_PARENT_FETCH_SOURCE)
    except Exception:
        return {}

    out: dict[str, dict[str, Any]] = {}
    for doc in res.get("docs", []):
        if doc.get("found") and doc.get("_source"):
            out[str(doc["_id"])] = doc["_source"]
    return out


def _parent_section_body(parent_payload: dict[str, Any], *, max_chars: int = 4200) -> str:
    text = (parent_payload.get("text") or "").strip()
    if text:
        return text[:max_chars]
    dense = (parent_payload.get("content_dense") or "").strip()
    if "근거:" in dense:
        dense = dense.split("근거:", 1)[1].strip()
    return dense[:max_chars]


def _build_answer_block_from_payload(
    payload: dict[str, Any],
    parent_payloads: dict[str, dict[str, Any]] | None = None,
) -> str:
    """
    LLM 프롬프트용 답변 블록.
    child/faq hit → parent_doc_id로 parent_section.text 전체를 주입 (검색 hit text는 보조).
    """
    p = payload or {}
    chunk_type = str(p.get("chunk_type") or "")

    if p.get("answer_expand_parent"):
        parent_doc_id = str(p.get("parent_doc_id") or "").strip()
        if parent_doc_id and parent_payloads:
            parent = parent_payloads.get(parent_doc_id)
            if parent:
                parent_title = (
                    parent.get("section_title")
                    or p.get("parent_section_title")
                    or "상위 섹션"
                )
                parent_body = _parent_section_body(parent)
                if parent_body:
                    parts = [f"▸ {parent_title}", parent_body]
                    if chunk_type == "faq_evidence":
                        question = str(p.get("question") or "").strip()
                        answer = str(p.get("answer") or "").strip()
                        if question:
                            parts.append(f"Q: {question}")
                        if answer:
                            parts.append(f"A: {answer[:1200]}")
                    return "\n".join(parts)

    parts: list[str] = []
    section = (
        p.get("parent_section_title")
        or p.get("section_title")
        or ""
    )
    if section:
        parts.append(f"▸ {section}")

    body = (p.get("text") or p.get("llm_context") or "").strip()
    if body:
        parts.append(body[:2200])

    if chunk_type == "faq_evidence":
        question = str(p.get("question") or "").strip()
        answer = str(p.get("answer") or "").strip()
        if question:
            parts.append(f"Q: {question}")
        if answer:
            parts.append(f"A: {answer[:1200]}")

    if parts:
        return "\n".join(parts)

    return _compact_chunk_for_context(p)


def collapse_hits_by_parent(
    hits: list[SearchHit],
    *,
    max_parents: int | None = None,
) -> list[SearchHit]:
    """
    child/faq/attachment evidence hit들을 **parent_section 단위**로 병합한다.

    - 그룹 키: parent_doc_id (없으면 doc_id 자체를 키로 사용)
    - 답변 컨텍스트: parent_section.text 전체 (mget)
    - 리소스: parent 청크의 urls / attachments 필드 → _format_resources_context_block
    - 반환 SearchHit.doc_id = parent_doc_id (Slack [1][2] = 섹션 단위)
    """
    if not hits:
        return []

    if max_parents is None:
        max_parents = RAG_MAX_PAGES

    # ── parent_doc_id 수집 & mget ─────────────────────────────────────────────
    parent_ids: list[str] = [
        str((hit.payload or {}).get("parent_doc_id") or "").strip()
        for hit in hits
    ]
    parent_ids = [pid for pid in parent_ids if pid]
    parent_payloads = fetch_parent_payloads_by_ids(parent_ids, index_name=ES_INDEX_NAME)

    # ── parent_doc_id 기준 그룹화 ─────────────────────────────────────────────
    by_parent: dict[str, list[SearchHit]] = {}
    for hit in hits:
        p = hit.payload or {}
        key = str(p.get("parent_doc_id") or "").strip() or hit.doc_id
        by_parent.setdefault(key, []).append(hit)

    merged: list[SearchHit] = []
    for parent_id, group in by_parent.items():
        group.sort(key=lambda h: h.score, reverse=True)
        best_payload = dict(group[0].payload or {})

        # parent payload — mget에서 가져온 것 우선
        parent = parent_payloads.get(parent_id) or {}

        # ── 답변 본문: parent.text 전체 사용 ─────────────────────────────────
        parent_body = _parent_section_body(parent) if parent else ""
        section_title = (
            parent.get("section_title")
            or best_payload.get("section_title")
            or ""
        )

        existing_merged = best_payload.get("merged_chunks") or []
        if parent_body:
            main_block = f"▸ {section_title}\n{parent_body}" if section_title else parent_body
            chunk_parts = [main_block]
        elif existing_merged:
            chunk_parts = list(existing_merged)
        else:
            # parent 없는 경우(attachment_evidence 등) child 텍스트 fallback
            seen_ctx: set[str] = set()
            chunk_parts = []
            for h in group:
                hp = h.payload or {}
                ctx_key = _llm_context_hash(hp)
                if ctx_key in seen_ctx:
                    continue
                seen_ctx.add(ctx_key)
                chunk_parts.append(
                    f"[{hp.get('chunk_type', 'evidence')}]\n"
                    + _build_answer_block_from_payload(hp, parent_payloads)
                )

        # ── payload 구성: parent 정보 우선, child 정보 보조 ───────────────────
        payload = best_payload.copy()
        if parent:
            payload["page_title"] = parent.get("page_title") or best_payload.get("page_title", "")
            payload["title_path"] = parent.get("title_path") or best_payload.get("title_path", "")
            payload["page_url"] = parent.get("page_url") or best_payload.get("page_url", "")
            payload["page_summary"] = parent.get("page_summary") or best_payload.get("page_summary", "")
            payload["section_title"] = section_title
            if parent.get("attachments"):
                payload["attachments"] = parent["attachments"]
            if parent.get("urls"):
                payload["urls"] = parent["urls"]

        payload["merged_chunks"] = chunk_parts
        payload["_source_chunk_ids"] = [h.doc_id for h in group]
        payload["_chunk_count"] = len(group)
        payload["_evidence_count"] = len(chunk_parts)
        payload["_parent_id"] = parent_id

        citation_url = _resolve_citation_url(payload)
        if citation_url:
            payload["_citation_url"] = citation_url
            payload["page_url"] = citation_url

        # ── 리소스: parent의 urls/attachments 우선, child에서 보조 ──────────
        att, links = _extract_resources_from_payload(parent) if parent else ([], [])
        child_att, child_links = _collect_page_resources(group)
        att, links = _merge_resource_lists(att, links, child_att, child_links)
        payload["_attachments"] = att
        payload["_links"] = links
        payload["_force_keep"] = any((h.payload or {}).get("_force_keep") for h in group)
        payload["_diversity_slot"] = any((h.payload or {}).get("_diversity_slot") for h in group)

        chunk_count = len(group)
        max_child_score = max(h.score for h in group)
        section_density_bonus = 1.0 + (min(chunk_count, 5) * 0.03)
        payload["_section_density_bonus"] = section_density_bonus

        merged.append(
            SearchHit(
                doc_id=parent_id,
                payload=payload,
                score=min(max_child_score * section_density_bonus, 1.0),
                semantic_score=max(h.semantic_score for h in group),
                keyword_score=max(h.keyword_score for h in group),
            )
        )

    merged.sort(key=lambda h: h.score, reverse=True)
    if max_parents is not None and max_parents > 0:
        return merged[:max_parents]
    return merged


# 하위 호환 alias
def collapse_hits_by_page(hits: list[SearchHit], **kwargs: Any) -> list[SearchHit]:
    return collapse_hits_by_parent(hits, max_parents=kwargs.get("max_pages"))


def deduplicate_search_hits(hits: list[SearchHit], **kwargs: Any) -> list[SearchHit]:
    return collapse_hits_by_parent(hits)


def _compact_chunk_for_context(payload: dict[str, Any]) -> str:
    """프롬프트용: 메타 반복을 줄이고 섹션+근거만 추출."""
    dense = (payload.get("content_dense") or payload.get("text") or "").strip()
    section = (payload.get("section_title") or "").strip()
    if not section:
        for line in dense.split("\n"):
            line = line.strip()
            if line.startswith("섹션:"):
                section = line.removeprefix("섹션:").strip()
                break

    evidence = ""
    if "근거:" in dense:
        evidence = dense.split("근거:", 1)[1].strip()
    elif dense:
        evidence = dense

    parts: list[str] = []
    if section:
        parts.append(f"▸ {section}")
    if evidence:
        parts.append(evidence[:900])

    chunk_type = payload.get("chunk_type", "")
    if chunk_type == "faq_evidence":
        q = payload.get("question", "")
        a = payload.get("answer", "")
        if q:
            parts.append(f"Q: {q}")
        if a:
            parts.append(f"A: {a}")

    if chunk_type in ("attachment_summary", "attachment_collection"):
        att_url = resolve_attachment_url(
            saved_path=str(payload.get("attachment_saved_path") or ""),
            fallback_url=str(payload.get("attachment_url") or ""),
        )
        att_title = payload.get("attachment_title", "첨부")
        if att_url:
            parts.append(f"첨부 URL: {att_url} ({att_title})")

    if chunk_type == "reference_link":
        link_url = _normalize_url(str(payload.get("link_url") or ""))
        link_text = payload.get("link_text", "") or payload.get("link_purpose", "")
        if link_url:
            parts.append(f"링크: {link_text} → {link_url}")

    return "\n".join(parts) if parts else dense[:900]


def hybrid_search(
    query_or_queries: str | list[str] | None = None,
    top_k: int | None = None,
    semantic_limit: int | None = None,
    keyword_limit: int | None = None,
    index_name: str = ES_INDEX_NAME,
    rerank_pool: int | None = None,
    rerank_query: str | None = None,
    *,
    direct_query: str | None = None,
    policy_query: str | None = None,
) -> list[SearchHit]:
    '''
    1. child evidence retrieval — (direct|policy) × semantic/BM25 각 top-N
    2. child RRF fusion — merge only (rough ordering)
    3. reranker — RRF pool 재순위화
    4. page aggregation — chunk·semantic·bm25·subquery·rank 통계
    5. page ranking — page relevance (rerank score 기반)
    5b. page rerank gap filter — 상위 RAG_PAGE_GAP_MIN_KEEP page 고정 + gap 이내 추가 page
    6. page cut — RAG_MAX_PAGES (gap 필터 후 잔여만 상한 적용)
    7. selected page child → parent_section collapse → Turn2

    권장: direct_query + policy_query (Router tool 스키마와 동일).
    '''
    if semantic_limit is None:
        semantic_limit = RAG_SEMANTIC_LIMIT
    if keyword_limit is None:
        keyword_limit = RAG_KEYWORD_LIMIT
    if top_k is None:
        top_k = RAG_MAX_PAGES
    if rerank_pool is None:
        rerank_pool = RAG_RERANK_POOL

    retrieval_specs = _build_retrieval_specs(
        query_or_queries,
        direct_query=direct_query,
        policy_query=policy_query,
    )
    query_texts = [q for _role, q in retrieval_specs]

    _log_search_step(
        "pipeline_start",
        retrieval_specs=[
            {"role": role, "query": q} for role, q in retrieval_specs
        ],
        retrieval_queries=query_texts,
        direct_query=(direct_query or "").strip() or None,
        policy_query=(policy_query or "").strip() or None,
        rerank_query=rerank_query,
        input_query_count=len(retrieval_specs),
        semantic_limit=semantic_limit,
        keyword_limit=keyword_limit,
        rerank_pool=rerank_pool,
        max_pages=top_k,
        reranker=RERANKER_MODEL_NAME,
        search_chunk_types=sorted(_SEARCH_CHUNK_TYPES),
    )

    if not retrieval_specs:
        _log_search_step("pipeline_empty", reason="no_queries")
        return []

    all_children: dict[str, SearchHit] = {}
    page_subqueries: dict[str, set[int]] = {}
    raw_chunk_total = 0

    retrieval_rows = _retrieve_queries_parallel(
        retrieval_specs,
        semantic_limit=semantic_limit,
        keyword_limit=keyword_limit,
        index_name=index_name,
    )

    for qi, role, q, semantic_hits, keyword_hits in retrieval_rows:
        raw_chunk_total += len(semantic_hits) + len(keyword_hits)

        fused = _rrf_fuse_child_hits(semantic_hits, keyword_hits)
        for hit in fused:
            _merge_fused_child(
                all_children,
                hit,
                subquery_idx=qi,
                page_subqueries=page_subqueries,
            )

        _log_search_step(
            "rrf_per_query",
            retrieval_role=role,
            sub_query=q,
            subquery_idx=qi,
            semantic_hit_count=len(semantic_hits),
            keyword_hit_count=len(keyword_hits),
            fused_doc_count=len(fused),
            fused_docs=_debug_hits(
                fused,
                extra_fn=lambda h, _r: {
                    "semantic_rrf": h.semantic_score,
                    "keyword_rrf": h.keyword_score,
                    "combined_rrf": h.score,
                },
            ),
            fused_truncated=len(fused) > _SEARCH_DEBUG_MAX_HITS,
        )

    child_list = sorted(all_children.values(), key=lambda h: h.score, reverse=True)
    _log_search_step(
        "rrf_fused_all",
        unique_chunks=len(child_list),
        hits=_debug_hits(
            child_list,
            extra_fn=lambda h, _r: {
                "semantic_rrf": h.semantic_score,
                "keyword_rrf": h.keyword_score,
                "combined_rrf": h.score,
            },
        ),
        truncated=len(child_list) > _SEARCH_DEBUG_MAX_HITS,
    )

    if not child_list:
        _log_search_step("pipeline_empty", reason="no_candidates_after_rrf")
        return []

    rerank_q = _rerank_query_text(
        rerank_query or " | ".join(query_texts),
        query_texts,
    )
    child_list = _rerank_child_candidates(
        child_list,
        query_text=rerank_q,
        pool_size=rerank_pool,
    )
    if child_list:
        rerank_scores = [h.score for h in child_list if (h.payload or {}).get("_rerank_score") is not None]
        _log_search_step(
            "post_rerank_scores",
            pool_size=rerank_pool,
            reranked_evidence_count=len(rerank_scores),
            top_scores=sorted(rerank_scores, reverse=True)[:10],
            max_score=round(max(rerank_scores), 4) if rerank_scores else None,
            min_score=round(min(rerank_scores), 4) if rerank_scores else None,
        )

    page_stats = _aggregate_pages_from_children(child_list, page_subqueries)
    ranked_pages = _rank_pages(page_stats, n_queries=len(retrieval_specs))
    ref_year = _extract_reference_year_from_queries(query_texts)

    def _page_rank_rows(pages: list[_PageStats], limit: int = 5) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for s in pages[:limit]:
            title = (s.hits[0].payload or {}).get("page_title") if s.hits else None
            rows.append(
                {
                    "page_id": s.page_id,
                    "page_title": title,
                    "page_score": round(float(s.page_score or 0.0), 6),
                }
            )
        return rows

    top_pages_before = _page_rank_rows(ranked_pages)
    ranked_pages = _apply_temporal_page_ranking(
        ranked_pages,
        reference_year=ref_year,
    )
    if ref_year is not None:
        _log_search_step(
            "temporal_page_ranking",
            reference_year=ref_year,
            top_pages_before=top_pages_before,
            top_pages_after=_page_rank_rows(ranked_pages),
        )
    ranked_before_gap = len(ranked_pages)
    ranked_pages, gap_dropped_pages = _filter_ranked_pages_by_rerank_gap(
        ranked_pages,
        relative_gap=RAG_PAGE_RERANK_RELATIVE_GAP,
        min_keep=RAG_PAGE_GAP_MIN_KEEP,
    )
    if gap_dropped_pages:
        _log_search_step(
            "page_rerank_gap_filter",
            relative_gap=RAG_PAGE_RERANK_RELATIVE_GAP,
            min_keep=RAG_PAGE_GAP_MIN_KEEP,
            pages_before=ranked_before_gap,
            pages_after=len(ranked_pages),
            top_max_rerank=round(_page_max_rerank_score(ranked_pages[0]), 6)
            if ranked_pages
            else None,
            dropped_pages=gap_dropped_pages,
        )
    selected_pages = ranked_pages[: max(0, int(top_k or 0))]
    selected_page_ids = {s.page_id for s in selected_pages}

    _log_search_step(
        "page_aggregate",
        page_count=len(page_stats),
        pages=[
            {
                "page_id": s.page_id,
                "chunk_count": s.chunk_count,
                "semantic_count": s.semantic_count,
                "bm25_count": s.bm25_count,
                "subquery_coverage": s.subquery_coverage,
                "top_rank": s.top_rank,
                "avg_rank": round(s.avg_rank, 2),
                "page_score": round(s.page_score, 6),
                "max_rerank_score": round(max(h.score for h in s.hits), 6) if s.hits else 0.0,
                "page_title": (s.hits[0].payload or {}).get("page_title") if s.hits else None,
            }
            for s in ranked_pages
        ],
        truncated=len(page_stats) > _SEARCH_DEBUG_MAX_HITS,
    )

    _log_search_step(
        "page_cut",
        max_pages=RAG_MAX_PAGES,
        selected_count=len(selected_pages),
        selected_page_ids=sorted(selected_page_ids),
        rejected_by_max_pages=[
            s.page_id for s in ranked_pages[int(top_k or 0) :]
        ],
        gap_filtered_count=len(gap_dropped_pages),
    )

    filtered_children = _filter_children_by_pages(child_list, selected_page_ids)
    collapsed = collapse_hits_by_parent(filtered_children, max_parents=0)
    final = select_diverse_parents(
        collapsed,
        max_total=RAG_MAX_PARENTS,
        min_pages=RAG_MIN_PAGES,
        max_per_page=RAG_MAX_PARENTS_PER_PAGE,
    )

    _log_search_step(
        "parent_collapse",
        child_hits_after_page_filter=len(filtered_children),
        collapsed_before_select=len(collapsed),
        collapsed_count=len(final),
        collapsed_parents=[
            {
                "parent_doc_id": h.doc_id,
                "page_id": _page_id_from_hit(h),
                "page_title": (h.payload or {}).get("page_title"),
                "section_title": (h.payload or {}).get("section_title"),
                "score": round(h.score, 6),
                "chunk_count": (h.payload or {}).get("_chunk_count"),
                "source_chunk_ids": (h.payload or {}).get("_source_chunk_ids"),
                "body_preview": " ".join(
                    (h.payload or {}).get("merged_chunks") or [""]
                )[:400],
            }
            for h in final
        ],
    )

    _log_search_step(
        "diversity_select",
        max_total=RAG_MAX_PARENTS,
        min_pages=RAG_MIN_PAGES,
        max_per_page=RAG_MAX_PARENTS_PER_PAGE,
        distinct_pages=len({_page_id_from_hit(h) for h in final}),
        parents_per_page={
            pid: sum(1 for h in final if _page_id_from_hit(h) == pid)
            for pid in sorted({_page_id_from_hit(h) for h in final})
        },
    )

    _log_search_step(
        "pipeline_final",
        raw_chunks_from_es=raw_chunk_total,
        unique_chunks_after_rrf=len(child_list),
        pages_selected=len(selected_pages),
        parents_for_llm=len(final),
        final_for_llm=[
            {
                "rank": i + 1,
                "parent_doc_id": h.doc_id,
                "page_id": _page_id_from_hit(h),
                "page_title": (h.payload or {}).get("page_title"),
                "section_title": (h.payload or {}).get("section_title"),
                "page_summary": str((h.payload or {}).get("page_summary") or "")[:300],
                "score": round(h.score, 6),
                "citation_url": (h.payload or {}).get("_citation_url")
                or (h.payload or {}).get("page_url"),
            }
            for i, h in enumerate(final)
        ],
    )

    return final


# =============================================================================
# 7. RAG Answer
# =============================================================================



def _build_enriched_parent_text(
    payload: dict[str, Any],
    body_text: str,
) -> str:
    """
    부모 청크 본문 상단에 메타데이터 헤더를 강제 주입(Context Enrichment).

    LLM이 문서의 맥락(어느 위키 경로인지, 어떤 섹션인지, 문서 전체 요약)을
    파악한 뒤 본문을 읽도록 하여 답변 정확도를 높인다.

    [예시 출력]
    [문서 경로] connecteve WIKI Home > 온보딩
    [섹션] 입사자 제출서류 및 명함·업무세팅(장비·계정)
    [문서 요약] 입사 기본 정보와 온보딩 일정…
    --------------------------------------------------
    (본문)
    """
    header_lines: list[str] = []

    title_path = str(payload.get("title_path") or "").strip()
    if title_path:
        header_lines.append(f"[문서 경로] {title_path}")

    section_title = str(payload.get("section_title") or "").strip()
    if section_title:
        header_lines.append(f"[섹션] {section_title}")

    page_summary = str(payload.get("page_summary") or "").strip()
    if page_summary:
        header_lines.append(f"[문서 요약] {page_summary}")

    if header_lines:
        header = "\n".join(header_lines)
        return f"{header}\n{'-' * 50}\n{body_text}"

    return body_text


def _build_context(docs: list[SearchHit]) -> str:
    '''
    검색 결과(이미 페이지 병합·llm_context 반영됨)를 프롬프트 컨텍스트 문자열로 만든다.
    각 문서 블록의 본문 상단에 메타데이터 헤더를 강제 주입하여 LLM 성능을 높인다.
    '''
    context_parts = []
    for i, doc in enumerate(docs, start=1):
        payload = doc.payload or {}
        chunk_bodies = payload.get("merged_chunks")
        if not chunk_bodies:
            chunk_bodies = [_build_answer_block_from_payload(payload)]
        combined_text = "\n\n".join(chunk_bodies)

        # ── 메타데이터 헤더 강제 주입 (Context Enrichment) ──────────────────
        combined_text = _build_enriched_parent_text(payload, combined_text)

        resources_block = _format_resources_context_block(
            payload.get("_attachments") or [],
            payload.get("_links") or [],
        )
        if resources_block:
            combined_text = f"{combined_text}\n\n{resources_block}"

        citation_url = payload.get("_citation_url") or _resolve_citation_url(payload)
        part = (
            f"[문서 {i}]\n"
            f"제목: {payload.get('page_title', '제목 없음')}\n"
            f"URL: {citation_url}\n"
            f"내용:\n{combined_text}"
        )
        context_parts.append(part)

    return "\n\n========================================\n\n".join(context_parts)

# =============================================================================
# 8. Init (외부에서 호출)
# =============================================================================

def init_vectordb(force_rebuild: bool = False) -> None:
    '''
    앱 시작 시 1회 호출하여 ES 연결 확인 + index 생성.
    '''
    # 디버그 로거 초기화 (항상 활성화)
    rag_debug_logger.init(
        debug_dir=PROJECT_DIR / "debug",
        enabled=True,
    )

    try:
        prompt_blob = get_router_instruction()
        rag_debug_logger.write_run_manifest(
            {
                "debug_run_ts": rag_debug_logger.get_run_ts(),
                "router_model": ROUTER_MODEL_NAME,
                "llm_model": LLM_MODEL_NAME,
                "rag_answer_model": RAG_ANSWER_MODEL_NAME,
                "high_risk_fallback_model": HIGH_RISK_FALLBACK_MODEL_NAME,
                "rag_max_parents": RAG_MAX_PARENTS,
                "rag_research_enabled": RAG_RESEARCH_ENABLED,
                "prompt_hash": hashlib.sha256(
                    prompt_blob.encode("utf-8")
                ).hexdigest()[:16],
                "prompt_chars": len(prompt_blob),
            }
        )
    except Exception as exc:
        print(f"⚠️ run manifest 기록 실패: {exc}")

    try:
        if es.ping():
            print("✅ Elasticsearch 연결 성공!")
        else:
            print("❌ Elasticsearch 연결 실패: ping 응답 없음")
            return
    except Exception as e:
        print(f"❌ Elasticsearch 연결 에러: {e}")
        return

    build_elasticsearch_db(force_rebuild=force_rebuild)

    print("ℹ️ 런타임 임베딩·리랭킹은 ml_inference ProcessPool에서 처리합니다.")