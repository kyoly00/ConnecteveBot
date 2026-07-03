import os
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urljoin, unquote, urlparse

import scripts._bootstrap  # noqa: F401
from dotenv import load_dotenv
from app.core.config import (
    ATLASSIAN_DOMAIN,
    BASE_CONFLUENCE_URL,
    METADATA_DIR,
    PLAYWRIGHT_PROFILE_DIR,
    POLICY_PAGE_DIR,
    ATTACHMENTS_DIR,
)

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright  # 👈 Playwright 동기 방식 임포트

# =========================================================
# 설정 (config.PROJECT_ROOT 기준 절대 경로)
# =========================================================

POLICYPAGE_DIR = POLICY_PAGE_DIR

load_dotenv()

EXCLUDE_URL_PREFIXES = (
    "https://connecteve-prod.atlassian.net/wiki/people",
)

TARGET_EXTENSIONS = {
    ".docx", ".hwp", ".pdf", ".pptx", ".xlsx", ".zip",
    ".heic", ".jpg", ".jpeg", ".png", ".mov", ".mp4",
    ".link", ".image",
}

# Chrome user_data_dir 와 동일 — 로그인·세션이 디스크에 유지됨
_PLAYWRIGHT_PROFILE = Path(
    os.getenv("PLAYWRIGHT_USER_DATA_DIR", str(PLAYWRIGHT_PROFILE_DIR))
).resolve()
PLAYWRIGHT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _playwright_headless() -> bool:
    return os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() in ("1", "true", "yes")

def _parse_atlassian_cookies(cookie_string: str) -> list[dict]:
    """ATLASSIAN_COOKIE 문자열 → Playwright cookie dict 목록."""
    out: list[dict] = []
    for pair in cookie_string.split(";"):
        if "=" not in pair:
            continue
        key, value = pair.strip().split("=", 1)
        out.append({
            "name": key,
            "value": value,
            "domain": f".{ATLASSIAN_DOMAIN}",
            "path": "/",
        })
    return out


def _looks_like_login_page(page) -> bool:
    """Atlassian 로그인/SSO 화면 여부 (휴리스틱)."""
    try:
        url = (page.url or "").lower()
        if "login" in url or "id.atlassian.com" in url:
            return True
        title = (page.title() or "").lower()
        if "log in" in title or "로그인" in title:
            return True
    except Exception:
        pass
    return False


def _ensure_atlassian_session(page, *, profile_dir: Path) -> None:
    """
    영구 프로필에 세션이 없으면 Confluence를 열어 수동 로그인 대기.
    PLAYWRIGHT_LOGIN_WAIT_SEC>0 이면 항상 해당 초만큼 대기(최초 1회 설정용).
    """
    wait_sec = int(os.getenv("PLAYWRIGHT_LOGIN_WAIT_SEC", "0") or "0")
    first_profile = not (profile_dir / "Default").exists() and not any(profile_dir.iterdir())

    if wait_sec <= 0 and not first_profile:
        if os.getenv("PLAYWRIGHT_ENSURE_LOGIN", "").lower() not in ("1", "true", "yes"):
            return

    seed_url = os.getenv("PLAYWRIGHT_LOGIN_URL", f"{BASE_CONFLUENCE_URL}/spaces/CW")
    print(f"🔐 Confluence 세션 확인: {seed_url}")
    if first_profile:
        print(f"   최초 실행 — 프로필 폴더: {profile_dir}")
        print("   열린 Chromium 창에서 로그인하면 이후 실행부터 세션이 유지됩니다.")

    page.goto(seed_url, wait_until="domcontentloaded", timeout=120000)

    if _looks_like_login_page(page) and wait_sec <= 0:
        wait_sec = int(os.getenv("PLAYWRIGHT_LOGIN_WAIT_ON_AUTH", "120") or "120")
        print(f"   로그인 화면 감지 — 최대 {wait_sec}초 대기 (PLAYWRIGHT_LOGIN_WAIT_ON_AUTH)")

    if wait_sec > 0:
        print(f"   {wait_sec}초 대기 중… (로그인 완료 후 자동 진행)")
        page.wait_for_timeout(wait_sec * 1000)


# =========================================================
# 휴지통 UI (viewtrash) — REST Trash API 불가 시 Playwright
# =========================================================

_PAGE_ID_FROM_HREF_RE = re.compile(
    r"(?:[?&]pageId=|/pages/|/content/)(\d+)",
    re.IGNORECASE,
)


