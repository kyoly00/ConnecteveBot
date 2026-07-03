# uvicorn main:app --port 3000 --reload
# uvicorn app.main:app --port 3000 --reload
# ngrok http 3000 --url=liberty-victory-sherry.ngrok-free.dev

import json
import logging
import asyncio
import os
import re
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_CONN_BOT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _CONN_BOT_DIR.parent
os.chdir(_CONN_BOT_DIR)

import app  # noqa: F401 — scripts/ parsers path bootstrap

from dotenv import load_dotenv

load_dotenv(_CONN_BOT_DIR / ".env")

from app.core.config import (  # noqa: E402
    ATTACHMENTS_DIR,
    ATTACHMENTS_STATIC_MOUNT,
    BOT_JOB_COMPLETED_RETENTION_DAYS,
    BOT_JOB_FAILED_RETENTION_DAYS,
    BOT_JOB_PURGE_BATCH_SIZE,
    BOT_JOB_WORKER_COUNT,
    GOV_FILES_MOUNT,
    GOV_PROJECTS_DAILY_DIR,
    PROJECT_ROOT,
    PUBLIC_BASE_URL,
    APP_HOME_GUIDE_TEXT,
)

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from slack_bolt.app.async_app import AsyncApp
from slack_sdk.signature import SignatureVerifier

from app.rag.vectordb import init_vectordb
from app.agent.router import async_agent_chat, close_async_openai
from app.services.slack_attachments import SubmitterInfo, UserAttachmentBundle, ingest_slack_files
from app.ml.inference import start_ml_inference, shutdown_ml_inference
from app.slack.ui import (
    SlackFormatter,
    build_gov_response_blocks,
    build_rag_response_blocks,
    build_general_response_blocks,
    make_answer_blocks,
    clean_blocks,
)
from app.services.gov_project.gov_project import resolve_gov_file_on_disk
from app.slack.streaming import SlackStreamUpdater
from app.core import rag_debug_logger

# DB & Services
from app.db.connection import init_db, close_db
from app.services.bot_jobs.post_response import enqueue_post_response_job
from app.services.bot_jobs.slack_ingress import (
    enqueue_slack_event_callback,
    enqueue_slack_slash_command,
)
from app.services.bot_jobs.webhook_ingress import (
    enqueue_confluence_webhook,
    enqueue_graph_notification,
)
from app.services.bot_jobs.queue import purge_old_bot_jobs
from app.services.bot_jobs.worker import start_bot_job_worker
from app.services.chat import chat_service, memory_service, improvement_service
from app.core.settings import get_settings
from app.api.routes.health import router as health_router
from app.services.webhook_security import (
    verify_confluence_webhook_request,
    verify_graph_notification_payload,
)

# Confluence Webhook Sync
from app.integrations.confluence_webhook_sync import (
    parse_confluence_webhook,
    poll_trashed_pages_and_remove,
)

# 로거 설정
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 1. 환경 변수
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")
KST = ZoneInfo("Asia/Seoul")

# =============================================================================
# 백그라운드 스케줄 (KST) — 폴링 주기·실행 시각
# =============================================================================
CONFLUENCE_TRASH_POLL_INTERVAL_SEC = int(os.getenv("CONFLUENCE_TRASH_POLL_INTERVAL_SEC", "3600"))

GOV_PIPELINE_HOUR = int(os.getenv("GOV_PIPELINE_HOUR", "10"))
GOV_PIPELINE_MINUTE = int(os.getenv("GOV_PIPELINE_MINUTE", "32"))

FLEX_HR_DAILY_POLL_INTERVAL_SEC = int(os.getenv("FLEX_HR_POLL_INTERVAL_SEC", "3600"))

FLEX_HR_DAILY_SLACK_HOUR = int(os.getenv("FLEX_HR_DAILY_HOUR", "10"))
FLEX_HR_DAILY_SLACK_MINUTE = int(os.getenv("FLEX_HR_DAILY_MINUTE", "32"))

FLEX_HR_MONTHLY_UPDATE_HOUR = int(os.getenv("FLEX_HR_MONTHLY_NOON_HOUR", "10"))
FLEX_HR_MONTHLY_UPDATE_MINUTE = int(os.getenv("FLEX_HR_MONTHLY_NOON_MINUTE", "35"))

NEWS_DAILY_SLACK_HOUR = int(os.getenv("NEWS_DAILY_SLACK_HOUR", "10"))
NEWS_DAILY_SLACK_MINUTE = int(os.getenv("NEWS_DAILY_SLACK_MINUTE", "35"))

ROOM_REMINDER_POLL_INTERVAL_SEC = int(os.getenv("ROOM_REMINDER_POLL_INTERVAL_SEC", "60"))

MANAGED_ROOM_DAILY_SYNC_HOUR = int(os.getenv("MANAGED_ROOM_DAILY_SYNC_HOUR", "10"))
MANAGED_ROOM_DAILY_SYNC_MINUTE = int(os.getenv("MANAGED_ROOM_DAILY_SYNC_MINUTE", "40"))

DATA_CLEANUP_RETENTION_DAYS = int(os.getenv("DATA_CLEANUP_RETENTION_DAYS", "10"))
DATA_CLEANUP_HOUR = int(os.getenv("DATA_CLEANUP_HOUR", "10"))
DATA_CLEANUP_MINUTE = int(os.getenv("DATA_CLEANUP_MINUTE", "40"))


def _next_scheduled_time(
    now: datetime,
    *,
    hour: int,
    minute: int,
    earliest_date=None,
) -> datetime:
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if earliest_date is not None and target.date() < earliest_date:
        target = datetime.combine(earliest_date, target.time())
    if target <= now:
        target += timedelta(days=1)
    return target


def _next_flex_hr_monthly_scheduled_run(
    now: datetime | None = None,
    *,
    earliest_date=None,
) -> datetime:
    now = now or datetime.now()
    return _next_scheduled_time(
        now,
        hour=FLEX_HR_MONTHLY_UPDATE_HOUR,
        minute=FLEX_HR_MONTHLY_UPDATE_MINUTE,
        earliest_date=earliest_date,
    )


# 2. 비동기 슬랙 앱 (API client) 및 서명 검증
slack_app = AsyncApp(token=SLACK_BOT_TOKEN, signing_secret=SLACK_SIGNING_SECRET)
_slack_signature_verifier = SignatureVerifier(SLACK_SIGNING_SECRET)

app = FastAPI()
app.include_router(health_router)

app.mount(
    ATTACHMENTS_STATIC_MOUNT,
    StaticFiles(directory=str(ATTACHMENTS_DIR)),
    name="attachments",
)

