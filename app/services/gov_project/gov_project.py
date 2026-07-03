"""
정부과제 통합 서비스 (ConnBot/services/gov_project.py)

- 일일 파이프라인: Collect → Filter → Archive → Analyze → Serve
- ConnBot 연동: latest_gov.json 조회, query_gov_projects tool, /gov_files 서빙

CLI:
  python -m services.gov_project
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

_CONN_BOT_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _CONN_BOT_DIR.parent
for _path in (_CONN_BOT_DIR, _REPO_ROOT):
    _s = str(_path)
    if _s not in sys.path:
        sys.path.insert(0, _s)

import pandas as pd
import requests
from bs4 import BeautifulSoup

from app.core.config import (
    GOV_PROJECTS_DAILY_DIR,
    GOV_PROJECTS_LATEST_INDEX,
    PROJECT_ROOT,
    build_gov_file_public_url,
)


# =============================================================================
# ConnBot — 브리핑 조회 / 파일 URL / latest_gov.json
# =============================================================================
WIKI_REDIRECT_HINT = (
    "이 질문은 일일 브리핑에 수록된 외부 지원사업 공고가 아닐 수 있습니다. "
    "정부과제 현황·사내 운영 절차·전자연구노트·내부 신청/정산 방법은 "
    "Confluence 사내 위키(search_company_wiki)에서 확인하세요."
)


@dataclass
class GovQueryResult:
    """query_gov_projects tool 실행 결과."""

    content: str
    attachments: list[dict[str, Any]] = field(default_factory=list)
    matched_count: int = 0
    target_date: str = ""
    suggest_wiki: bool = False


def _resolve_path(raw: str) -> Path:
    p = Path(raw)
    if not p.is_absolute():
        p = (PROJECT_ROOT / p).resolve()
    else:
        p = p.resolve()
    return p


GOV_QUERY_KEYWORDS: tuple[str, ...] = (
    "정부",
    "과제",
    "지원사업",
    "지원금",
    "공고",
    "브리핑",
    "바우처",
    "수혜",
    "R&D",
    "연구개발",
    "지원 프로그램",
    "지원프로그램",
)


def match_gov_in_query(query: str) -> bool:
    """질문에 정부과제 브리핑 관련 키워드가 포함됐는지 확인한다."""
    q = (query or "").strip()
    if not q:
        return False
    return any(keyword in q for keyword in GOV_QUERY_KEYWORDS)


def _catalog_lines(notices: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for n in notices:
        idx = n.get("idx")
        title = str(n.get("공고명") or "").strip()
        period = str(n.get("접수기간") or n.get("공고_기간") or "").strip()
        suffix = f" (접수: {period})" if period else ""
        lines.append(f"- [{idx}] {title}{suffix}")
    return lines


# Turn1 catalog 전체 주입 상한 — 초과 시 compact(lazy) 블록만 주입
GOV_CATALOG_TURN1_FULL_MAX = 6


def _gov_catalog_header_lines(target_date: str) -> list[str]:
    return [
        f'<gov_briefing_catalog date="{target_date}">',
        "query_gov_projects는 아래 일일 브리핑에 수록된 외부 지원사업 공고에만 사용한다.",
        "정부과제 현황·사내 운영·전자연구노트·내부 신청/정산 절차 → search_company_wiki.",
        "신청양식·첨부·지원서류 파일 → action=files, idx는 아래 [idx] 사용(브리핑 1.2. 순번 아님).",
        "",
    ]


def build_gov_briefing_catalog_block(*, force_lazy: bool = False) -> str:
    """Router Turn1 system prompt용 — 브리핑 수록 공고 카탈로그 (공고 많으면 lazy)."""
    index = load_latest_gov_index()
    if not index:
        return ""

    target_date = str(index.get("target_date") or "")
    notices: list[dict[str, Any]] = list(index.get("notices") or [])
    use_lazy = force_lazy or len(notices) > GOV_CATALOG_TURN1_FULL_MAX

    lines = _gov_catalog_header_lines(target_date)
    if use_lazy:
        lines.append(
            f"브리핑 수록 공고 {len(notices)}건 — 목록·idx는 query_gov_projects action=list 결과를 사용한다."
        )
        lines.append("detail/files 호출 시 keyword 또는 list·이전 대화의 idx를 사용한다.")
    else:
        lines.extend(_catalog_lines(notices) or ["- (현재 수록 공고 없음)"])
    lines.append("</gov_briefing_catalog>")
    return "\n".join(lines)


def load_latest_gov_index() -> dict[str, Any] | None:
    """Data/government_projects/latest_gov.json 로드."""
    path = GOV_PROJECTS_LATEST_INDEX
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def find_archive_folder(target_date: str, idx: int) -> Path | None:
    """03_archive/{idx:03d}_* 폴더 경로."""
    archive_root = GOV_PROJECTS_DAILY_DIR / target_date / "03_archive"
    if not archive_root.is_dir():
        return None
    prefix3 = f"{idx:03d}_"
    prefix_plain = f"{idx}_"
    for name in sorted(os.listdir(archive_root)):
        if name.startswith(prefix3) or name.startswith(prefix_plain):
            folder = archive_root / name
            if folder.is_dir():
                return folder
    return None


def resolve_gov_file_on_disk(target_date: str, idx: int, filename: str) -> Path | None:
    """
    /gov_files/{date}/{idx}/{filename} 요청을 실제 파일 경로로 해석.
    filename은 공고문_및_요약·신청양식_및_참고자료 하위 파일명(또는 상대경로)만 허용.
    """
    folder = find_archive_folder(target_date, idx)
    if not folder:
        return None

    raw = (filename or "").strip().replace("\\", "/")
    if not raw or ".." in raw.split("/"):
        return None

    base_name = Path(raw).name
    if not base_name:
        return None

    candidates: list[Path] = []
    for sub in ("공고문_및_요약", "신청양식_및_참고자료"):
        sub_dir = folder / sub
        if not sub_dir.is_dir():
            continue
        direct = sub_dir / base_name
        if direct.is_file():
            candidates.append(direct.resolve())
        for root, _, files in os.walk(sub_dir):
            for fn in files:
                if fn == base_name:
                    candidates.append(Path(root, fn).resolve())

    if not candidates:
        return None

    # 아카이브 폴더 밖 탈출 방지
    archive_resolved = folder.resolve()
    for c in candidates:
        try:
            c.relative_to(archive_resolved)
            if c.is_file():
                return c
        except ValueError:
            continue
    return None


def _load_summary_text(archive_dir: Path) -> str:
    summary_path = archive_dir / "공고문_및_요약" / "공고안내_상세요약.txt"
    if not summary_path.is_file():
        return ""
    try:
        return summary_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _basic_info_block(notice: dict[str, Any]) -> str:
    lines = [
        f"idx: {notice.get('idx')}",
        f"공고명: {notice.get('공고명', '')}",
        f"접수기간: {notice.get('접수기간') or notice.get('공고_기간', '')}",
        f"출처: {notice.get('출처', '')}",
        f"최종적합도: {notice.get('최종적합도', '')}",
        f"지원규모: {notice.get('지원규모', '')}",
        f"지원대상: {notice.get('지원대상', '')}",
        f"적합사유: {notice.get('적합사유', '')}",
        f"상세링크: {notice.get('상세링크', '')}",
    ]
    return "\n".join(lines)


def _match_notices(
    notices: list[dict[str, Any]],
    *,
    idx: int | None,
    keyword: str,
) -> list[dict[str, Any]]:
    if idx is not None:
        matched = [n for n in notices if n.get("idx") == idx]
        if matched:
            return matched

    kw = (keyword or "").strip().casefold()
    if not kw:
        return list(notices)

    scored: list[tuple[int, dict[str, Any]]] = []
    for n in notices:
        title = str(n.get("공고명") or "").casefold()
        if kw in title:
            scored.append((0 if title == kw else 1, n))
    scored.sort(key=lambda x: x[0])
    return [n for _, n in scored]


_ARCHIVE_SCAN_SKIP_NAMES = frozenset({
    "공고안내_상세요약.txt",
    "meta.json",
})


def _resolve_archive_dir_for_notice(
    notice: dict[str, Any],
    *,
    target_date: str,
) -> Path | None:
    """notice 메타 또는 날짜+idx로 아카이브 폴더를 찾습니다."""
    raw = str(notice.get("아카이브경로") or "").strip()
    if raw:
        path = _resolve_path(raw)
        if path.is_dir():
            return path

    idx = notice.get("idx")
    if target_date and idx is not None:
        return find_archive_folder(target_date, int(idx))
    return None


def _scan_archive_category_files(
    archive_dir: Path,
    category: str,
) -> list[Path]:
    """인덱스 메타 없을 때 디스크에서 공고문·신청양식 파일을 스캔합니다."""
    sub_name = "공고문_및_요약" if category == "공고문" else "신청양식_및_참고자료"
    sub_dir = archive_dir / sub_name
    if not sub_dir.is_dir():
        return []

    found: list[Path] = []
    for root, _, files in os.walk(sub_dir):
        for fn in files:
            if fn in _ARCHIVE_SCAN_SKIP_NAMES:
                continue
            path = Path(root, fn)
            if path.is_file():
                found.append(path.resolve())
    return sorted(found, key=lambda p: p.name.casefold())


def _collect_notice_file_paths(
    notice: dict[str, Any],
    *,
    target_date: str,
    category: str,
) -> list[Path]:
    """latest_gov 메타 경로 우선, 없으면 아카이브 폴더 디스크 스캔."""
    key = "공고문_파일" if category == "공고문" else "신청양식_파일"
    resolved: list[Path] = []
    seen: set[str] = set()

    for raw in notice.get(key) or []:
        path = _resolve_path(str(raw))
        if not path.is_file():
            continue
        key_id = str(path.resolve())
        if key_id in seen:
            continue
        seen.add(key_id)
        resolved.append(path.resolve())

    if resolved:
        return resolved

    archive_dir = _resolve_archive_dir_for_notice(notice, target_date=target_date)
    if not archive_dir:
        return []

    for path in _scan_archive_category_files(archive_dir, category):
        key_id = str(path)
        if key_id in seen:
            continue
        seen.add(key_id)
        resolved.append(path)
    return resolved


def _file_entries_for_notice(
    notice: dict[str, Any],
    *,
    target_date: str,
    file_category: str,
) -> list[dict[str, Any]]:
    idx = int(notice.get("idx") or 0)
    cat = (file_category or "전체").strip()
    entries: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    def _add(paths: list[Path], category: str) -> None:
        for path in paths:
            name = path.name
            url = build_gov_file_public_url(target_date, idx, name)
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            entries.append({
                "title": name,
                "download_url": url,
                "category": category,
                "notice_idx": idx,
                "notice_name": notice.get("공고명", ""),
            })

    if cat in ("전체", "공고문"):
        _add(
            _collect_notice_file_paths(
                notice, target_date=target_date, category="공고문"
            ),
            "공고문",
        )
    if cat in ("전체", "신청양식"):
        _add(
            _collect_notice_file_paths(
                notice, target_date=target_date, category="신청양식"
            ),
            "신청양식",
        )

    return entries


def query_gov_projects(
    *,
    action: str,
    idx: int | None = None,
    keyword: str = "",
    file_category: str = "전체",
) -> GovQueryResult:
    """query_gov_projects tool 본문."""
    index = load_latest_gov_index()
    if not index:
        return GovQueryResult(
            content="정부과제 브리핑 데이터가 없습니다. 파이프라인이 아직 실행되지 않았을 수 있습니다.",
            matched_count=0,
        )

    target_date = str(index.get("target_date") or "")
    notices: list[dict[str, Any]] = list(index.get("notices") or [])
    act = (action or "list").strip().casefold()

    if act == "list":
        briefing = str(index.get("briefing_md") or "").strip()
        lines = [
            f"[정부과제 브리핑] 날짜={target_date}, 적합 공고 {len(notices)}건",
            "",
        ]
        if briefing:
            lines.append(briefing)
        else:
            for n in notices:
                lines.append(_basic_info_block(n))
                lines.append("")
        return GovQueryResult(
            content="\n".join(lines).strip(),
            matched_count=len(notices),
            target_date=target_date,
        )

    matched = _match_notices(notices, idx=idx, keyword=keyword)
    if not matched:
        hint = f"idx={idx}" if idx is not None else f"keyword={keyword!r}"
        catalog = "\n".join(_catalog_lines(notices)) or "- (수록 공고 없음)"
        return GovQueryResult(
            content=(
                f"브리핑에서 해당 공고를 찾지 못했습니다. ({hint})\n\n"
                f"[현재 브리핑 수록 공고]\n{catalog}\n\n"
                f"{WIKI_REDIRECT_HINT}"
            ),
            matched_count=0,
            target_date=target_date,
            suggest_wiki=True,
        )

    if act == "files":
        all_attachments: list[dict[str, Any]] = []
        parts: list[str] = [f"[정부과제 첨부파일] 날짜={target_date}"]
        for notice in matched:
            entries = _file_entries_for_notice(
                notice,
                target_date=target_date,
                file_category=file_category,
            )
            all_attachments.extend(entries)
            parts.append(_basic_info_block(notice))
            parts.append("")
            if entries:
                parts.append("[첨부파일]")
                for i, e in enumerate(entries, start=1):
                    parts.append(
                        f"첨부자료 {i} ({e['category']}): {e['title']} → {e['download_url']}"
                    )
            else:
                parts.append("(다운로드 가능한 첨부파일 없음 — 아직 수집 중이거나 해당 유형 파일 없음)")
            parts.append("")

        return GovQueryResult(
            content="\n".join(parts).strip(),
            attachments=all_attachments,
            matched_count=len(matched),
            target_date=target_date,
        )

    if act == "detail":
        parts: list[str] = [f"[정부과제 상세] 날짜={target_date}"]
        for notice in matched:
            parts.append("")
            parts.append("=== 기본 정보 ===")
            parts.append(_basic_info_block(notice))
            archive_raw = notice.get("아카이브경로") or ""
            archive_dir = _resolve_path(archive_raw) if archive_raw else None
            if archive_dir and archive_dir.is_dir():
                summary = _load_summary_text(archive_dir)
            elif target_date and notice.get("idx"):
                folder = find_archive_folder(target_date, int(notice["idx"]))
                summary = _load_summary_text(folder) if folder else ""
            else:
                summary = ""
            parts.append("")
            parts.append("=== 공고안내_상세요약 (전문) ===")
            parts.append(summary or "(상세요약 파일 없음)")
        return GovQueryResult(
            content="\n".join(parts).strip(),
            matched_count=len(matched),
            target_date=target_date,
        )

    return GovQueryResult(
        content=f"지원하지 않는 action입니다: {action!r}. list/detail/files 중 하나를 사용하세요.",
        matched_count=0,
        target_date=target_date,
    )


def write_latest_gov_index(
    *,
    target_date: str,
    session_dir: str,
    qualified_screens: list[dict[str, Any]],
    archive_metas: list[dict[str, Any]],
    briefing_md: str,
) -> Path:
    """파이프라인 종료 시 latest_gov.json 갱신."""
    meta_by_idx = {m.get("idx"): m for m in archive_metas if m.get("idx") is not None}
    notices: list[dict[str, Any]] = []
    for q in qualified_screens:
        q_idx = q.get("idx")
        meta = meta_by_idx.get(q_idx, {})
        notices.append({
            "idx": q_idx,
            "공고명": q.get("공고명") or meta.get("공고명", ""),
            "접수기간": q.get("접수기간") or meta.get("공고_기간", ""),
            "출처": meta.get("출처", ""),
            "최종적합도": q.get("최종적합도", ""),
            "지원규모": q.get("지원규모", ""),
            "지원대상": q.get("지원대상", ""),
            "적합사유": q.get("적합사유", ""),
            "상세링크": q.get("상세링크") or meta.get("상세링크", ""),
            "아카이브경로": meta.get("아카이브경로", ""),
            "공고문_파일": meta.get("공고문_파일") or [],
            "신청양식_파일": meta.get("신청양식_파일") or [],
        })

    payload = {
        "target_date": target_date,
        "session_dir": session_dir,
        "generated_at": __import__("datetime").datetime.now().isoformat(),
        "briefing_md": briefing_md,
        "qualified_count": len(notices),
        "notices": notices,
    }
    path = GOV_PROJECTS_LATEST_INDEX
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path
# =============================================================================
# 일일 파이프라인 — Collect / Filter / Archive / Analyze / Serve
# =============================================================================
# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

DEFAULT_OUTPUT_ROOT = str(GOV_PROJECTS_DAILY_DIR.relative_to(PROJECT_ROOT).as_posix())
REQUEST_DELAY = 0.5
DETAIL_DELAY = 1.5

WHITELIST_KEYWORDS = [
    "AI",
    "디지털 헬스",
    "디지털헬스",
    "의료기기",
    "소프트웨어",
    "임상",
    "바이오",
    "헬스케어",
    "SaMD",
    "의료",
    "헬스",
    "딥테크",
    "컴퓨팅",
]

BLACKLIST_KEYWORDS = [
    "소상공인",
    "전통시장",
    "뿌리산업",
    "소매업",
    "푸드",
    "농어촌",
    "로컬크리에이터",
    "산림인",
]

NOTICE_FILE_HINTS = ("공고문", "공고", "모집공고", "notice", "요령", "안내")
SKIP_LINK_TEXTS = {"바로보기", "다운로드", "새창열림", "보기"}

COMPANY_PROFILE = {
    "name": "코넥티브(Connecteve)",
    "location": "서울 소재",
    "years_in_business": "7년 미만",
    "domain": "정형외과 AI SaMD(의료기기 소프트웨어) 개발",
    "strengths": [
        "AI 의료영상 분석",
        "의료기기 소프트웨어(SaMD)",
        "디지털 헬스케어",
        "수술 계획 소프트웨어",
        "수술 로봇 연동",
    ],
    "products": {
        "CONNEVO KOA": (
            "무릎 X-ray를 분석해 퇴행성관절염 유무·진행도를 KL Grade 기준으로 판별"
        ),
        "CONNEVO ALI/METRIC": (
            "하지 X-ray를 분석해 valgus/varus(변형), 다리 길이, HKAA를 측정"
        ),
        "CONNEVO R": (
            "무릎 임플란트 수술 로봇 시스템, AI 기반 수술 계획 SW와 연동"
        ),
        "CONNEVO ASYST": (
            "무릎 수술 중 motor 기반 다리 위치 조절 장치, foot switch 등으로 각도 제어"
        ),
    },
    "internal_references": [
        "과제 위키",
        "회사 소개 자료",
        "실적·수행 현황 자료",
    ],
}

SCREEN_SYSTEM_PROMPT = f"""당신은 코넥티브의 정부지원사업 2차 스크리너입니다.
제공된 공고안내_상세요약만 철저한 근거로 판단하고, 코드블록이나 지적 설명 없이 오직 JSON 배열만 출력합니다.

