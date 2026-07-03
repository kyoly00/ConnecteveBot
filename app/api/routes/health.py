"""Health / readiness probes."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.core.settings import Settings, get_settings
from app.db.connection import get_db_session

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


async def _database_live() -> bool:
    try:
        async with get_db_session() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@router.get("/readyz")
async def readyz(settings: Settings = Depends(get_settings)):
    checks = settings.readiness_config_checks()
    checks["database_live"] = await _database_live()

    failed = [name for name, ok in checks.items() if not ok]

    if failed:
        return {
            "status": "not_ready",
            "failed": failed,
            "checks": checks,
        }

    return {
        "status": "ready",
        "checks": checks,
    }
