"""
ml_inference.py — ProcessPool + 배치 큐 기반 ML 추론 (임베딩·리랭킹).

- 임베딩: asyncio 배치 큐 → 워커 프로세스에서 encode
- 리랭킹: ProcessPoolExecutor로 워커에 위임
- sync 호출(vectordb hybrid_search 스레드): run_coroutine_threadsafe
"""

from __future__ import annotations

import asyncio
import logging
import sys
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import (
    ML_EMBED_BATCH_MAX_SIZE,
    ML_EMBED_BATCH_WAIT_MS,
    ML_INFERENCE_TIMEOUT_SEC,
    ML_WORKER_PROCESSES,
)

logger = logging.getLogger(__name__)

# =============================================================================
# ProcessPool 워커 (자식 프로세스에서 실행 — 모듈 최상위 함수 유지)
# =============================================================================

_worker_embedder = None
_worker_reranker = None


def init_worker() -> None:
    """ProcessPool 워커 초기화 — 모델 1회 로드."""
    global _worker_embedder, _worker_reranker

    from sentence_transformers import SentenceTransformer

    from app.core.config import EMBEDDING_MODEL_NAME
    from app.rag.vectordb import get_ort_reranker

    _worker_embedder = SentenceTransformer(EMBEDDING_MODEL_NAME)
    _worker_embedder.max_seq_length = 512
    _worker_reranker = get_ort_reranker()
    _worker_reranker._ensure_loaded()


def embed_batch(texts: list[str]) -> list[list[float]]:
    """쿼리/문서 텍스트 배치 임베딩 → float32 list."""
    if not texts:
        return []

    if _worker_embedder is None:
        init_worker()

    import numpy as np

    vectors = _worker_embedder.encode(
        texts,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    arr = np.asarray(vectors, dtype="float32")
    if arr.ndim == 1:
        return [arr.tolist()]
    return [row.tolist() for row in arr]


def rerank_pairs(pairs: list[tuple[str, str]]) -> list[float]:
    """(query, document) 쌍 리랭킹 점수."""
    if not pairs:
        return []

    if _worker_reranker is None:
        init_worker()

    return _worker_reranker.predict(pairs)


# =============================================================================
# 메인 프로세스 — 배치 큐 · ProcessPool 관리
# =============================================================================

_executor: ProcessPoolExecutor | None = None
_main_loop: asyncio.AbstractEventLoop | None = None
_batcher: "EmbedBatcher | None" = None


class EmbedBatcher:
    """여러 쿼리를 짧은 시간 모아 워커 프로세스에 배치 임베딩."""

    def __init__(
        self,
        executor: ProcessPoolExecutor,
        loop: asyncio.AbstractEventLoop,
        *,
        wait_ms: int = ML_EMBED_BATCH_WAIT_MS,
        max_batch_size: int = ML_EMBED_BATCH_MAX_SIZE,
    ) -> None:
        self._executor = executor
        self._loop = loop
        self._wait_sec = wait_ms / 1000.0
        self._max_batch_size = max(1, max_batch_size)
        self._queue: asyncio.Queue[tuple[str, asyncio.Future]] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._batch_loop(), name="ml_embed_batcher")

    async def embed(self, text: str) -> list[float]:
        text = (text or "").strip()
        if not text:
            return []

        fut: asyncio.Future = self._loop.create_future()
        await self._queue.put((text, fut))
        return await asyncio.wait_for(fut, timeout=ML_INFERENCE_TIMEOUT_SEC)

    async def _batch_loop(self) -> None:
        while True:
            text, fut = await self._queue.get()
            batch: list[tuple[str, asyncio.Future]] = [(text, fut)]
            deadline = self._loop.time() + self._wait_sec

            while len(batch) < self._max_batch_size:
                remaining = deadline - self._loop.time()
                if remaining <= 0:
                    break
                try:
                    item = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=remaining,
                    )
                    batch.append(item)
                except asyncio.TimeoutError:
                    break

            texts = [t for t, _ in batch]
            futures = [f for _, f in batch]
            try:
                vectors = await self._loop.run_in_executor(
                    self._executor,
                    partial(embed_batch, texts),
                )
                for vec, f in zip(vectors, futures):
                    if not f.done():
                        f.set_result(vec)
            except Exception as exc:
                logger.exception("배치 임베딩 실패: %s", exc)
                for f in futures:
                    if not f.done():
                        f.set_exception(exc)


async def start_ml_inference() -> None:
    """앱 startup — ProcessPool + 배치 루프 시작."""
    global _executor, _main_loop, _batcher

    if _executor is not None:
        return

    _main_loop = asyncio.get_running_loop()
    _executor = ProcessPoolExecutor(
        max_workers=ML_WORKER_PROCESSES,
        initializer=init_worker,
    )

    await _main_loop.run_in_executor(_executor, partial(embed_batch, ["워밍업"]))
    logger.info(
        "ML inference 워커 시작 (processes=%s, embed_batch_wait_ms=%s)",
        ML_WORKER_PROCESSES,
        ML_EMBED_BATCH_WAIT_MS,
    )

    _batcher = EmbedBatcher(_executor, _main_loop)
    _batcher.start()


async def shutdown_ml_inference() -> None:
    """앱 shutdown — ProcessPool 정리."""
    global _executor, _batcher, _main_loop

    if _batcher and _batcher._task and not _batcher._task.done():
        _batcher._task.cancel()
        try:
            await _batcher._task
        except asyncio.CancelledError:
            pass

    _batcher = None

    if _executor is not None:
        _executor.shutdown(wait=True, cancel_futures=True)
        _executor = None
        logger.info("ML inference 워커 종료")

    _main_loop = None


async def embed_query_async(text: str) -> list[float]:
    """비동기 단일 쿼리 임베딩."""
    if _batcher is None:
        raise RuntimeError("ML inference가 시작되지 않았습니다. start_ml_inference()를 호출하세요.")
    return await _batcher.embed(text)


def embed_query_sync(text: str) -> list[float]:
    """동기 컨텍스트(스레드 풀 내 hybrid_search)용 임베딩."""
    if _main_loop is None or _batcher is None:
        raise RuntimeError("ML inference가 시작되지 않았습니다.")

    fut = asyncio.run_coroutine_threadsafe(
        _batcher.embed(text),
        _main_loop,
    )
    return fut.result(timeout=ML_INFERENCE_TIMEOUT_SEC)


def rerank_pairs_sync(pairs: list[tuple[str, str]]) -> list[float]:
    """동기 컨텍스트용 리랭킹."""
    if _executor is None or _main_loop is None:
        raise RuntimeError("ML inference가 시작되지 않았습니다.")

    fut = asyncio.run_coroutine_threadsafe(
        rerank_pairs_async(pairs),
        _main_loop,
    )
    return fut.result(timeout=ML_INFERENCE_TIMEOUT_SEC)


async def rerank_pairs_async(pairs: list[tuple[str, str]]) -> list[float]:
    """비동기 리랭킹 — ProcessPool."""
    if _executor is None:
        raise RuntimeError("ML inference가 시작되지 않았습니다.")

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _executor,
        partial(rerank_pairs, pairs),
    )
