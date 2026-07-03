# -*- coding: utf-8 -*-
"""
정형외과 AI SaMD / 수술로봇 뉴스 요약 봇

프로세스 (daily_news_crawling/deduped.json 입력):
1) deduped.json 로드
2) 소스 tier + 키워드 1차 필터링
3) 임베딩 topic 유사도 랭킹 부스트
4) 제목 기준 LLM 2차 필터링
5) 제목+본문 100자 기준 LLM 최종 랭킹/한줄 요약 → top N
6) filtered_primary.json / top10.json / top10.md 저장
7) (일일) 전일 크롤 → 요약 → Slack Top 10 전송

주의:
- 각 사이트 robots.txt, 이용약관, 저작권 정책을 확인하고 과도한 요청을 피하세요.
- 본문은 요약 판단용 100자만 저장하도록 설계했습니다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urldefrag
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

_CONN_BOT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _CONN_BOT_DIR.parent
for _path in (_CONN_BOT_DIR, _REPO_ROOT):
    _s = str(_path)
    if _s not in sys.path:
        sys.path.insert(0, _s)

from app.core.config import DAILY_NEWS_CRAWLING_DIR, EMBEDDING_MODEL_NAME
from app.services.news.news_config import (
    EMBED_TOPIC_ANCHORS,
    KEYWORD_SETS,
    MONITORING_FOCUS,
    NEWS_SOURCES,
    SITE_TIER_A,
    SITE_TIER_B,
    TARGET_COMPANY,
    TIER_SOURCE_BONUS,
)


logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

# -----------------------------
# 기본 설정
# -----------------------------
USER_AGENT = (
    "Mozilla/5.0 (compatible; OrthoAINewsBot/1.0; "
    "+https://github.com/connrobot/ConnBot; contact=admin@connrobot.com)"
)
TIMEOUT = 12
REQUEST_SLEEP_SEC = 0.5
NEWS_TOP_N = 10
OUTPUT_DIR = DAILY_NEWS_CRAWLING_DIR / "outputs"

# crawl_type별 추가 시도 경로 (list_url 없을 때 테스트용)
_CRAWL_TYPE_EXTRA_PATHS: dict[str, tuple[str, ...]] = {
    "news_list": ("/news", "/News", "/News/List"),
    "news_search": ("/news", "/search"),
    "news_and_events": ("/news", "/events"),
    "press_release": ("/news", "/press", "/press-release", "/media"),
    "press_release_and_news_search": ("/news", "/press"),
    "announcement": ("/board", "/notice", "/bbs"),
    "announcement_and_policy": ("/board", "/notice", "/policy"),
    "policy_announcement": ("/policy", "/notice"),
    "rss_or_page_monitor": ("/news", "/rss"),
    "own_company_monitor": ("/news", "/blog"),
}


@dataclass
class ArticleCandidate:
    source_key: str
    source_name: str
    source_priority: str
    title: str
    url: str
    body_100: str = ""
    primary_score: int = 0
    llm_title_score: int = 0
    llm_title_reason: str = ""
    final_score: int = 0
    category: str = ""
    summary: str = ""


# -----------------------------
# 유틸
# -----------------------------
def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def text_key(text: str) -> str:
    return normalize_space(text).casefold()


def normalize_url(url: str) -> str:
    url, _frag = urldefrag(url)
    return url.strip()


def stable_id(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]


def contains_any(text: str, keywords: Iterable[str]) -> bool:
    t = text_key(text)
    return any(text_key(k) in t for k in keywords if k)


def count_matches(text: str, keywords: Iterable[str]) -> int:
    t = text_key(text)
    return sum(1 for k in keywords if k and text_key(k) in t)


def priority_weight(priority: str) -> int:
    return {"S": 6, "A": 4, "B": 2, "C": 1}.get(priority, 0)


def is_probably_article_url(url: str) -> bool:
    """
    너무 엄격하면 놓치므로, 기본적으로 허용하되 명백한 비기사 링크만 제외.
    """
    lowered = url.casefold()
    bad_fragments = [
        "/login", "/member", "/signup", "/privacy", "/terms", "/contact",
        "javascript:", "mailto:", ".pdf", ".jpg", ".jpeg", ".png", ".gif",
        ".zip", ".hwp", ".doc", ".xls", ".ppt",
    ]
    return not any(x in lowered for x in bad_fragments)


def is_good_title(title: str) -> bool:
    title = normalize_space(title)
    if len(title) < 8:
        return False
    bad_titles = {
        "home", "login", "로그인", "회원가입", "구독", "메뉴", "검색",
        "privacy policy", "contact", "뉴스", "전체기사",
    }
    return text_key(title) not in bad_titles


def iter_news_sources() -> Iterator[Tuple[str, Dict[str, Any]]]:
    """NEWS_SOURCES 2단 중첩 구조를 (source_key, source)로 펼친다."""
    for category_key, category in NEWS_SOURCES.items():
        if category_key == "meta":
            continue
        if not isinstance(category, dict):
            continue
        for source_key, source in category.items():
            if not isinstance(source, dict):
                continue
            if source.get("url") and source.get("name"):
                yield f"{category_key}/{source_key}", source


def _normalize_host(netloc: str) -> str:
    host = (netloc or "").casefold()
    if host.startswith("www."):
        return host[4:]
    return host


def is_same_site(site_root_url: str, href: str) -> bool:
    """동일 registrable domain 또는 서브도메인만 허용."""
    base_host = _normalize_host(urlparse(site_root_url).netloc)
    href_host = _normalize_host(urlparse(href).netloc)
    if not base_host or not href_host:
        return True
    return href_host == base_host or href_host.endswith("." + base_host)


def resolve_crawl_urls(source: Dict[str, Any]) -> List[str]:
    """
    크롤 시작 URL 목록.
    list_url > url > crawl_type별 추가 경로 순으로 중복 없이 반환.
    """
    seen: set[str] = set()
    urls: List[str] = []

    def add(url: str) -> None:
        normalized = normalize_url(url)
        if normalized and normalized not in seen:
            seen.add(normalized)
            urls.append(normalized)

    list_url = source.get("list_url")
    if list_url:
        add(str(list_url))

    base = str(source.get("url") or "").rstrip("/")
    if base:
        add(base)

    crawl_type = str(source.get("crawl_type") or "")
    for suffix in _CRAWL_TYPE_EXTRA_PATHS.get(crawl_type, ()):
        if base:
            add(base + suffix)

    return urls


# -----------------------------
# 크롤링
# -----------------------------
def fetch_html(url: str) -> Optional[str]:
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ko,en;q=0.8"},
            timeout=TIMEOUT,
        )
        if resp.status_code >= 400:
            return None
        resp.encoding = resp.apparent_encoding or resp.encoding
        return resp.text
    except requests.RequestException:
        return None


def _collect_links_from_html(
    source_key: str,
    source: Dict[str, Any],
    site_root_url: str,
    page_url: str,
    html: str,
    *,
    max_links: int,
    seen: set[tuple[str, str]],
    candidates: List[ArticleCandidate],
) -> None:
    soup = BeautifulSoup(html, "lxml")

    for a in soup.find_all("a", href=True):
        if len(candidates) >= max_links:
            return

        title = normalize_space(a.get_text(" ", strip=True))
        href = normalize_url(urljoin(page_url, a["href"]))

        if not title or not is_good_title(title):
            continue
        if not is_probably_article_url(href):
            continue
        if not is_same_site(site_root_url, href):
            continue

        key = (text_key(title), href)
        if key in seen:
            continue
        seen.add(key)

        candidates.append(
            ArticleCandidate(
                source_key=source_key,
                source_name=source["name"],
                source_priority=source.get("priority", "C"),
                title=title,
                url=href,
            )
        )


def extract_links_from_page(source_key: str, source: Dict[str, Any], max_links: int) -> List[ArticleCandidate]:
    site_root_url = str(source["url"])
    candidates: List[ArticleCandidate] = []
    seen: set[tuple[str, str]] = set()

    for page_url in resolve_crawl_urls(source):
        if len(candidates) >= max_links:
            break
        html = fetch_html(page_url)
        if not html:
            continue
        _collect_links_from_html(
            source_key,
            source,
            site_root_url,
            page_url,
            html,
            max_links=max_links,
            seen=seen,
            candidates=candidates,
        )

    return candidates


def extract_article_body_100(article: ArticleCandidate) -> ArticleCandidate:
    html = fetch_html(article.url)
    time.sleep(REQUEST_SLEEP_SEC)

    if not html:
        return article

    soup = BeautifulSoup(html, "lxml")

    # title 보강: h1 > og:title > 기존 title
    h1 = soup.find("h1")
    og_title = soup.find("meta", property="og:title")
    if h1 and is_good_title(h1.get_text(" ", strip=True)):
        article.title = normalize_space(h1.get_text(" ", strip=True))
    elif og_title and og_title.get("content") and is_good_title(og_title["content"]):
        article.title = normalize_space(og_title["content"])

    # 본문 후보: meta description + p 태그
    desc = ""
    meta_desc = soup.find("meta", attrs={"name": "description"})
    og_desc = soup.find("meta", property="og:description")
    if meta_desc and meta_desc.get("content"):
        desc = meta_desc["content"]
    elif og_desc and og_desc.get("content"):
        desc = og_desc["content"]

    paragraphs = []
    for p in soup.find_all(["p", "div"]):
        txt = normalize_space(p.get_text(" ", strip=True))
        if len(txt) >= 30:
            paragraphs.append(txt)

    body = normalize_space(" ".join([desc] + paragraphs))
    article.body_100 = body[:100]
    return article


def crawl_all_sources(max_per_source: int, enrich_body_limit: int) -> List[ArticleCandidate]:
    all_candidates: List[ArticleCandidate] = []

    for source_key, source in iter_news_sources():
        print(f"[crawl] {source['name']} ...")
        items = extract_links_from_page(source_key, source, max_links=max_per_source)
        print(f"        found {len(items)} links")
        all_candidates.extend(items)
        time.sleep(REQUEST_SLEEP_SEC)

    # 제목/URL 중복 제거
    deduped: List[ArticleCandidate] = []
    seen_titles = set()
    seen_urls = set()
    for item in all_candidates:
        title_id = text_key(item.title)
        url_id = normalize_url(item.url)
        if title_id in seen_titles or url_id in seen_urls:
            continue
        seen_titles.add(title_id)
        seen_urls.add(url_id)
        deduped.append(item)

    # 요청사항: 제목/링크/본문 100자를 크롤링
    # 전체 후보가 너무 많으면 사이트 과부하 방지를 위해 상한 적용
    enriched = []
    for item in deduped[:enrich_body_limit]:
        enriched.append(extract_article_body_100(item))

    return enriched


# -----------------------------
# deduped.json 입력
# -----------------------------
def site_tier(site_name: str) -> str:
    if site_name in SITE_TIER_A:
        return "A"
    if site_name in SITE_TIER_B:
        return "B"
    return "C"


def site_to_priority(site_name: str) -> str:
    return {"A": "A", "B": "A", "C": "C"}.get(site_tier(site_name), "C")


def filter_text(item: ArticleCandidate) -> str:
    body = (item.body_100 or "").strip()
    return normalize_space(f"{item.title} {body}")


def load_deduped_articles(
    target_date: str | None = None,
    *,
    input_path: Path | None = None,
) -> List[dict]:
    if input_path is not None:
        path = input_path
    else:
        day = target_date or dt.datetime.now().strftime("%Y-%m-%d")
        path = DAILY_NEWS_CRAWLING_DIR / day / "deduped.json"
    if not path.exists():
        raise FileNotFoundError(f"deduped.json not found: {path}")
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    return data.get("articles", [])


def _parse_run_date(run_date: str | date) -> date:
    if isinstance(run_date, date):
        return run_date
    return date.fromisoformat(str(run_date).strip()[:10])


def format_previous_top10_for_prompt(articles: List[dict]) -> str:
    if not articles:
        return "(없음)"
    lines: list[str] = []
    for i, article in enumerate(articles, start=1):
        title = normalize_space(str(article.get("title") or ""))
        url = normalize_space(str(article.get("url") or ""))
        summary = normalize_space(str(article.get("summary") or ""))[:80]
        category = normalize_space(str(article.get("category") or ""))
        block = f"{i}. title={title} | url={url}"
        if category:
            block += f" | category={category}"
        if summary:
            block += f" | summary={summary}"
        lines.append(block)
    return "\n".join(lines)


def load_previous_top10_for_prompt(run_date: str | date) -> str:
    """전날 top10.json을 LLM 중복 제외용 프롬프트 텍스트로 변환."""
    day = _parse_run_date(run_date)
    prev_day = (day - timedelta(days=1)).isoformat()
    path = DAILY_NEWS_CRAWLING_DIR / prev_day / "top10.json"
    if not path.exists():
        logger.info("[news] previous top10 not found: %s", path)
        return "(없음)"
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    articles = [dict(title=article.get("title")) or []]
    return format_previous_top10_for_prompt(articles)


def articles_to_candidates(articles: List[dict]) -> List[ArticleCandidate]:
    candidates: List[ArticleCandidate] = []
    for article in articles:
        site = str(article.get("site") or "")
        body = (article.get("body") or article.get("summary") or "").strip()
        candidates.append(
            ArticleCandidate(
                source_key=site,
                source_name=site,
                source_priority=site_to_priority(site),
                title=str(article.get("title") or ""),
                url=str(article.get("url") or ""),
                body_100=body[:100],
                category=str(article.get("category") or ""),
            )
        )
    return candidates


# -----------------------------
# 1차 제목 키워드 필터
# -----------------------------
def primary_title_filter(candidates: List[ArticleCandidate]) -> List[ArticleCandidate]:
    primary_keywords = KEYWORD_SETS["primary_filter_kr"] + KEYWORD_SETS["primary_filter_en"]
    medical_gate = KEYWORD_SETS["medical_gate_kr"] + KEYWORD_SETS["medical_gate_en"]
    boost_keywords = (
        KEYWORD_SETS["boost_keywords_kr"]
        + KEYWORD_SETS["boost_keywords_en"]
        + KEYWORD_SETS["company_keywords"]
        + KEYWORD_SETS["opportunity_keywords_kr"]
        + KEYWORD_SETS["opportunity_keywords_en"]
    )
    negative_keywords = KEYWORD_SETS["negative_keywords_soft"]

    filtered: List[ArticleCandidate] = []

    for item in candidates:
        text = filter_text(item)
        tier = site_tier(item.source_name)

        if not contains_any(text, primary_keywords):
            continue
        if tier == "C" and not contains_any(text, medical_gate):
            continue

        neg_hits = count_matches(text, negative_keywords)
        if neg_hits >= 2:
            continue

        score = 10
        score += TIER_SOURCE_BONUS.get(tier, 0)
        score += priority_weight(item.source_priority)
        score += count_matches(text, boost_keywords) * 4
        score += count_matches(text, primary_keywords)
        score -= neg_hits * 3

        item.primary_score = score
        filtered.append(item)

    filtered.sort(key=lambda x: x.primary_score, reverse=True)
    return filtered


_embedder = None


def _get_embedder():
    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer

        _embedder = SentenceTransformer(EMBEDDING_MODEL_NAME)
        _embedder.max_seq_length = 256
    return _embedder


def apply_embedding_rank_boost(
    candidates: List[ArticleCandidate],
    *,
    skip: bool = False,
) -> List[ArticleCandidate]:
    """1차 통과 후보에 topic anchor 유사도 점수 부스트 (하드 컷 아님)."""
    if skip or not candidates:
        return candidates

    import numpy as np

    embedder = _get_embedder()
    anchor_vecs = np.asarray(
        embedder.encode(
            EMBED_TOPIC_ANCHORS,
            normalize_embeddings=True,
            show_progress_bar=False,
        ),
        dtype=np.float32,
    )
    texts = [filter_text(c) for c in candidates]
    vecs = np.asarray(
        embedder.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
        ),
        dtype=np.float32,
    )

    for i, item in enumerate(candidates):
        max_sim = float(np.max(vecs[i] @ anchor_vecs.T))
        boost = int(max(0.0, (max_sim - 0.45) * 25))
        item.primary_score += boost

    candidates.sort(key=lambda x: x.primary_score, reverse=True)
    return candidates


# -----------------------------
# LLM
# -----------------------------
def llm_company_context() -> str:
    """2차·최종 LLM 프롬프트에 공통으로 넣는 목표 기업/모니터링 맥락."""
    products = "\n".join(f"- {p}" for p in TARGET_COMPANY["core_products"])
    focus = ", ".join(MONITORING_FOCUS)
    return f"""목표 기업:
- {TARGET_COMPANY["location"]}, {TARGET_COMPANY["years_in_business"]} 기업
- {TARGET_COMPANY["domain"]}
- 제품:
{products}

모니터링 관심사:
- {focus}
"""


def get_openai_client():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None

    try:
        from openai import OpenAI

        kwargs = {"api_key": api_key}
        if os.getenv("OPENAI_BASE_URL"):
            kwargs["base_url"] = os.getenv("OPENAI_BASE_URL")
        return OpenAI(**kwargs)
    except Exception:
        return None


def llm_json(prompt: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    client = get_openai_client()
    if client is None:
        return None

    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

    try:
        # 범용 호환성을 위해 chat.completions + JSON object 모드 사용
        resp = client.chat.completions.create(
            model=model,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "너는 정형외과 AI SaMD, 의료기기 소프트웨어, 의료영상 AI, "
                        "수술로봇, 병원 도입, 인허가/수가 뉴스를 선별하는 리서치 어시스턴트다. "
                        "반드시 유효한 JSON만 출력한다."
                    ),
                },
                {
                    "role": "user",
                    "content": prompt + "\n\nINPUT_JSON:\n" + json.dumps(payload, ensure_ascii=False),
                },
            ],
        )
        content = resp.choices[0].message.content or "{}"
        return json.loads(content)
    except Exception as e:
        print(f"[llm] error: {e}")
        return None


def llm_title_filter(candidates: List[ArticleCandidate], batch_size: int = 50) -> List[ArticleCandidate]:
    """
    제목만 보고 2차 필터링.
    API Key가 없거나 실패하면 primary_score 기반 fallback.
    """
    if not candidates:
        return []

    selected: List[ArticleCandidate] = []

    prompt = f"""
