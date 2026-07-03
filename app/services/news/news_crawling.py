# -*- coding: utf-8 -*-
"""
뉴스 요약 봇용 슬림 config

방향:
1) 실제 직접 크롤링은 '국내 의료 전문지'만 유지
2) 정부기관/지원사업/경쟁사/해외기업/로봇기업은 크롤링 타깃이 아니라
   네이버 뉴스 API, 검색 API, LLM 필터링에 도움되는 SEARCH_HINTS로 축약
3) 1차 필터는 의료 AI/로봇/의료기기/병원/수가/인허가를 넓게 통과
4) 정형외과/근골격계/하지정렬/수술계획/경쟁사는 가산점으로 활용
"""


# =========================================================
# 0. 모니터링 목적
# =========================================================
TARGET_COMPANY = {
    "location": "서울 소재",
    "years_in_business": "7년 미만",
    "domain": "정형외과 AI SaMD 및 수술 로봇 연동 소프트웨어 개발",
    "core_products": [
        "CONNEVO KOA: 무릎 X-ray 기반 KL Grade / 퇴행성관절염 분석",
        "CONNEVO ALI/METRIC: 하지 X-ray 기반 valgus/varus, 다리 길이, HKAA 등 계측",
        "CONNEVO R: 무릎 임플란트 수술 로봇 시스템",
        "CONNEVO ASYST: 수술 중 motor 기반 다리 위치 조절 장치",
    ],
}

MONITORING_FOCUS = [
    "의료 AI",
    "의료기기 소프트웨어",
    "디지털헬스",
    "의료 로봇",
    "수술 로봇",
    "병원 AI/AX",
    "의료영상 AI",
    "X-ray AI",
    "AI 의료기기 인허가",
    "수가/비급여/혁신의료기술",
    "정부지원사업/바우처/R&D",
    "의료 AI 투자/M&A/IPO",
    "정형외과/근골격계/무릎/하지정렬/수술계획",
]


# =========================================================
# 1. 검색 확장용 힌트
# - 직접 크롤링하지 않고, 네이버 뉴스 API/검색 API 쿼리 확장과
#   LLM 2차 필터링 맥락으로만 사용
# =========================================================
SEARCH_HINTS = {
    "regulation_reimbursement_kr": {
        "mfds": {
            "name": "식품의약품안전처",
            "search_hint": "AI 의료기기 인허가, SaMD 허가심사, 디지털의료기기 가이드라인, 혁신의료기기",
        },
        "nids": {
            "name": "한국의료기기안전정보원",
            "search_hint": "의료기기 인증, RA, 국제규격, 혁신의료기기 기술지원, 의료기기 안전성",
        },
        "hira": {
            "name": "건강보험심사평가원",
            "search_hint": "수가, 비급여, 디지털의료기술 비급여정보, 보험등재, 임시등재",
        },
        "neca": {
            "name": "한국보건의료연구원",
            "search_hint": "신의료기술평가, 혁신의료기술, 임상근거, 의료기술평가",
        },
        "mohw": {
            "name": "보건복지부",
            "search_hint": "의료 정책, 디지털헬스 정책, 혁신 의료 기술, 건강보험 수가, 병원 AI 정책",
        },
    },

    "government_rnd_and_grants": {
        "khidi": {
            "name": "한국보건산업진흥원",
            "search_hint": "의료기기 사업공고, 디지털헬스 실증지원, 의료 AI 사업화, 글로벌 진출 지원",
        },
        "kmdf": {
            "name": "범부처전주기의료기기연구개발사업단",
            "search_hint": "의료기기 R&D, 수술로봇 과제, AI 로봇, 사업공고, RFP, 미충족 의료수요",
        },
        "khis": {
            "name": "한국보건의료정보원",
            "search_hint": "의료데이터, EMR, 의료 AI 데이터 활용 바우처, 보건의료 표준화, 병원 데이터 연계",
        },
    },

    "korea_competitors": {
        "connecteve": {
            "name": "코넥티브",
            "search_hint": "정형외과 AI SaMD, 무릎 X-ray AI, 하지정렬 계측, 수술로봇 연동",
        },
        "crescom": {
            "name": "크레스콤",
            "search_hint": "하지 X-ray 계측 및 근골격계 AI 분야 경쟁사, HKAA, KL Grade, MediAI-OA, MediAI-SG",
        },
        "lunit": {
            "name": "루닛",
            "search_hint": "의료 AI SaMD 사업화, FDA, 글로벌 진출, 의료영상 AI 벤치마크",
        },
    },

    "global_msk_ai_competitors": {
        "gleamer": {
            "name": "Gleamer",
            "search_hint": "MSK X-ray AI, BoneMetrics, X-ray 계측, 근골격계 영상 AI",
        },
        "imagebiopsy_lab": {
            "name": "ImageBiopsy Lab",
            "search_hint": "KOALA, LAMA, 무릎 골관절염 X-ray AI, KL Grade, 하지정렬 자동 계측",
        },
        "azmed": {
            "name": "AZmed",
            "search_hint": "X-ray AI, AZmeasure, osteo-articular measurement, 의료영상 워크플로우",
        },
        "peekmed": {
            "name": "PeekMed / Peek Health",
            "search_hint": "AI 기반 정형외과 수술계획, 2D X-ray to 3D knee reconstruction",
        },
    },

    "global_orthopedic_robotics": {
        "zimmer": {
            "name": "Zimmer Biomet",
            "search_hint": "ROSA Knee, 무릎 수술로봇, 인공관절, orthopedic analytics, 수술계획",
        },
        "think_surgical": {
            "name": "THINK Surgical",
            "search_hint": "TMINI, 무릎 수술로봇, TKA, robotic surgery",
        },
        "corin": {
            "name": "Corin Group",
            "search_hint": "ApolloKnee, OMNIBotics, 무릎 인공관절, 수술계획 플랫폼, 정형외과 로봇 벤치마크",
        },
    },
}


