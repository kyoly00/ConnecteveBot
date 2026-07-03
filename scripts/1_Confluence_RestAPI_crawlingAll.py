import os
import json
import re
import requests
import urllib
from dotenv import load_dotenv
from urllib.parse import urljoin
from requests.auth import HTTPBasicAuth


# .env 로드
load_dotenv()

ATLASSIAN_DOMAIN = "connecteve-prod.atlassian.net"

EMAIL = os.getenv("ATLASSIAN_EMAIL")
API_TOKEN = os.getenv("ATLASSIAN_API_TOKEN")

if not EMAIL or not API_TOKEN:
    raise RuntimeError("ATLASSIAN_EMAIL, ATLASSIAN_API_TOKEN 환경변수를 설정하세요.")

BASE_URL = f"https://{ATLASSIAN_DOMAIN}/wiki/"
AUTH = (EMAIL, API_TOKEN)

session = requests.Session()
session.auth = AUTH
session.headers.update({
    "Accept": "application/json",
    "Content-Type": "application/json"
})


def get_json(path: str, params: dict | None = None):
    url = urljoin(BASE_URL, path.lstrip("/"))

    res = session.get(url, params=params, timeout=60)

    print("GET", res.url)
    print("STATUS", res.status_code)

    res.raise_for_status()
    return res.json()


def get_page_content(page_id: str):
    """
    1. 페이지 본문 가져오기 (v1 API 유지)
    """
    return get_json(
        f"/rest/api/content/{page_id}",
        params={
            "expand": ",".join([
                "body.storage",
                "body.view",
                "body.export_view",
                "version",
                "space",
                "ancestors",
                "metadata.labels"
            ])
        }
    )


def get_child_pages(page_id: str):
    """
    2. Child page list 가져오기 (v1 API 유지)
    """
    results = []
    start = 0
    limit = 50

    while True:
        data = get_json(
            f"/rest/api/content/{page_id}/child/page",
            params={
                "start": start,
                "limit": limit,
                "expand": "version,space"
            }
        )

        batch = data.get("results", [])
        results.extend(batch)

        if len(batch) < limit:
            break

        start += limit

    return results


def get_attachments(page_id: str):
    """
    3. 첨부파일 목록 가져오기 (🎯 Confluence REST API v2 전면 적용 및 404 예외 처리)
    """
    results = []
    # v2 API 엔드포인트 (해당 페이지의 첨부파일 목록 및 ID 조회)
    url = urljoin(BASE_URL, f"api/v2/pages/{page_id}/attachments")
    
    while url:
        try:
            res = session.get(url, timeout=60)
            print("GET (v2 List)", res.url)
            
            res.raise_for_status()
            
            data = res.json()
            batch = data.get("results", [])
            results.extend(batch)
            
            # v2 API는 페이징을 위해 _links.next 에 다음 페이지 URL(Cursor 포함)을 전달합니다.
            next_path = data.get("_links", {}).get("next")
            if next_path:
                base_domain = f"https://{ATLASSIAN_DOMAIN}"
                url = urljoin(base_domain, next_path)
            else:
                break
                
        except requests.exceptions.HTTPError as e:
            # 🎯 404 에러 발생 시: 첨부파일이 없는 것으로 간주하고 조용히 넘어감
            if res.status_code == 404:
                print(f"⚠️ [알림] ID {page_id}: 첨부파일이 없거나 지원되지 않는 문서 타입입니다 (404 Skip).")
            else:
                print(f"❌ [에러] 첨부파일 목록 조회 중 API 에러 발생: {e}")
            break # 에러가 발생하면 반복문을 빠져나가고 지금까지 모은 results(빈 배열) 반환
            
        except Exception as e:
            print(f"❌ [에러] 첨부파일 목록 조회 중 알 수 없는 에러 발생: {e}")
            break

    return results


def normalize_attachment(att: dict):
    """
    첨부파일 ID로 v2 Attachment 개별 API를 재호출하여 
    정확한 base와 download 경로를 조합해 다운로드 URL을 생성합니다.
    """
    att_id = att.get("id")
    if not att_id:
        return None
        
    # 🎯 1. 첨부파일 ID를 이용해 개별 Attachment API 명시적 호출
    # (예: https://connecteve-prod.atlassian.net/wiki/api/v2/attachments/att37651104)
    detail_url = urljoin(BASE_URL, f"api/v2/attachments/{att_id}")
    
    try:
        res = session.get(detail_url, timeout=30)
        res.raise_for_status()
        detail_data = res.json()
    except Exception as e:
        print(f"❌ [에러] 첨부파일(ID: {att_id}) 상세 정보 조회 실패: {e}")
        return None
        
    # 🎯 2. 개별 호출 응답에서 정확한 _links 데이터 추출
    links = detail_data.get("_links", {})
    base_url = links.get("base")
    download_path = links.get("download")
    
    if not base_url or not download_path:
        print(f"⚠️ [경고] ID {att_id}: base 또는 download 링크가 존재하지 않습니다.")
        return None
        
    # 🎯 3. base와 download를 문자열로 완벽하게 결합 (urljoin의 / 증발 문제 차단)
    download_url = f"{base_url.rstrip('/')}/{download_path.lstrip('/')}"
    
    webui_path = links.get("webui", "")
    webui_url = f"{base_url.rstrip('/')}/{webui_path.lstrip('/')}" if webui_path else ""
    
    return {
        "id": detail_data.get("id"), 
        "title": detail_data.get("title"), 
        "type": "attachment",
        "media_type": detail_data.get("mediaType"), 
        "file_size": detail_data.get("fileSize"),
        "download_url": download_url, 
        "webui": webui_url, 
        "version": detail_data.get("version", {})
    }


