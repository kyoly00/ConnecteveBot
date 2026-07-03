"""Flex HR 수집·파싱 — 일간(타임라인) / 월간(일별 카드) 통합."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

from dotenv import load_dotenv

from app.core.config import (
    FLEX_HR_DIR,
    FLEX_PLAYWRIGHT_PROFILE_DIR,
    flex_hr_monthly_json_path,
    now_iso,
)

from .day_parser import (
    LEAVE_TYPES,
    parse_flex_hr_day_html,
    parse_flex_hr_html,
    is_daily_html,
)
from .month_parser import (
    flatten_flex_hr_monthly,
    is_monthly_html,
    parse_flex_hr_monthly_html,
)

load_dotenv()

FLEX_URL = os.getenv("FLEX_URL", "").strip()
PLAYWRIGHT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

__all__ = [
    "LEAVE_TYPES",
    "FLEX_URL",
    "detect_flex_hr_period",
    "parse_flex_hr_html",
    "parse_flex_hr_day_html",
    "parse_flex_hr_monthly_html",
    "parse_flex_hr_auto",
    "parse_flex_hr_html_fixed",
    "flatten_flex_hr_monthly",
    "fetch_flex_hr_html",
    "fetch_flex_hr_monthly",
    "save_flex_hr_json",
    "save_flex_hr_monthly_json",
    "run_flex_hr_pipeline",
    "run_flex_hr_monthly_pipeline",
    "is_daily_html",
    "is_monthly_html",
]


def _playwright_headless() -> bool:
    return os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() in ("1", "true", "yes")


def _is_monthly_period(period: str | None, query: dict) -> bool:
    if period == "monthly":
        return True
    return (query.get("period") or [""])[0].lower() == "monthly"


def _normalize_month_date(date: str | None) -> str:
    if not date:
        return datetime.now().strftime("%Y-%m-01")
    if re.fullmatch(r"\d{4}-\d{2}", date):
        return f"{date}-01"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return f"{date[:7]}-01"
    return date


def _resolve_flex_url(url=None, date=None, *, period: str | None = None):
    """FLEX_URL에 date·period 쿼리를 보완한다."""
    base = (url or FLEX_URL or "").strip()
    if not base:
        raise ValueError("FLEX_URL 환경변수 또는 url 인자가 필요합니다.")

    parsed = urlparse(base)
    query = parse_qs(parsed.query, keep_blank_values=True)
    monthly = _is_monthly_period(period, query)

    if monthly:
        query["period"] = ["monthly"]
        # 월 전환은 URL date가 아닌 UI 네비게이션으로 처리한다.
        query.pop("date", None)
    else:
        query.pop("period", None)
        if "date" not in query or not query["date"][0]:
            query["date"] = [date or datetime.now().strftime("%Y-%m-%d")]

    new_query = urlencode({k: v[0] for k, v in query.items()})
    return urlunparse(parsed._replace(query=new_query))


def detect_flex_hr_period(html_content: str, *, url: str | None = None) -> str:
    """HTML·URL 기준 daily / monthly 판별."""
    if url and "period=monthly" in url:
        return "monthly"
    if is_monthly_html(html_content):
        return "monthly"
    if is_daily_html(html_content):
        return "daily"
    return "monthly" if "c-fQnhbx" in html_content else "daily"


def parse_flex_hr_auto(html_content: str, *, url: str | None = None) -> dict:
    """HTML 유형에 따라 일간·월간 파서를 자동 선택한다."""
    if detect_flex_hr_period(html_content, url=url) == "monthly":
        return parse_flex_hr_monthly_html(html_content)
    return parse_flex_hr_day_html(html_content)


parse_flex_hr_html_fixed = parse_flex_hr_html


def _looks_like_login_page(page) -> bool:
    try:
        url = (page.url or "").lower()
        if any(k in url for k in ("login", "signin", "sign-in", "auth", "oauth")):
            return True
        title = (page.title() or "").lower()
        if any(k in title for k in ("log in", "login", "로그인", "sign in")):
            return True
        content = page.content()
        if "이메일" in content and "비밀번호" in content:
            if not any(token in content for token in ("c-gNZGXI", "c-bZNrxE", "c-fQnhbx")):
                return True
    except Exception:
        pass
    return False


def _ensure_flex_session(page, *, profile_dir: Path) -> None:
    wait_sec = int(os.getenv("FLEX_LOGIN_WAIT_SEC", "0") or "0")
    first_profile = not profile_dir.exists() or not any(profile_dir.iterdir())

    if wait_sec <= 0 and not first_profile:
        if os.getenv("FLEX_ENSURE_LOGIN", "").lower() not in ("1", "true", "yes"):
            return

    login_url = os.getenv("FLEX_LOGIN_URL", FLEX_URL or "").strip()
    if not login_url:
        login_url = page.url

    print(f"Flex 로그인 확인: {login_url}")
    if first_profile:
        print(f"  최초 실행 — 프로필 저장 위치: {profile_dir}")
        print("  열린 Chromium 창에서 로그인하면 이후부터 세션이 유지됩니다.")

    page.goto(login_url, wait_until="domcontentloaded", timeout=120000)

    if _looks_like_login_page(page) and wait_sec <= 0:
        wait_sec = int(os.getenv("FLEX_LOGIN_WAIT_ON_AUTH", "180") or "180")
        print(f"  로그인 화면 감지 — 최대 {wait_sec}초 대기")

    if wait_sec > 0:
        print(f"  {wait_sec}초 대기 중… (로그인 완료 후 자동 진행)")
        page.wait_for_timeout(wait_sec * 1000)


def _scroll_flex_timeline(page) -> None:
    page.wait_for_selector(".c-gNZGXI, .c-eqXmhd", timeout=120000)

    prev_count = 0
    stable_rounds = 0
    for _ in range(120):
        count = page.locator(".c-gNZGXI").count()
        if count == prev_count:
            stable_rounds += 1
            if stable_rounds >= 4:
                break
        else:
            stable_rounds = 0
            prev_count = count

        page.evaluate(
            """
            () => {
                const nodes = document.querySelectorAll(
                    '.c-lldrJN, .c-gLEuSl, .c-eqXmhd, [class*="c-hzjAHE"]'
                );
                for (const el of nodes) {
                    el.scrollTop = el.scrollHeight;
                }
                window.scrollTo(0, document.body.scrollHeight);
            }
            """
        )
        page.wait_for_timeout(350)

    page.wait_for_timeout(800)


def _launch_flex_page(target_url: str, *, monthly: bool):
    """Playwright persistent context를 열고 Flex HR 페이지까지 이동한다."""
    from playwright.sync_api import sync_playwright

    profile_dir = Path(
        os.getenv("FLEX_PLAYWRIGHT_PROFILE_DIR", str(FLEX_PLAYWRIGHT_PROFILE_DIR))
    ).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    headless = _playwright_headless()

    print(f"Flex HR 접속: {target_url}")
    print(f"  프로필: {profile_dir} (headless={headless}, monthly={monthly})")

    playwright = sync_playwright().start()
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=headless,
        user_agent=PLAYWRIGHT_USER_AGENT,
    )
    page = context.pages[0] if context.pages else context.new_page()
    _ensure_flex_session(page, profile_dir=profile_dir)

    page.goto(target_url, wait_until="domcontentloaded", timeout=120000)
    if _looks_like_login_page(page):
        _ensure_flex_session(page, profile_dir=profile_dir)
        page.goto(target_url, wait_until="domcontentloaded", timeout=120000)

    return playwright, context, page


def _close_flex_page(playwright, context) -> None:
    context.close()
    playwright.stop()


_INJECT_TIMELINE_POSITIONS_JS = """
() => {
  const rows = document.querySelectorAll('.c-eqXmhd .c-hzjAHE');
  let injected = 0;

  const pickVisual = (wrapper) => {
    return (
      wrapper.querySelector('[class*="c-iyXem"]') ||
      wrapper.querySelector('[class*="c-hOEbFA"]') ||
      wrapper.querySelector('[class*="c-jMQbTZ"]') ||
      wrapper.querySelector('.c-gwshsc') ||
      wrapper
    );
  };

  const applyPct = (el, leftPct, rightPct) => {
    if (leftPct < -1 || rightPct < -1 || leftPct + rightPct > 100.5) return false;
    el.style.position = 'absolute';
    el.style.left = `${leftPct.toFixed(4)}%`;
    el.style.right = `${rightPct.toFixed(4)}%`;
    el.setAttribute('data-flex-injected', '1');
    injected += 1;
    return true;
  };

  rows.forEach((row) => {
    const track = row.querySelector('section.c-iomxRD') || row;
    const trackRect = track.getBoundingClientRect();
    const trackW = trackRect.width;
    if (trackW < 10) return;

    row.querySelectorAll('.c-gAAhAz').forEach((wrapper) => {
      const visual = pickVisual(wrapper);
      const rect = visual.getBoundingClientRect();
      if (rect.width < 2 || rect.height < 2) return;
      const leftPct = ((rect.left - trackRect.left) / trackW) * 100;
      const rightPct = ((trackRect.right - rect.right) / trackW) * 100;
      applyPct(wrapper, leftPct, rightPct);
    });

    row.querySelectorAll('[class*="c-jMQbTZ"]').forEach((seg) => {
      const rect = seg.getBoundingClientRect();
      if (rect.width < 2) return;
      const leftPct = ((rect.left - trackRect.left) / trackW) * 100;
      const rightPct = ((trackRect.right - rect.right) / trackW) * 100;
      applyPct(seg, leftPct, rightPct);
    });
  });

  return { rows: rows.length, injected };
}
"""


def _inject_timeline_positions(page) -> dict:
    settle_ms = int(os.getenv("FLEX_INJECT_SETTLE_MS", "300") or "300")
    if settle_ms > 0:
        page.wait_for_timeout(settle_ms)
    stats = page.evaluate(_INJECT_TIMELINE_POSITIONS_JS)
    print(f"  타임라인 위치 주입: {stats.get('injected', 0)}개 / {stats.get('rows', 0)}행")
    return stats


def _inject_timeline_positions_enabled() -> bool:
    return os.getenv("FLEX_INJECT_TIMELINE_POSITIONS", "true").lower() in (
        "1",
        "true",
        "yes",
    )


def _parse_target_year_month(date: str | None) -> tuple[int, int]:
    """수집 대상 연·월 (date 인자 또는 오늘 기준)."""
    normalized = _normalize_month_date(date)
    dt = datetime.strptime(normalized[:10], "%Y-%m-%d")
    return dt.year, dt.month


def _html_save_path(target_url: str, *, year_month: str | None = None) -> Path:
    monthly = "period=monthly" in target_url
    if monthly:
        file_key = year_month or datetime.now().strftime("%Y-%m")
        return FLEX_HR_DIR / f"flex_HR_{file_key}.html"

    date_match = re.search(r"date=(\d{4}-\d{2}-\d{2})", target_url)
    file_date = date_match.group(1) if date_match else datetime.now().strftime("%Y-%m-%d")
    return FLEX_HR_DIR / f"flex_HR_{file_date}.html"


def fetch_flex_hr_html(url=None, date=None, *, period: str | None = None, save_html=True):
    """Playwright로 Flex HR 일간 URL에 접속해 HTML을 가져온다."""
    from app.services.flex_hr.flex_hr import flex_playwright_session

    target_url = _resolve_flex_url(url, date=date, period=period or "daily")
    if "period=monthly" in target_url:
        raise ValueError("월간 수집은 fetch_flex_hr_monthly()를 사용하세요.")

    with flex_playwright_session("daily"):
        playwright, context, page = _launch_flex_page(target_url, monthly=False)
        try:
            _scroll_flex_timeline(page)
            if _inject_timeline_positions_enabled():
                _inject_timeline_positions(page)
            html = page.content()
        finally:
            _close_flex_page(playwright, context)

    html_path = None
    if save_html:
        FLEX_HR_DIR.mkdir(parents=True, exist_ok=True)
        html_path = _html_save_path(target_url)
        html_path.write_text(html, encoding="utf-8")
        print(f"  HTML 저장: {html_path}")

    return html, html_path


def fetch_flex_hr_monthly(url=None, date=None, *, save_html=True):
    """Playwright로 Flex HR 월간 그리드를 가상 스크롤하며 수집한다."""
    from app.services.flex_hr.flex_hr import flex_playwright_session
    from app.services.flex_hr.flex_parser.month_crawler import (
        crawl_flex_hr_monthly,
        navigate_to_target_month,
    )

    target_year, target_month = _parse_target_year_month(date)
    target_url = _resolve_flex_url(url, date=None, period="monthly")
    print(f"  수집 대상 월: {target_year}.{target_month} (UI 네비게이션)")

    with flex_playwright_session("monthly"):
        playwright, context, page = _launch_flex_page(target_url, monthly=True)
        html_path = None
        try:
            navigate_to_target_month(page, target_year, target_month)
            result = crawl_flex_hr_monthly(page, target_url=target_url)
            if save_html:
                FLEX_HR_DIR.mkdir(parents=True, exist_ok=True)
                year_month = result.get("year_month") or f"{target_year:04d}-{target_month:02d}"
                html_path = _html_save_path(target_url, year_month=year_month)
                html_path.write_text(page.content(), encoding="utf-8")
                print(f"  HTML 저장(스냅샷): {html_path}")
        finally:
            _close_flex_page(playwright, context)

    return result, html_path


def save_flex_hr_json(
    html_content=None,
    output_path=None,
    *,
    html_path=None,
):
    """일간 Flex HR HTML을 파싱하고 JSON으로 저장한다."""
    if html_content is None:
        if html_path is None:
            raise ValueError("html_content 또는 html_path가 필요합니다.")
        html_content = Path(html_path).read_text(encoding="utf-8")

    result = parse_flex_hr_day_html(html_content)

    if output_path is None:
        date = result.get("date") or datetime.now().strftime("%Y-%m-%d")
        FLEX_HR_DIR.mkdir(parents=True, exist_ok=True)
        output_path = FLEX_HR_DIR / f"flex_hr_parsed_{date}.json"
    else:
        output_path = Path(output_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result, output_path


def save_flex_hr_monthly_json(
    html_content: str | None = None,
    output_path: str | Path | None = None,
    *,
    html_path: str | Path | None = None,
    result: dict | None = None,
) -> tuple[dict, Path]:
    """월간 Flex HR 결과를 JSON으로 저장한다."""
    if result is None:
        if html_content is None:
            if html_path is None:
                raise ValueError("result, html_content, html_path 중 하나가 필요합니다.")
            html_content = Path(html_path).read_text(encoding="utf-8")
        result = parse_flex_hr_monthly_html(html_content)

    if output_path is None:
        year_month = result.get("year_month") or datetime.now().strftime("%Y-%m")
        FLEX_HR_DIR.mkdir(parents=True, exist_ok=True)
        output_path = flex_hr_monthly_json_path(year_month)
    else:
        output_path = Path(output_path)
        year_month = result.get("year_month") or output_path.stem.replace("flex_hr_", "").replace("_monthly", "")

    payload = dict(result)
    payload["updated_at"] = now_iso()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload, output_path


def run_flex_hr_pipeline(url=None, date=None):
    """일간: URL 접속 → HTML·JSON 저장 → 파싱 결과 반환."""
    html, html_path = fetch_flex_hr_html(url=url, date=date, period="daily", save_html=True)
    result, json_path = save_flex_hr_json(html_content=html)
    return result, html_path, json_path


def run_flex_hr_monthly_pipeline(url=None, date=None):
    """월간: 가상 스크롤 수집 → JSON 저장 → 파싱 결과 반환."""
    result, html_path = fetch_flex_hr_monthly(url=url, date=date, save_html=True)
    _, json_path = save_flex_hr_monthly_json(result=result)
    return result, html_path, json_path


if __name__ == "__main__":
    mode = (sys.argv[1] if len(sys.argv) > 1 else "daily").lower()
    if mode == "monthly":
        result, html_path, json_path = run_flex_hr_monthly_pipeline()
        print(f"HTML 저장: {html_path}")
        print(f"JSON 저장: {json_path}")
        print(f"대상 월: {result.get('year_month')}")
        print(f"직원 수: {result.get('meta', {}).get('member_count')}")
        print(f"일자 수: {result.get('meta', {}).get('day_count')}")
    else:
        result, html_path, json_path = run_flex_hr_pipeline()
        print(f"HTML 저장: {html_path}")
        print(f"JSON 저장: {json_path}")
        print(f"총 {len(result['members'])}명 파싱")
