"""
PostgreSQL 초기화 — Docker 볼륨 삭제 후 DDL 전체 재적용.

사용법 (ConnBot 루트):
    python scripts/reset_db.py
    python scripts/reset_db.py --no-docker   # 컨테이너/볼륨 유지, 스키마만 drop+create
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DDL_DIR = ROOT / "ddl"

DDL_FILES = (
    "001_initial_schema.sql",
    "002_room_bookings.sql",
    "003_room_booking_reminders.sql",
    "003_chat_attachments.sql",
    "004_managed_room_events.sql",
    "005_bot_jobs.sql",
)

CONTAINER = os.getenv("POSTGRES_CONTAINER", "connbot-postgres")
PG_USER = os.getenv("POSTGRES_USER", "connbot")
PG_DB = os.getenv("POSTGRES_DB", "connbot")


def _run(cmd: list[str], *, check: bool = True, cwd: Path | None = None) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, cwd=cwd or ROOT, text=True)


def _docker_ok() -> bool:
    try:
        _run(["docker", "info"], check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def recreate_postgres_container() -> None:
    if not _docker_ok():
        raise RuntimeError("Docker를 사용할 수 없습니다.")

    _run(["docker", "compose", "stop", "postgres"], check=False)
    _run(["docker", "compose", "rm", "-f", "postgres"], check=False)

    vol_list = subprocess.run(
        ["docker", "volume", "ls", "-q", "--filter", "name=connbot_postgres_data"],
        capture_output=True,
        text=True,
        check=True,
    )
    for vol in vol_list.stdout.splitlines():
        vol = vol.strip()
        if vol:
            _run(["docker", "volume", "rm", "-f", vol], check=False)

    _run(["docker", "compose", "up", "-d", "postgres"])

    for _ in range(30):
        probe = subprocess.run(
            [
                "docker", "exec", CONTAINER,
                "pg_isready", "-U", PG_USER, "-d", PG_DB,
            ],
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0:
            print("PostgreSQL ready.")
            return
        time.sleep(1)
    raise RuntimeError("PostgreSQL 컨테이너가 준비되지 않았습니다.")


def apply_ddl() -> None:
    for name in DDL_FILES:
        path = DDL_DIR / name
        if not path.exists():
            print(f"skip missing ddl: {name}")
            continue
        sql = path.read_text(encoding="utf-8")
        proc = subprocess.run(
            ["docker", "exec", "-i", CONTAINER, "psql", "-U", PG_USER, "-d", PG_DB],
            input=sql,
            text=True,
            capture_output=True,
        )
        if proc.returncode != 0:
            print(proc.stderr or proc.stdout)
            raise RuntimeError(f"DDL 실패: {name}")
        print(f"applied {name}")


def verify() -> None:
    proc = subprocess.run(
        [
            "docker", "exec", CONTAINER,
            "psql", "-U", PG_USER, "-d", PG_DB,
            "-c",
            "\\dt",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    print(proc.stdout)


async def verify_async_connection() -> None:
    sys.path.insert(0, str(ROOT))
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    from app.db.connection import close_db, init_db

    await init_db()
    await close_db()
    print("async DATABASE_URL 연결 OK")


def main() -> int:
    parser = argparse.ArgumentParser(description="ConnBot PostgreSQL reset")
    parser.add_argument(
        "--no-docker",
        action="store_true",
        help="컨테이너/볼륨 재생성 없이 DDL만 재적용",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("ConnBot PostgreSQL reset")
    print("=" * 60)

    if not args.no_docker:
        recreate_postgres_container()
    else:
        if not _docker_ok():
            raise RuntimeError("Docker를 사용할 수 없습니다.")

    apply_ddl()
    verify()

    import asyncio

    asyncio.run(verify_async_connection())
    print("\n완료. 서버 재시작 시 managed_room_events는 Graph 동기화로 채워집니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