아래 기사 후보들을 제목만 보고 2차 필터링해라.

{llm_company_context()}
판단 기준:
- 제목만 보고도 의료 AI, 디지털헬스, 의료기기 SW, 정형외과, 근골격계, 수술로봇, 인허가, 수가, 병원 도입, 정부사업, 경쟁사/투자와 관련 있으면 keep.
- 목표 기업 제품(KOA/ALI/METRIC/R/ASYST) 및 정형외과·무릎·하지정렬·수술로봇 연동과 직접 관련되면 score를 높여라.
- 내용이 중복된다고 생각하는 기사는 1개만 남기고 제외(특정 병원명이나 기업명이 반복되는 경우 중복 가능성 높음음).
- 너무 좁게 보지 말고, 의료기기/AI/로봇/수가/병원사업의 넓은 뉴스도 허용.
- 단순 건강상식, 신약/제약, 미용, 생활정보는 낮게 봐라.

출력 JSON:
{{
  "items": [
    {{"index": 0, "keep": true, "score": 0-100, "reason": "짧은 이유"}}
  ]
}}
"""

    for start in range(0, len(candidates), batch_size):
        batch = candidates[start : start + batch_size]
        payload = {
            "articles": [
                {
                    "index": i,
                    "source": a.source_name,
                    "priority": a.source_priority,
                    "title": a.title,
                    "url": a.url,
                    "primary_score": a.primary_score,
                }
                for i, a in enumerate(batch)
            ]
        }
        result = llm_json(prompt, payload)

        if not result or "items" not in result:
            # fallback: 상위 60% 정도만 통과
            cutoff = max(10, int(len(batch) * 0.6))
            for a in batch[:cutoff]:
                a.llm_title_score = min(90, a.primary_score + 30)
                a.llm_title_reason = "LLM 미사용 fallback: 1차 키워드 점수 기반 통과"
                selected.append(a)
            continue

        for item in result["items"]:
            try:
                idx = int(item["index"])
                keep = bool(item["keep"])
                if 0 <= idx < len(batch) and keep:
                    a = batch[idx]
                    a.llm_title_score = int(item.get("score", 50))
                    a.llm_title_reason = str(item.get("reason", ""))[:120]
                    selected.append(a)
            except Exception:
                continue

    selected.sort(key=lambda x: (x.llm_title_score, x.primary_score), reverse=True)
    return selected


def llm_final_summarize(
    candidates: List[ArticleCandidate],
    top_n: int = 10,
    *,
    previous_top10_text: str = "(없음)",
) -> List[ArticleCandidate]:
    """
    제목+링크+본문100자 기반 최종 랭킹 및 한줄 요약.
    API Key가 없으면 fallback 요약 생성.
    """
    candidates = candidates[:80]  # 비용 관리
    prompt = f"""