# 앱 시작 시 VectorDB + PostgreSQL 초기화
@app.on_event("startup")
async def startup_event():
    # PostgreSQL 연결 확인
    try:
        await init_db()
        logger.info("PostgreSQL 초기화 완료")
        if os.getenv("BOT_JOB_WORKER_ENABLED", "true").lower() in ("1", "true", "yes"):
            start_bot_job_worker()
            logger.info("bot_jobs 워커 %d개 시작 (PostgreSQL 큐)", BOT_JOB_WORKER_COUNT)
    except Exception as e:
        logger.warning("PostgreSQL 연결 실패 (채팅 메모리 비활성화): %s", e)

    # ML 워커(ProcessPool + 배치 임베딩) — VectorDB 검색 전에 시작
    await start_ml_inference()

    # VectorDB 초기화 (ES 연결·인덱스; 임베딩/리랭킹은 ml_inference 워커)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: init_vectordb(force_rebuild=False))
    logger.info("VectorDB 초기화 완료")
    logger.info("PROJECT_ROOT=%s", PROJECT_ROOT)
    logger.info("GOV_PROJECTS_DAILY_DIR=%s", GOV_PROJECTS_DAILY_DIR)
    logger.info(
        "첨부 정적 서빙: %s%s (PUBLIC_BASE_URL=%s)",
        PUBLIC_BASE_URL,
        ATTACHMENTS_STATIC_MOUNT,
        PUBLIC_BASE_URL,
    )

    if os.getenv("CONFLUENCE_TRASH_POLL_ENABLED", "true").lower() in ("1", "true", "yes"):
        asyncio.create_task(_trash_poll_loop(CONFLUENCE_TRASH_POLL_INTERVAL_SEC))
        logger.info(
            "휴지통 Playwright 폴링 시작 (incremental interval=%ds, full_scan=%ss)",
            CONFLUENCE_TRASH_POLL_INTERVAL_SEC,
            os.getenv("CONFLUENCE_TRASH_POLL_FULL_SCAN_INTERVAL_SEC", "86400"),
        )

    if os.getenv("GOV_PIPELINE_ENABLED", "true").lower() in ("1", "true", "yes"):
        asyncio.create_task(_gov_pipeline_loop())

    if os.getenv("FLEX_HR_POLL_ENABLED", "true").lower() in ("1", "true", "yes"):
        asyncio.create_task(_flex_hr_poll_loop(FLEX_HR_DAILY_POLL_INTERVAL_SEC))
        logger.info("Flex HR 일간 폴링 시작 (interval=%ds)", FLEX_HR_DAILY_POLL_INTERVAL_SEC)

    if os.getenv("FLEX_HR_MONTHLY_SCHEDULE_ENABLED", "true").lower() in ("1", "true", "yes"):
        asyncio.create_task(_flex_hr_monthly_noon_loop())
        logger.info(
            "Flex HR 월간 정기 갱신 시작 (매일 %02d:%02d KST, 이번달+다음달)",
            FLEX_HR_MONTHLY_UPDATE_HOUR,
            FLEX_HR_MONTHLY_UPDATE_MINUTE,
        )

    if os.getenv("FLEX_HR_MONTHLY_BOOTSTRAP_ON_STARTUP", "true").lower() in ("1", "true", "yes"):
        asyncio.create_task(_flex_hr_monthly_bootstrap_once())

    if os.getenv("FLEX_HR_DAILY_SLACK_ENABLED", "true").lower() in ("1", "true", "yes"):
        asyncio.create_task(_flex_hr_daily_slack_loop())

    if os.getenv("ROOM_REMINDER_POLL_ENABLED", "true").lower() in ("1", "true", "yes"):
        asyncio.create_task(_room_reminder_poll_loop(ROOM_REMINDER_POLL_INTERVAL_SEC))
        logger.info("회의실 리마인더 폴링 시작 (interval=%ds)", ROOM_REMINDER_POLL_INTERVAL_SEC)

    if os.getenv("MANAGED_ROOM_SYNC_ENABLED", "true").lower() in ("1", "true", "yes"):
        if os.getenv("MANAGED_ROOM_SYNC_ON_STARTUP", "true").lower() in ("1", "true", "yes"):
            asyncio.create_task(_managed_room_sync_once())
        asyncio.create_task(_managed_room_daily_sync_loop())
        logger.info(
            "managed_room_events 일일 동기화 시작 (매일 %02d:%02d KST)",
            MANAGED_ROOM_DAILY_SYNC_HOUR,
            MANAGED_ROOM_DAILY_SYNC_MINUTE,
        )

    if os.getenv("NEWS_DAILY_SLACK_ENABLED", "true").lower() in ("1", "true", "yes"):
        asyncio.create_task(_news_daily_slack_loop())
        logger.info(
            "뉴스 일일 슬랙 시작 (매일 %02d:%02d KST, 전일 Top 10)",
            NEWS_DAILY_SLACK_HOUR,
            NEWS_DAILY_SLACK_MINUTE,
        )

    if os.getenv("OUTLOOK_ROOM_WEBHOOK_ENABLED", "true").lower() in ("1", "true", "yes"):
        asyncio.create_task(_outlook_room_subscription_loop())
        logger.info("Outlook 회의실 webhook subscription 루프 시작")

    if os.getenv("DATA_CLEANUP_ENABLED", "true").lower() in ("1", "true", "yes"):
        asyncio.create_task(_data_cleanup_loop())
        logger.info("데이터 정리 루프 시작")


async def _managed_room_sync_once() -> None:
    from app.services.outlook_room.managed_room_sync import sync_all_managed_rooms

    try:
        stats = await sync_all_managed_rooms()
        logger.info("[ManagedRoomSync] startup sync %s", stats)
    except Exception as e:
        logger.error("[ManagedRoomSync] sync failed: %s", e)


def _next_managed_room_daily_sync_kst() -> datetime:
    now = datetime.now(KST)
    target = datetime.combine(
        now.date(),
        datetime.min.time(),
        tzinfo=KST,
    ).replace(
        hour=MANAGED_ROOM_DAILY_SYNC_HOUR,
        minute=MANAGED_ROOM_DAILY_SYNC_MINUTE,
    )
    if target <= now:
        target += timedelta(days=1)
    return target


async def _managed_room_daily_sync_loop() -> None:
    while True:
        target = _next_managed_room_daily_sync_kst()
        sleep_sec = max(1.0, (target - datetime.now(KST)).total_seconds())
        logger.info("[ManagedRoomSync] next daily sync at %s", target.isoformat())
        await asyncio.sleep(sleep_sec)
        await _managed_room_sync_once()