## 코넥티브 프로필
{COMPANY_PROFILE}

## 판단 우선순위 및 규칙

"1단계(적격 판단)를 최우선 검토하며, 1단계 통과 공고에 한해서만 2단계(최종적합도)를 평가한다."

### 1단계: 지원대상_적격 (직접 신청 자격 — 엄격)
"본문의 [신청대상], [사업개요] 등에서 자격을 추출하여 코넥티브(서울 / 창업 5년차 / AI·로봇·의료기기 SW)가 '주관·단독'으로 직접 지원 가능한지 대조한다."

■ false (탈락 — 하나라도 해당 시 즉시 false 처리 후 JSON 제외)
- [지역 제한]: 서울 외 소관부처·지자체가 특정 시·도이고, 지원내용이 해당 권역 클러스터·인프라·전시 중심일 떄
- [업력·유형]: 업력 7년 초과 기업, 예비창업자·소상공인·전통시장·로컬크리에이터 전용일 때

■ true (통과 — 아래를 모두 만족)
- 코넥티브 스펙으로 주관·단독 신청이 가능하고, 서울/수도권/전국 대상일 때

■ 필수 출력 규칙
- `지원대상` 필드: 주관·신청 주체의 핵심 자격을 1줄 인용·요약 (40자 이내)
- 탈락 시 `지원대상_적격=false`로 판단하며, 이후 단계(최종적합도) 검토 없이 즉시 JSON 배열에서 제외

