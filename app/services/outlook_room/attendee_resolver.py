"""
회의실 예약 참석자 email resolve — roster 정규화 + PostgreSQL users + Graph.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from sqlalchemy import or_, select

from app.db.connection import get_db_session
from app.db.models import User
from app.services.outlook_room import ms_graph_room as graph

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^\s@]+$")


def _match_employees_in_query(query: str) -> list[str]:
    from app.services.flex_hr.flex_hr import match_employees_in_query

    return match_employees_in_query(query)


def name_lookup_variants(name: str) -> list[str]:
    """
    DB/Graph 조회용 이름 후보 — roster 전체 이름 + 성 제외 변형 + 원문.

    예: '금교현' → ['금교현', '교현'], '금교현' → ['금교현', '교현']
    """
    needle = (name or "").strip()
    if not needle:
        return []

    roster_full = _match_employees_in_query(needle)
    ordered: list[str] = []

    def _add(value: str) -> None:
        v = (value or "").strip()
        if v and v not in ordered:
            ordered.append(v)

    for full in roster_full:
        _add(full)
        if len(full) >= 2:
            _add(full[1:])

    _add(needle)
    return ordered


async def lookup_user_by_name(name: str) -> dict[str, str] | None:
    """users 테이블에서 이름으로 email resolve (공개 API)."""
    return await _lookup_user_by_name_variants(name)


async def _lookup_user_by_name_variants(name: str) -> dict[str, str] | None:
    for variant in name_lookup_variants(name):
        hit = await _lookup_user_in_db(name=variant)
        if hit:
            return hit
    return None


async def resolve_organizer_email(
    *,
    slack_user_id: str | None = None,
    fallback_email: str | None = None,
    fallback_name: str | None = None,
) -> tuple[str | None, str | None]:
    """
    Slack 요청자 → 주최자 (email, name).

    slack_user_id로 users 테이블 조회 후, 없으면 fallback_email 사용.
    """
    sid = (slack_user_id or "").strip()
    if sid:
        hit = await _lookup_user_in_db(slack_user_id=sid)
        if hit and hit.get("email"):
            return hit["email"].strip(), (hit.get("name") or fallback_name or "").strip() or None

    email = (fallback_email or "").strip()
    if email and "@" in email:
        name = (fallback_name or "").strip() or email.split("@")[0]
        return email, name or None

    return None, None


async def _lookup_user_in_db(
    *,
    email: str = "",
    name: str = "",
    slack_user_id: str = "",
) -> dict[str, str] | None:
    """
    users 테이블에서 email resolve.

    slack_user_id → email, email exact, name(유일 매칭) 순.
    """
    async with get_db_session() as session:
        if slack_user_id:
            stmt = select(User).where(User.slack_user_id == slack_user_id.strip())
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row and row.email:
                return {
                    "email": row.email.strip(),
                    "name": (row.name or row.email.split("@")[0]).strip(),
                }

        if email and "@" in email:
            stmt = select(User).where(User.email.ilike(email.strip()))
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row and row.email:
                return {
                    "email": row.email.strip(),
                    "name": (row.name or row.email.split("@")[0]).strip(),
                }

        if name:
            stmt = select(User).where(
                or_(
                    User.name.ilike(name.strip()),
                    User.name.ilike(f"%{name.strip()}%"),
                )
            ).limit(5)
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
            with_email = [r for r in rows if r.email]
            if len(with_email) == 1:
                row = with_email[0]
                return {
                    "email": row.email.strip(),
                    "name": (row.name or row.email.split("@")[0]).strip(),
                }
    return None


async def _resolve_attendee_hit(
    *,
    email: str,
    name: str,
    slack_user_id: str,
    headers: dict | None,
) -> dict[str, str] | None:
    if email and _EMAIL_RE.match(email):
        return {"email": email, "name": name or email.split("@")[0]}

    if slack_user_id:
        hit = await _lookup_user_in_db(slack_user_id=slack_user_id)
        if hit:
            return hit

    if email:
        hit = await _lookup_user_in_db(email=email)
        if hit:
            return hit

    if name:
        hit = await _lookup_user_by_name_variants(name)
        if hit:
            return hit

    if not headers or not (email or name):
        return None

    loop = asyncio.get_running_loop()
    if email:
        hit = await loop.run_in_executor(
            None,
            lambda e=email: graph.search_graph_user(headers, email=e),
        )
        if hit:
            return hit

    for variant in name_lookup_variants(name) if name else []:
        hit = await loop.run_in_executor(
            None,
            lambda vn=variant: graph.search_graph_user(headers, name=vn),
        )
        if hit:
            return hit
    return None


async def resolve_attendees(
    raw_attendees: list[dict[str, Any]] | None,
    *,
    organizer_email: str,
    headers: dict | None = None,
) -> tuple[list[dict[str, str]], list[str]]:
    """
    참석자 목록 → Graph required attendees용 [{email, name}, ...].

    resolve 순서: email → users(slack/email/roster·성 변형 name) → Graph.
    주최자(organizer_email)는 required 목록에서 제외.

    Returns
    -------
    (resolved, unresolved_labels)
    """
    organizer_key = (organizer_email or "").strip().lower()
    seen: set[str] = set()
    resolved: list[dict[str, str]] = []
    unresolved: list[str] = []

    for item in raw_attendees or []:
        if not isinstance(item, dict):
            continue

        email = str(item.get("email") or item.get("address") or "").strip()
        name = str(item.get("name") or "").strip()
        slack_user_id = str(item.get("slack_user_id") or "").strip()
        if not email and not name and not slack_user_id:
            continue

        label = email or name or slack_user_id or "참석자"
        hit = await _resolve_attendee_hit(
            email=email,
            name=name,
            slack_user_id=slack_user_id,
            headers=headers,
        )

        if not hit:
            unresolved.append(label)
            continue

        addr = hit["email"].strip().lower()
        if not addr or addr == organizer_key or addr in seen:
            continue
        seen.add(addr)
        resolved.append({
            "email": hit["email"].strip(),
            "name": hit.get("name") or hit["email"].split("@")[0],
        })

    return resolved, unresolved