def _page_id_from_href(href: str) -> str | None:
    if not href:
        return None
    parsed = urlparse(href)
    qs = parse_qs(parsed.query)
    if "contentId" in qs and qs["contentId"]:
        return str(qs["contentId"][0]).strip()
    m = _PAGE_ID_FROM_HREF_RE.search(href)
    return m.group(1) if m else None


def _collect_trash_entries_from_page(page) -> list[dict[str, str]]:
    """버리기(Purge) 버튼의 href에서 contentId를 추출하는 방식으로 수정."""
    rows: list[dict[str, str]] = page.evaluate(
        """() => {
            const out = [];
            // 버리기(purgetrashitem.action) 링크를 타겟팅합니다.
            const links = document.querySelectorAll('a[href*="purgetrashitem.action"]');
            for (const a of links) {
                const href = a.href || '';
                // aria-label에서 제목을 가져옵니다 (예: '버리기 챗봇 테스트')
                const label = a.getAttribute('aria-label') || '';
                const title = label.replace(/^버리기 /, '').trim();
                out.push({ href, title });
            }
            return out;
        }"""
    )
    
    entries: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    
    for row in rows:
        href = row.get("href", "")
        title = row.get("title", "")

        # href에서 contentId 파라미터 추출
        pid = _page_id_from_href(href)
        
        if pid and pid not in seen_ids:
            seen_ids.add(pid)
            entries.append({"id": pid, "title": title})
            
    return entries

def get_trashed_pages_via_playwright(
    space_key: str,
    *,
    top_n: int | None = None,
) -> list[dict[str, str]]:
    """
    viewtrash.action?key={space} 에서 휴지통 페이지 id 목록.
    top_n: 상위 N개만 (증분 폴링). None 이면 페이징 포함 전체 스캔.
  """
    space_key = (space_key or "CW").strip()
    trash_url = f"{BASE_CONFLUENCE_URL}/pages/viewtrash.action?key={space_key}"
    max_pages = int(os.getenv("CONFLUENCE_TRASH_PLAYWRIGHT_MAX_PAGES", "50") or "50")

    profile_dir = _PLAYWRIGHT_PROFILE
    profile_dir.mkdir(parents=True, exist_ok=True)
    headless = _playwright_headless()
    cookie_string = (os.getenv("ATLASSIAN_COOKIE") or "").strip()

    all_entries: list[dict[str, str]] = []
    seen: set[str] = set()

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            user_agent=PLAYWRIGHT_USER_AGENT,
        )
        if cookie_string:
            try:
                context.add_cookies(_parse_atlassian_cookies(cookie_string))
            except Exception:
                pass

        page = context.pages[0] if context.pages else context.new_page()
        _ensure_atlassian_session(page, profile_dir=profile_dir)
        page.goto(trash_url, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(10000)

        if _looks_like_login_page(page):
            context.close()
            raise RuntimeError("login_required: Confluence 로그인 세션이 없습니다 (viewtrash)")

        for _ in range(max_pages):
            for ent in _collect_trash_entries_from_page(page):
                pid = ent["id"]
                if pid in seen:
                    continue
                seen.add(pid)
                all_entries.append(ent)
                if top_n is not None and len(all_entries) >= top_n:
                    context.close()
                    return all_entries[:top_n]

            if top_n is not None:
                break

            next_clicked = page.evaluate(
                """() => {
                const labels = ['Next', '다음', 'next'];
                for (const a of document.querySelectorAll('a[href]')) {
                    const t = (a.innerText || '').trim();
                    const aria = (a.getAttribute('aria-label') || '').trim();
                    for (const lab of labels) {
                        if (t === lab || aria === lab) { a.click(); return true; }
                    }
                }
                return false;
            }"""
            )
            if not next_clicked:
                break
            page.wait_for_timeout(30000)

        context.close()

    return all_entries if top_n is None else all_entries[:top_n]


# =========================================================
# 🛠️ [Playwright 코어 구현] 영구 프로필 + 다운로드 엔진
# =========================================================

def download_attachment_with_playwright(page_data_list):
    """
    launch_persistent_context(user_data_dir)로 Chrome과 같이 로그인 상태를 유지하며
    이번 배치의 첨부파일을 연속 다운로드합니다.
    """
    if not page_data_list:
        return

    profile_dir = _PLAYWRIGHT_PROFILE
    profile_dir.mkdir(parents=True, exist_ok=True)
    headless = _playwright_headless()

    cookie_string = (os.getenv("ATLASSIAN_COOKIE") or "").strip()
    if cookie_string:
        print("ℹ️ ATLASSIAN_COOKIE 가 있으면 프로필 세션에 추가 주입합니다.")
    else:
        print("ℹ️ ATLASSIAN_COOKIE 없음 — 영구 프로필 세션만 사용합니다.")

    print(f"\n🌐 Playwright Chromium (영구 프로필): {profile_dir}")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            user_agent=PLAYWRIGHT_USER_AGENT,
            accept_downloads=True,
        )

        if cookie_string:
            try:
                context.add_cookies(_parse_atlassian_cookies(cookie_string))
            except Exception as e:
                print(f"⚠️ 쿠키 주입 실패(프로필 세션 사용): {e}")

        page = context.pages[0] if context.pages else context.new_page()
        _ensure_atlassian_session(page, profile_dir=profile_dir)

        for download_job in page_data_list:
            url = download_job["url"]
            save_path = download_job["save_path"]
            filename = os.path.basename(save_path)

            if os.path.exists(save_path):
                print(f"⏩ 스킵: 이미 존재함 ({filename})")
                continue

            try:
                if url.startswith("/"):
                    target_url = urljoin(BASE_CONFLUENCE_URL, url)
                else:
                    target_url = url

                print(f"📥 [브라우저 다운로드 시도] URL: {target_url}")
                
                # 1. 파일 스트림이 떨어질 때까지 대기하는 컨텍스트 오픈
                with page.expect_download(timeout=30000) as download_info:
                    try:
                        # 🎯 [핵심 수정] page.goto가 ERR_ABORTED를 던져도 무시하도록 가드 처리
                        # 또는 page.goto 대신 window.location.href를 변경하는 방식을 사용하면 에러가 안 납니다.
                        page.evaluate(f"window.location.href = '{target_url}';")
                        
                        # 만약 기존 goto 방식을 선호하신다면 아래 주석처럼 예외처리 해도 됩니다.
                        # page.goto(target_url, wait_until="commit")
                    except Exception as goto_err:
                        # ERR_ABORTED 경고는 다운로드 트리거 작동 시 발생하는 정상적인 신호이므로 패스합니다.
                        if "ERR_ABORTED" in str(goto_err):
                            pass
                        else:
                            raise goto_err
                
                # 2. 파일 세이브 속행
                download = download_info.value
                download.save_as(save_path)
                
                file_size = os.path.getsize(save_path)
                if file_size < 200:
                    print(f"⚠️ [경고] {filename} 크기가 너무 작습니다({file_size} bytes). HTML 덤프 의심")
                else:
                    print(f"✅ 다운로드 성공: {filename} ({file_size:,} bytes)")

            except Exception as e:
                print(f"❌ {filename} 다운로드 실패 최종 에러: {e}")
                if os.path.exists(save_path) and os.path.getsize(save_path) < 1000:
                    os.remove(save_path)

        context.close()
        print(f"💾 브라우저 프로필 저장됨: {profile_dir}")