아래 후보 중 목표 기업에 가장 fit한 기사만 골라 최종 랭킹해라.

{llm_company_context()}
선정 기준:
- 정형외과 AI SaMD, 무릎 X-ray AI, 하지정렬, 수술계획 SW, 수술로봇 연동, 인허가, 수가, 병원 도입, 정부사업, 경쟁사/투자와의 연관성을 fit_score에 반영.
- 목표 제품(KOA/ALI·METRIC/R/ASYST) 및 모니터링 관심사와 직접 관련된 기사를 우선.
- 너무 좁게 보지 말되, 단순 건강상식·신약/제약·미용·생활정보는 제외.

출력 조건:
- 최대 {top_n}개
- title, url은 원문 그대로 유지
- summary는 한국어 한줄 요약. 80자 이내.
- category는 다음 중 하나:
  ["인허가/규제", "수가/보험", "병원도입/사업화", "정부사업/R&D", "정형외과/근골격계", "수술로봇/인공관절", "의료AI/영상분석", "경쟁사/투자"]
- fit_score는 0~100
- 기업명, 병원명 같이 동일한 상호명이 반복되는 기사는 1개만 남기고 제외.
- 전날 Slack Top 10과 중복되는 기사는 제외 (제목·URL·요약 기준).
    > 전날 Top 10:
    {previous_top10_text}