---

### 2단계: 최종적합도 (1단계 true인 공고만)
- 높음: 자격 충족 + 코넥티브 핵심(AI, GPU, 로봇, 딥테크, 의료기기 SW, 임상·인허가, 서울바이오허브)에 직결
- 보통: 자격 충족 + 도메인 직접성이 다소 약하거나 지원 규모가 미비함
- 낮음 (★출력 제외★): 아래 오판 방지 기준에 해당하면 무조건 제외
  * [오판1] 하드웨어/생체 소재 중심 비임상 및 시험평가
  * [오판2] 펫케어, 기후테크, 탄소중립, 신약/백신 개발 등 도메인 불일치
  * [오판3] 단순 교육, 주거, 네트워킹·전시회, 입주 지원, 비의료 판로 개척

---

### 공통 규칙
- idx 규칙: 입력 헤더 `### [N]`의 N을 정수형태로 유지 (중간 공고가 탈락해도 재번호하지 말 것)
- 출력 규칙: 1단계 true이면서 최종적합도가 '보통' 또는 '높음'인 BEST 공고만 JSON 배열에 포함. 만족 공고가 없으면 `[]` 반환.

## 중복 공고 제거 (필수)
- **기업마당·K-Startup 등 동일 사업·동일 모집**이 서로 다른 idx로 함께 입력된 경우, **실질적으로 같은 프로그램은 1건만** JSON 배열에 포함하십시오.
- 판별: 공고명 핵심(주관·사업명·연도·지역), 접수기간, 지원내용이 동일·유사하면 중복으로 간주.
- **유지 우선순위**: ① 기업마당 idx ② K-Startup idx. 중복된 나머지 idx는 배열에서 **완전 제외** (재번호 금지).

{{
  "idx": 1,
  "지원대상_적격": true,
  "최종적합도": "높음",
  "도메인": "높음|보통",
  "기술": "높음|보통",
  "지원규모": "지원금액·한도·크레딧 등 수치가 있으면 기재. 없으면 [지원내용] 핵심 혜택을 50자 이내 요약. 수치·혜택 모두 없을 때만 미확인",
  "지원대상": "주관·단독 신청 주체의 핵심 자격 요약 (40자 이내, 상세요약 인용)",
  "적합사유": "자격 충족 근거 + 코넥티브 실익 (80자 이내)"
}}"""

_BIZINFO_DROP_SECTIONS = frozenset({"사업신청 방법", "문의처"})
_KSTARTUP_DROP_SECTIONS = frozenset({"선정절차 및 평가방법", "문의처"})
_SUMMARY_DROP_HEADER_PREFIXES = ("▶ 출처", "▶ 원본링크", "▶ 수집시각", "▶ 공고명")

# ---------------------------------------------------------------------------
# Stage 1 — Collect
# ---------------------------------------------------------------------------
def crawl_bizinfo_table(start_page: int = 1, end_page: int = 5) -> pd.DataFrame:
    """기업마당 지원사업 공고 목록을 수집합니다."""
    base_url = "https://www.bizinfo.go.kr/sii/siia/selectSIIA200View.do"
    params = {
        "schJrsdCodeTy": "",
        "orderGb": "1",
        "preKeywords": "",
        "pblancId": "",
        "sort": "desc",
        "schPblancDiv": "",
        "schAreaDetailCodes": "",
        "schEndAt": "N",
        "condition": "searchPblancNm",
        "condition1": "AND",
        "rowsSel": "6",
        "schWntyAt": "",
        "hashCode": "",
        "cat": "",
        "keyword": "",
        "rows": "15",
        "cpage": "1",
    }

    all_data: list[dict[str, str]] = []
    print(f"[1·Collect] 기업마당 {start_page}~{end_page}페이지")

    for page in range(start_page, end_page + 1):
        params["cpage"] = str(page)
        try:
            response = requests.get(
                base_url, params=params, headers=HEADERS, timeout=15
            )
            if response.status_code != 200:
                print(f"  [오류] {page}페이지 status={response.status_code}")
                continue

            soup = BeautifulSoup(response.text, "html.parser")
            tbody = soup.find("tbody")
            if not tbody:
                continue

            for row in tbody.find_all("tr"):
                cols = row.find_all("td")
                if len(cols) < 7:
                    continue

                title_tag = cols[2].find("a")
                title = (
                    title_tag.text.strip()
                    if title_tag
                    else cols[2].text.strip()
                )
                detail_link = title_tag["href"].strip() if title_tag else ""
                if detail_link.startswith("/"):
                    detail_link = "https://www.bizinfo.go.kr" + detail_link

                all_data.append(
                    {
                        "출처": "기업마당",
                        "번호": cols[0].text.strip(),
                        "구분": cols[1].text.strip(),
                        "공고명": title,
                        "접수기간": cols[3].text.strip(),
                        "소관부처": cols[4].text.strip(),
                        "수행기관": cols[5].text.strip(),
                        "등록일": cols[6].text.strip(),
                        "조회수": cols[7].text.strip() if len(cols) > 7 else "",
                        "상세링크": detail_link,
                    }
                )
            time.sleep(REQUEST_DELAY)
        except Exception as e:
            print(f"  [예외] {page}페이지: {e}")

    print(f"  → {len(all_data)}건")
    return pd.DataFrame(all_data)


def crawl_kstartup_list(start_page: int = 1, end_page: int = 5) -> pd.DataFrame:
    """K-Startup 진행 중 공고 목록을 수집합니다."""
    list_url = "https://www.k-startup.go.kr/web/contents/bizpbanc-ongoing.do"
    params = {"pbancClssCd": "PBC010", "page": "1"}

    all_data: list[dict[str, str]] = []
    print(f"[1·Collect] K-Startup {start_page}~{end_page}페이지")

    for page in range(start_page, end_page + 1):
        params["page"] = str(page)
        try:
            response = requests.get(
                list_url, params=params, headers=HEADERS, timeout=15
            )
            if response.status_code != 200:
                print(f"  [오류] {page}페이지 status={response.status_code}")
                continue

            soup = BeautifulSoup(response.text, "html.parser")
            items = soup.find_all("li", class_=["notice", "normal"])
            if not items:
                ul_tag = soup.find("ul", class_="notice_list") or soup.find("ul")
                if ul_tag:
                    items = ul_tag.find_all("li", recursive=False)
            if not items:
                continue

            for item in items:
                tit_tag = item.find("p", class_="tit")
                if not tit_tag:
                    continue

                flag_tag = item.find("span", class_=lambda x: x and "type" in x)
                dday_tag = item.find("span", class_="day")
                agency_tag = item.find("span", class_="flag_agency")

                detail_link = ""
                a_tag = item.find("a", href=True)
                if a_tag and "go_view" in a_tag["href"]:
                    match = re.search(r"\d+", a_tag["href"])
                    if match:
                        detail_link = (
                            f"{list_url}?schM=view&pbancSn={match.group()}"
                            f"&page={page}&pbancClssCd=PBC010"
                        )

                sub_title = inst_name = reg_date = start_date = end_date = views = ""
                bottom = item.find("div", class_="bottom")
                if bottom:
                    for span in bottom.find_all("span", class_="list"):
                        text = span.text.strip()
                        if "등록일자" in text:
                            reg_date = text.replace("등록일자", "").strip()
                        elif "시작일자" in text:
                            start_date = text.replace("시작일자", "").strip()
                        elif "마감일자" in text:
                            end_date = text.replace("마감일자", "").strip()
                        elif "조회" in text:
                            views = text.replace("조회", "").strip()
                        elif not sub_title:
                            sub_title = text
                        else:
                            inst_name = text

                all_data.append(
                    {
                        "출처": "K-Startup",
                        "구분": flag_tag.text.strip() if flag_tag else "",
                        "기관분류": agency_tag.text.strip() if agency_tag else "",
                        "디데이": dday_tag.text.strip() if dday_tag else "",
                        "공고명": tit_tag.text.strip(),
                        "부제목": sub_title,
                        "수행기관": inst_name,
                        "등록일자": reg_date,
                        "시작일자": start_date,
                        "마감일자": end_date,
                        "조회수": views,
                        "상세링크": detail_link,
                    }
                )
            time.sleep(REQUEST_DELAY)
        except Exception as e:
            print(f"  [예외] {page}페이지: {e}")

    print(f"  → {len(all_data)}건")
    return pd.DataFrame(all_data)


def _normalize_date(value: str) -> str:
    return re.sub(r"[./]", "-", str(value).strip())[:10]


def _meta_str(value: Any) -> str:
    """DataFrame/ dict 값을 표시용 문자열로 변환 (NaN·빈값 제거)."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return "" if not text or text.lower() == "nan" else text


def _add_display_columns(df: pd.DataFrame) -> pd.DataFrame:
    """기관·기간 표시 컬럼을 한 번만 채웁니다."""
    out = df.copy()
    out["공고_기관"] = out.apply(
        lambda r: _meta_str(r.get("소관부처")) or _meta_str(r.get("수행기관")),
        axis=1,
    )
    out["공고_기간"] = out.apply(
        lambda r: _meta_str(r.get("접수기간"))
        or " ~ ".join(
            x for x in (_meta_str(r.get("시작일자")), _meta_str(r.get("마감일자"))) if x
        ),
        axis=1,
    )
    return out


def collect_daily(
    target_date: str | None = None,
    start_page: int = 1,
    end_page: int = 5,
) -> pd.DataFrame:
    """당일 등록 공고만 합쳐 df_daily를 생성합니다."""
    target_date = target_date or datetime.now().strftime("%Y-%m-%d")
    print(f"[1·Collect] 대상일: {target_date}")

    df_biz = crawl_bizinfo_table(start_page, end_page)
    df_ks = crawl_kstartup_list(start_page, end_page)

    frames = [df for df in (df_biz, df_ks) if not df.empty]
    if not frames:
        return pd.DataFrame()

    df_all = pd.concat(frames, ignore_index=True, sort=False)
    df_all["등록일_원본"] = df_all["등록일"].combine_first(df_all["등록일자"])
    df_all["등록일_정규화"] = df_all["등록일_원본"].map(_normalize_date)
    df_all = _add_display_columns(df_all)

    df_daily = df_all[df_all["등록일_정규화"] == target_date].copy()
    df_daily.reset_index(drop=True, inplace=True)
    print(f"[1·Collect] 당일 공고 {len(df_daily)}건 / 전체 {len(df_all)}건")
    return df_daily


