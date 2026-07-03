# =============================================================================
# Connecteve Wiki — ES Bulk Uploader (rag_tree_hybrid)
# =============================================================================
"""
rag_tree_hybrid_vector_payload.jsonl  (child_evidence — embed + index)
rag_tree_hybrid_parent_store.jsonl    (parent_section — index, no embed)
→ Elasticsearch 인덱스 업로드

역할:
  - child_evidence / attachment_doc 청크: text + lexical_boost 중심 embedding → kNN/BM25 검색
  - parent_section: 임베딩 없이 저장 → child hit의 parent_doc_id mget으로 답변 본문 조회
  - urls / attachments / images / keywords 등 후처리 필드 포함 저장

실행:
  python 6_build_ES_tree_hybrid.py
  RAG_REGISTRY_SUBDIR=rag_tree_registry python 6_build_ES_tree_hybrid.py

증분 업로드(기본):
  - ES 인덱스에 동일 _id 문서가 있으면 건너뜀
  - 없는 문서만 임베딩 후 추가

전체 재구축:
  ES_FORCE_REBUILD=1 python 6_build_ES_tree_hybrid.py
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
from dotenv import load_dotenv
from elasticsearch import Elasticsearch, helpers
from elasticsearch.helpers import scan
from sentence_transformers import SentenceTransformer
from tqdm import tqdm

# =============================================================================
# 0. Config
# =============================================================================
load_dotenv()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from app.core.config import (  # noqa: E402
    ES_INDEX_NAME,
    TREE_REGISTRY_DIR,
    ELASTICSEARCH_URL,
    EMBEDDING_MODEL_NAME,
    EMBEDDING_BATCH_SIZE,
    EMBED_MAX_CHARS,
)

REGISTRY_DIR = Path(os.getenv("TREE_HYBRID_REGISTRY_DIR", str(TREE_REGISTRY_DIR))).resolve()
VECTOR_JSONL_PATH = REGISTRY_DIR / "rag_tree_hybrid_vector_payload.jsonl"
PARENT_STORE_JSONL_PATH = REGISTRY_DIR / "rag_tree_hybrid_parent_store.jsonl"

ELASTICSEARCH_API_KEY = os.getenv("ELASTICSEARCH_API_KEY", "")

# child_evidence 임베딩 대상 chunk type
_EMBED_CHUNK_TYPES = frozenset({"child_evidence", "faq_evidence", "attachment_evidence"})
# 임베딩 없이 mget 전용으로 저장하는 chunk type
_STORE_ONLY_CHUNK_TYPES = frozenset({"parent_section"})

# =============================================================================
# 1. Elasticsearch Client
# =============================================================================


def create_es_client() -> Elasticsearch:
    if ELASTICSEARCH_API_KEY:
        return Elasticsearch(
            ELASTICSEARCH_URL,
            api_key=ELASTICSEARCH_API_KEY,
            verify_certs=False,
            request_timeout=120,
        )
    return Elasticsearch(ELASTICSEARCH_URL, request_timeout=120)


es = create_es_client()
try:
    if not es.ping():
        raise RuntimeError("Elasticsearch ping 실패")
    print(f"Elasticsearch 연결 성공: {ELASTICSEARCH_URL}")
except Exception as exc:
    raise RuntimeError(f"Elasticsearch 연결 실패: {exc}") from exc


# =============================================================================
# 2. Embedding Model
# =============================================================================
_embedder: SentenceTransformer | None = None


def get_embedder() -> SentenceTransformer:
    global _embedder
    if _embedder is None:
        print(f"임베딩 모델 로드: {EMBEDDING_MODEL_NAME}")
        _embedder = SentenceTransformer(EMBEDDING_MODEL_NAME)
        _embedder.max_seq_length = 512
    return _embedder


# =============================================================================
# 3. ES Index Schema
# =============================================================================

ES_MAPPINGS: dict[str, Any] = {
    "properties": {
        # --- identity ---
        "id":              {"type": "keyword"},
        "group_id":        {"type": "keyword"},
        "parent_id":       {"type": "keyword"},
        "parent_doc_id":   {"type": "keyword"},
        "section_id":      {"type": "keyword"},

        # --- page meta ---
        "page_id":    {"type": "keyword"},
        "page_title": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
        "page_url":   {"type": "keyword"},
        "title_path": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},

        # --- chunk meta ---
        "chunk_type":          {"type": "keyword"},
        "source_kind":         {"type": "keyword"},
        "section_type":        {"type": "keyword"},
        "section_title":       {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
        "answer_expand_parent":{"type": "boolean"},
        "index_for_embedding": {"type": "boolean"},

        # --- text fields (검색·임베딩·LLM) ---
        "text":           {"type": "text"},
        "lexical_boost":  {"type": "text"},
        "embedding_text": {"type": "text"},
        "page_summary":   {"type": "text"},

        # --- keywords (BM25 가중치용) ---
        "keywords": {"type": "keyword"},

        # --- 후처리 enrichment (urls / attachments / images) ---
        # list of {text, url} — dynamic sub-fields
        "urls": {
            "type": "object",
            "dynamic": True,
        },
        # list of {title, download_url, file_size, saved_path, webui}
        "attachments": {
            "type": "object",
            "dynamic": True,
        },
        # list of {filename, url, description}
        "images": {
            "type": "object",
            "dynamic": True,
        },

        # --- 첨부 전용 (attachment_doc children) ---
        "attachment_title":      {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
        "attachment_extension":  {"type": "keyword"},
        "attachment_url":        {"type": "keyword"},
        "attachment_saved_path": {"type": "keyword"},

        # --- 링크 전용 ---
        "link_text":    {"type": "text"},
        "link_url":     {"type": "keyword"},
        "link_purpose": {"type": "text"},

        # --- 벡터 (dims는 실행 시 모델에서 결정) ---
        "embedding": {
            "type": "dense_vector",
            "dims": 1024,
            "index": True,
            "similarity": "cosine",
        },
    }
}


def create_es_index(
    es_client: Elasticsearch,
    index_name: str,
    vector_dim: int,
    force_rebuild: bool = True,
) -> None:
    exists = es_client.indices.exists(index=index_name)
    if exists and force_rebuild:
        es_client.indices.delete(index=index_name)
        exists = False
    if exists:
        return

    mappings = dict(ES_MAPPINGS)
    mappings["properties"]["embedding"]["dims"] = vector_dim

    es_client.indices.create(
        index=index_name,
        settings={"number_of_shards": 1, "number_of_replicas": 0},
        mappings=mappings,
    )
    print(f"인덱스 생성 완료: {index_name} (dims={vector_dim})")


# =============================================================================
# 4. Embedding text 선택
# =============================================================================


def _field_text(value: Any) -> str:
    """임베딩 입력 조립용 문자열 정리."""
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(str(x).strip() for x in value if str(x).strip())
    return str(value).strip()


def _join_unique_for_embedding(*parts: Any) -> str:
    """
    text + lexical_boost 조립 시 같은 문자열 반복을 줄인다.

    lexical_boost는 키워드/날짜/금액/약어 보강용이고,
    text는 실제 child 원문이므로 둘 다 embedding_text에 포함한다.
    """
    out: list[str] = []
    seen: set[str] = set()

    for part in parts:
        s = _field_text(part)
        if not s:
            continue
        key = " ".join(s.lower().split())
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(s)

    return "\n".join(out).strip()


def embedding_text_for_doc(doc: dict[str, Any]) -> str:
    """
    임베딩 입력 텍스트 결정 규칙:

    child_evidence / faq_evidence:
      section_title + lexical_boost + text 중심.
      - text: 실제 검색 대상 원문
      - lexical_boost: 날짜/금액/고유명사/약어/BM25 보강 키워드

    attachment_evidence / attachment_doc child:
      attachment_title 또는 section_title + lexical_boost + text 중심.

    parent_section:
      임베딩 없음 → 빈 문자열 반환.
      parent는 child hit 이후 parent_doc_id mget으로 조회한다.

    공통:
      title_path를 "문서 경로: ..." prefix로 붙여 경로/부서 맥락을 약하게 보강한다.
    """
    chunk_type = str(doc.get("chunk_type") or "")
    source_kind = str(doc.get("source_kind") or "")

    if chunk_type in _STORE_ONLY_CHUNK_TYPES:
        return ""

    page_title = doc.get("page_title", "")
    section_title = doc.get("section_title", "")
    attachment_title = doc.get("attachment_title", "")
    lexical_boost = doc.get("lexical_boost", "")
    text = doc.get("text", "")

    if chunk_type in {"child_evidence", "faq_evidence"}:
        if source_kind == "attachment_doc":
            src = _join_unique_for_embedding(
                attachment_title,
                section_title,
                lexical_boost,
                text
            )
        else:
            src = _join_unique_for_embedding(
                page_title,
                section_title,
                lexical_boost,
                text
            )

    elif chunk_type == "attachment_evidence":
        src = _join_unique_for_embedding(
            attachment_title,
            section_title,
            lexical_boost,
            text
        )

    else:
        src = _join_unique_for_embedding(
            page_title,
            section_title,
            lexical_boost,
            text
        )

    # title_path prefix: 이미 포함된 경우 중복되지 않도록
    path = str(doc.get("title_path") or "").strip()
    if path and "문서 경로:" not in src[:160]:
        src = f"문서 경로: {path}\n\n{src}"

    return src[:EMBED_MAX_CHARS]


# =============================================================================
# 5. Document preparation
# =============================================================================


def prepare_doc(doc: dict[str, Any]) -> dict[str, Any]:
    out = dict(doc)
    out["id"] = str(out.get("id") or "")

    # answer_expand_parent: parent_doc_id 없으면 강제 False
    if out.get("answer_expand_parent") and not str(out.get("parent_doc_id") or "").strip():
        out["answer_expand_parent"] = False

    out["embedding_text"] = embedding_text_for_doc(out)
    return out


def validate_doc(doc: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    ct = str(doc.get("chunk_type") or "")

    if not doc.get("id"):
        warnings.append("missing id")
    if ct in {"child_evidence", "faq_evidence"} and not (doc.get("text")):
        warnings.append("evidence missing text")
    if ct in {"child_evidence", "faq_evidence"} and doc.get("answer_expand_parent") and not doc.get("parent_doc_id"):
        warnings.append("answer_expand_parent=True but no parent_doc_id")
    return warnings


# =============================================================================
# 6. JSONL IO
# =============================================================================


def fetch_existing_doc_ids(es_client: Elasticsearch, index_name: str) -> set[str]:
    """ES 인덱스에 이미 존재하는 문서 _id 집합."""
    if not es_client.indices.exists(index=index_name):
        return set()

    ids: set[str] = set()
    for hit in scan(es_client, index=index_name, query={"query": {"match_all": {}}}, _source=False):
        doc_id = hit.get("_id")
        if doc_id:
            ids.add(str(doc_id))
    return ids


def filter_new_docs(docs: list[dict[str, Any]], existing_ids: set[str]) -> tuple[list[dict[str, Any]], int]:
    """existing_ids에 없는 문서만 반환. (new_docs, skipped_count)"""
    if not existing_ids:
        return docs, 0

    new_docs: list[dict[str, Any]] = []
    skipped = 0
    for doc in docs:
        doc_id = str(doc.get("id") or "")
        if doc_id and doc_id in existing_ids:
            skipped += 1
            continue
        new_docs.append(doc)
    return new_docs, skipped


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    if not path.exists():
        print(f"  파일 없음 (건너뜀): {path}")
        return docs
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                docs.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"  JSON 파싱 오류 line {line_no}: {exc}")
    return docs


# =============================================================================
# 7. Upload
# =============================================================================


def upload_to_es(
    vector_jsonl: Path = VECTOR_JSONL_PATH,
    parent_store_jsonl: Path = PARENT_STORE_JSONL_PATH,
    index_name: str = ES_INDEX_NAME,
    batch_size: int = EMBEDDING_BATCH_SIZE,
    force_rebuild: bool = True,
) -> None:
    print(f"\n인덱스: {index_name}")
    print(f"vector payload: {vector_jsonl}")
    print(f"parent store  : {parent_store_jsonl}\n")

    embedder = get_embedder()
    vector_dim = embedder.get_sentence_embedding_dimension()

    print(f"인덱스 {'재생성' if force_rebuild else '생성/확인(증분)'} (dims={vector_dim})")
    create_es_index(es, index_name, vector_dim=vector_dim, force_rebuild=force_rebuild)

    existing_ids: set[str] = set()
    if not force_rebuild:
        print("ES 기존 문서 ID 조회 중...")
        existing_ids = fetch_existing_doc_ids(es, index_name)
        print(f"  ES 기존 문서: {len(existing_ids)}개")

    # ── 로드 ─────────────────────────────────────────────────────────────────
    print("JSONL 로드 중...")
    vector_raws = load_jsonl(vector_jsonl)
    parent_raws = load_jsonl(parent_store_jsonl)
    print(f"  vector_payload : {len(vector_raws)}개")
    print(f"  parent_store   : {len(parent_raws)}개")

    chunk_counter: Counter[str] = Counter()
    validation_warn = 0

    embed_docs: list[dict[str, Any]] = []   # 임베딩 대상
    store_docs: list[dict[str, Any]] = []   # 임베딩 없이 저장

    for raw in vector_raws:
        doc = prepare_doc(raw)
        ct = str(doc.get("chunk_type") or "unknown")
        chunk_counter[ct] += 1
        warns = validate_doc(doc)
        if warns:
            validation_warn += 1
            if validation_warn <= 5:
                print(f"  [WARN] id={doc.get('id')} : {', '.join(warns)}")
        embed_docs.append(doc)

    for raw in parent_raws:
        doc = prepare_doc(raw)
        doc["index_for_embedding"] = False
        ct = str(doc.get("chunk_type") or "unknown")
        chunk_counter[f"{ct}(store)"] += 1
        warns = validate_doc(doc)
        if warns:
            validation_warn += 1
        store_docs.append(doc)

    embed_docs, skip_embed = filter_new_docs(embed_docs, existing_ids)
    store_docs, skip_store = filter_new_docs(store_docs, existing_ids)
    skipped_total = skip_embed + skip_store

    all_docs = embed_docs + store_docs
    new_chunk_counter: Counter[str] = Counter()
    for doc in all_docs:
        ct = str(doc.get("chunk_type") or "unknown")
        if doc.get("index_for_embedding") is False and ct == "parent_section":
            ct = f"{ct}(store)"
        new_chunk_counter[ct] += 1

    print(f"\nJSONL 총 {len(vector_raws) + len(parent_raws)}개 → 신규 업로드 {len(all_docs)}개")
    print(f"  embed={len(embed_docs)}, store={len(store_docs)}, ES 스킵={skipped_total}")
    print(f"chunk_type 분포(신규): {dict(sorted(new_chunk_counter.items()))}")
    if validation_warn:
        print(f"[WARN] validation 경고 {validation_warn}건 (상위 5건만 출력)")

    if not all_docs:
        es.indices.refresh(index=index_name)
        doc_count = es.count(index=index_name).get("count", 0)
        print("-" * 55)
        print(f"추가할 신규 문서 없음. ES 유지: '{index_name}' (총 docs: {doc_count})")
        return

    # ── 임베딩 + 업로드 ───────────────────────────────────────────────────────
    success_total = 0
    embed_counter: Counter[str] = Counter()

    for start in tqdm(range(0, len(all_docs), batch_size), desc="Embedding + Index"):
        batch = all_docs[start : start + batch_size]

        # 임베딩 대상 분리
        to_embed: list[tuple[int, dict[str, Any]]] = []   # (batch_idx, doc)
        for i, d in enumerate(batch):
            ct = str(d.get("chunk_type") or "")
            is_store = ct in _STORE_ONLY_CHUNK_TYPES or d.get("index_for_embedding") is False
            if not is_store and d.get("embedding_text"):
                to_embed.append((i, d))

        # 벡터 계산
        if to_embed:
            texts = [d["embedding_text"] for _, d in to_embed]
            vectors = embedder.encode(
                texts,
                batch_size=batch_size,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            vectors = np.asarray(vectors, dtype="float32")
            for (i, d), vec in zip(to_embed, vectors):
                d["embedding"] = vec.tolist()
                embed_counter[str(d.get("chunk_type") or "other")] += 1

        # ES bulk
        actions = []
        for d in batch:
            if "embedding" not in d:
                embed_counter["no_embed"] += 1
            action: dict[str, Any] = {
                "_op_type": "index",
                "_index": index_name,
                "_source": d,
            }
            if d.get("id"):
                action["_id"] = d["id"]
            actions.append(action)

        try:
            success, errors = helpers.bulk(es, actions, raise_on_error=False)
            success_total += success
            if errors:
                print(f"\n  bulk 부분 실패 {len(errors)}건")
                for err in errors[:3]:
                    print(f"    {err}")
        except Exception as exc:
            print(f"\n  bulk 에러: {exc}")

    es.indices.refresh(index=index_name)
    doc_count = es.count(index=index_name).get("count", 0)

    print("-" * 55)
    print(f"업로드 완료: 신규 {success_total}개 (스킵 {skipped_total}개) → '{index_name}' (총 docs: {doc_count})")
    print(f"임베딩 통계: {dict(sorted(embed_counter.items()))}")
    print(
        "검색 흐름: child/attachment embedding → kNN+BM25 → "
        "parent_doc_id mget → LLM에 parent_section.text"
    )


# =============================================================================
# 8. Run
# =============================================================================
if __name__ == "__main__":
    upload_to_es(
        vector_jsonl=VECTOR_JSONL_PATH,
        parent_store_jsonl=PARENT_STORE_JSONL_PATH,
        index_name=ES_INDEX_NAME,
        force_rebuild=True,
    )