async def _outlook_room_subscription_loop() -> None:
    from app.services.outlook_room.managed_room_sync import create_or_refresh_subscriptions

    notification_url = os.getenv("OUTLOOK_ROOM_WEBHOOK_NOTIFICATION_URL", "").strip()
    if not notification_url and PUBLIC_BASE_URL:
        notification_url = f"{PUBLIC_BASE_URL.rstrip('/')}/api/graph/webhook"
    client_state = get_settings().OUTLOOK_ROOM_WEBHOOK_CLIENT_STATE
    if not notification_url or not client_state:
        logger.warning(
            "Outlook webhook 비활성: OUTLOOK_ROOM_WEBHOOK_NOTIFICATION_URL/PUBLIC_BASE_URL "
            "및 OUTLOOK_ROOM_WEBHOOK_CLIENT_STATE 필요"
        )
        return

    interval = int(os.getenv("OUTLOOK_ROOM_SUBSCRIPTION_RENEW_INTERVAL_SEC", "43200"))
    expiration_hours = int(os.getenv("OUTLOOK_ROOM_SUBSCRIPTION_EXPIRATION_HOURS", "48"))
    loop = asyncio.get_event_loop()
    while True:
        try:
            await loop.run_in_executor(
                None,
                lambda: create_or_refresh_subscriptions(
                    notification_url=notification_url,
                    client_state=client_state,
                    expiration_hours=expiration_hours,
                ),
            )
        except Exception as e:
            logger.error("[OutlookRoomWebhook] subscription refresh failed: %s", e)
        await asyncio.sleep(interval)


async def _room_reminder_poll_loop(interval_sec: int) -> None:
    """회의실 예약 챗봇 Slack 리마인더 — due 예약 DM 발송."""
    from app.services.outlook_room.room_reminder import process_due_room_reminders

    while True:
        try:
            sent = await process_due_room_reminders(slack_app.client)
            if sent:
                logger.info("[RoomReminder] 폴링 발송 %d건", sent)
        except Exception as e:
            logger.error("[RoomReminder] 폴링 실패: %s", e)
        await asyncio.sleep(interval_sec)


async def _flex_hr_poll_loop(interval_sec: int) -> None:
    """Flex HR — 일간(당일 타임라인) HTML 수집·JSON 갱신."""
    from app.services.flex_hr.flex_hr import run_flex_hr_updates

    loop = asyncio.get_event_loop()
    try:
        report = await loop.run_in_executor(None, run_flex_hr_updates)
        logger.info("Flex HR 일간 폴링(기동): %s", report)
    except Exception as e:
        logger.error("Flex HR 일간 폴링(기동) 실패: %s", e)

    while True:
        await asyncio.sleep(interval_sec)
        try:
            report = await loop.run_in_executor(None, run_flex_hr_updates)
            logger.info("Flex HR 일간 폴링: %s", report)
        except Exception as e:
            logger.error("Flex HR 일간 폴링 실패: %s", e)



_flex_hr_monthly_last_run_date = None

async def _flex_hr_monthly_bootstrap_once() -> None:
    """기동 시 월간 부트스트랩 — 전월·당월·익월 중 누락분 수집."""
    from app.services.flex_hr.flex_hr import run_flex_hr_monthly_bootstrap

    loop = asyncio.get_event_loop()
    try:
        report = await loop.run_in_executor(None, run_flex_hr_monthly_bootstrap)
        logger.info("Flex HR 월간 부트스트랩(기동): %s", report)
    except Exception as e:
        logger.error("Flex HR 월간 부트스트랩(기동) 실패: %s", e)


async def _flex_hr_monthly_noon_loop() -> None:
    """Flex HR — 매일 정오 이번 달·다음 달 월간 근태 갱신."""
    global _flex_hr_monthly_last_run_date
    from datetime import datetime, timedelta
    from app.services.flex_hr.flex_hr import run_flex_hr_monthly_scheduled_update

    loop = asyncio.get_event_loop()
    logger.info(
        "Flex HR 월간 정기 갱신 시각: %02d:%02d KST",
        FLEX_HR_MONTHLY_UPDATE_HOUR,
        FLEX_HR_MONTHLY_UPDATE_MINUTE,
    )

    while True:
        now = datetime.now()
        today = now.date()

        if _flex_hr_monthly_last_run_date == today:
            next_run = _next_flex_hr_monthly_scheduled_run(
                now,
                earliest_date=today + timedelta(days=1),
            )
        else:
            next_run = _next_flex_hr_monthly_scheduled_run(now)

        wait_sec = max(1.0, (next_run - now).total_seconds())
        logger.info(
            "Flex HR 월간 정기 갱신 다음 실행: %s (%.0fs 후)",
            next_run.isoformat(),
            wait_sec,
        )
        await asyncio.sleep(wait_sec)

        today = datetime.now().date()
        if _flex_hr_monthly_last_run_date == today:
            continue

        try:
            report = await loop.run_in_executor(None, run_flex_hr_monthly_scheduled_update)
            logger.info("Flex HR 월간 정기 갱신 완료: %s", report)
            _flex_hr_monthly_last_run_date = today
        except Exception as e:
            logger.error("Flex HR 월간 정기 갱신 실패: %s", e)



_flex_hr_last_sent_date = None  # date | None — 프로세스 내 하루 1회 전송

async def _flex_hr_daily_slack_loop() -> None:
    """Flex HR — 매일 지정 시각 재택/휴가자 명단 Slack 전송 (하루 1회)."""
    global _flex_hr_last_sent_date
    from datetime import datetime, timedelta
    from app.services.flex_hr.flex_hr import next_flex_hr_roster_run, send_slack_daily_roster

    loop = asyncio.get_event_loop()
    logger.info(
        "Flex HR 일일 명단 슬랙 시각: %02d:%02d (주말·공휴일 제외)",
        FLEX_HR_DAILY_SLACK_HOUR,
        FLEX_HR_DAILY_SLACK_MINUTE,
    )

    while True:
        now = datetime.now()
        today = now.date()

        if _flex_hr_last_sent_date == today:
            next_run = next_flex_hr_roster_run(
                now,
                hour=FLEX_HR_DAILY_SLACK_HOUR,
                minute=FLEX_HR_DAILY_SLACK_MINUTE,
                earliest_date=today + timedelta(days=1),
            )
        else:
            next_run = next_flex_hr_roster_run(
                now,
                hour=FLEX_HR_DAILY_SLACK_HOUR,
                minute=FLEX_HR_DAILY_SLACK_MINUTE,
            )

        wait_sec = (next_run - now).total_seconds()
        logger.info(
            "Flex HR 일일 명단 다음 실행 예정: %s (%.0fs 후 대기)",
            next_run.isoformat(),
            wait_sec,
        )
        await asyncio.sleep(wait_sec)

        today = datetime.now().date()
        if _flex_hr_last_sent_date == today:
            continue

        try:
            ok = await loop.run_in_executor(None, send_slack_daily_roster)
            logger.info("Flex HR 일일 명단 슬랙 전송: ok=%s", ok)
            if ok:
                _flex_hr_last_sent_date = today
        except Exception as e:
            logger.error("Flex HR 일일 명단 슬랙 실패: %s", e)