# ---------------------------------------------------------------------------
# Stage 2 — Filter
# ---------------------------------------------------------------------------
def filter_by_keywords(df: pd.DataFrame) -> pd.DataFrame:
    """목록 단계 1차 컷오프: 화이트리스트 포함 + 블랙리스트 제외."""
    if df.empty:
        return df.copy()

    titles = df["공고명"].fillna("").str.lower()
    whitelist = "|".join(re.escape(k) for k in WHITELIST_KEYWORDS)
    blacklist = "|".join(re.escape(k) for k in BLACKLIST_KEYWORDS)

    match_white = titles.str.contains(whitelist, case=False, regex=True)
    match_black = titles.str.contains(blacklist, case=False, regex=True)

    filtered = df[match_white & ~match_black].copy().reset_index(drop=True)
    print(f"[2·Filter] {len(df)}건 → {len(filtered)}건")
    return filtered


# ---------------------------------------------------------------------------
# Stage 3 — Archive (첨부파일 다운로드 수정)
# ---------------------------------------------------------------------------
def _sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
    return name or "unnamed_file"


def _parse_content_disposition(header: str) -> str:
    if not header:
        return ""
    match = re.search(r"filename\*=UTF-8''(.+?)(?:;|$)", header, re.I)
    if match:
        return unquote(match.group(1))
    match = re.search(r"filename\*=([^;]+)", header, re.I)
    if match:
        return unquote(match.group(1).split("''")[-1])
    match = re.search(r'filename="?([^";]+)"?', header, re.I)
    return unquote(match.group(1)) if match else ""


def _fix_mojibake(name: str) -> str:
    """서버가 latin-1로 전달한 한글 파일명을 복원합니다."""
    if not name or "%" in name:
        return name
    for encoding in ("utf-8", "cp949"):
        try:
            fixed = name.encode("latin-1").decode(encoding)
            if fixed != name:
                return fixed
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
    return name


def _guess_extension(content_type: str) -> str:
    if "pdf" in content_type:
        return ".pdf"
    if "hwp" in content_type:
        return ".hwp"
    if "spreadsheet" in content_type or "excel" in content_type:
        return ".xlsx"
    if "zip" in content_type:
        return ".zip"
    if "jpeg" in content_type or "jpg" in content_type:
        return ".jpg"
    if "text/plain" in content_type:
        return ".txt"
    return ".bin"


def _resolve_filename(
    hint: str,
    content_disposition: str,
    content_type: str,
    url: str = "",
) -> str:
    """HTML 페이지명·URL인코딩 파일명 대신 실제 저장 가능한 이름을 결정합니다."""
    hint = _fix_mojibake(_sanitize_filename(hint))
    cd_name = _fix_mojibake(_sanitize_filename(_parse_content_disposition(content_disposition)))

    if hint and hint not in SKIP_LINK_TEXTS and "." in hint:
        filename = hint
    elif cd_name and cd_name not in SKIP_LINK_TEXTS:
        filename = cd_name
    elif hint and hint not in SKIP_LINK_TEXTS:
        filename = hint
    else:
        ext = _guess_extension(content_type)
        digest = hashlib.md5(url.encode()).hexdigest()[:8] if url else str(int(time.time()))
        filename = f"attachment_{digest}{ext}"

    if len(filename) > 120:
        base, ext = os.path.splitext(filename)
        filename = base[:100] + ext
    return _sanitize_filename(filename)


def _is_notice_file(filename: str) -> bool:
    lower = filename.lower()
    if any(h in lower for h in NOTICE_FILE_HINTS):
        return True
    return lower.endswith(".pdf")


def _extract_bizinfo_attachments(soup: BeautifulSoup, detail_url: str) -> list[dict]:
    attachments = []
    for li in soup.select(".attached_file_list li"):
        name_div = li.find("div", class_="file_name")
        down_a = li.find("a", href=lambda h: h and "fileDown.do" in h)
        if not name_div or not down_a:
            continue
        href = down_a["href"]
        down_url = href if href.startswith("http") else f"https://www.bizinfo.go.kr{href}"
        fname = _sanitize_filename(name_div.text.strip())
        attachments.append(
            {
                "url": down_url,
                "filename": fname,
                "category": "notice" if _is_notice_file(fname) else "form",
                "referer": detail_url,
            }
        )
    return attachments


def _extract_kstartup_attachments(soup: BeautifulSoup, detail_url: str) -> list[dict]:
    attachments = []
    seen_urls: set[str] = set()
    
    board = soup.find("div", class_="board_file")
    if not board:
        return attachments

    # 💡 수정 1: 하위 버튼용 li까지 긁지 않도록, 최상위 ul 바로 밑의 li(class_="clear")만 탐색
    file_list = board.find("ul")
    if not file_list:
        return attachments
        
    items = file_list.find_all("li", class_="clear", recursive=False)
    
    for seq, item in enumerate(items, 1):
        down_a = item.find("a", href=lambda h: h and "/afile/fileDownload/" in h)
        if not down_a:
            continue

        href = down_a["href"]
        down_url = href if href.startswith("http") else f"https://www.k-startup.go.kr{href}"
        if down_url in seen_urls:
            continue
        seen_urls.add(down_url)

        fname = ""
        # 1순위: 파일명을 명시적으로 가지고 있는 a.file_bg 태그의 title 속성 및 text
        file_bg_a = item.find("a", class_="file_bg")
        if file_bg_a:
            # '[첨부파일] ' 문구가 붙어있을 경우를 대비해 title 우선 추출 후 정제
            raw_title = file_bg_a.get("title", "").replace("[첨부파일]", "").strip()
            fname = raw_title if raw_title else file_bg_a.get_text(strip=True)

        # 2순위: 기존 방어 로직 유지 (폴백용)
        if not fname or fname in SKIP_LINK_TEXTS:
            view_a = item.find("a", onclick=lambda x: x and "fnPdfView" in (x or ""))
            if view_a:
                fname = view_a.get("title", "").replace("새창열림", "").strip()
                
        if not fname or fname in SKIP_LINK_TEXTS:
            for sib in item.find_all(["span", "p", "div"]):
                text = sib.get_text(strip=True)
                if text and text not in SKIP_LINK_TEXTS and "." in text:
                    fname = text
                    break
                    
        if not fname or fname in SKIP_LINK_TEXTS:
            file_id = href.rstrip("/").split("/")[-1]
            fname = f"첨부파일_{seq}_{file_id}"

        attachments.append(
            {
                "url": down_url,
                "filename": _sanitize_filename(fname),
                "category": "notice" if _is_notice_file(fname) else "form",
                "referer": detail_url,
            }
        )
        
    return attachments


def _download_attachment(
    session: requests.Session,
    attachment: dict,
    save_dir: str,
) -> str | None:
    """세션·Referer를 유지하며 실제 파일을 저장합니다."""
    url = attachment["url"]
    referer = attachment.get("referer", "")
    hint = attachment.get("filename", "")

    try:
        res = session.get(
            url,
            headers={**HEADERS, "Referer": referer},
            stream=True,
            timeout=60,
        )
        if res.status_code != 200:
            print(f"        -> [실패] status={res.status_code} {hint or url}")
            return None

        content_type = res.headers.get("Content-Type", "")
        if "text/html" in content_type:
            print("        -> [실패] HTML 응답 (권한/세션 문제)")
            return None

        filename = _resolve_filename(
            hint,
            res.headers.get("Content-Disposition", ""),
            content_type,
            url=url,
        )
        # 실제 확장자 기준으로 공고문/양식 재분류에 활용
        attachment["resolved_filename"] = filename

        os.makedirs(save_dir, exist_ok=True)
        file_path = os.path.join(save_dir, filename)
        with open(file_path, "wb") as f:
            for chunk in res.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        size_kb = os.path.getsize(file_path) // 1024
        print(f"        -> 저장 완료 ({size_kb}KB)")
        return file_path
    except Exception as e:
        print(f"        -> [예외] 다운로드 실패: {e}")
        return None


