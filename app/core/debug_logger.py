"""
debug_logger.py — 파이프라인 단계별 디버그 JSON 기록기

파이프라인의 4개 핵심 단계를 각각 별도 JSONL 파일로 기록합니다.

기록 파일 (debug/<session_ts>/ 디렉토리):
  01_parser_results.jsonl   — 파서(PDF/PPTX/HWP/…)의 원본 추출 결과
  02_vlm_inputs.jsonl       — VLM에 전달되는 프롬프트·컨텍스트
  03_vlm_outputs.jsonl      — VLM 응답 원문과 파싱된 결과
  04_vectordb_points.jsonl  — Qdrant에 upsert되는 point payload
  05_vlm_io.jsonl           — VLM 호출별 input/output 전체 기록 (JSONL 누적)
  05_vlm_io/                — VLM 호출 건별 개별 JSON 파일

사용법:
  DEBUG_PIPELINE=true python build_vectordb.py   (환경변수로 활성화)
  또는 build_full() 내에서 debug_logger.init(enabled=True) 직접 호출

주의:
  - JSONL 형식 (줄당 JSON 1개) → 대용량에서도 스트리밍 읽기 가능
  - 이미지 base64는 저장하지 않고 파일명만 기록 (파일 크기 절약)
  - 빌드 시작 시 타임스탬프 기반 세션 디렉토리에 새 파일 생성
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

# 활성화 여부 — init()에서 덮어쓸 수 있음
_ENABLED: bool = os.getenv("DEBUG_PIPELINE", "false").lower() in ("1", "true", "yes")

# 세션 출력 디렉토리 (init() 호출 후 확정)
_DEBUG_DIR: Path = Path("debug")

# 각 단계별 파일 핸들 (lazy open)
_handles: dict[str, Any] = {}
_lock = Lock()
_session_ts: str = ""
_vlm_io_dir: Path = Path("debug") / "vlm_io"


# =============================================================================
# 초기화
# =============================================================================

def init(debug_dir: Path | None = None, enabled: bool | None = None) -> None:
    """
    빌드 시작 시 한 번 호출합니다.

    Args:
        debug_dir: 기록 디렉토리 (None이면 PROJECT_DIR/debug)
        enabled:   True/False로 강제 설정. None이면 환경변수 따름.

    주의: build_full()에서 debug_logger.init()을 호출하기 전에
    write_vlm_io_log()나 log_*() 함수를 부르면 로그가 누락됩니다.
    반드시 init() 이후에 파이프라인을 시작하세요.
    """
    global _ENABLED, _DEBUG_DIR, _session_ts, _vlm_io_dir

    if enabled is not None:
        _ENABLED = enabled

    if not _ENABLED:
        logger.debug("디버그 파이프라인 로거 비활성화 상태")
        return

    _session_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = debug_dir or Path("debug")
    _DEBUG_DIR = base / _session_ts
    _DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    _vlm_io_dir = _DEBUG_DIR / "vlm_io"
    _vlm_io_dir.mkdir(parents=True, exist_ok=True)

    logger.info("파이프라인 디버그 로거 초기화 → %s", _DEBUG_DIR)


def get_debug_dir() -> Path:
    """현재 세션의 디버그 디렉토리 경로를 반환합니다."""
    return _DEBUG_DIR


def is_enabled() -> bool:
    return _ENABLED


# =============================================================================
# 내부 헬퍼
# =============================================================================

def _get_handle(filename: str):
    """파일 핸들을 lazy open하여 캐시합니다."""
    if filename not in _handles:
        path = _DEBUG_DIR / filename
        _handles[filename] = path.open("a", encoding="utf-8")
    return _handles[filename]


def _write(filename: str, record: dict[str, Any]) -> None:
    """레코드를 JSONL 형식으로 append합니다."""
    if not _ENABLED:
        return
    try:
        with _lock:
            fh = _get_handle(filename)
            fh.write(json.dumps(record, ensure_ascii=False, default=str))
            fh.write("\n")
            fh.flush()
    except Exception as exc:
        logger.warning("디버그 로그 기록 실패 (%s): %s", filename, exc)


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


# =============================================================================
# 1단계: Parser 결과
# =============================================================================

def log_parser_result(
    *,
    stage: str,                     # "attachment" | "web" | "text_section"
    page_id: str,
    page_title: str,
    source_name: str,               # 파일명 or URL
    ext: str,                       # 확장자 or 빈문자열
    parse_method: str,
    confidence: float | None,
    raw_text_length: int,
    raw_text_preview: str,          # 앞 500자
    tables_count: int,
    tables_preview: list[str],      # 테이블별 앞 200자
    error: str | None = None,
) -> None:
    """
    파서(PDF/PPTX/HWP/web 등)가 반환한 원본 추출 결과를 기록합니다.

    파일: 01_parser_results.jsonl
    """
    _write("01_parser_results.jsonl", {
        "ts": _ts(),
        "stage": stage,
        "page_id": page_id,
        "page_title": page_title,
        "source_name": source_name,
        "ext": ext,
        "parse_method": parse_method,
        "confidence": confidence,
        "raw_text_length": raw_text_length,
        "raw_text_preview": raw_text_preview,
        "tables_count": tables_count,
        "tables_preview": tables_preview,
        "error": error,
    })


# =============================================================================
# 2단계: VLM 입력
# =============================================================================

def log_vlm_input(
    *,
    call_type: str,                 # "text" | "image"
    page_id: str,
    page_title: str,
    source_name: str,
    source_type: str,
    section_path: list[str],
    prompt: str,
    raw_text_length: int,
    has_image: bool = False,
    image_filename: str = "",       # 이미지 파일명만 (base64 제외)
) -> None:
    """
    VLM에 전달되는 프롬프트와 컨텍스트를 기록합니다.

    파일: 02_vlm_inputs.jsonl
    """
    _write("02_vlm_inputs.jsonl", {
        "ts": _ts(),
        "call_type": call_type,
        "page_id": page_id,
        "page_title": page_title,
        "source_name": source_name,
        "source_type": source_type,
        "section_path": section_path,
        "raw_text_length": raw_text_length,
        "has_image": has_image,
        "image_filename": image_filename,
        "prompt_length": len(prompt),
        "prompt": prompt,           # 전체 프롬프트 기록
    })


# =============================================================================
# 3단계: VLM 출력
# =============================================================================

def log_vlm_output(
    *,
    call_type: str,                 # "text" | "image"
    page_id: str,
    page_title: str,
    source_name: str,
    vlm_raw_output: str,            # VLM 응답 원문
    parse_success: bool,
    refined_text_length: int,
    refined_text_preview: str,
    semantic_title: str,
    semantic_description: str,
    search_keywords: list[str],
    image_description: str = "",
    fallback_used: bool = False,
    error: str | None = None,
) -> None:
    """
    VLM의 응답 원문과 파싱된 구조화 결과를 기록합니다.

    파일: 03_vlm_outputs.jsonl
    """
    _write("03_vlm_outputs.jsonl", {
        "ts": _ts(),
        "call_type": call_type,
        "page_id": page_id,
        "page_title": page_title,
        "source_name": source_name,
        "vlm_raw_output_length": len(vlm_raw_output),
        "vlm_raw_output": vlm_raw_output,
        "parse_success": parse_success,
        "fallback_used": fallback_used,
        "refined_text_length": refined_text_length,
        "refined_text_preview": refined_text_preview,
        "semantic_title": semantic_title,
        "semantic_description": semantic_description,
        "search_keywords": search_keywords,
        "image_description": image_description,
        "error": error,
    })


# =============================================================================
# 4단계: Vector DB 저장
# =============================================================================

def log_vectordb_point(
    *,
    chunk_id: str,
    qdrant_point_id: str,
    source_type: str,
    page_id: str,
    page_title: str,
    title_path: str,
    text_length: int,
    text_preview: str,              # 앞 300자
    embedding_text_length: int,
    embedding_text_preview: str,    # 앞 300자
    payload: dict[str, Any],
    vector_dim: int,
    vector_preview: list[float],    # 앞 8개 차원
) -> None:
    """
    Qdrant에 upsert되는 각 point의 payload와 벡터 정보를 기록합니다.

    파일: 04_vectordb_points.jsonl
    """
    _write("04_vectordb_points.jsonl", {
        "ts": _ts(),
        "chunk_id": chunk_id,
        "qdrant_point_id": qdrant_point_id,
        "source_type": source_type,
        "page_id": page_id,
        "page_title": page_title,
        "title_path": title_path,
        "text_length": text_length,
        "text_preview": text_preview,
        "embedding_text_length": embedding_text_length,
        "embedding_text_preview": embedding_text_preview,
        "vector_dim": vector_dim,
        "vector_preview": vector_preview,
        "payload": payload,
    })


# =============================================================================
# 세션 종료
# =============================================================================

# =============================================================================
# VLM I/O 통합 로그 (build_vectordb.py의 write_vlm_io_log 대체)
# =============================================================================

def _json_safe(value: Any) -> Any:
    """JSON으로 안전하게 변환합니다."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict
        return _json_safe(asdict(value))
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump())
    if hasattr(value, "__dict__"):
        return _json_safe(vars(value))
    return repr(value)