_news_daily_last_sent_date = None  # date | None — 프로세스 내 하루 1회 전송

async def _news_daily_slack_loop() -> None:
    """전일 뉴스 크롤·요약 → Slack Top 10 (매일 지정 시각, 하루 1회)."""
    global _news_daily_last_sent_date
    from app.services.news.news_summary import run_daily_news_job

    loop = asyncio.get_event_loop()
    logger.info(
        "뉴스 일일 슬랙 시각: %02d:%02d KST (대상: 전일)",
        NEWS_DAILY_SLACK_HOUR,
        NEWS_DAILY_SLACK_MINUTE,
    )

    while True:
        now = datetime.now()
        today = now.date()

        if _news_daily_last_sent_date == today:
            next_run = _next_scheduled_time(
                now,
                hour=NEWS_DAILY_SLACK_HOUR,
                minute=NEWS_DAILY_SLACK_MINUTE,
                earliest_date=today + timedelta(days=1),
            )
        else:
            next_run = _next_scheduled_time(
                now,
                hour=NEWS_DAILY_SLACK_HOUR,
                minute=NEWS_DAILY_SLACK_MINUTE,
            )

        wait_sec = max(1.0, (next_run - now).total_seconds())
        logger.info(
            "뉴스 일일 슬랙 다음 실행: %s (%.0fs 후)",
            next_run.isoformat(),
            wait_sec,
        )
        await asyncio.sleep(wait_sec)

        today = datetime.now().date()
        if _news_daily_last_sent_date == today:
            continue

        skip_slack = not bool(os.getenv("SLACK_BOT_TOKEN"))
        try:
            report = await loop.run_in_executor(
                None,
                lambda: run_daily_news_job(skip_slack=skip_slack),
            )
            logger.info("뉴스 일일 슬랙 작업 완료: %s", report)
            if report.get("slack_ok") or report.get("slack_skipped"):
                _news_daily_last_sent_date = today
        except Exception as e:
            logger.error("뉴스 일일 슬랙 실패: %s", e)


async def _gov_pipeline_loop() -> None:
    """정부과제 일일 파이프라인 — 매일 지정 시각 실행 (주말·공휴일 제외)."""
    from datetime import datetime
    from app.services.business_calendar import next_business_day_run, should_skip_daily_notification
    from app.services.gov_project.gov_project import run_daily_pipeline

    loop = asyncio.get_event_loop()
    logger.info(
        "정부과제 파이프라인 실행 시각: %02d:%02d (주말·공휴일 제외)",
        GOV_PIPELINE_HOUR,
        GOV_PIPELINE_MINUTE,
    )

    while True:
        now = datetime.now()
        next_run = next_business_day_run(
            now,
            hour=GOV_PIPELINE_HOUR,
            minute=GOV_PIPELINE_MINUTE,
        )
        wait_sec = (next_run - now).total_seconds()
        logger.info(
            "정부과제 파이프라인 다음 실행 예정: %s (%.0fs 후 대기)",
            next_run.isoformat(),
            wait_sec,
        )
        await asyncio.sleep(wait_sec)

        skip = should_skip_daily_notification()
        if skip:
            logger.info("정부과제 파이프라인 스킵 — %s", skip)
            continue

        target_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            report = await loop.run_in_executor(
                None,
                lambda: run_daily_pipeline(
                    target_date=target_date,
                    output_root=str(GOV_PROJECTS_DAILY_DIR),
                    skip_archive=False,
                    skip_slack=not bool(os.getenv("SLACK_BOT_TOKEN")),
                ),
            )
            logger.info("정부과제 파이프라인 완료: %s", report.get("session_dir"))
        except Exception as e:
            logger.error("정부과제 파이프라인 실패: %s", e)


async def _trash_poll_loop(interval_sec: int) -> None:
    """증분(기본 1h) + 전체 스캔(기본 24h) 휴지통 폴링 — 삭제는 웹훅이 아닌 여기서만."""
    loop = asyncio.get_event_loop()
    # 기동 직후 1회
    try:
        report = await loop.run_in_executor(None, poll_trashed_pages_and_remove)
        logger.info("휴지통 폴링(기동): %s", report)
    except Exception as e:
        logger.error("휴지통 폴링(기동) 실패: %s", e)

    while True:
        await asyncio.sleep(interval_sec)
        try:
            report = await loop.run_in_executor(None, poll_trashed_pages_and_remove)
            logger.info("휴지통 폴링: %s", report)
        except Exception as e:
            logger.error("휴지통 폴링 실패: %s", e)