# =========================================================
# 2. 구글 뉴스 API / 검색 API용 쿼리 세트
# - 메인 수집은 검색 기반으로 넓게 수집
# =========================================================
SEARCH_QUERIES = {
    "medical_ai": [
        "의료 AI",
        "의료 인공지능",
        "헬스케어 AI",
        "AI 의료기기",
        "의료영상 AI",
        "AI 진단",
        "AI 판독",
        "병원 AI",
        "병원 AX",
    ],
    "medical_device_samd": [
        "의료기기 AI",
        "의료기기 소프트웨어",
        "소프트웨어 의료기기",
        "SaMD",
        "디지털의료기기",
        "혁신의료기기",
        "의료기기 식약처 허가",
        "의료기기 FDA 인증",
        "AI 의료기기 FDA",
    ],
    "robotics": [
        "의료 로봇",
        "수술 로봇",
        "수술로봇",
        "로봇수술",
        "재활 로봇",
        "웨어러블 로봇 의료기기",
        "AI 로봇 의료",
        "정형외과 로봇",
        "인공관절 로봇",
    ],
    "hospital_adoption": [
        "병원 AI 도입",
        "의료 AI 도입",
        "병원 디지털 전환",
        "스마트병원",
        "디지털치료기기 도입",
        "의료 AI 실증",
        "의료 AI 상용화",
        "의료데이터 플랫폼",
    ],
    "government_support": [
        "의료 AI 바우처",
        "의료데이터 활용 바우처",
        "혁신의료기기 기술지원",
        "의료기기 R&D",
        "디지털헬스 실증",
        "의료기기 사업화 지원",
        "보건의료 데이터 사업",
        "AI 의료기기 지원사업",
    ],
    "investment_business": [
        "의료 AI 투자",
        "디지털헬스 투자",
        "의료기기 투자유치",
        "의료 로봇 투자",
        "헬스케어 스타트업 투자",
        "의료 AI IPO",
        "의료기기 M&A",
        "디지털헬스 M&A",
    ],
    "ortho_boost": [
        "정형외과 AI",
        "근골격계 AI",
        "무릎 AI",
        "인공관절 AI",
        "하지정렬 AI",
        "X-ray AI",
        "엑스레이 AI",
        "수술계획 AI",
        "퇴행성관절염 AI",
    ],
    "company_watch": [
        "크레스콤",
        "루닛",
        "ImageBiopsy Lab",
        "Radiobotics",
        "Gleamer",
        "AZmed",
        "PeekMed",
        "코넥티브",
        "이지메디봇",
        "잇피",
        "엑스큐브",
        "휴로틱스",
        "제이앤피메디",
        "디알젬",
        "레이",
    ],
}

# news_crawler.py

import json
import re
import sys
import time
import argparse
from dataclasses import dataclass, asdict
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlencode, urljoin

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

_CONN_BOT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _CONN_BOT_DIR.parent
for _p in (_CONN_BOT_DIR, _REPO_ROOT):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

from app.core.config import (
    DAILY_NEWS_CRAWLING_DIR,
    EMBEDDING_BATCH_SIZE,
    EMBEDDING_MODEL_NAME,
    NEWS_DEDUP_EMBED_THRESHOLD,
)

KST = ZoneInfo("Asia/Seoul")


def today_kst() -> date:
    return datetime.now(KST).date()


@dataclass
class Article:
    site: str
    category: str
    title: str = ""
    press: str = ""
    subtitle: str = ""
    summary: str = ""
    body: str = ""
    url: str = ""
    published_at: str = ""
    crawled_at: str = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


# -----------------------------
# 공통 유틸
# -----------------------------

def sleep(sec=0.5):
    time.sleep(sec)