- 단순 건강상식, 신약/제약, 미용, 생활정보는 제외

출력 JSON:
{{
  "items": [
    {{
      "source": "매체명",
      "title": "제목",
      "url": "링크",
      "summary": "한줄 요약",
      "category": "카테고리",
      "fit_score": 0-100
    }}
  ]
}}
"""

    payload = {
        "articles": [
            {
                "source": a.source_name,
                "title": a.title,
                "url": a.url,
                "body_100": a.body_100,
                "primary_score": a.primary_score,
                "title_filter_score": a.llm_title_score,
                "title_filter_reason": a.llm_title_reason,
            }
            for a in candidates
        ]
    }

    result = llm_json(prompt, payload)
    if not result or "items" not in result:
        # fallback
        fallback = sorted(candidates, key=lambda x: (x.llm_title_score, x.primary_score), reverse=True)[:top_n]
        for a in fallback:
            a.final_score = a.llm_title_score or a.primary_score
            a.category = infer_category(a.title)
            a.summary = fallback_summary(a)
        return fallback

    by_url = {a.url: a for a in candidates}
    final: List[ArticleCandidate] = []

    for item in result["items"][:top_n]:
        url = item.get("url")
        if url not in by_url:
            # URL이 조금 바뀌었을 경우 title로 보정
            title = text_key(item.get("title", ""))
            match = next((a for a in candidates if text_key(a.title) == title), None)
            if not match:
                continue
            article = match
        else:
            article = by_url[url]

        article.summary = normalize_space(str(item.get("summary", "")))[:120]
        article.category = normalize_space(str(item.get("category", "")))[:40]
        try:
            article.final_score = int(item.get("fit_score", 0))
        except Exception:
            article.final_score = article.llm_title_score
        final.append(article)

    final.sort(key=lambda x: x.final_score, reverse=True)
    return final[:top_n]


def infer_category(title: str) -> str:
    t = text_key(title)
    mapping = [
        ("인허가/규제", ["허가", "인증", "FDA", "510(k)", "식약처", "regulatory", "clearance"]),
        ("수가/보험", ["수가", "급여", "비급여", "보험", "reimbursement", "coverage"]),
        ("정부사업/R&D", ["정부과제", "실증", "시범사업", "R&D", "grant", "pilot"]),
        ("수술로봇/인공관절", ["로봇", "수술로봇", "인공관절", "robot", "arthroplasty"]),
        ("정형외과/근골격계", ["정형외과", "근골격계", "무릎", "orthopedic", "MSK", "knee"]),
        ("의료AI/영상분석", ["AI", "인공지능", "의료영상", "영상", "imaging"]),
        ("경쟁사/투자", ["투자", "IPO", "M&A", "인수", "funding", "acquisition"]),
    ]
    for cat, keys in mapping:
        if any(text_key(k) in t for k in keys):
            return cat
    return "병원도입/사업화"


def fallback_summary(article: ArticleCandidate) -> str:
    if article.body_100:
        return f"{article.body_100[:70]}..."
    return "정형외과 AI SaMD/의료기기 사업과 연관 가능한 뉴스입니다."


# -----------------------------
# 저장
# -----------------------------
def save_filtered_primary(items: List[ArticleCandidate], run_date: str) -> Path:
    out_dir = DAILY_NEWS_CRAWLING_DIR / run_date
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "filtered_primary.json"
    payload = {
        "date": run_date,
        "count": len(items),
        "articles": [asdict(x) for x in items],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def save_daily_top_outputs(items: List[ArticleCandidate], run_date: str) -> Dict[str, str]:
    out_dir = DAILY_NEWS_CRAWLING_DIR / run_date
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "top10.json"
    md_path = out_dir / "top10.md"

    payload = {
        "date": run_date,
        "count": len(items),
        "articles": [asdict(x) for x in items],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        f"# 정형외과 AI SaMD 뉴스 Top {len(items)} - {run_date}",
        "",
        "| No | 카테고리 | 제목 | 출처 | 한줄 요약 |",
        "|---:|---|---|---|---|",
    ]
    for i, a in enumerate(items, start=1):
        title_md = f"[{a.title}]({a.url})"
        lines.append(
            f"| {i} | {a.category} | {title_md} | {a.source_name} | {a.summary} |"
        )
    lines.extend([
        "",
        "## 처리 기준",
        "- deduped.json → 키워드 1차 (소스 tier) → 임베딩 랭킹 부스트",
        "- LLM 제목 2차 필터 → LLM 최종 요약",
    ])
    md_path.write_text("\n".join(lines), encoding="utf-8")

    return {"top10_json": str(json_path), "top10_md": str(md_path)}


def save_outputs(items: List[ArticleCandidate], run_date: Optional[str] = None) -> Dict[str, str]:
    OUTPUT_DIR.mkdir(exist_ok=True)
    run_date = run_date or dt.datetime.now().strftime("%Y-%m-%d")

    json_path = OUTPUT_DIR / f"news_{run_date}.json"
    md_path = OUTPUT_DIR / f"news_{run_date}.md"

    with json_path.open("w", encoding="utf-8") as f:
        json.dump([asdict(x) for x in items], f, ensure_ascii=False, indent=2)

    lines = [
        f"# 정형외과 AI SaMD 뉴스 요약 - {run_date}",
        "",
        "| No | 카테고리 | 제목 | 출처 | 한줄 요약 |",
        "|---:|---|---|---|---|",
    ]

    for i, a in enumerate(items, start=1):
        title_md = f"[{a.title}]({a.url})"
        lines.append(
            f"| {i} | {a.category} | {title_md} | {a.source_name} | {a.summary} |"
        )

    lines.append("")
    lines.append("## 처리 기준")
    lines.append("- deduped.json → 키워드 1차 → 임베딩 랭킹 부스트")
    lines.append("- LLM 제목 2차 필터 → LLM 최종 요약")

    md_path.write_text("\n".join(lines), encoding="utf-8")

    return {"json": str(json_path), "markdown": str(md_path)}


# -----------------------------
# 실행
# -----------------------------
def run_from_deduped(
    target_date: str | None = None,
    *,
    input_path: Path | None = None,
    top_n: int = 10,
    llm_cap: int = 60,
    skip_embed_boost: bool = False,
) -> List[ArticleCandidate]:
    load_dotenv(_REPO_ROOT / ".env")
    day = target_date or dt.datetime.now().strftime("%Y-%m-%d")

    print(f"[step 1] load deduped ({day})")
    articles = load_deduped_articles(day, input_path=input_path)
    candidates = articles_to_candidates(articles)
    print(f"        loaded: {len(candidates)}")

    print("[step 2] primary keyword filter")
    primary = primary_title_filter(candidates)
    print(f"        passed: {len(primary)}")
    save_filtered_primary(primary, day)

    print("[step 3] embedding rank boost")
    primary = apply_embedding_rank_boost(primary, skip=skip_embed_boost)

    llm_input = primary[:llm_cap]
    print(f"[step 4] LLM title filter (input={len(llm_input)})")
    second = llm_title_filter(llm_input)
    print(f"        passed: {len(second)}")

    print(f"[step 5] LLM final top {top_n}")
    previous_top10_text = load_previous_top10_for_prompt(day)
    final = llm_final_summarize(second, top_n=top_n, previous_top10_text=previous_top10_text)
    print(f"        final: {len(final)}")

    daily_paths = save_daily_top_outputs(final, day)
    legacy_paths = save_outputs(final, run_date=day)
    print(f"[save] {daily_paths['top10_json']}")
    print(f"[save] {daily_paths['top10_md']}")
    print(f"[save] {legacy_paths['json']}")
    return final


def run_once(max_per_source: int, enrich_body_limit: int, top_n: int) -> List[ArticleCandidate]:
    load_dotenv(_REPO_ROOT / ".env")

    print("[step 1] crawling sources")
    crawled = crawl_all_sources(max_per_source=max_per_source, enrich_body_limit=enrich_body_limit)
    print(f"[step 1] crawled: {len(crawled)}")

    print("[step 2] primary title keyword filter")
    primary = primary_title_filter(crawled)
    print(f"[step 2] passed: {len(primary)}")

    print("[step 3] embedding rank boost")
    primary = apply_embedding_rank_boost(primary)

    print("[step 4] LLM title filter")
    second = llm_title_filter(primary[:60])
    print(f"[step 4] passed: {len(second)}")

    print("[step 5] LLM final summarize")
    day = dt.datetime.now(KST).strftime("%Y-%m-%d")
    previous_top10_text = load_previous_top10_for_prompt(day)
    final = llm_final_summarize(second, top_n=top_n, previous_top10_text=previous_top10_text)
    print(f"[step 5] final: {len(final)}")

    paths = save_outputs(final)
    print(f"[save] {paths['markdown']}")
    print(f"[save] {paths['json']}")

    return final


# -----------------------------
# 일일 Slack 전송 (크롤 + 요약 + Top N)
# -----------------------------
def yesterday_kst() -> date:
    return (dt.datetime.now(KST) - timedelta(days=1)).date()


def resolve_target_date(value: date | str | None = None) -> date:
    if value is None:
        return yesterday_kst()
    if isinstance(value, str):
        return date.fromisoformat(value)
    return value


def build_slack_news_text(items: List[ArticleCandidate], target_date: str) -> str:
    """Slack mrkdwn — 제목(링크), 한줄 요약."""
    n = len(items)
    lines = [f"📰 *의료 AI·로봇 뉴스 Top {n}* ({target_date})", ""]
    for i, article in enumerate(items, start=1):
        lines.append(f"*{i}. <{article.url}|{article.title}>*")
        summary = (article.summary or "").strip()
        if summary:
            lines.append(f"   *{summary}*")
        lines.append("")
    return "\n".join(lines).strip()


def send_slack_daily_news(text: str, channel: str | None = None) -> bool:
    token = os.getenv("SLACK_BOT_TOKEN", "")
    channel = (
        channel
        or os.getenv("NEWS_DAILY_SLACK_CHANNEL", "")
        or os.getenv("GOV_PROJECT_SLACK_CHANNEL", "")
        or os.getenv("FLEX_HR_SLACK_CHANNEL", "")
    )
    if not token or not channel:
        logger.warning(
            "SLACK_BOT_TOKEN / NEWS_DAILY_SLACK_CHANNEL 미설정 — 슬랙 전송 스킵"
        )
        return False

    try:
        res = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "channel": channel,
                "text": text,
                "unfurl_links": False,
                "unfurl_media": False,
            },
            timeout=30,
        )
        data = res.json()
        if data.get("ok"):
            logger.info("뉴스 일일 슬랙 전송 완료 → %s", channel)
            return True
        logger.error("뉴스 슬랙 전송 실패: %s", data.get("error"))
    except Exception as e:
        logger.error("뉴스 슬랙 전송 예외: %s", e)
    return False


def run_daily_news_job(
    target_date: date | str | None = None,
    *,
    skip_crawl: bool = False,
    skip_slack: bool = False,
    top_n: int = 10,
    llm_cap: int = 60,
    skip_embed_boost: bool = False,
) -> dict[str, Any]:
    """전일 뉴스: 크롤 → dedup → 필터/요약 → Slack Top N."""
    load_dotenv(_REPO_ROOT / ".env")
    day = resolve_target_date(target_date)
    day_str = day.isoformat()

    report: dict[str, Any] = {
        "target_date": day_str,
        "crawl_ok": False,
        "summary_count": 0,
        "slack_ok": False,
        "slack_skipped": False,
    }

    if not skip_crawl:
        from app.services.news.news_crawling import run as run_news_crawl

        try:
            logger.info("[news daily] 크롤 시작 — %s", day_str)
            run_news_crawl(target_date=day)
            report["crawl_ok"] = True
        except Exception as e:
            logger.exception("[news daily] 크롤 실패: %s", e)
            report["crawl_error"] = str(e)

    try:
        logger.info("[news daily] 요약 시작 — %s", day_str)
        final = run_from_deduped(
            target_date=day_str,
            top_n=top_n,
            llm_cap=llm_cap,
            skip_embed_boost=skip_embed_boost,
        )
        report["summary_count"] = len(final)
    except FileNotFoundError as e:
        logger.error("[news daily] deduped.json 없음: %s", e)
        report["summary_error"] = str(e)
        return report
    except Exception as e:
        logger.exception("[news daily] 요약 실패: %s", e)
        report["summary_error"] = str(e)
        return report

    if skip_slack:
        report["slack_skipped"] = True
        return report

    if final:
        text = build_slack_news_text(final, day_str)
    else:
        text = f"📰 *의료 AI·로봇 뉴스* ({day_str})\n\n선정된 기사가 없습니다."

    report["slack_ok"] = send_slack_daily_news(text)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="뉴스 크롤·요약·Slack")
    parser.add_argument("--daily", action="store_true", help="전일 크롤+요약+Slack 일괄 실행")
    parser.add_argument("--date", default=None, help="대상 날짜 YYYY-MM-DD")
    parser.add_argument("--input", default=None, help="deduped.json 경로 (--date 대신)")
    parser.add_argument("--top-n", type=int, default=int(os.getenv("NEWS_TOP_N", "10")), help="최종 뉴스 개수")
    parser.add_argument("--llm-cap", type=int, default=60, help="LLM 2차 입력 상한")
    parser.add_argument("--skip-crawl", action="store_true", help="(--daily) 크롤 생략")
    parser.add_argument("--skip-slack", action="store_true", help="(--daily) Slack 전송 생략")
    parser.add_argument("--skip-embed-boost", action="store_true", help="임베딩 랭킹 부스트 생략")
    parser.add_argument("--legacy-crawl", action="store_true", help="NEWS_SOURCES 직접 크롤 모드")
    parser.add_argument("--max-per-source", type=int, default=40, help="(legacy) 소스별 최대 링크")
    parser.add_argument("--enrich-body-limit", type=int, default=250, help="(legacy) 본문 수집 상한")
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    args = parse_args()
    if args.daily:
        result = run_daily_news_job(
            target_date=args.date,
            skip_crawl=args.skip_crawl,
            skip_slack=args.skip_slack,
            top_n=args.top_n,
            llm_cap=args.llm_cap,
            skip_embed_boost=args.skip_embed_boost,
        )
        print(result)
    elif args.legacy_crawl:
        run_once(
            max_per_source=args.max_per_source,
            enrich_body_limit=args.enrich_body_limit,
            top_n=args.top_n,
        )
    else:
        input_path = Path(args.input) if args.input else None
        run_from_deduped(
            target_date=args.date,
            input_path=input_path,
            top_n=args.top_n,
            llm_cap=args.llm_cap,
            skip_embed_boost=args.skip_embed_boost,
        )