def run_data_cleanup() -> None:
    """
    데이터 일별 파일 및 폴더 관리 함수.
    DATA_CLEANUP_RETENTION_DAYS 설정보다 오래된 파일/폴더를 삭제합니다.
    flex_hr의 월간 근태 보고서 파일들은 삭제 대상에서 제외합니다.
    """
    import shutil
    retention_days = DATA_CLEANUP_RETENTION_DAYS
    logger.info(f"[DataCleanup] 데이터 폴더 정리 시작 (보관 일수 = {retention_days}일)...")

    # 1. 정부과제 (Data/government_projects/daily)
    gov_dir = GOV_PROJECTS_DAILY_DIR
    if gov_dir.exists() and gov_dir.is_dir():
        gov_subdirs = []
        for p in gov_dir.iterdir():
            if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name):
                try:
                    dt = datetime.strptime(p.name, "%Y-%m-%d")
                    gov_subdirs.append((dt, p))
                except ValueError:
                    pass
        gov_subdirs.sort(key=lambda x: x[0], reverse=True)
        if len(gov_subdirs) > retention_days:
            for dt, p in gov_subdirs[retention_days:]:
                try:
                    shutil.rmtree(p)
                    logger.info(f"[DataCleanup] 오래된 정부과제 디렉토리 삭제 완료: {p.name}")
                except Exception as e:
                    logger.error(f"[DataCleanup] 정부과제 디렉토리({p.name}) 삭제 실패: {e}")

    # 2. 일일 뉴스 (Data/daily_news_crawling)
    news_dir = PROJECT_ROOT / "Data" / "daily_news_crawling"
    if news_dir.exists() and news_dir.is_dir():
        # (a) YYYY-MM-DD 형식의 하위 디렉토리 정리
        news_subdirs = []
        for p in news_dir.iterdir():
            if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name):
                try:
                    dt = datetime.strptime(p.name, "%Y-%m-%d")
                    news_subdirs.append((dt, p))
                except ValueError:
                    pass
        news_subdirs.sort(key=lambda x: x[0], reverse=True)
        if len(news_subdirs) > retention_days:
            for dt, p in news_subdirs[retention_days:]:
                try:
                    shutil.rmtree(p)
                    logger.info(f"[DataCleanup] 오래된 뉴스 디렉토리 삭제 완료: {p.name}")
                except Exception as e:
                    logger.error(f"[DataCleanup] 뉴스 디렉토리({p.name}) 삭제 실패: {e}")

        # (b) outputs 디렉토리 내의 파일 정리
        outputs_dir = news_dir / "outputs"
        if outputs_dir.exists() and outputs_dir.is_dir():
            news_files = {}
            for p in outputs_dir.iterdir():
                if p.is_file():
                    m = re.match(r"^news_(\d{4}-\d{2}-\d{2})\.(json|md)$", p.name)
                    if m:
                        date_str = m.group(1)
                        try:
                            dt = datetime.strptime(date_str, "%Y-%m-%d")
                            if dt not in news_files:
                                news_files[dt] = []
                            news_files[dt].append(p)
                        except ValueError:
                            pass
            sorted_dates = sorted(news_files.keys(), reverse=True)
            if len(sorted_dates) > retention_days:
                for dt in sorted_dates[retention_days:]:
                    for p in news_files[dt]:
                        try:
                            p.unlink()
                            logger.info(f"[DataCleanup] 오래된 뉴스 결과 파일 삭제 완료: {p.name}")
                        except Exception as e:
                            logger.error(f"[DataCleanup] 뉴스 결과 파일({p.name}) 삭제 실패: {e}")

    # 3. Flex HR (Data/flex_hr)
    flex_dir = PROJECT_ROOT / "Data" / "flex_hr"
    if flex_dir.exists() and flex_dir.is_dir():
        flex_files = {}
        for p in flex_dir.iterdir():
            if p.is_file():
                m1 = re.match(r"^flex_HR_(\d{4}-\d{2}-\d{2})\.html$", p.name)
                m2 = re.match(r"^flex_hr_parsed_(\d{4}-\d{2}-\d{2})\.json$", p.name)
                m = m1 or m2
                if m:
                    date_str = m.group(1)
                    try:
                        dt = datetime.strptime(date_str, "%Y-%m-%d")
                        if dt not in flex_files:
                            flex_files[dt] = []
                        flex_files[dt].append(p)
                    except ValueError:
                        pass
        sorted_dates = sorted(flex_files.keys(), reverse=True)
        if len(sorted_dates) > retention_days:
            for dt in sorted_dates[retention_days:]:
                for p in flex_files[dt]:
                    try:
                        p.unlink()
                        logger.info(f"[DataCleanup] 오래된 Flex HR 일일 파일 삭제 완료: {p.name}")
                    except Exception as e:
                        logger.error(f"[DataCleanup] Flex HR 일일 파일({p.name}) 삭제 실패: {e}")



_data_cleanup_last_run_date = None


async def run_bot_job_cleanup() -> None:
    """bot_jobs completed/failed 오래된 행 정리."""
    if os.getenv("BOT_JOB_PURGE_ENABLED", "true").lower() not in ("1", "true", "yes"):
        return
    try:
        await purge_old_bot_jobs(
            completed_retention_days=BOT_JOB_COMPLETED_RETENTION_DAYS,
            failed_retention_days=BOT_JOB_FAILED_RETENTION_DAYS,
            batch_size=BOT_JOB_PURGE_BATCH_SIZE,
        )
    except Exception as e:
        logger.error("[BotJob] purge failed: %s", e)


async def _data_cleanup_loop() -> None:
    """
    데이터 정리 백그라운드 태스크 루프.
    서버 시작 시 1회 즉시 실행 후, 매일 지정된 시각(기본 12:00 PM)에 맞춰 실행됩니다.
    """
    global _data_cleanup_last_run_date
    loop = asyncio.get_event_loop()

    # 1. 서버 시작 시 즉시 1회 실행
    try:
        await loop.run_in_executor(None, run_data_cleanup)
        await run_bot_job_cleanup()
        _data_cleanup_last_run_date = datetime.now().date()
    except Exception as e:
        logger.error(f"[DataCleanup] 시작 시 초기 데이터 정리 실패: {e}")

    # 2. 매일 지정된 시각에 실행되는 반복 루프
    while True:
        now = datetime.now()
        today = now.date()

        if _data_cleanup_last_run_date == today:
            next_run = _next_scheduled_time(
                now,
                hour=DATA_CLEANUP_HOUR,
                minute=DATA_CLEANUP_MINUTE,
                earliest_date=today + timedelta(days=1),
            )
        else:
            next_run = _next_scheduled_time(
                now,
                hour=DATA_CLEANUP_HOUR,
                minute=DATA_CLEANUP_MINUTE,
            )

        wait_sec = max(1.0, (next_run - now).total_seconds())
        logger.info(
            f"[DataCleanup] 다음 데이터 정리 실행 예정 시각: {next_run.isoformat()} (대기 시간: {wait_sec:.0f}초)"
        )
        await asyncio.sleep(wait_sec)

        today = datetime.now().date()
        if _data_cleanup_last_run_date == today:
            continue

        try:
            await loop.run_in_executor(None, run_data_cleanup)
            await run_bot_job_cleanup()
            _data_cleanup_last_run_date = today
        except Exception as e:
            logger.error(f"[DataCleanup] 정기 데이터 정리 실행 실패: {e}")


@app.on_event("shutdown")
async def shutdown_event():
    await shutdown_ml_inference()
    await close_async_openai()
    await close_db()


async def fetch_slack_user_profile(user_id: str) -> dict:
    """Slack users.info로 실제 이름·이메일·프로필 이미지를 조회한다.
    이메일은 users:read.email 스코프가 있어야 채워진다. 실패 시 빈 dict 반환.
    """
    try:
        res = await slack_app.client.users_info(user=user_id)
        info = res.get("user") or {}
        profile = info.get("profile") or {}
        return {
            "name": (
                info.get("real_name")
                or profile.get("real_name")
                or profile.get("display_name")
                or None
            ),
            "email": profile.get("email") or None,
            "profile_image_url": (
                profile.get("image_512")
                or profile.get("image_192")
                or profile.get("image_original")
                or None
            ),
        }
    except Exception as e:
        logger.warning("Slack 유저 프로필 조회 실패: user_id=%s err=%s", user_id, e)
        return {}