def _extract_detail_text(soup: BeautifulSoup, source_url: str) -> str:
    parts: list[str] = []

    if "k-startup" in source_url:
        # 1. 상단 기본 정보 박스 추출 (bg_box)
        bg_box = soup.find("div", class_="bg_box")
        if bg_box:
            parts.append("[기본 정보]")
            for item in bg_box.find_all("li", class_="dot_list"):
                tit = item.find("p", class_="tit")
                txt = item.find("p", class_="txt")
                if tit:
                    tit_str = tit.text.strip()
                    txt_str = " ".join(txt.text.split()) if txt else ""
                    parts.append(f"- {tit_str}: {txt_str}")
        
        # 2. 중간 상세 정보 리스트 박스 추출 (information_list) - 요청하신 부분 반영
        info_lists = soup.find_all("div", class_="information_list")
        for info in info_lists:
            sec = info.find("p", class_="title")
            sec_title = sec.text.strip() if sec else "안내"
            parts.append(f"\n[{sec_title}]")
            
            for sub in info.find_all("li", class_="dot_list"):
                tit_p = sub.find("p", class_="tit")
                
                # 가변적인 본문 텍스트 영역 탐색 (div.txt -> p.txt -> div.list_wrap 순)
                txt_container = (
                    sub.find("div", class_="txt") or 
                    sub.find("p", class_="txt") or 
                    sub.find("div", class_="list_wrap")
                )
                
                if tit_p:
                    tit_text = tit_p.get_text(strip=True)
                    
                    if txt_container:
                        # 💡 온라인 접수 내부의 스크립트 기반 URL 주소 강제 파싱 및 텍스트화
                        links = []
                        for a_tag in txt_container.find_all("a", href=True):
                            href_val = a_tag["href"]
                            # 자바스크립트 새창 함수 내부의 실제 URL 문자열 추출 시도
                            if "fn_open_window" in href_val and "'" in href_val:
                                try:
                                    actual_url = href_val.split("'")[1]
                                    links.append(f"({actual_url})")
                                except IndexError:
                                    pass
                        
                        # 하위 태그(<br>, <p> 등) 간 줄바꿈을 유지하며 전체 텍스트 추출
                        txt_text = txt_container.get_text("\n", strip=True)
                        if links:
                            txt_text += f" " + " ".join(links)
                            
                        # 가독성을 위해 본문 내부 줄바꿈 시 인덴트 처리
                        indented_txt = txt_text.replace("\n", "\n  ")
                        parts.append(f"· {tit_text}\n  {indented_txt}")
                    else:
                        # 💡 신청 유의사항처럼 tit_p만 있고 서브 컨테이너가 없는 평평한 구조 대응
                        parts.append(f"· {tit_text}")
                        
        # 3. 하단 단순 상세 요약 박스 추출 (box) - 기존 보완 로직 유지
        box = soup.find("div", class_="box") or soup.find("div", class_="information_box-wrap")
        if box:
            # information_list와 중복 텍스트 축적을 막기 위한 방어선 (상세 리스트가 없을 때만 폴백 작동)
            if not info_lists:
                parts.append("\n[상세 안내]")
                detail_title = box.find("p", class_="tit_bl") or box.find("div", class_="title")
                if detail_title:
                    parts.append(f"📌 과제명: {detail_title.text.strip()}\n")
                detail_txt = box.find("p", class_="txt")
                if detail_txt:
                    parts.append(f"· 내용:\n  {detail_txt.get_text(strip=True)}")
                    
    else:
        view_cont = soup.find("div", class_="view_cont")
        if view_cont and view_cont.find("ul"):
            for li in view_cont.find("ul").find_all("li", recursive=False):
                s_title = li.find("span", class_="s_title")
                txt = li.find("div", class_="txt")
                if s_title and txt:
                    parts.append(f"[{s_title.text.strip()}]\n{txt.get_text(chr(10), strip=True)}")

    if not parts:
        fallback = soup.find("div", class_="view_cont") or soup.find("div", class_="box_inner")
        if fallback:
            parts.append(fallback.get_text("\n", strip=True))

    return "\n".join(parts)