# ── 🔄 자식의 자식까지 끝단 레이어까지 추적하는 재귀적 크롤러 함수 ──
visited_pages = set()

def crawl_node_depth_first(page_id: str, output_dir: str):
    if page_id in visited_pages:
        return
    visited_pages.add(page_id)

    try:
        page = get_page_content(page_id)
        children = get_child_pages(page_id)
        attachments = get_attachments(page_id)

        result = {
            "page": {
                "id": page.get("id"), "title": page.get("title"), "type": page.get("type"), "status": page.get("status"),
                "space": page.get("space", {}), "version": page.get("version", {}),
                "ancestors": [{"id": a.get("id"), "title": a.get("title"), "type": a.get("type")} for a in page.get("ancestors", [])],
                "labels": page.get("metadata", {}).get("labels", {}).get("results", []),
                "body_storage_html": page.get("body", {}).get("storage", {}).get("value"),
                "body_view_html": page.get("body", {}).get("view", {}).get("value"),
                "body_export_view_html": page.get("body", {}).get("export_view", {}).get("value"),
            },
            "children": [
                {"id": c.get("id"), "title": c.get("title"), "type": c.get("type"), "status": c.get("status"), "space": c.get("space", {}), "version": c.get("version", {}), "webui": urljoin(BASE_URL, c.get("_links", {}).get("webui", ""))}
                for c in children
            ],
            "attachments": [normalize_attachment(att) for att in attachments if normalize_attachment(att) is not None]
        }

        title_context = page.get('title', 'untitled').replace(' ', '_').replace('/', '_')
        safe_title = "".join([c if c.isalnum() or c in ("_", "-") else "_" for c in title_context])
        file_path = os.path.join(output_dir, f"confluence_page_{safe_title}.json")

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        print(f"--- 📥 [다운로드 완료] ID: {page_id} -> '{result['page']['title']}' ---")
        print(f"    └─ 직속 하위 자식문서 개수: {len(children)}개 | 첨부파일 개수: {len(result['attachments'])}개")

        # API 결과를 바탕으로 다시 자식 노드로 파고들어 다운로드 진행
        for child in children:
            child_id = child.get("id")
            if child_id:
                crawl_node_depth_first(child_id, output_dir)

    except Exception as e:
        print(f"❌ PAGE_ID {page_id} 트리 동기화 중 에러 발생: {e}")


def main():
    TARGET_HTML_URL = "https://connecteve-prod.atlassian.net/wiki/spaces/CW/overview" 
    output_dir = "Data/PolicyPage"
    os.makedirs(output_dir, exist_ok=True)

    print(f"🌐 원격 URL로부터 HTML 소스 가져오는 중... -> {TARGET_HTML_URL}")
    try:
        response = session.get(TARGET_HTML_URL, timeout=30)
        response.raise_for_status()
        content = response.text
        print(content)
        print(f"✅ HTML 데이터 수집 완료 (문자열 크기: {len(content)})")
    except Exception as e:
        print(f"❌ URL 소스 획득 실패: {e}")
        return

    # 💡 [핵심 보완 기믹]: 대문 페이지 내 모든 정규식 패턴 매칭 파트
    data_content_ids = re.findall(r'data-contentid="(\d+)"', content)
    url_path_ids = re.findall(r'/pages/(\d+)', content)
    
    # 요청하신 span id 타겟팅 정규 표현식 결합 패턴 적용
    span_id_pattern = r'<span\s+[^>]*id=["\' ](\d{7,10})["\' ][^>]*>'
    span_ids = re.findall(span_id_pattern, content)

    # 3가지 소스에서 나온 모든 추출 파일 ID 리스트 결합
    all_extracted_ids = data_content_ids + url_path_ids + span_ids
    print(f"📊 [파싱 스펙 정보] data-contentid: {len(data_content_ids)}개 | /pages/: {len(url_path_ids)}개 | span id: {len(span_ids)}개")

    if not all_extracted_ids:
        print("⚠️ HTML 내부에서 유효한 Confluence 고유 문서 ID 패턴을 포착하지 못했습니다.")
        return

    # 중복 제거 및 시작 기둥(Seed) 확정
    unique_seeds = list(dict.fromkeys(all_extracted_ids))
    print(f"🎯 총 {len(unique_seeds)}개의 고유 페이지 ID를 기반으로 트리 탐색을 개시합니다.")

    print("\n==============================================")
    print("▶️ 계층 구조 깊은 노드까지 원격 API 기반 정밀 동기화 시작")
    print("==============================================")

    for seed_id in unique_seeds:
        crawl_node_depth_first(seed_id, output_dir)

    print("\n==============================================")
    print(f"🏁 [성공 완료] 자식의 자식 문서까지 총 {len(visited_pages)}개의 위키 페이지 일괄 수집 완료!")
    print("==============================================")


if __name__ == "__main__":
    main()