async def send_app_welcome_message(client, user_id: str) -> None:
    """App Messages 탭 진입 시 DM으로 기능 안내 전송 (세션당 1회)."""
    open_res = await client.conversations_open(users=user_id)
    channel_id = (open_res.get("channel") or {}).get("id")
    if not channel_id:
        logger.warning("welcome DM 채널 open 실패: user_id=%s", user_id)
        return

    db_user = await chat_service.get_or_create_user(slack_user_id=user_id)
    if await chat_service.should_skip_app_welcome(
        user_id=db_user.id,
        slack_channel_id=channel_id,
    ):
        logger.info(
            "App welcome 생략 (이미 전송 또는 대화 있음): user_id=%s channel=%s",
            user_id,
            channel_id,
        )
        return

    blocks = clean_blocks(make_answer_blocks(APP_HOME_GUIDE_TEXT, docs=None))
    await client.chat_postMessage(
        channel=channel_id,
        blocks=blocks,
        text=APP_HOME_GUIDE_TEXT,
    )
    await chat_service.mark_app_welcome_sent(
        user_id=db_user.id,
        slack_channel_id=channel_id,
    )


async def process_and_respond(
    channel_id,
    user_id,
    text,
    thread_ts,
    *,
    channel_type: str = "",
    files: list | None = None,
):
    """
    채널에 메시지가 있을 때, 메시지를 처리하고 응답을 보낸다.
    """
    rag_debug_logger.set_session_id(thread_ts)
    try:
        rag_debug_logger._write("07_slack_io.jsonl", {
            "ts": rag_debug_logger._ts(),
            "type": "input",
            "channel_id": channel_id,
            "user_id": user_id,
            "thread_ts": thread_ts,
            "text": text,
            "files_count": len(files or []),
        })

        bot_token = os.getenv("SLACK_BOT_TOKEN", "")
        slack_profile = await fetch_slack_user_profile(user_id)
        attachment_bundle: UserAttachmentBundle = await ingest_slack_files(
            files,
            bot_token,
            session_id=thread_ts,
            user_text=text or "",
            submitter=SubmitterInfo(
                slack_user_id=user_id,
                name=str(slack_profile.get("name") or ""),
                email=str(slack_profile.get("email") or ""),
            ),
        )

        # 1. '분석 중' 초기 메시지 (스레드 답변)
        initial_res = await slack_app.client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text="처리 중...",
            blocks=[{
                "type": "context",
                "elements": [
                    {
                        "type": "image",
                        "image_url": f"{PUBLIC_BASE_URL}/media/attachments/Connbot_img/connbot_question.png",
                        "alt_text": "connbot_question"
                    },
                    {"type": "mrkdwn", "text": "어시스턴트가 답변을 생성하고 있습니다..."}]
            }]
        )

        try:
            # ─── DB 기반 세션/사용자 관리 ───
            db_user = None
            db_session = None
            user_msg = None
            conversation_history = None
            memory_context = ""
            session_summary_raw = None   # 디버그 로그용 raw
            memories_raw: list = []       # 디버그 로그용 raw

            try:
                db_user = await chat_service.get_or_create_user(
                    slack_user_id=user_id,
                    name=slack_profile.get("name"),
                    email=slack_profile.get("email"),
                    profile_image_url=slack_profile.get("profile_image_url"),
                )
                db_session = await chat_service.get_or_create_session(
                    user_id=db_user.id,
                    slack_channel_id=channel_id,
                    slack_thread_ts=thread_ts,
                )

                # user 메시지 저장
                user_msg = await chat_service.save_message(
                    session_id=db_session.id,
                    user_id=db_user.id,
                    role="user",
                    content=text or ("(첨부 파일)" if attachment_bundle.has_content else text),
                    metadata={
                        "slack_ts": thread_ts,
                        "channel_id": channel_id,
                        "attachment_ids": attachment_bundle.item_ids(),
                        "attachment_filenames": attachment_bundle.filenames,
                    },
                )

                if attachment_bundle.has_content:
                    try:
                        await chat_service.save_chat_attachments(
                            user_id=db_user.id,
                            session_id=db_session.id,
                            slack_thread_ts=thread_ts,
                            user_text=text,
                            records=attachment_bundle.to_db_records(user_text=text),
                        )
                    except Exception as att_db_err:
                        logger.warning("chat_attachments DB 저장 실패: %s", att_db_err)

                # 메모리 + 대화 히스토리 로드 (세션·채널 스코프)
                memory_context = await chat_service.get_memory_context_for_prompt(
                    db_user.id,
                    session_id=db_session.id,
                    slack_channel_id=channel_id,
                )
                conversation_history = await chat_service.get_conversation_history(db_session.id)

                # 디버그 로그용: 세션 요약 + 장기 메모리 raw 추출
                session_summary_raw, memories_raw = await chat_service.get_debug_context(
                    session_id=db_session.id,
                    user_id=db_user.id,
                    slack_channel_id=channel_id,
                )

            except Exception as db_err:
                logger.warning("DB 세션 관리 실패 (기존 방식으로 fallback): %s", db_err)

            # ─── Step 1: Agent로 의도 분류 및 답변 생성 (LLM stream → Slack 점진 갱신) ───
            slack_stream = SlackStreamUpdater(
                slack_app.client,
                channel_id=channel_id,
                message_ts=initial_res["ts"],
            )
            answer, docs, intent, source_doc_numbers, links_used_by_doc, attachments_used_by_doc, gov_attachments = await async_agent_chat(
                text,
                session_id=thread_ts,
                conversation_history=conversation_history,
                memory_context=memory_context,
                session_summary_raw=session_summary_raw,
                memories_raw=memories_raw,
                stream=slack_stream,
                requester_email=db_user.email if db_user else None,
                requester_name=db_user.name if db_user else None,
                requester_user_id=db_user.id if db_user else None,
                requester_slack_user_id=user_id,
                requester_slack_channel_id=channel_id,
                attachment_bundle=attachment_bundle if attachment_bundle.has_content else None,
                db_session_id=db_session.id if db_session else None,
            )

            rag_debug_logger._write("07_slack_io.jsonl", {
                "ts": rag_debug_logger._ts(),
                "type": "agent",
                "thread_ts": thread_ts,
                "intent": intent,
            })

            # ─── DB: assistant 메시지 저장 ───
            assistant_msg = None
            if db_user and db_session:
                try:
                    assistant_msg = await chat_service.save_message(
                        session_id=db_session.id,
                        user_id=db_user.id,
                        role="assistant",
                        content=answer,
                        metadata={
                            "intent": intent,
                            "docs_count": len(docs) if docs else 0,
                            "source_doc_numbers": source_doc_numbers,
                            "links_used_by_doc": links_used_by_doc,
                            "attachments_used_by_doc": attachments_used_by_doc,
                        },
                    )
                except Exception as db_err:
                    logger.warning("assistant 메시지 저장 실패: %s", db_err)

            formatted_answer = SlackFormatter.to_slack(answer)

            # ─── Step 2: Slack Block Kit 빌드 및 전송 ───
            if intent == "general":
                response_blocks = await build_general_response_blocks(user_id, formatted_answer)
                logger.info(f"[Main] general 응답 전송: '{text}'")

                rag_debug_logger._write("07_slack_io.jsonl", {
                    "ts": rag_debug_logger._ts(),
                    "type": "output_general",
                    "thread_ts": thread_ts,
                    "answer": formatted_answer,
                })
            elif intent.startswith("gov_project"):
                response_blocks = await build_gov_response_blocks(
                    user_id,
                    formatted_answer,
                    gov_attachments=gov_attachments,
                )
                logger.info(
                    "[Main] gov_project 응답 전송: '%s' (files=%s)",
                    text,
                    len(gov_attachments or []),
                )
                rag_debug_logger._write("07_slack_io.jsonl", {
                    "ts": rag_debug_logger._ts(),
                    "type": "output_gov",
                    "thread_ts": thread_ts,
                    "answer": formatted_answer,
                    "gov_files_count": len(gov_attachments or []),
                })
            else:
                response_blocks = await build_rag_response_blocks(
                    user_id,
                    formatted_answer,
                    docs,
                    source_doc_numbers=source_doc_numbers,
                    links_used_by_doc=links_used_by_doc,
                    attachments_used_by_doc=attachments_used_by_doc,
                )
                logger.info(f"[Main] RAG 응답 전송: '{text}' (docs={len(docs)})")

                rag_debug_logger._write("07_slack_io.jsonl", {
                    "ts": rag_debug_logger._ts(),
                    "type": "output_rag",
                    "thread_ts": thread_ts,
                    "answer": formatted_answer,
                    "docs_count": len(docs) if docs else 0
                })

            # 기존 메시지 업데이트
            await slack_app.client.chat_update(
                channel=channel_id,
                ts=initial_res["ts"],
                blocks=response_blocks,
                text=formatted_answer if formatted_answer else "답변이 완료되었습니다."
            )

            # ─── 응답 후: 메모리·품질 개선은 별도 큐 작업 (워커 슬롯 즉시 반환) ───
            if db_user and db_session:
                try:
                    await enqueue_post_response_job(
                        user_id=db_user.id,
                        session_id=db_session.id,
                        conversation_key=f"{channel_id}:{thread_ts}",
                        user_msg_id=user_msg.id if user_msg else None,
                        assistant_msg_id=assistant_msg.id if assistant_msg else None,
                        user_text=text,
                        assistant_text=answer,
                        intent=intent,
                        docs=docs,
                    )
                except Exception as queue_err:
                    logger.warning("post_response 큐 등록 실패, 인라인 fallback: %s", queue_err)
                    asyncio.create_task(run_post_response_tasks({
                        "user_id": str(db_user.id),
                        "session_id": str(db_session.id),
                        "user_msg_id": str(user_msg.id) if user_msg else None,
                        "assistant_msg_id": str(assistant_msg.id) if assistant_msg else None,
                        "user_text": text,
                        "assistant_text": answer,
                        "intent": intent,
                        "docs": [
                            {"score": float(getattr(d, "score", 0) or 0)}
                            for d in (docs or [])
                        ],
                    }))

        except Exception as e:
            logger.error(f"Error: {e}")

            # 에러 로깅
            rag_debug_logger._write("07_slack_io.jsonl", {
                "ts": rag_debug_logger._ts(),
                "type": "error",
                "thread_ts": thread_ts,
                "error_msg": str(e)
            })

            await slack_app.client.chat_update(
                channel=channel_id,
                ts=initial_res["ts"],
                text="⚠️ 답변 생성 중 오류가 발생했습니다."
            )
    finally:
        rag_debug_logger.cleanup_session(thread_ts)


