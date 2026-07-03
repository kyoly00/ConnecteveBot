"""
scripts/init_db.py — DB 초기화 스크립트

사용법:
    cd ConnBot
    python -m scripts.init_db
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.db.connection import init_db, close_db


async def main():
    print("=" * 60)
    print("ConnBot PostgreSQL 초기화")
    print("=" * 60)
    try:
        await init_db()
        print("\n✅ DB 연결 성공!")
    except Exception as e:
        print(f"\n❌ DB 연결 실패: {e}")
        print("  1. docker ps 로 컨테이너 확인")
        print("  2. .env에 DATABASE_URL 확인")
        return
    finally:
        await close_db()

    ddl_dir = Path(__file__).resolve().parent.parent.parent / "ddl"
    print(f"\n테이블 생성 명령:")
    for name in (
        "001_initial_schema.sql",
        "002_room_bookings.sql",
        "003_chat_attachments.sql",
        "004_managed_room_events.sql",
        "005_bot_jobs.sql",
    ):
        path = ddl_dir / name
        if path.exists():
            print(f'  docker exec -i connbot-postgres psql -U connbot -d connbot < "{path}"')


if __name__ == "__main__":
    asyncio.run(main())