def archive_announcement(
    url: str,
    title: str,
    idx: int,
    archive_root: str,
    source: str = "",
    collected_meta: dict[str, Any] | None = None,
    download_attachments: bool = True,
) -> dict[str, Any]:
    """상세 페이지 텍스트 추출 + 첨부파일 분류 아카이빙."""
    meta: dict[str, Any] = {
        "idx": idx,
        "공고명": title,
        "출처": source,
        "상세링크": url,
        "공고문_파일": [],
        "신청양식_파일": [],
        "오류": None,
    }

    if not url:
        meta["오류"] = "상세링크 없음"
        return meta

    if collected_meta:
        meta["공고_기관"] = _meta_str(collected_meta.get("공고_기관")) or "미확인"
        meta["공고_기간"] = _meta_str(collected_meta.get("공고_기간")) or "미확인"

    safe_title = _sanitize_filename(title)
    project_dir = os.path.join(archive_root, f"{idx:03d}_{safe_title}")
    doc_dir = os.path.join(project_dir, "공고문_및_요약")
    form_dir = os.path.join(project_dir, "신청양식_및_참고자료")
    os.makedirs(doc_dir, exist_ok=True)
    os.makedirs(form_dir, exist_ok=True)

    print(f"  [{idx:03d}] {title[:50]}")

    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        res = session.get(url, timeout=20)
        if res.status_code != 200:
            meta["오류"] = f"접근 실패 status={res.status_code}"
            return meta

        soup = BeautifulSoup(res.text, "html.parser")
        detail_text = _extract_detail_text(soup, url)

        # 2) 상세 텍스트에서 룰 기반으로 추가로 채울 수 있는 기본 필드들(LLM 없이)
        #    - 지원 대상: '지원대상', '신청자격' 섹션 등에서 추출 시도
        #    - 수행 기간: '수행기간', '사업기간' 등에서 추출 시도
        if detail_text:
            if not meta.get("지원_대상"):
                m = re.search(
                    r"(지원\s*대상|신청\s*자격)\s*[:\-]?\s*(.+)",
                    detail_text,
                )
                if m:
                    meta["지원_대상"] = m.group(2).strip()[:200]

            if not meta.get("수행_기간"):
                m = re.search(
                    r"(수행\s*기간|사업\s*기간)\s*[:\-]?\s*(.+)",
                    detail_text,
                )
                if m:
                    meta["수행_기간"] = m.group(2).strip()[:200]

        summary_lines = [
            f"▶ 공고명: {title}",
            f"▶ 출처: {source}",
            f"▶ 원본링크: {url}",
            f"▶ 수집시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "=" * 60,
            "",
            detail_text,
        ]
        summary_path = os.path.join(doc_dir, "공고안내_상세요약.txt")
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write("\n".join(summary_lines))

        if download_attachments:
            if "k-startup" in url:
                attachments = _extract_kstartup_attachments(soup, url)
            else:
                attachments = _extract_bizinfo_attachments(soup, url)

            print(f"    └ 첨부 {len(attachments)}건")
            for att in attachments:
                saved = _download_attachment(session, att, form_dir)
                if not saved:
                    time.sleep(0.3)
                    continue

                resolved = att.get("resolved_filename", os.path.basename(saved))
                is_notice = _is_notice_file(resolved) or saved.lower().endswith(".pdf")
                final_path = saved
                target_dir = doc_dir if is_notice else form_dir
                if os.path.dirname(saved) != target_dir:
                    os.makedirs(target_dir, exist_ok=True)
                    final_path = os.path.join(target_dir, os.path.basename(saved))
                    if final_path != saved:
                        os.replace(saved, final_path)

                key = "공고문_파일" if is_notice else "신청양식_파일"
                meta[key].append(final_path)
                time.sleep(0.3)

        meta["아카이브경로"] = project_dir
        with open(os.path.join(project_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    except Exception as e:
        meta["오류"] = str(e)
        print(f"    └ [예외] {e}")

    return meta


def archive_all(
    df: pd.DataFrame,
    archive_root: str,
    *,
    download_attachments: bool = False,
) -> list[dict[str, Any]]:
    if df.empty:
        print("[3·Archive] 대상 없음")
        return []

    os.makedirs(archive_root, exist_ok=True)
    print(f"[3·Archive] {len(df)}건 → {archive_root}")
    results = []

    for idx, row in df.iterrows():
        meta = archive_announcement(
            url=row.get("상세링크", ""),
            title=row.get("공고명", f"공고_{idx}"),
            idx=idx + 1,
            archive_root=archive_root,
            source=row.get("출처", ""),
            collected_meta=row.to_dict(),
            download_attachments=download_attachments,
        )
        results.append(meta)
        time.sleep(DETAIL_DELAY)

    print(f"[3·Archive] 완료 {len(results)}건")
    return results


# ---------------------------------------------------------------------------
# Attachment 다운로드(최종 필터 후)
# ---------------------------------------------------------------------------
def download_attachments_for_meta(
    meta: dict[str, Any],
    *,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """
    아카이브 폴더(`아카이브경로`)가 이미 존재한다는 전제에서,
    첨부파일만(양식/공고문) 다운로드합니다.
    """
    project_dir = meta.get("아카이브경로", "")
    if not project_dir or not os.path.isdir(project_dir):
        return meta

    url = meta.get("상세링크", "")
    if not url:
        return meta

    meta.setdefault("공고문_파일", [])
    meta.setdefault("신청양식_파일", [])
    # 이미 다운로드가 되어 있으면 재다운로드를 피합니다.
    if meta["공고문_파일"] and meta["신청양식_파일"]:
        return meta

    doc_dir = os.path.join(project_dir, "공고문_및_요약")
    form_dir = os.path.join(project_dir, "신청양식_및_참고자료")
    os.makedirs(doc_dir, exist_ok=True)
    os.makedirs(form_dir, exist_ok=True)

    local_session = session or requests.Session()
    local_session.headers.update(HEADERS)

    try:
        res = local_session.get(url, timeout=20)
        if res.status_code != 200:
            meta["오류"] = f"첨부 접근 실패 status={res.status_code}"
            return meta

        soup = BeautifulSoup(res.text, "html.parser")
        if "k-startup" in url:
            attachments = _extract_kstartup_attachments(soup, url)
        else:
            attachments = _extract_bizinfo_attachments(soup, url)

        print(f"  [Attachment] idx={meta.get('idx')} 첨부 {len(attachments)}건 다운로드")
        for att in attachments:
            saved = _download_attachment(local_session, att, form_dir)
            if not saved:
                time.sleep(0.3)
                continue

            resolved = att.get("resolved_filename", os.path.basename(saved))
            is_notice = _is_notice_file(resolved) or saved.lower().endswith(".pdf")
            final_path = saved
            target_dir = doc_dir if is_notice else form_dir
            if os.path.dirname(saved) != target_dir:
                os.makedirs(target_dir, exist_ok=True)
                final_path = os.path.join(target_dir, os.path.basename(saved))
                if final_path != saved:
                    os.replace(saved, final_path)

            key = "공고문_파일" if is_notice else "신청양식_파일"
            if final_path not in meta[key]:
                meta[key].append(final_path)
            time.sleep(0.3)

        # 업데이트된 meta.json 저장(챗봇 서빙용)
        with open(os.path.join(project_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    except Exception as e:
        meta["오류"] = f"첨부 다운로드 예외: {e}"

    return meta


# ---------------------------------------------------------------------------
# Stage 4 — Analyze
# ---------------------------------------------------------------------------
def _extract_pdf_text(file_path: str, max_chars: int = 12000) -> str:
    try:
        from parsers.pdf_parser import PDFParser

        result = PDFParser().parse(Path(file_path))
        return (result.text or "")[:max_chars]
    except Exception:
        try:
            import fitz

            doc = fitz.open(file_path)
            text = "\n".join(page.get_text() for page in doc)
            doc.close()
            return text[:max_chars]
        except Exception:
            return ""


def _consolidate_summary_text(raw: str, *, max_chars: int = 12000) -> str:
    """공고안내_상세요약.txt 본문을 정리합니다."""
    if not raw or not raw.strip():
        return ""

    lines: list[str] = []
    prev_blank = False
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            if not prev_blank and lines:
                lines.append("")
            prev_blank = True
            continue
        prev_blank = False
        lines.append(stripped)

    text = "\n".join(lines)
    text = re.sub(r"={5,}", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:max_chars]


def _summary_source_kind(meta: dict[str, Any]) -> str:
    source = _meta_str(meta.get("출처")).lower()
    url = _meta_str(meta.get("상세링크")).lower()
    if "k-startup" in source or "k-startup" in url:
        return "kstartup"
    if "기업마당" in source or "bizinfo" in url:
        return "bizinfo"
    return "unknown"


def _drop_bracket_sections(text: str, drop_titles: frozenset[str]) -> str:
    """[섹션명] 블록 단위로 제거합니다."""
    lines = text.splitlines()
    out: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        match = re.match(r"^\[([^\]]+)\]", stripped)
        if match:
            title = match.group(1).strip()
            skipping = title in drop_titles
            if skipping:
                continue
        if skipping:
            continue
        out.append(line)
    return "\n".join(out)


def _trim_summary_for_llm(raw: str, meta: dict[str, Any]) -> str:
    """LLM 입력용 — 메타 헤더·불필요 섹션 제거."""
    lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith(_SUMMARY_DROP_HEADER_PREFIXES):
            continue
        if re.fullmatch(r"=+", stripped):
            continue
        lines.append(line)

    text = "\n".join(lines)
    kind = _summary_source_kind(meta)
    if kind == "bizinfo":
        text = _drop_bracket_sections(text, _BIZINFO_DROP_SECTIONS)
    elif kind == "kstartup":
        text = _drop_bracket_sections(text, _KSTARTUP_DROP_SECTIONS)

    return text


def _load_detail_summary(meta: dict[str, Any], *, for_llm: bool = False) -> str:
    """아카이브의 공고안내_상세요약.txt를 읽어 반환합니다."""
    archive_dir = meta.get("아카이브경로", "")
    if not archive_dir:
        return ""

    summary_path = os.path.join(archive_dir, "공고문_및_요약", "공고안내_상세요약.txt")
    if not os.path.exists(summary_path):
        return ""

    with open(summary_path, encoding="utf-8") as f:
        raw = f.read()
    if for_llm:
        raw = _trim_summary_for_llm(raw, meta)
    return _consolidate_summary_text(raw)


def _call_llm(prompt: str, system: str = "") -> str:
    content, _ = _call_llm_detailed(prompt, system=system)
    return content


def _call_llm_detailed(
    prompt: str,
    system: str = "",
) -> tuple[str, dict[str, Any]]:
    """LLM 호출 + 디버깅용 메타(모델·토큰·에러) 반환."""
    model = os.getenv("OPENAI_ANALYSIS_MODEL", "gpt-5-mini")
    meta: dict[str, Any] = {
        "model": model,
        "system_chars": len(system),
        "prompt_chars": len(prompt),
        "usage": None,
        "error": None,
        "finish_reason": None,
    }
    openai_key = os.getenv("OPENAI_API_KEY", "")

    if not openai_key:
        meta["error"] = "OPENAI_API_KEY 미설정"
        print(f"  [Analyze] {meta['error']}")
        return "", meta

    try:
        from openai import OpenAI

        client = OpenAI(api_key=openai_key)
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = client.chat.completions.create(
            model=model,
            messages=messages
        )
        choice = resp.choices[0]
        meta["finish_reason"] = choice.finish_reason
        if resp.usage:
            meta["usage"] = {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
                "total_tokens": resp.usage.total_tokens,
            }
        return choice.message.content or "", meta
    except Exception as e:
        meta["error"] = str(e)
        print(f"  [Analyze] OpenAI 실패: {e}")
        return "", meta


def _save_llm_debug(
    debug_dir: str,
    *,
    tag: str,
    system: str,
    prompt: str,
    raw_response: str,
    llm_meta: dict[str, Any],
    parsed: Any = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """04_analyze/llm_debug 에 프롬프트·응답·파싱 결과를 저장합니다."""
    os.makedirs(debug_dir, exist_ok=True)
    prefix = os.path.join(debug_dir, tag)

    with open(f"{prefix}_system.txt", "w", encoding="utf-8") as f:
        f.write(system)
    with open(f"{prefix}_user_prompt.txt", "w", encoding="utf-8") as f:
        f.write(prompt)
    with open(f"{prefix}_response_raw.txt", "w", encoding="utf-8") as f:
        f.write(raw_response)

    record: dict[str, Any] = {
        "tag": tag,
        "ts": datetime.now().isoformat(),
        "llm": llm_meta,
        "response_chars": len(raw_response),
        "parsed": parsed,
        "extra": extra or {},
    }
    save_json(record, f"{prefix}_io.json")

    jsonl_path = os.path.join(debug_dir, "llm_io.jsonl")
    with open(jsonl_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"  [Analyze·Debug] 저장 → {debug_dir}/{tag}_*")


def _strip_code_fence(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_llm_json_array(raw: str) -> list[dict[str, Any]]:
    text = _strip_code_fence(raw)
    if not text:
        return []

    # JSON 배열 우선
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
        except json.JSONDecodeError:
            pass

    # 단일 객체 → 1건으로 간주
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, dict):
                if isinstance(data.get("results"), list):
                    return [x for x in data["results"] if isinstance(x, dict)]
                if isinstance(data.get("screenings"), list):
                    return [x for x in data["screenings"] if isinstance(x, dict)]
                return [data]
        except json.JSONDecodeError:
            pass

    return []


def _build_batch_screen_prompt(
    archive_metas: list[dict[str, Any]],
    *,
    target_date: str,
) -> str:
    blocks: list[str] = []
    for meta in archive_metas:
        summary = _load_detail_summary(meta, for_llm=True)
        if not summary.strip():
            continue
        blocks.append(
            f"### [{meta.get('idx')}] {meta.get('공고명', '')}\n"
            f"- 접수기간: {_meta_str(meta.get('공고_기간')) or '미확인'}\n\n"
            f"{summary}"
        )

    combined = "\n\n---\n\n".join(blocks)
    n = len(blocks)

    return f"""{target_date} 키워드 1차 통과 공고 {n}건의 상세요약이다.

**각 공고마다 먼저** 상세요약에서 신청·지원 자격을 추출하라.
자격(0→1단계) 통과 후에만 도메인 적합도(2단계)를 판단하고, system 지침에 따라 BEST 공고만 JSON 배열로 출력하라.

[공고 목록 — 총 {n}건]
{combined}"""


def _merge_screen_with_meta(
    parsed: dict[str, Any],
    meta: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(parsed)
    merged["idx"] = meta.get("idx")
    merged["공고명"] = meta.get("공고명", "")
    merged["접수기간"] = _meta_str(meta.get("공고_기간")) or "미확인"
    merged["상세링크"] = meta.get("상세링크", "")
    return merged


def _screen_all_announcements(
    archive_metas: list[dict[str, Any]],
    *,
    target_date: str,
    debug_dir: str | None = None,
) -> list[dict[str, Any]]:
    """1차 통과 공고 전체를 한 번에 LLM 2차 스크리닝합니다."""
    prompt = _build_batch_screen_prompt(archive_metas, target_date=target_date)
    raw, llm_meta = _call_llm_detailed(prompt, SCREEN_SYSTEM_PROMPT)
    parsed_list = _parse_llm_json_array(raw)

    meta_by_idx = {m.get("idx"): m for m in archive_metas}
    screenings: list[dict[str, Any]] = []

    for item in parsed_list:
        idx = item.get("idx")
        meta = meta_by_idx.get(idx)
        if not meta:
            print(f"  [Analyze] 알 수 없는 idx={idx} — 스킵")
            continue
        screenings.append(_merge_screen_with_meta(item, meta))

    best_idxs = {s.get("idx") for s in screenings}
    excluded_idxs = [m.get("idx") for m in archive_metas if m.get("idx") not in best_idxs]
    print(
        f"  [Analyze] BEST {len(screenings)}건 / 입력 {len(archive_metas)}건"
        + (f" (제외 idx={excluded_idxs})" if excluded_idxs else "")
    )

    if debug_dir:
        _save_llm_debug(
            debug_dir,
            tag="screen_batch",
            system=SCREEN_SYSTEM_PROMPT,
            prompt=prompt,
            raw_response=raw,
            llm_meta=llm_meta,
            parsed=screenings,
            extra={
                "input_count": len(archive_metas),
                "parsed_count": len(parsed_list),
                "best_count": len(screenings),
                "excluded_idxs": excluded_idxs,
                "target_date": target_date,
            },
        )

    if not screenings and parsed_list:
        print("  [Analyze] JSON 파싱은 됐으나 idx 매칭 실패")
    elif not parsed_list:
        print("  [Analyze] LLM JSON 배열 파싱 실패")

    return screenings


def _is_qualified_screen(screen: dict[str, Any]) -> bool:
    if not screen.get("지원대상_적격", False):
        return False
    fit = str(screen.get("최종적합도", "")).strip()
    return fit in ("보통", "높음")


def _sanitize_table_cell(value: Any) -> str:
    text = str(value or "").replace("\n", " ").replace("|", "/").strip()
    return text or "미확인"


_LLM_BRIEFING_FIELD_PREFIX = re.compile(
    r"^(?:[🎯💡📅💰🔗]\s*)?"
    r"(?:지원대상|적합사유|지원규모)\s*"
    r"(?:—|-|:)\s*",
    re.IGNORECASE,
)


def _clean_llm_briefing_field(value: Any) -> str:
    """LLM이 필드 라벨·이모지를 값에 포함한 경우 제거합니다."""
    text = _sanitize_table_cell(value)
    text = _LLM_BRIEFING_FIELD_PREFIX.sub("", text).strip()
    return text or "미확인"


_SOURCE_PRIORITY: dict[str, int] = {
    "기업마당": 0,
    "k-startup": 1,
    "kstartup": 1,
}


def _dedupe_qualified_screens(
    qualified: list[dict[str, Any]],
    meta_by_idx: dict[Any, dict[str, Any]],
) -> list[dict[str, Any]]:
    """동일 프로그램 중복 공고를 1건으로 합칩니다 (기업마당 우선)."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in qualified:
        key = _program_title_key(row.get("공고명", ""))
        bucket = key or f"__idx_{row.get('idx')}"
        groups.setdefault(bucket, []).append(row)

    deduped: list[dict[str, Any]] = []
    fit_order = {"높음": 0, "보통": 1}

    for key, rows in groups.items():
        if len(rows) == 1:
            deduped.append(rows[0])
            continue

        def _rank(r: dict[str, Any]) -> tuple[int, int, int]:
            meta = meta_by_idx.get(r.get("idx"), {})
            source = str(meta.get("출처") or "").strip().casefold()
            return (
                _SOURCE_PRIORITY.get(source, 9),
                fit_order.get(str(r.get("최종적합도", "")), 9),
                int(r.get("idx") or 999),
            )

        best = min(rows, key=_rank)
        removed = [r.get("idx") for r in rows if r is not best]
        print(
            f"  [Analyze] 중복 제거 - 유지 idx={best.get('idx')}, "
            f"제외 idx={removed} (key={key[:24]}...)"
        )
        deduped.append(best)

    deduped.sort(
        key=lambda r: (
            fit_order.get(str(r.get("최종적합도", "")), 9),
            r.get("idx", 0),
        )
    )
    return deduped


def _infer_support_scale_from_summary(meta: dict[str, Any] | None) -> str:
    """지원금액이 없을 때 [지원내용]에서 핵심 혜택 요약을 추출합니다."""
    if not meta:
        return ""

    summary = _load_detail_summary(meta, for_llm=False)
    if not summary:
        return ""

    match = re.search(r"\[지원내용\](.*?)(?=\n\[|\Z)", summary, re.DOTALL)
    if not match:
        return ""

    content = match.group(1)
    parts: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if re.match(r"^\d+\.", stripped):
            parts.append(re.sub(r"^\d+\.?", "", stripped).strip())
        elif stripped.startswith("- "):
            bullet = stripped[2:].strip()
            if any(k in bullet for k in ("IR", "인허가", "방문", "교육", "미팅", "컨설팅", "멘토")):
                parts.append(bullet)

    if not parts:
        return ""

    text = " · ".join(parts[:3])
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 50:
        text = text[:47].rstrip(" ·") + "..."
    return text


def _program_title_key(title: str) -> str:
    text = re.sub(r"\[[^\]]+\]", "", str(title or ""))
    for ch in ("ㆍ", "·", "/", "-", "_", " "):
        text = text.replace(ch, "")
    text = re.sub(r"[^\w가-힣]", "", text)
    return text.lower()


def _find_duplicate_program_meta(
    row: dict[str, Any],
    meta_by_idx: dict[Any, dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """동일 프로그램의 다른 출처(예: K-Startup) 아카이브 메타를 찾습니다."""
    if not meta_by_idx:
        return None

    # 공고명 정제 인라인 헬퍼 람다식
    clean_fn = lambda t: re.sub(r"[^\w\d]", "", re.sub(r"^\[[^\]]+\]", "", (t or "").strip())).lower()

    target_title = row.get("공고명", "")
    title_key = clean_fn(target_title)
    if not title_key:
        return None

    for idx, meta in meta_by_idx.items():
        if idx == row.get("idx"):
            continue
            
        if clean_fn(meta.get("공고명", "")) == title_key:
            return meta

    return None


def _resolve_support_scale(
    row: dict[str, Any],
    meta_by_idx: dict[Any, dict[str, Any]] | None,
) -> str:
    scale = _sanitize_table_cell(row.get("지원규모"))
    if scale != "미확인":
        return scale

    own_meta = (meta_by_idx or {}).get(row.get("idx"))
    inferred = _infer_support_scale_from_summary(own_meta)
    if inferred:
        return inferred

    dup_meta = _find_duplicate_program_meta(row, meta_by_idx)
    if dup_meta:
        inferred = _infer_support_scale_from_summary(dup_meta)
        if inferred:
            return inferred

    return scale


def _body_document_links(
    meta: dict[str, Any] | None,
    *,
    target_date: str,
) -> list[tuple[str, str]]:
    """아카이브된 공고문(PDF/HWP 등) → (라벨, 공개 URL) 목록."""
    if not meta:
        return []

    idx = meta.get("idx")
    if idx is None:
        return []

    links: list[tuple[str, str]] = []
    for i, raw in enumerate(meta.get("공고문_파일") or []):
        path = _resolve_path(str(raw))
        if not path.is_file():
            continue
        url = build_gov_file_public_url(target_date, int(idx), path.name)
        if not url:
            continue
        label = "본문" if i == 0 else f"본문{i + 1}"
        links.append((label, url))
    return links


BRIEFING_CHATBOT_FOOTER = (
    "---\n\n"
    "💬 공고에 대해 추가 질문이나 신청 양식·첨부 자료가 필요하면 "
    "`/connbot`으로 챗봇을 실행해 주세요."
)
BRIEFING_FOOTER_END_MARKER = "챗봇을 실행해 주세요."


def _normalize_briefing_markdown(raw: str, *, target_date: str) -> str:
    text = (raw or "").strip()
    if not text:
        return f"# [{target_date}] 코넥티브 맞춤 정부과제 브리핑 (0건)\n\n_오늘 코넥티브 적합 공고가 없습니다._\n"
    if not text.startswith("#"):
        text = f"# [{target_date}] 코넥티브 맞춤 정부과제 브리핑\n\n{text}"
    return text + ("\n" if not text.endswith("\n") else "")


def _render_briefing_cards(
    qualified: list[dict[str, Any]],
    *,
    target_date: str,
    meta_by_idx: dict[Any, dict[str, Any]] | None = None,
) -> str:
    """2차 필터 통과 건을 공고별 카드 브리핑으로 렌더링합니다."""
    fit_order = {"높음": 0, "보통": 1}
    sorted_rows = sorted(
        qualified,
        key=lambda r: (fit_order.get(str(r.get("최종적합도", "")), 9), r.get("idx", 0)),
    )
    lines = [
        f"# [{target_date}] 코넥티브 맞춤 정부과제 브리핑 ({len(sorted_rows)}건)",
        "",
    ]
    for i, row in enumerate(sorted_rows, start=1):
        title = _sanitize_table_cell(row.get("공고명"))
        period = _sanitize_table_cell(row.get("접수기간"))
        target_audience = _clean_llm_briefing_field(row.get("지원대상"))
        scale = _resolve_support_scale(row, meta_by_idx)
        reason = _clean_llm_briefing_field(row.get("적합사유"))
        fit = _sanitize_table_cell(row.get("최종적합도"))
        link = str(row.get("상세링크") or "").strip()
        meta = (meta_by_idx or {}).get(row.get("idx"))
        body_links = _body_document_links(meta, target_date=target_date)

        card_lines = [
            f"{i}. {title}",
            "",
            f"📅 접수기간: {period}",
            "",
            f"🎯 지원대상: {target_audience}",
            "",
            f"💰 지원규모: {scale}",
            "",
            f"💡 적합사유 (최종 적합도: {fit}):",
            reason,
        ]

        link_parts: list[str] = []
        if link.startswith(("http://", "https://")):
            link_parts.append(f"[공고 바로가기]({link})")
        for label, doc_url in body_links:
            link_parts.append(f"[{label}]({doc_url})")

        card_lines.append("")
        if link_parts:
            card_lines.append("🔗 " + " · ".join(link_parts))
        else:
            card_lines.append("🔗 공고 바로가기·본문: 링크 없음")

        lines.extend(card_lines)
        if i < len(sorted_rows):
            lines.extend(["", "---", ""])
    lines.extend(["", BRIEFING_CHATBOT_FOOTER, ""])
    return "\n".join(lines).strip() + "\n"


def analyze_all(
    archive_metas: list[dict[str, Any]],
    *,
    target_date: str | None = None,
    analyze_dir: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """1차 통과 공고 일괄 LLM 2차 스크리닝 → 적격·적합 건만 카드 브리핑으로 반환."""
    target_date = target_date or datetime.now().strftime("%Y-%m-%d")
    valid = [
        m for m in archive_metas
        if not m.get("오류") and _load_detail_summary(m, for_llm=True).strip()
    ]
    print(f"[4·Analyze] {len(archive_metas)}건 중 상세요약 {len(valid)}건 → 일괄 2차 스크리닝")

    if not valid:
        return _normalize_briefing_markdown("", target_date=target_date), []

    debug_dir = os.path.join(analyze_dir, "llm_debug") if analyze_dir else None
    screenings = _screen_all_announcements(
        valid,
        target_date=target_date,
        debug_dir=debug_dir,
    )

    qualified: list[dict[str, Any]] = []
    for screen in screenings:
        eligible = screen.get("지원대상_적격", False)
        fit = screen.get("최종적합도", "낮음")
        print(f"  [Analyze] [{screen.get('idx')}] 적격={eligible}, 적합도={fit}")
        if _is_qualified_screen(screen):
            qualified.append(screen)

    meta_by_idx = {m.get("idx"): m for m in archive_metas}
    qualified_before = len(qualified)
    qualified = _dedupe_qualified_screens(qualified, meta_by_idx)
    if qualified_before != len(qualified):
        print(
            f"[4·Analyze] 중복 제거 {qualified_before}건 → {len(qualified)}건"
        )

    if analyze_dir:
        os.makedirs(analyze_dir, exist_ok=True)
        save_json(
            {
                "input_count": len(valid),
                "screenings": screenings,
                "qualified": qualified,
                "qualified_count": len(qualified),
                "generated_at": datetime.now().isoformat(),
            },
            os.path.join(analyze_dir, "screening_report.json"),
        )

    print(f"[4·Analyze] 스크리닝 완료 — 브리핑 {len(qualified)}건")
    if not qualified:
        return _normalize_briefing_markdown("", target_date=target_date), []

    briefing = _render_briefing_cards(
        qualified,
        target_date=target_date,
        meta_by_idx=meta_by_idx,
    )
    print(f"[4·Analyze] 완료 ({len(briefing)}자)")
    return briefing, qualified


# ---------------------------------------------------------------------------
# Stage 5 — Serve
# ---------------------------------------------------------------------------
def _briefing_slack_fallback_text(briefing_md: str) -> str:
    """알림·접근성용 폴백 텍스트 (본문은 blocks로 표시)."""
    for line in (briefing_md or "").strip().splitlines():
        if line.startswith("# "):
            return f"📋 {line[2:].strip()}"
    return "코넥티브 맞춤 정부과제 브리핑"


def send_slack_briefing(
    briefing_md: str,
    channel: str | None = None,
) -> bool:
    """슬랙 채널에 브리핑을 Block Kit(카드형 section)으로 전송합니다."""
    from app.services.business_calendar import should_skip_daily_notification
    from app.slack.ui import SlackFormatter, build_gov_briefing_blocks

    skip = should_skip_daily_notification()
    if skip:
        print(f"[5·Serve] 슬랙 전송 스킵 — {skip}")
        return True

    token = os.getenv("SLACK_BOT_TOKEN", "")
    channel = channel or os.getenv("GOV_PROJECT_SLACK_CHANNEL", "")

    if not token or not channel:
        print("[5·Serve] SLACK_BOT_TOKEN / GOV_PROJECT_SLACK_CHANNEL 미설정 — 브리핑 저장만 수행")
        return False

    if not (briefing_md or "").strip():
        print("[5·Serve] 브리핑 본문 없음 — 슬랙 전송 스킵")
        return False

    formatted = SlackFormatter.to_slack(briefing_md.strip())
    blocks = build_gov_briefing_blocks(formatted)
    if not blocks:
        print("[5·Serve] Slack blocks 생성 실패 — 브리핑 저장만 수행")
        return False

    try:
        res = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "channel": channel,
                "text": _briefing_slack_fallback_text(briefing_md),
                "blocks": blocks,
                "unfurl_links": False,
                "unfurl_media": False,
            },
            timeout=15,
        )
        data = res.json()
        if data.get("ok"):
            print(f"[5·Serve] 슬랙 전송 완료 (Block Kit) → {channel}")
            return True
        print(f"[5·Serve] 슬랙 전송 실패: {data.get('error')}")
    except Exception as e:
        print(f"[5·Serve] 슬랙 예외: {e}")
    return False


def serve_application_files(
    project_idx: int,
    archive_root: str,
    request_type: str = "양식",
) -> dict[str, Any]:
    """챗봇 온디맨드 파일 서빙 — 신청양식 또는 공고문 목록 반환."""
    if not os.path.isdir(archive_root):
        return {"오류": "아카이브 경로 없음"}

    folders = sorted(
        f for f in os.listdir(archive_root)
        if f.startswith(f"{project_idx:03d}_") or f.startswith(f"{project_idx}_")
    )
    if not folders:
        return {"오류": f"{project_idx}번 과제를 찾을 수 없습니다."}

    target = os.path.join(archive_root, folders[0])
    sub = "신청양식_및_참고자료" if request_type == "양식" else "공고문_및_요약"
    folder = os.path.join(target, sub)

    if not os.path.isdir(folder):
        return {"오류": f"{sub} 폴더 없음"}

    files = [
        f for f in os.listdir(folder)
        if os.path.isfile(os.path.join(folder, f))
    ]
    return {
        "과제번호": project_idx,
        "공고명": folders[0].split("_", 1)[-1],
        "요청유형": request_type,
        "파일목록": files,
        "절대경로": [os.path.abspath(os.path.join(folder, f)) for f in files],
        "아카이브경로": target,
    }


def upload_file_to_slack(
    file_path: str,
    channel: str,
    title: str = "",
) -> bool:
    """슬랙 Web API로 파일을 업로드합니다 (챗봇 서빙용)."""
    token = os.getenv("SLACK_BOT_TOKEN", "")
    if not token or not os.path.isfile(file_path):
        return False

    try:
        with open(file_path, "rb") as f:
            res = requests.post(
                "https://slack.com/api/files.upload",
                headers={"Authorization": f"Bearer {token}"},
                data={"channels": channel, "title": title or os.path.basename(file_path)},
                files={"file": f},
                timeout=60,
            )
        return res.json().get("ok", False)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 유틸리티
# ---------------------------------------------------------------------------
def save_dataframe(df: pd.DataFrame, path_without_ext: str) -> None:
    if df.empty:
        print(f"  [저장] 데이터 없음: {path_without_ext}")
        return
    os.makedirs(os.path.dirname(path_without_ext) or ".", exist_ok=True)
    df.to_csv(f"{path_without_ext}.csv", index=False, encoding="utf-8-sig")
    try:
        df.to_excel(f"{path_without_ext}.xlsx", index=False)
    except Exception:
        pass
    print(f"  [저장] {path_without_ext}.csv ({len(df)}건)")


def save_json(data: Any, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 메인 파이프라인
# ---------------------------------------------------------------------------
def run_daily_pipeline(
    target_date: str | None = None,
    start_page: int = 1,
    end_page: int = 5,
    output_root: str = DEFAULT_OUTPUT_ROOT,
    skip_archive: bool = True,
    skip_analyze: bool = False,
    skip_slack: bool = True,
) -> dict[str, Any]:
    """5단계 일일 파이프라인을 순차 실행합니다."""
    session_dir = os.path.join(output_root, target_date)
    os.makedirs(session_dir, exist_ok=True)

    print("=" * 60)
    print(f"정부과제 일일 파이프라인 — {target_date}")
    print("=" * 60)

    # 1. Collect
    df_daily = collect_daily(target_date, start_page, end_page)
    save_dataframe(df_daily, os.path.join(session_dir, "01_collect", "df_daily"))

    # 2. Filter
    df_filtered = filter_by_keywords(df_daily)
    save_dataframe(df_filtered, os.path.join(session_dir, "02_filter", "df_filtered"))

    archive_metas: list[dict[str, Any]] = []
    briefing_md = ""
    qualified_screens: list[dict[str, Any]] = []
    archive_index_path = os.path.join(session_dir, "03_archive", "archive_index.json")

    if not skip_archive and not df_filtered.empty:
        # 3. Archive
        archive_root = os.path.join(session_dir, "03_archive")
        archive_metas = archive_all(df_filtered, archive_root, download_attachments=False)
        save_json(archive_metas, archive_index_path)
    else:
        if os.path.exists(archive_index_path):
            with open(archive_index_path, encoding="utf-8") as f:
                archive_metas = json.load(f)

    if not skip_analyze and archive_metas:
        # 4. Analyze — LLM 2차 필터 + 마크다운 표
        analyze_dir = os.path.join(session_dir, "04_analyze")
        os.makedirs(analyze_dir, exist_ok=True)
        briefing_md, qualified_screens = analyze_all(
            archive_metas,
            target_date=target_date,
            analyze_dir=analyze_dir,
        )
        with open(os.path.join(analyze_dir, "briefing.md"), "w", encoding="utf-8") as f:
            f.write(briefing_md)
        save_json(
            {"briefing": briefing_md, "generated_at": datetime.now().isoformat()},
            os.path.join(analyze_dir, "analysis_report.json"),
        )

        # 5. (다운로드) BEST 공고에서만 attachment 다운로드
        if qualified_screens:
            best_idxs = {q.get("idx") for q in qualified_screens if q.get("idx") is not None}
            if best_idxs:
                for meta in archive_metas:
                    if meta.get("idx") in best_idxs:
                        download_attachments_for_meta(meta)
                save_json(archive_metas, archive_index_path)
    elif skip_analyze and archive_metas:
        screening_path = os.path.join(session_dir, "04_analyze", "screening_report.json")
        briefing_path = os.path.join(session_dir, "04_analyze", "briefing.md")
        serve_briefing = os.path.join(session_dir, "05_serve", "briefing_table.md")
        if os.path.exists(screening_path):
            with open(screening_path, encoding="utf-8") as f:
                qualified_screens = json.load(f).get("qualified") or []
        for bp in (serve_briefing, briefing_path):
            if os.path.exists(bp):
                with open(bp, encoding="utf-8") as f:
                    briefing_md = f.read()
                break

    # 5. Serve
    serve_dir = os.path.join(session_dir, "05_serve")
    os.makedirs(serve_dir, exist_ok=True)
    slack_text = _briefing_slack_fallback_text(briefing_md) if briefing_md else ""
    with open(os.path.join(serve_dir, "slack_briefing.txt"), "w", encoding="utf-8") as f:
        f.write(slack_text)
    with open(os.path.join(serve_dir, "briefing_table.md"), "w", encoding="utf-8") as f:
        f.write(briefing_md)

    if not skip_slack and briefing_md:
        send_slack_briefing(briefing_md)

    try:
        write_latest_gov_index(
            target_date=target_date,
            session_dir=session_dir,
            qualified_screens=qualified_screens,
            archive_metas=archive_metas,
            briefing_md=briefing_md,
        )
        print(f"[5·Serve] latest_gov.json 갱신 ({len(qualified_screens)}건)")
    except Exception as e:
        print(f"[5·Serve] latest_gov.json 갱신 실패: {e}")

    count_match = re.search(r"브리핑 \((\d+)건\)", briefing_md)
    qualified_count = int(count_match.group(1)) if count_match else 0
    print("=" * 60)
    print(f"완료 → {session_dir}")
    print(f"  수집 {len(df_daily)} / 필터 {len(df_filtered)} / 브리핑 {max(qualified_count, 0)}건")
    print("=" * 60)

    return {
        "session_dir": session_dir,
        "df_daily": df_daily,
        "df_filtered": df_filtered,
        "archive_metas": archive_metas,
        "briefing_md": briefing_md,
    }

def main() -> None:
    """파이프라인 CLI 진입점."""
    run_daily_pipeline(
        start_page=1,
        end_page=10,
        skip_slack=not bool(os.getenv("SLACK_BOT_TOKEN")),
    )


if __name__ == "__main__":
    main()