async def run_post_response_tasks(payload: dict) -> None:
    """응답 전송 후 큐 워커가 실행 — 품질 이벤트 + 세션 요약/메모리 추출."""
    import uuid
    from types import SimpleNamespace

    try:
        user_id = uuid.UUID(str(payload["user_id"]))
        session_id = uuid.UUID(str(payload["session_id"]))
        user_msg_id = uuid.UUID(payload["user_msg_id"]) if payload.get("user_msg_id") else None
        assistant_msg_id = (
            uuid.UUID(payload["assistant_msg_id"])
            if payload.get("assistant_msg_id")
            else None
        )
        user_text = str(payload.get("user_text") or "")
        assistant_text = str(payload.get("assistant_text") or "")
        intent = str(payload.get("intent") or "")
        docs = [
            SimpleNamespace(score=d.get("score"))
            for d in (payload.get("docs") or [])
        ]

        await improvement_service.detect_and_log_improvement_events(
            user_id=user_id,
            session_id=session_id,
            user_msg_id=user_msg_id,
            assistant_msg_id=assistant_msg_id,
            user_text=user_text,
            assistant_text=assistant_text,
            docs=docs,
            intent=intent,
        )

        await memory_service.check_and_run_background_tasks(
            session_id=session_id,
            user_id=user_id,
            user_text=user_text,
        )
    except Exception as e:
        logger.error("post_response 작업 실패: %s", e)


# --- 슬랙 커맨드/이벤트는 /slack/events → bot_jobs 큐 → 워커에서 처리 ---