def clean_text(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def safe_text(locator, timeout=1000) -> str:
    try:
        if locator.count() == 0:
            return ""
        return clean_text(locator.first.inner_text(timeout=timeout))
    except Exception:
        return ""


def safe_attr(locator, attr: str, timeout=1000) -> str:
    try:
        if locator.count() == 0:
            return ""
        value = locator.first.get_attribute(attr, timeout=timeout)
        return value or ""
    except Exception:
        return ""


def absolute_url(base_url: str, href: str) -> str:
    if not href:
        return ""
    return urljoin(base_url, href)


def scroll_to_bottom(page, wait=0.5):
    """단일 스크롤 (하위 호환)."""
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    sleep(wait)


def scroll_page_to_end(page, *, max_rounds: int = 40, pause: float = 0.35):
    """Lazy load 대응 — 뷰포트 단위로 반복 스크롤."""
    for _ in range(max_rounds):
        page.evaluate(
            "() => { window.scrollBy(0, Math.max(500, window.innerHeight * 0.85)); }"
        )
        sleep(pause)


def scroll_until_items_stable(
    page,
    items_locator,
    *,
    max_rounds: int = 50,
    pause: float = 0.4,
    stable_limit: int = 4,
) -> int:
    """
    항목 locator 개수가 더 늘지 않을 때까지 스크롤.
    Returns: 최종 개수
    """
    prev = -1
    stable = 0
    for _ in range(max_rounds):
        scroll_page_to_end(page, max_rounds=1, pause=pause)
        try:
            count = items_locator.count()
        except Exception:
            break
        if count > prev:
            prev = count
            stable = 0
        else:
            stable += 1
            if stable >= stable_limit:
                break
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    sleep(pause)
    try:
        return items_locator.count()
    except Exception:
        return prev if prev >= 0 else 0


def click_more_until_done(
    page,
    button_xpath: str,
    *,
    max_clicks: int = 80,
    pause_after_click: float = 0.9,
) -> int:
    """
    스크롤 끝 → '더보기' 클릭 → 버튼이 안 보일 때까지 반복.
    Returns: 클릭 횟수
    """
    clicks = 0
    for _ in range(max_clicks):
        scroll_page_to_end(page)
        btn = page.locator(f"xpath={button_xpath}")
        if btn.count() == 0:
            break
        try:
            target = btn.first
            if not target.is_visible(timeout=1500):
                break
            target.scroll_into_view_if_needed(timeout=5000)
            sleep(0.2)
            target.click(timeout=5000)
            clicks += 1
            sleep(pause_after_click)
        except Exception:
            break
    scroll_page_to_end(page)
    return clicks


def _click_naver_more_articles(page, items_locator) -> int:
    """
    네이버 섹션 '기사 더보기' (과거 날짜 등). 당일은 버튼 없을 수 있음.
    visible 실패 시 JS click 시도 후 항목 수 증가 대기.
    """
    more_xpath = '//*[@id="newsct"]/div[2]/div/div[2]/a'
    clicks = 0
    for _ in range(80):
        before = items_locator.count()
        scroll_page_to_end(page, max_rounds=3, pause=0.35)

        btn = page.locator(f"xpath={more_xpath}")
        if btn.count() == 0:
            btn = page.locator('#newsct a:has-text("기사 더보기")')

        if btn.count() == 0:
            break

        target = btn.first
        clicked = False
        try:
            if target.is_visible(timeout=800):
                target.scroll_into_view_if_needed(timeout=3000)
                target.click(timeout=5000)
                clicked = True
        except Exception:
            pass

        if not clicked:
            try:
                target.evaluate("el => el.click()")
                clicked = True
            except Exception:
                break

        if not clicked:
            break

        clicks += 1
        sleep(1.0)
        scroll_until_items_stable(page, items_locator, max_rounds=15, stable_limit=3)
        after = items_locator.count()
        if after <= before:
            break

    return clicks


def goto(page, url: str):
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=7000)
    except PlaywrightTimeoutError:
        pass


def parse_date(text: str, today: date | None = None):
    today = today or today_kst()
    """
    여러 언론사 날짜 포맷 대응:
    - 2026.06.25
    - 2026-06-25
    - 2026/06/25
    - 06.25
    - 25.06.25
    - 입력 2026.06.25 10:30
    - 3분 전, 2시간 전, 오늘
    """
    if not text:
        return None

    t = clean_text(text)

    if any(x in t for x in ["분 전", "시간 전", "방금", "오늘"]):
        return today

    # 2026.06.25 / 2026-06-25 / 2026/06/25
    m = re.search(r"(20\d{2})[.\-/년\s]+(\d{1,2})[.\-/월\s]+(\d{1,2})", t)
    if m:
        y, mo, d = map(int, m.groups())
        try:
            return date(y, mo, d)
        except ValueError:
            return None

    # 26.06.25
    m = re.search(r"\b(\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})\b", t)
    if m:
        y, mo, d = map(int, m.groups())
        y += 2000
        try:
            return date(y, mo, d)
        except ValueError:
            return None

    # 06.25
    m = re.search(r"\b(\d{1,2})[.\-/](\d{1,2})\b", t)
    if m:
        mo, d = map(int, m.groups())
        try:
            return date(today.year, mo, d)
        except ValueError:
            return None

    return None


def is_today(text: str, today: date | None = None) -> bool:
    today = today or today_kst()
    parsed = parse_date(text, today)
    return parsed == today


def is_older_than_today(text: str, today: date | None = None) -> bool:
    today = today or today_kst()
    parsed = parse_date(text, today)
    return parsed is not None and parsed < today


def dedupe_articles(articles):
    seen = set()
    result = []

    for article in articles:
        key = article.url or f"{article.site}:{article.title}"
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(article)

    return result