def _short_file_safe(text: str, max_len: int = 80) -> str:
    """파일명에 안전한 짧은 문자열을 만듭니다."""
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in text)
    safe = safe.strip("_") or "unknown"
    return safe[:max_len]


def write_vlm_io_log(
    *,
    call_type: str,
    phase: str,
    page_id: str,
    page_title: str,
    source_name: str,
    source_type: str,
    input_payload: dict[str, Any],
    output_payload: Any | None = None,
    error: str | None = None,
) -> None:
    """
    VLM 호출 1건의 입출력을 즉시 파일로 저장합니다.

    저장 위치 (모두 _DEBUG_DIR 하위):
    - 05_vlm_io.jsonl        — 전체 호출 시간순 누적 JSONL
    - vlm_io/<ts>_<id>.json  — 호출 1건 개별 JSON (사람이 열어보기 편함)

    주의: init()을 먼저 호출해야 _ENABLED=True로 기록됩니다.
    """
    if not _ENABLED:
        return

    ts = datetime.now(timezone.utc).isoformat()
    event = {
        "timestamp": ts,
        "call_type": call_type,
        "phase": phase,
        "page_id": page_id,
        "page_title": page_title,
        "source_name": source_name,
        "source_type": source_type,
        "input": _json_safe(input_payload),
        "output": _json_safe(output_payload),
        "error": error,
    }

    try:
        with _lock:
            # 누적 JSONL
            jsonl_path = _DEBUG_DIR / "05_vlm_io.jsonl"
            with jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
                f.flush()

            # 개별 JSON
            file_key = f"{ts}_{page_id}_{source_type}_{source_name}"
            file_name = _short_file_safe(file_key) + ".json"
            with (_vlm_io_dir / file_name).open("w", encoding="utf-8") as f:
                json.dump(event, f, ensure_ascii=False, indent=2, default=str)
                f.flush()

        logger.debug(
            "VLM I/O 저장: phase=%s type=%s page=%s src=%s",
            phase, call_type, page_id, source_name,
        )
    except Exception as exc:
        logger.warning("VLM I/O 로그 저장 실패: %s", exc)


# =============================================================================
# 세션 종료
# =============================================================================

def close() -> None:
    """빌드 완료 후 모든 파일 핸들을 닫습니다."""
    with _lock:
        for fh in _handles.values():
            try:
                fh.close()
            except Exception:
                pass
        _handles.clear()

    if _ENABLED:
        logger.info("파이프라인 디버그 로그 저장 완료 → %s", _DEBUG_DIR)