# =========================================================
# 유틸 함수
# =========================================================

def safe_filename(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]", "_", name)
    return name.strip()

def get_extension(filename: str) -> str:
    return Path(filename).suffix.lower()

def process_content_with_attachments(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-image-src") or ""
        filename = unquote(os.path.basename(urlparse(src).path))
        if not filename:
            filename = "[Unknown 이미지]"
            
        placeholder = f"[이미지: {filename}]"
        wrapper = img.find_parent("span", class_="confluence-embedded-file-wrapper")
        if wrapper:
            wrapper.replace_with(placeholder)
        else:
            img.replace_with(placeholder)

    for tag in soup.find_all(attrs={"data-file-src": True}):
        src = tag["data-file-src"]
        filename = unquote(os.path.basename(urlparse(src).path))
        placeholder = f"[첨부파일: {filename}]"
        tag.replace_with(placeholder)

    return str(soup)

def is_excluded_url(url: str) -> bool:
    return any(url.startswith(prefix) for prefix in EXCLUDE_URL_PREFIXES)


def process_content_with_links(html: str, base_url: str):
    soup = BeautifulSoup(html, "html.parser")
    urls_list = []

    for a in soup.find_all("a", href=True):
        original_text = a.get_text(strip=True)
        href = a["href"]

        if is_excluded_url(href):
            continue
        
        full_url = urljoin(base_url, href)
        urls_list.append({"text": original_text, "url": full_url})
        new_content = f"[{original_text}]"
        a.replace_with(new_content)

    processed_text = soup.get_text(separator="\n", strip=True)
    processed_text = re.sub(r"\n{3,}", "\n\n", processed_text)
    return processed_text, urls_list

def extract_links_from_html(html: str):
    soup = BeautifulSoup(html, "html.parser")
    extracted = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        filename = unquote(os.path.basename(urlparse(href).path))
        ext = get_extension(filename)
        if ext in TARGET_EXTENSIONS:
            extracted.append({"tag": "a", "filename": filename, "extension": ext, "url": href})

    for img in soup.find_all("img"):
        src = img.get("src")
        data_image_src = img.get("data-image-src")
        for candidate in [src, data_image_src]:
            if not candidate:
                continue
            filename = unquote(os.path.basename(urlparse(candidate).path))
            ext = get_extension(filename)
            if ext in TARGET_EXTENSIONS:
                extracted.append({"tag": "img", "filename": filename, "extension": ext, "url": candidate})

    for tag in soup.find_all(attrs={"data-file-src": True}):
        url = tag["data-file-src"]
        filename = unquote(os.path.basename(urlparse(url).path))
        ext = get_extension(filename)
        if ext in TARGET_EXTENSIONS:
            extracted.append({"tag": "data-file-src", "filename": filename, "extension": ext, "url": url})

    unique = {}
    for item in extracted:
        unique[item["url"]] = item
    return list(unique.values())


# =========================================================
# 메인 전처리 및 스케줄링 큐 구성 파트
# =========================================================

# Playwright 일괄 다운로드 엔진에 던질 글로벌 작업 큐 정의
global_download_jobs = []

def process_policy_page_metadata(json_path):
    print(f"\n[METADATA PROCESS] {json_path}")

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    page = data.get("page", {})
    attachments = data.get("attachments", [])
    page_id = page.get("id")
    title = page.get("title", "")
    body_html = page.get("body_view_html", "")

    extracted_links = extract_links_from_html(body_html)
    body_html_with_attachments = process_content_with_attachments(body_html)
    processed_text, urls_metadata = process_content_with_links(body_html_with_attachments, BASE_CONFLUENCE_URL)

    downloaded_attachments_metadata = []

    # 각 문서들의 첨부파일을 루프돌며 즉시 다운로드하지 않고, Playwright 작업 스케줄 큐에 빌딩
    for att in attachments:
        filename = att.get("title", "")
        download_url = att.get("download_url")

        if not filename or not download_url:
            continue

        ext = get_extension(filename)
        if ext not in TARGET_EXTENSIONS:
            continue

        safe_name = safe_filename(filename)
        page_attachment_dir = os.path.join(ATTACHMENTS_DIR, str(page_id))
        os.makedirs(page_attachment_dir, exist_ok=True)
        save_path = os.path.join(page_attachment_dir, safe_name)

        # 🎯 핵심: 일회성 요청을 다발로 날리지 않고 다운로드 작업을 큐(Queue)에 예약함
        global_download_jobs.append({
            "url": download_url,
            "save_path": save_path
        })

        downloaded_attachments_metadata.append({
            "id": att.get("id"),
            "title": filename,
            "extension": ext,
            "media_type": att.get("media_type"),
            "file_size": att.get("file_size"),
            "download_url": download_url,
            "saved_path": save_path,
            "webui": att.get("webui")
        })

    metadata = {
        "page": {"id": page_id, "title": title, "type": page.get("type"), "status": page.get("status")},
        "content": {"body_text": processed_text, "urls": urls_metadata, "body_text_length": len(processed_text)},
        "extracted_links": extracted_links,
        "attachments": downloaded_attachments_metadata,
    }

    metadata_filename = safe_filename(f"{page_id}_{title}.metadata.json")
    metadata_path = os.path.join(METADATA_DIR, metadata_filename)

    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)


def main():
    json_files = list(Path(POLICYPAGE_DIR).rglob("*.json"))
    print(f"📊 총 {len(json_files)}개 JSON 메타 명세서 분석 전처리 시작")

    # 1단계: 모든 파일들의 텍스트 파싱을 진행하면서 첨부파일 주소들을 다운로드 큐에 모집
    for json_file in json_files:
        try:
            process_policy_page_metadata(str(json_file))
        except Exception as e:
            print(f"[ERROR] 명세 파싱 실패: {json_file} | {e}")

    print(f"\n🎯 준비 완료: 총 {len(global_download_jobs)}개의 첨부파일 다운로드 예약 스케줄 확보.")
    
    # 2단계: 수집 완료된 큐를 들고 딱 한 번만 Playwright 브라우저 컨텍스트를 띄워 완전무결 일괄 다운로드 실행
    if global_download_jobs:
        download_attachment_with_playwright(global_download_jobs)
        
    print("\n🏁 모든 위키 본문 정제 및 첨부파일 크롤링 동기화 완료!")


if __name__ == "__main__":
    main()