_news_embedder = None


def _get_news_embedder():
    """크롤 스크립트 전용 lazy embedder (vectordb/ES 의존 없음)."""
    global _news_embedder
    if _news_embedder is None:
        import numpy as np  # noqa: F401 — sentence_transformers 의존
        from sentence_transformers import SentenceTransformer

        _news_embedder = SentenceTransformer(EMBEDDING_MODEL_NAME)
        _news_embedder.max_seq_length = 256
    return _news_embedder


def _article_embed_text(article: Article) -> str:
    """임베딩 dedup용 텍스트 — 제목 우선, 부제·본문 일부 보조."""
    parts: list[str] = []
    if article.title.strip():
        parts.append(article.title.strip())
    if article.subtitle.strip():
        parts.append(article.subtitle.strip())
    body = (article.body or article.summary or "").strip()
    if body:
        parts.append(body[:200])
    return " ".join(parts) or article.title or "empty"


def dedupe_articles_by_embedding(
    articles: list[Article],
    *,
    threshold: float | None = None,
) -> tuple[list[Article], list[dict]]:
    """
    URL/제목 exact dedup 후 임베딩 cosine 유사도로 교차 사이트 중복 제거.
    먼저 나온 기사를 대표로 유지한다.
    """
    import numpy as np

    rows = dedupe_articles(articles)
    if len(rows) <= 1:
        return rows, []

    thresh = threshold if threshold is not None else NEWS_DEDUP_EMBED_THRESHOLD
    texts = [_article_embed_text(a) for a in rows]
    embedder = _get_news_embedder()
    vectors = embedder.encode(
        texts,
        batch_size=EMBEDDING_BATCH_SIZE,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    mat = np.asarray(vectors, dtype=np.float32)

    keep_indices: list[int] = []
    duplicate_records: list[dict] = []

    for i in range(len(rows)):
        if not keep_indices:
            keep_indices.append(i)
            continue

        sims = mat[keep_indices] @ mat[i]
        best_local = int(np.argmax(sims))
        best_sim = float(sims[best_local])

        if best_sim >= thresh:
            rep_idx = keep_indices[best_local]
            duplicate_records.append({
                "title": rows[i].title,
                "url": rows[i].url,
                "site": rows[i].site,
                "duplicate_of_title": rows[rep_idx].title,
                "duplicate_of_site": rows[rep_idx].site,
                "duplicate_of_url": rows[rep_idx].url,
                "similarity": round(best_sim, 4),
            })
        else:
            keep_indices.append(i)

    unique = [rows[i] for i in keep_indices]
    return unique, duplicate_records


# -----------------------------
# 1. 네이버 IT/과학 컴퓨터
# -----------------------------

def crawl_naver_it_computer(page, today: date | None = None):
    today = today or today_kst()
    articles = []
    today_str = today.strftime("%Y%m%d")

    url = f"https://news.naver.com/breakingnews/section/105/283?date={today_str}"
    goto(page, url)

    # 첫 ul만 보면 6건 — 섹션 내 모든 ul/li 대상
    items = page.locator('xpath=//*[@id="newsct"]/div[2]//ul/li')

    scroll_until_items_stable(page, items)

    # 과거 날짜: 기사 더보기로 추가 로드 (당일은 보통 없음)
    if today < today_kst():
        _click_naver_more_articles(page, items)
        scroll_until_items_stable(page, items)

    seen_urls: set[str] = set()
    for i in range(items.count()):
        item = items.nth(i)

        title = safe_text(item.locator("xpath=.//div/div/div[2]/a/strong"))
        if not title:
            title = safe_text(item.locator("xpath=.//a/strong"))
        press = safe_text(item.locator("xpath=.//div/div/div[2]/div[2]/div[1]/div[1]"))
        href = safe_attr(item.locator("xpath=.//div/div/div[2]/a"), "href")
        if not href:
            href = safe_attr(item.locator("xpath=.//a[.//strong]"), "href")

        if not title or not href:
            continue

        article_url = absolute_url(url, href)
        if article_url in seen_urls:
            continue
        seen_urls.add(article_url)

        articles.append(Article(
            site="네이버뉴스",
            category="IT/과학 컴퓨터",
            title=title,
            press=press,
            url=article_url,
            published_at=today.strftime("%Y-%m-%d"),
        ))

    return articles


# -----------------------------
# 2. 구글 뉴스 검색
# -----------------------------

def build_google_news_url(query: str, recent_1day=False):
    params = {
        "q": query,
        "tbm": "nws",
        "hl": "ko",
        "gl": "KR",
    }

    if recent_1day:
        params["tbs"] = "qdr:d"

    return "https://www.google.com/search?" + urlencode(params)


def crawl_google_news(page, query: str, recent_1day=False, max_pages=5):
    articles = []
    url = build_google_news_url(query, recent_1day=recent_1day)
    print(url)
    goto(page, url)

    for page_no in range(max_pages):
        scroll_page_to_end(page)

        result_links = page.locator('xpath=//*[@id="rso"]/div/div/div/div/div/a')
        if result_links.count() == 0:
            result_links = page.locator("#rso a.WlydOe[href]")

        for i in range(result_links.count()):
            a = result_links.nth(i)

            href = safe_attr(a, "href")
            title = safe_text(a.locator("xpath=.//div/div[2]/div[2]"))
            press = safe_text(a.locator("xpath=.//div/div[2]/div[1]/div/span"))
            body = safe_text(a.locator("xpath=.//div/div[2]/div[3]"))

            if not title:
                title = safe_text(a.locator(".n0jPhd"))
            if not press:
                press = safe_text(a.locator(".MgUUmf span"))
            if not body:
                body = safe_text(a.locator(".GI74Re, .UqSP2b"))

            if not title or not href:
                continue

            articles.append(Article(
                site="Google News",
                category="keyword_search",
                title=title,
                press=press,
                body=body,
                url=href,
                published_at="최근 1일" if recent_1day else "전체기간",
            ))

        next_btn = page.locator('xpath=//*[@id="pnnext"]')
        if next_btn.count() == 0:
            next_btn = page.locator("#pnnext")

        if next_btn.count() == 0:
            break

        href = safe_attr(next_btn, "href")
        if not href:
            break

        next_url = absolute_url("https://www.google.com", href)
        goto(page, next_url)

    return articles


def _medicaltimes_page_url(page_no: int) -> str:
    if page_no <= 0:
        return "https://www.medicaltimes.com/Main/News/List.html?MainCate=4"
    return (
        "https://www.medicaltimes.com/Main/News/List.html"
        f"?page={page_no}&MainCate=4&SubCate=&News_Level=&SectionTop=&ReporterID=&TargetDate=&keyword="
    )


# -----------------------------
# 3. 메디컬타임스
# -----------------------------

def crawl_medicaltimes(page, today: date | None = None, max_pages=20):
    today = today or today_kst()
    articles = []
    base = "https://www.medicaltimes.com"

    for page_no in range(max_pages):
        url = _medicaltimes_page_url(page_no)
        goto(page, url)
        scroll_page_to_end(page)

        items = page.locator('xpath=//*[@id="container"]/div/section[1]/div[3]/article')

        if items.count() == 0:
            break

        stop = False

        for i in range(items.count()):
            item = items.nth(i)

            title = safe_text(item.locator("xpath=.//a/div[2]/h4"))
            body = safe_text(item.locator("xpath=.//a/div[2]/div[2]"))
            href = safe_attr(item.locator("xpath=.//a"), "href")
            published_at = safe_text(item.locator("xpath=.//a/span"))

            if not title:
                continue

            if is_today(published_at, today):
                articles.append(Article(
                    site="메디컬타임스",
                    category="MainCate=4",
                    title=title,
                    body=body,
                    url=absolute_url(base, href),
                    published_at=published_at,
                ))
            elif is_older_than_today(published_at, today):
                stop = True

        if stop:
            break

    return articles


def _dailymedi_page_url(page_no: int) -> str:
    if page_no <= 1:
        return "https://www.dailymedi.com/news/news_list.php?ca_id=2206"
    return f"https://www.dailymedi.com/news/news_list.php?ca_id=2206&&page={page_no}"


# -----------------------------
# 4. 데일리메디
# -----------------------------

def crawl_dailymedi(page, today: date | None = None, max_pages=20):
    today = today or today_kst()
    articles = []
    base = "https://www.dailymedi.com"

    for page_no in range(1, max_pages + 1):
        url = _dailymedi_page_url(page_no)
        goto(page, url)
        scroll_page_to_end(page)

        items = page.locator('xpath=//*[@id="list_sub1"]/div/div/div[1]/div[2]/ul/li')

        if items.count() == 0:
            break

        stop = False

        for i in range(items.count()):
            item = items.nth(i)

            title = safe_text(item.locator("xpath=.//a/div[2]/div[1]"))
            subtitle = safe_text(item.locator("xpath=.//a/div[2]/div[2]"))
            body = safe_text(item.locator("xpath=.//a/div[2]/div[3]"))
            published_at = safe_text(item.locator("xpath=.//a/div[2]/div[2]/span"))
            href = safe_attr(item.locator("xpath=.//a"), "href")

            if not title:
                continue

            if is_today(published_at, today):
                articles.append(Article(
                    site="데일리메디",
                    category="ca_id=2206",
                    title=title,
                    subtitle=subtitle,
                    body=body,
                    url=absolute_url(base, href),
                    published_at=published_at,
                ))
            elif is_older_than_today(published_at, today):
                stop = True

        if stop:
            break

    return articles


def _docdocdoc_page_url(page_no: int) -> str:
    base = "https://www.docdocdoc.co.kr/news/articleList.html"
    if page_no <= 1:
        return f"{base}?view_type=sm"
    return f"{base}?page={page_no}&total=179001&box_idxno=&view_type=sm"


# -----------------------------
# 5. 청년의사
# -----------------------------

def crawl_docdocdoc(page, today: date | None = None, max_pages=20):
    today = today or today_kst()
    articles = []
    base = "https://www.docdocdoc.co.kr"

    for page_no in range(1, max_pages + 1):
        url = _docdocdoc_page_url(page_no)
        goto(page, url)
        scroll_page_to_end(page)

        items = page.locator('xpath=//*[@id="section-list"]/ul/li')

        if items.count() == 0:
            break

        stop = False

        for i in range(items.count()):
            item = items.nth(i)

            title = safe_text(item.locator("xpath=.//div/h4/a"))
            body = safe_text(item.locator("xpath=.//div/p/a"))
            href = safe_attr(item.locator("xpath=.//div/h4/a"), "href")
            published_at = safe_text(item.locator("xpath=.//div/span/em[3]"))

            if not title:
                continue

            if is_today(published_at, today):
                articles.append(Article(
                    site="청년의사",
                    category="전체",
                    title=title,
                    body=body,
                    url=absolute_url(base, href),
                    published_at=published_at,
                ))
            elif is_older_than_today(published_at, today):
                stop = True

        if stop:
            break

    return articles


# -----------------------------
# 6. 메디게이트뉴스
# -----------------------------

def crawl_medigate_section(page, section_url: str, category: str, today: date | None = None, max_pages=20):
    today = today or today_kst()
    articles = []
    base = "https://www.medigatenews.com"

    for page_no in range(1, max_pages + 1):
        if page_no <= 1:
            url = section_url
        else:
            url = f"{section_url}?canada=&page_no={page_no}&page_size=10"
        goto(page, url)
        scroll_page_to_end(page)

        items = page.locator("xpath=/html/body/div[3]/div[1]/div/div")

        if items.count() == 0:
            break

        stop = False

        for i in range(items.count()):
            item = items.nth(i)

            title = safe_text(item.locator("xpath=.//a/div[2]/p[1]"))
            subtitle = safe_text(item.locator("xpath=.//a/div[2]/p[2]/span[1]"))
            body = safe_text(item.locator("xpath=.//a/div[2]/p[2]/span[2]"))
            href = safe_attr(item.locator("xpath=.//a"), "href")
            published_at = safe_text(item.locator("xpath=.//p/span[1]"))

            if not title:
                continue

            if is_today(published_at, today):
                articles.append(Article(
                    site="메디게이트뉴스",
                    category=category,
                    title=title,
                    subtitle=subtitle,
                    body=body,
                    url=absolute_url(base, href),
                    published_at=published_at,
                ))
            elif is_older_than_today(published_at, today):
                stop = True

        if stop:
            break

    return articles


def crawl_medigate(page, today: date | None = None):
    today = today or today_kst()
    urls = [
        ("https://www.medigatenews.com/section/medical_equipment/list", "medical_equipment"),
        ("https://www.medigatenews.com/section/medical_it/list", "medical_it"),
    ]

    result = []
    for url, category in urls:
        result.extend(crawl_medigate_section(page, url, category, today=today))

    return result


# -----------------------------
# 7, 8, 9 공통: 인터넷신문솔루션 계열 더보기 구조
# 의학신문 / AI타임스 / 로봇신문
# -----------------------------

def crawl_more_button_section_list(
    page,
    site_name: str,
    category: str,
    start_url: str,
    title_xpath: str,
    body_xpath: str,
    url_xpath: str,
    date_xpath: str,
    more_button_xpath: str,
    today: date | None = None,
    max_clicks=50,
):
    today = today or today_kst()
    articles = []
    base = start_url
    goto(page, start_url)

    processed_urls = set()
    stop = False
    more_xpath = more_button_xpath

    for _ in range(max_clicks + 1):
        scroll_page_to_end(page)

        items = page.locator('xpath=//*[@id="section-list"]/ul/li')
        if items.count() == 0:
            break

        for i in range(items.count()):
            item = items.nth(i)

            title = safe_text(item.locator(f"xpath=.//{title_xpath}"))
            body = safe_text(item.locator(f"xpath=.//{body_xpath}"))
            href = safe_attr(item.locator(f"xpath=.//{url_xpath}"), "href")
            published_at = safe_text(item.locator(f"xpath=.//{date_xpath}"))

            article_url = absolute_url(base, href)

            if not title or not article_url or article_url in processed_urls:
                continue

            processed_urls.add(article_url)

            if is_today(published_at, today):
                articles.append(Article(
                    site=site_name,
                    category=category,
                    title=title,
                    body=body,
                    url=article_url,
                    published_at=published_at,
                ))
            elif is_older_than_today(published_at, today):
                stop = True

        if stop:
            break

        btn = page.locator(f"xpath={more_xpath}")
        if btn.count() == 0:
            break

        try:
            target = btn.first
            if not target.is_visible(timeout=1500):
                break
            target.scroll_into_view_if_needed(timeout=5000)
            sleep(0.2)
            target.click(timeout=5000)
            sleep(1.0)
        except Exception:
            break

    return articles


def crawl_bosa(page, today: date | None = None):
    return crawl_more_button_section_list(
        page=page,
        site_name="의학신문",
        category="S1N4",
        start_url="https://www.bosa.co.kr/news/articleList.html?sc_section_code=S1N4&view_type=sm",
        title_xpath="div/h2/a",
        body_xpath="div/p",
        url_xpath="div/h2/a",
        date_xpath="div/div/div[2]",
        more_button_xpath='//*[@id="section-list"]/button',
        today=today,
    )


def crawl_aitimes(page, today: date | None = None):
    return crawl_more_button_section_list(
        page=page,
        site_name="AI타임스",
        category="전체",
        start_url="https://www.aitimes.com/news/articleList.html?view_type=sm",
        title_xpath="div/h2/a",
        body_xpath="div/p",
        url_xpath="div/h2/a",
        date_xpath="div/div/div[3]",
        more_button_xpath='//*[@id="section-list"]/button',
        today=today,
    )


def crawl_irobotnews(page, today: date | None = None):
    return crawl_more_button_section_list(
        page=page,
        site_name="로봇신문",
        category="S1N1",
        start_url="https://www.irobotnews.com/news/articleList.html?sc_section_code=S1N1&view_type=sm",
        title_xpath="div/h2/a",
        body_xpath="div/p",
        url_xpath="div/h2/a",
        date_xpath="div/div/div[3]",
        more_button_xpath='//*[@id="section-list"]/button',
        today=today,
    )


# -----------------------------
# 저장
# -----------------------------

def site_file_slug(site_key: str) -> str:
    """파일명용 slug (한글·영문 유지, 특수문자 → _)."""
    slug = re.sub(r"[^\w가-힣]+", "_", (site_key or "").strip()).strip("_")
    return slug or "unknown"


def daily_output_dir(target_date: date | None = None) -> Path:
    day = target_date or today_kst()
    return DAILY_NEWS_CRAWLING_DIR / day.isoformat()


def save_site_daily(
    site_key: str,
    articles: list[Article],
    *,
    target_date: date | None = None,
) -> Path:
    """사이트별 당일 크롤 결과 → Data/daily_news_crawling/{YYYY-MM-DD}/{site}.json"""
    day = target_date or today_kst()
    out_dir = daily_output_dir(day)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = dedupe_articles(articles)
    payload = {
        "date": day.isoformat(),
        "site": site_key,
        "crawled_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(rows),
        "articles": [asdict(a) for a in rows],
    }
    out_path = out_dir / f"{site_file_slug(site_key)}.json"
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out_path


def save_daily_manifest(
    entries: list[dict],
    *,
    target_date: date | None = None,
    dedup: dict | None = None,
) -> Path:
    """당일 크롤 manifest → Data/daily_news_crawling/{YYYY-MM-DD}/manifest.json"""
    day = target_date or today_kst()
    out_dir = daily_output_dir(day)
    out_dir.mkdir(parents=True, exist_ok=True)

    total = sum(int(e.get("count") or 0) for e in entries)
    payload: dict = {
        "date": day.isoformat(),
        "crawled_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
        "total_articles": total,
        "sites": entries,
    }
    if dedup:
        payload["dedup"] = dedup
    out_path = out_dir / "manifest.json"
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out_path


def save_deduped_daily(
    articles: list[Article],
    duplicate_records: list[dict],
    *,
    raw_count: int,
    threshold: float,
    target_date: date | None = None,
) -> Path:
    """임베딩 dedup 결과 → deduped.json"""
    day = target_date or today_kst()
    out_dir = daily_output_dir(day)
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "date": day.isoformat(),
        "deduped_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
        "dedupe_method": "url_title_exact + embedding_cosine",
        "embedding_model": EMBEDDING_MODEL_NAME,
        "threshold": threshold,
        "raw_count": raw_count,
        "unique_count": len(articles),
        "removed_count": len(duplicate_records),
        "articles": [asdict(a) for a in articles],
        "duplicates": duplicate_records,
    }
    out_path = out_dir / "deduped.json"
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out_path


