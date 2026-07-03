"""
database/connection.py — PostgreSQL async 연결 관리

Docker 컨테이너 기반 PostgreSQL에 asyncpg로 연결한다.
DATABASE_URL 환경변수 또는 config.py에서 URL을 가져온다.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import DATABASE_URL

logger = logging.getLogger(__name__)

# =============================================================================
# Engine & Session Factory
# =============================================================================

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            DATABASE_URL,
            echo=False,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            pool_recycle=3600,
        )
        logger.info("PostgreSQL async engine 생성: %s", DATABASE_URL.split("@")[-1])
    return _engine


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=_get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


# =============================================================================
# Public API
# =============================================================================

@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    비동기 DB 세션 context manager.

    사용법:
        async with get_db_session() as session:
            result = await session.execute(...)
    """
    factory = _get_session_factory()
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def init_db() -> None:
    """
    앱 시작 시 호출하여 DB 연결을 확인한다.
    테이블 생성은 DDL 스크립트로 별도 진행한다.
    cat ddl/001_initial_schema.sql | docker exec -i connbot-postgres psql -U connbot -d connbot
    """
    engine = _get_engine()
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                __import__("sqlalchemy").text("SELECT 1")
            )
            result.scalar()
        logger.info("✅ PostgreSQL 연결 성공!")
    except Exception as e:
        logger.error("❌ PostgreSQL 연결 실패: %s", e)
        raise


async def close_db() -> None:
    """앱 종료 시 engine dispose."""
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("PostgreSQL engine 종료")