@app.post("/webhook-endpoint")
async def confluence_webhook(
    request: Request,
    x_atlassian_webhook_event: Optional[str] = Header(None),
):
    """
    Confluence Cloud 웹훅 (CW 스페이스만, 생성/수정만).
    삭제는 Playwright viewtrash 폴링(CONFLUENCE_TRASH_POLL_*) — page_removed 웹훅은 ignored.
    """
    settings = get_settings()
    body = await request.body()
    verify_confluence_webhook_request(request, body, settings)

    header_event = (x_atlassian_webhook_event or "").strip()

    try:
        payload = json.loads(body.decode("utf-8") if body else "{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning("Confluence 웹훅 JSON 파싱 실패: %s", e)
        return {"status": "error", "message": "invalid JSON body"}

    if settings.is_production:
        logger.info(
            "Confluence 웹훅 수신 event_header=%s content_length=%s",
            header_event or "-",
            len(body),
        )
    else:
        logger.info("Confluence 웹훅 수신 headers=%s", dict(request.headers))
        logger.info(
            "Confluence 웹훅 body=%s",
            json.dumps(payload, ensure_ascii=False)[:8000],
        )

    parsed = parse_confluence_webhook(payload, header_event=header_event)
    page_id = parsed["page_id"]
    title = parsed["title"]
    event = parsed["event"]
    space_key = parsed.get("space_key") or ""

    logger.info(
        "Confluence 웹훅 해석 page_id=%s title=%s space=%s event=%s source=%s "
        "updateTrigger=%s body.event=%s header=%s page.version=%s top_keys=%s",
        page_id,
        title,
        space_key,
        event or "(없음)",
        parsed.get("event_source") or "-",
        parsed.get("update_trigger"),
        parsed.get("raw_body_event"),
        parsed.get("raw_header_event"),
        parsed.get("page_version"),
        parsed.get("payload_top_keys"),
    )

    if not parsed.get("space_allowed"):
        logger.info(
            "Confluence 웹훅 스킵 (스페이스): page_id=%s spaceKey=%s allowed=%s",
            page_id,
            space_key,
            parsed.get("allowed_space_keys"),
        )
        return {
            "status": "ignored",
            "message": "대상 스페이스가 아님",
            "page_id": page_id,
            "space_key": space_key,
            "allowed_space_keys": parsed.get("allowed_space_keys"),
        }

    if not page_id:
        return {"status": "ignored", "message": "수정 대상 page_id 없음"}

    if not event:
        return {
            "status": "ignored",
            "message": "이벤트를 알 수 없음 (header/body.event/updateTrigger 확인)",
            "page_id": page_id,
            "space_key": space_key,
            "update_trigger": parsed.get("update_trigger"),
        }

    if event == "page_removed":
        logger.info(
            "Confluence 웹훅 삭제 무시 (Trash 폴링): page_id=%s space=%s",
            page_id,
            space_key,
        )
        return {
            "status": "ignored",
            "message": "delete_handled_by_trash_poll_only",
            "page_id": page_id,
            "space_key": space_key,
        }

    await enqueue_confluence_webhook(
        event=event,
        page_id=page_id,
        title=title,
        webhook_meta=parsed,
    )
    return {
        "status": "accepted",
        "message": "동기화 작업 큐 등록",
        "event": event,
        "page_id": page_id,
        "space_key": space_key,
    }


@app.get(f"{GOV_FILES_MOUNT}/{{target_date}}/{{idx}}/{{filename:path}}")
async def gov_file_endpoint(target_date: str, idx: int, filename: str):
    """정부과제 아카이브 첨부파일 서빙."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", target_date):
        raise HTTPException(status_code=400, detail="invalid date")
    file_path = resolve_gov_file_on_disk(target_date, idx, filename)
    if not file_path:
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(
        path=str(file_path),
        filename=file_path.name,
        media_type="application/octet-stream",
    )


@app.api_route("/api/graph/webhook", methods=["GET", "POST"])
async def outlook_graph_webhook(request: Request):
    """Microsoft Graph change notification endpoint for room calendar events."""
    validation_token = request.query_params.get("validationToken")
    if validation_token:
        return PlainTextResponse(validation_token)

    if request.method == "GET":
        return PlainTextResponse("")

    settings = get_settings()
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid graph payload")

    notifications = verify_graph_notification_payload(payload, settings)
    accepted = 0
    for notification in notifications:
        await enqueue_graph_notification(notification)
        accepted += 1

    return {"status": "accepted", "count": accepted}


# --- 엔드포인트 ---
@app.get("/slack/oauth/callback")
async def slack_oauth_callback(code: Optional[str] = None, state: Optional[str] = None):
    """
    Slack OAuth redirect callback endpoint.
    Handles Slack app installation redirect.
    """
    client_id = os.getenv("SLACK_CLIENT_ID")
    client_secret = os.getenv("SLACK_CLIENT_SECRET")
    
    if client_id and client_secret:
        try:
            # Exchange code for token
            response = await slack_app.client.oauth_v2_access(
                client_id=client_id,
                client_secret=client_secret,
                code=code,
            )
            if response.get("ok"):
                bot_user_id = response.get("bot_user_id")
                app_id = response.get("app_id")
                team_name = response.get("team", {}).get("name")
                logger.info(
                    "Slack app successfully installed! team=%s, app_id=%s, bot_user_id=%s",
                    team_name, app_id, bot_user_id
                )
                return PlainTextResponse("설치가 성공적으로 완료되었습니다! 이 창을 닫으셔도 좋습니다.")
            else:
                logger.error("Slack OAuth access failed: %s", response.get("error"))
                return PlainTextResponse(f"설치 실패: {response.get('error')}", status_code=400)
        except Exception as e:
            logger.error("Slack OAuth exception: %s", e)
            return PlainTextResponse("설치 중 에러가 발생했습니다.", status_code=500)
            
    return PlainTextResponse("OAuth callback received successfully (client credentials not configured).")


@app.post("/slack/events")
async def slack_endpoint(request: Request):
    """
    Slack Events API / Slash Commands — 즉시 200, 실제 처리는 bot_jobs 워커.
    재시도(X-Slack-Retry-Num)는 (source, source_event_id) UNIQUE로 멱등 처리.
    """
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not _slack_signature_verifier.is_valid(body.decode("utf-8"), timestamp, signature):
        raise HTTPException(status_code=401, detail="invalid slack signature")

    retry_num = request.headers.get("X-Slack-Retry-Num")
    content_type = (request.headers.get("content-type") or "").lower()

    if "application/json" in content_type:
        payload = json.loads(body)
        if payload.get("type") == "url_verification":
            return PlainTextResponse(payload.get("challenge", ""))
        if payload.get("type") == "event_callback":
            if retry_num:
                logger.info(
                    "Slack retry #%s event_id=%s",
                    retry_num,
                    payload.get("event_id"),
                )
            await enqueue_slack_event_callback(payload)
            return {"ok": True}
        raise HTTPException(status_code=400, detail="unsupported slack json payload")

    if "application/x-www-form-urlencoded" in content_type:
        if retry_num:
            logger.info("Slack slash command retry #%s", retry_num)
        await enqueue_slack_slash_command(body)
        return JSONResponse({
            "response_type": "ephemeral",
            "text": "🔍 처리 중...",
        })

    raise HTTPException(status_code=400, detail="unsupported content type")