# -----------------------------
# 메인 실행
# -----------------------------

def run(
    google_queries=None,
    google_recent_1day=True,
    headless=True,
    target_date: date | None = None,
    *,
    dedup_threshold: float | None = None,
    skip_embed_dedup: bool = False,
):
    if google_queries is None:
        google_queries = [
            "의료 AI", "병원 AI", "의료 로봇", "병원 로봇", "정형외과 AI", "정형외과 로봇",
        ]
    day = target_date or today_kst()
    all_articles: list[Article] = []
    manifest_entries: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )

        context = browser.new_context(
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1365, "height": 900},
        )

        page = context.new_page()

        crawlers = [
            ("네이버뉴스", lambda: crawl_naver_it_computer(page, today=day)),
            ("메디컬타임스", lambda: crawl_medicaltimes(page, today=day)),
            ("데일리메디", lambda: crawl_dailymedi(page, today=day)),
            ("청년의사", lambda: crawl_docdocdoc(page, today=day)),
            ("메디게이트뉴스", lambda: crawl_medigate(page, today=day)),
            ("의학신문", lambda: crawl_bosa(page, today=day)),
            ("AI타임스", lambda: crawl_aitimes(page, today=day)),
            ("로봇신문", lambda: crawl_irobotnews(page, today=day)),
        ]

        for name, func in crawlers:
            print(f"[START] {name}")
            try:
                data = func()
                out_path = save_site_daily(name, data, target_date=day)
                manifest_entries.append({
                    "site": name,
                    "file": out_path.name,
                    "count": len(dedupe_articles(data)),
                })
                all_articles.extend(data)
                print(f"[DONE] {name}: {len(data)}건 → {out_path}")
            except Exception as e:
                print(f"[ERROR] {name}: {e}")
                manifest_entries.append({
                    "site": name,
                    "file": None,
                    "count": 0,
                    "error": str(e),
                })

        for query in google_queries:
            site_key = f"Google News - {query}"
            print(f"[START] {site_key}")
            try:
                data = crawl_google_news(
                    page,
                    query=query,
                    recent_1day=google_recent_1day,
                    max_pages=5,
                )
                out_path = save_site_daily(site_key, data, target_date=day)
                manifest_entries.append({
                    "site": site_key,
                    "file": out_path.name,
                    "count": len(dedupe_articles(data)),
                })
                all_articles.extend(data)
                print(f"[DONE] {site_key}: {len(data)}건 → {out_path}")
            except Exception as e:
                print(f"[ERROR] {site_key}: {e}")
                manifest_entries.append({
                    "site": site_key,
                    "file": None,
                    "count": 0,
                    "error": str(e),
                })

        context.close()
        browser.close()

    raw_articles = dedupe_articles(all_articles)
    raw_count = len(raw_articles)
    thresh = dedup_threshold if dedup_threshold is not None else NEWS_DEDUP_EMBED_THRESHOLD

    if skip_embed_dedup:
        unique_articles = raw_articles
        duplicate_records = []
        dedup_sec = 0.0
        deduped_path = None
        dedup_meta = None
    else:
        print(f"[DEDUP] 임베딩 중복 제거 시작 (raw={raw_count}, threshold={thresh})")
        dedup_t0 = time.perf_counter()
        unique_articles, duplicate_records = dedupe_articles_by_embedding(
            raw_articles,
            threshold=thresh,
        )
        dedup_sec = time.perf_counter() - dedup_t0
        deduped_path = save_deduped_daily(
            unique_articles,
            duplicate_records,
            raw_count=raw_count,
            threshold=thresh,
            target_date=day,
        )
        dedup_meta = {
            "method": "url_title_exact + embedding_cosine",
            "embedding_model": EMBEDDING_MODEL_NAME,
            "threshold": thresh,
            "raw_count": raw_count,
            "unique_count": len(unique_articles),
            "removed_count": len(duplicate_records),
            "deduped_file": deduped_path.name,
            "duration_sec": round(dedup_sec, 2),
        }

    manifest_path = save_daily_manifest(
        manifest_entries,
        target_date=day,
        dedup=dedup_meta,
    )

    if skip_embed_dedup:
        print(
            f"\n[SUMMARY] {day.isoformat()} 총 {raw_count}건 "
            f"({len(manifest_entries)}개 소스, embed dedup 생략)"
        )
    else:
        print(
            f"\n[SUMMARY] {day.isoformat()} raw {raw_count}건 → "
            f"unique {len(unique_articles)}건 (제거 {len(duplicate_records)}건, {dedup_sec:.1f}s)"
        )
        print(f"[DEDUPED] {deduped_path}")
    print(f"[MANIFEST] {manifest_path}")
    return unique_articles, manifest_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="의료/AI 뉴스 사이트별 당일 크롤 → Data/daily_news_crawling",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="브라우저 창을 보면서 실행",
    )
    parser.add_argument(
        "--google-query",
        nargs="*",
        default=["의료 AI", "병원 AI", "의료 로봇", "병원 로봇", "정형외과 AI", "정형외과 로봇"],
        help="구글 뉴스 검색어 목록",
    )
    parser.add_argument(
        "--google-all-period",
        action="store_true",
        help="구글 뉴스 기간 전체 검색. 기본은 최근 1일",
    )
    parser.add_argument(
        "--date",
        default="",
        help="크롤 기준일 (YYYY-MM-DD). 기본은 오늘(KST)",
    )
    parser.add_argument(
        "--dedup-threshold",
        type=float,
        default=None,
        help=f"임베딩 cosine 중복 임계값 (기본 {NEWS_DEDUP_EMBED_THRESHOLD})",
    )
    parser.add_argument(
        "--skip-embed-dedup",
        action="store_true",
        help="임베딩 중복 제거 생략 (사이트별 JSON만 저장)",
    )

    args = parser.parse_args()

    target = today_kst()
    if args.date.strip():
        target = date.fromisoformat(args.date.strip())

    # run() 내부에서 threshold는 NEWS_DEDUP_EMBED_THRESHOLD 사용;
    # CLI override는 run 시그니처 확장 없이 모듈 상수 임시 패치 대신 run 인자 추가
    run_kwargs = dict(
        google_queries=args.google_query,
        google_recent_1day=not args.google_all_period,
        headless=not args.headful,
        target_date=target,
        skip_embed_dedup=args.skip_embed_dedup,
    )
    if args.dedup_threshold is not None:
        run_kwargs["dedup_threshold"] = args.dedup_threshold

    run(**run_kwargs)