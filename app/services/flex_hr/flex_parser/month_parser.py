"""Flex HR 월간(일별 카드) HTML 파싱."""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

# 월간 카드 배경/테두리 색상 → 근태 유형 (텍스트가 있으면 텍스트 우선)
MONTHLY_TYPE_BY_COLOR = {
    "rgb(210,244,241)": "근무",
    "rgb(34,168,168)": "근무",
    "rgb(252,222,243)": "외근",
    "rgb(249,239,189)": "재택근무",
    "rgb(155,93,197)": "휴가",
    "rgb(231,242,196)": "출장",
}

_MONTH_NAV_LABEL_RE = re.compile(
    r"(\d{4})\s*(?:[\.\-/년]\s*|\s+)(\d{1,2})(?:\s*월)?"
)


def parse_month_nav_label(text: str) -> tuple[int, int] | None:
    """월간 네비 라벨(예: '2026. 6', '2026년 6월') → (year, month)."""
    if not text:
        return None
    m = _MONTH_NAV_LABEL_RE.search(text.strip())
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    if 1 <= month <= 12:
        return year, month
    return None


MONTHLY_TYPE_BY_TEXT = {
    "재택 근무": "재택근무",
    "재택근무": "재택근무",
    "근무": "근무",
    "휴가": "휴가",
    "외근": "외근",
    "출장": "출장",
    "휴게": "휴게시간",
    "휴게시간": "휴게시간",
}

MONTHLY_KNOWN_TYPES = frozenset(MONTHLY_TYPE_BY_TEXT.keys())
WEEKEND_NAMES = frozenset({"토", "일"})


def _norm_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _class_names(element) -> list[str]:
    raw = element.get("class", []) or []
    if isinstance(raw, str):
        return raw.split()
    return list(raw)


def _has_class_contains(element, token: str) -> bool:
    return any(token in cls for cls in _class_names(element))


def _normalize_rgb(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(
        r"rgb\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)",
        value,
        flags=re.I,
    )
    if not match:
        return None
    r, g, b = match.groups()
    return f"rgb({int(r)},{int(g)},{int(b)})"


def _extract_background_color(style: str | None) -> str | None:
    if not style:
        return None
    match = re.search(r"background-color\s*:\s*(rgb\([^)]+\))", style, flags=re.I)
    return _normalize_rgb(match.group(1)) if match else None


def _extract_box_shadow_color(style: str | None) -> str | None:
    if not style:
        return None
    match = re.search(r"box-shadow\s*:\s*(rgb\([^)]+\))", style, flags=re.I)
    return _normalize_rgb(match.group(1)) if match else None


def _extract_block_colors(block) -> dict:
    bg_color = _extract_background_color(block.get("style", ""))
    accent_color = None
    accent_el = block.select_one(".c-kgISVe")
    if accent_el:
        accent_color = _extract_box_shadow_color(accent_el.get("style", ""))
    return {
        "background_color": bg_color,
        "accent_color": accent_color,
    }


def _parse_duration_to_minutes(text: str | None) -> int | None:
    if not text:
        return None
    text = _norm_text(text)
    hour_match = re.search(r"(\d+)\s*h", text, flags=re.I)
    minute_match = re.search(r"(\d+)\s*m", text, flags=re.I)
    if not hour_match and not minute_match:
        return None
    hours = int(hour_match.group(1)) if hour_match else 0
    minutes = int(minute_match.group(1)) if minute_match else 0
    return hours * 60 + minutes


def _format_duration_minutes(minutes: int | None) -> str | None:
    if minutes is None:
        return None
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def _extract_duration_text(block_text: str) -> str | None:
    match = re.search(r"\b\d+\s*h(?:\s*\d+\s*m)?\b|\b\d+\s*m\b", block_text, flags=re.I)
    return _norm_text(match.group(0)) if match else None


def parse_tooltip_time(text: str | None) -> dict:
    """
    툴팁 텍스트에서 시각 구간을 파싱한다.

    예: 오전 8:00 – 오후 6:00 (9시간) / 오전 10:00 - 오전 11:00 (2시간)
    """
    text = _norm_text(text)
    pattern = (
        r"(오전|오후)\s*(\d{1,2}):(\d{2})\s*[–\-]\s*"
        r"(오전|오후)\s*(\d{1,2}):(\d{2})\s*\(([^)]+)\)"
    )
    match = re.search(pattern, text)
    if not match:
        return {
            "start_text": "",
            "end_text": "",
            "total_time": "",
            "start_24h": "",
            "end_24h": "",
        }

    start_ampm, sh, sm, end_ampm, eh, em, total_time = match.groups()
    start_text = f"{start_ampm} {sh}:{sm}"
    end_text = f"{end_ampm} {eh}:{em}"

    def to_24h(ampm: str, hour: str, minute: str) -> str:
        h = int(hour)
        m = int(minute)
        if ampm == "오전":
            if h == 12:
                h = 0
        elif h != 12:
            h += 12
        return f"{h:02d}:{m:02d}"

    return {
        "start_text": start_text,
        "end_text": end_text,
        "total_time": _norm_text(total_time),
        "start_24h": to_24h(start_ampm, sh, sm),
        "end_24h": to_24h(end_ampm, eh, em),
    }


def _minutes_from_24h(time_text: str | None) -> int | None:
    if not time_text:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", _norm_text(time_text))
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _minutes_between_24h(start: str | None, end: str | None) -> int | None:
    start_m = _minutes_from_24h(start)
    end_m = _minutes_from_24h(end)
    if start_m is None or end_m is None or end_m <= start_m:
        return None
    return end_m - start_m


def _parse_korean_duration_minutes(text: str | None) -> int | None:
    """'(9시간)', '2시간 30분' 등 한국어 누적 시간."""
    if not text:
        return None
    text = _norm_text(text)
    hour_match = re.search(r"(\d+)\s*시간", text)
    minute_match = re.search(r"(\d+)\s*분", text)
    if not hour_match and not minute_match:
        return None
    hours = int(hour_match.group(1)) if hour_match else 0
    minutes = int(minute_match.group(1)) if minute_match else 0
    return hours * 60 + minutes


def _extract_injected_times(block) -> dict:
    start_time = _norm_text(block.get("data-flex-start-time"))
    end_time = _norm_text(block.get("data-flex-end-time"))
    tooltip = _norm_text(block.get("data-flex-tooltip"))
    return {
        "start_time": start_time or None,
        "end_time": end_time or None,
        "tooltip": tooltip or None,
    }


def _build_monthly_item(block) -> dict | None:
    raw_text = _norm_text(block.get_text(" ", strip=True))
    if not raw_text:
        return None

    colors = _extract_block_colors(block)
    label = _extract_monthly_label_from_block(block)
    duration_text = _extract_duration_text(raw_text)
    injected = _extract_injected_times(block)

    start_time = injected.get("start_time")
    end_time = injected.get("end_time")

    duration_minutes = _parse_duration_to_minutes(duration_text)
    if duration_minutes is None and start_time and end_time:
        duration_minutes = _minutes_between_24h(start_time, end_time)
    if duration_minutes is None and injected.get("tooltip"):
        parsed_tooltip = parse_tooltip_time(injected["tooltip"])
        duration_minutes = _parse_korean_duration_minutes(parsed_tooltip.get("total_time"))

    if duration_text is None and duration_minutes is not None:
        duration_text = _format_duration_minutes(duration_minutes)

    schedule_type, type_source = _resolve_monthly_type(
        label=label,
        colors=colors,
        raw_text=raw_text,
    )

    item = {
        "type": schedule_type,
        "label": _norm_text(label),
        "duration": duration_text,
        "duration_minutes": duration_minutes,
        "type_source": type_source,
        "background_color": colors.get("background_color"),
        "accent_color": colors.get("accent_color"),
        "raw_text": raw_text,
    }
    if start_time:
        item["start_time"] = start_time
    if end_time:
        item["end_time"] = end_time

    return {k: v for k, v in item.items() if v is not None and v != ""}


def _extract_month_from_html(html_content: str, soup: BeautifulSoup) -> tuple[int | None, int | None]:
    for el in soup.select('[class*="periodType-monthly"]'):
        parsed = parse_month_nav_label(el.get_text(" ", strip=True))
        if parsed:
            return parsed

    date_match = re.search(r"date=(\d{4})-(\d{2})-\d{2}", html_content)
    if date_match:
        return int(date_match.group(1)), int(date_match.group(2))

    href_date = soup.select_one('a[href*="date="]')
    if href_date and href_date.get("href"):
        parsed = urlparse(href_date["href"])
        query = parse_qs(parsed.query)
        date_values = query.get("date") or []
        if date_values:
            m = re.match(r"(\d{4})-(\d{2})-\d{2}", date_values[0])
            if m:
                return int(m.group(1)), int(m.group(2))

    corner = soup.select_one('[data-role="corner-cell"]')
    corner_text = corner.get_text(" ", strip=True) if corner else ""
    month_match = re.search(r"(\d{1,2})\s*월", corner_text)
    if month_match:
        return datetime.now().year, int(month_match.group(1))

    return None, None


def _extract_monthly_headers(soup: BeautifulSoup, year: int | None, month: int | None) -> list[dict]:
    headers = []

    for cell in soup.select('[data-role="header-row-cell"]'):
        if not _has_class_contains(cell, "period-monthly"):
            continue

        spans = [_norm_text(span.get_text()) for span in cell.select("span")]
        spans = [s for s in spans if s]
        if not spans:
            continue

        day_text = spans[0]
        if not day_text.isdigit():
            continue

        day = int(day_text)
        weekday = spans[1] if len(spans) >= 2 else None

        inner = cell.select_one("[class*='isWeekend']")
        class_blob = " ".join(_class_names(inner or cell))
        is_weekend = "isWeekend-true" in class_blob or weekday in WEEKEND_NAMES

        date = None
        if year and month:
            try:
                date = f"{year:04d}-{month:02d}-{day:02d}"
            except ValueError:
                date = None

        headers.append(
            {
                "index": len(headers),
                "date": date,
                "day": day,
                "weekday": weekday,
                "is_weekend": is_weekend,
            }
        )

    return headers


def _extract_user_from_monthly_row(row) -> dict:
    header = row.select_one('[data-role="header-column-cell"]') or row
    user = {}

    title = header.select_one('[data-scope="avatar-meta"] [data-part="title"]')
    if title:
        user["name"] = _norm_text(title.get_text())

    role = header.select_one('[data-scope="avatar-meta"] [data-part="subtext"]')
    if role:
        user["role"] = _norm_text(role.get_text())

    link = header.select_one('a[href*="/time-tracking/work-record/"], a[href*="/work-record/"]')
    if link and link.get("href"):
        href = link["href"]
        record_match = re.search(r"/work-record/([^?/#]+)", href)
        if record_match:
            user["record_id"] = record_match.group(1)

        parsed = urlparse(href)
        query = parse_qs(parsed.query)
        if query.get("user_profile_uid"):
            user["user_profile_uid"] = query["user_profile_uid"][0]

    return user


def _extract_summary_time_from_row(row) -> str | None:
    header = row.select_one('[data-role="header-column-cell"]') or row
    summary = header.select_one(".c-ifYJlR")
    if summary:
        return _norm_text(summary.get_text())
    return None


def _extract_monthly_label_from_block(block) -> str | None:
    label_el = block.select_one(".c-dowUzf span")
    if label_el:
        label = _norm_text(label_el.get_text())
        if label:
            return label

    span_texts = [_norm_text(span.get_text()) for span in block.select("span")]
    span_texts = [t for t in span_texts if t]

    for text in span_texts:
        if _parse_duration_to_minutes(text) is not None:
            continue
        if text in MONTHLY_KNOWN_TYPES:
            return text

    raw_text = _norm_text(block.get_text(" ", strip=True))
    for label in sorted(MONTHLY_TYPE_BY_TEXT.keys(), key=len, reverse=True):
        if label in raw_text:
            return label

    return None


def _resolve_monthly_type(label: str | None, colors: dict, raw_text: str) -> tuple[str, str]:
    normalized_label = _norm_text(label)

    if normalized_label in MONTHLY_TYPE_BY_TEXT:
        return MONTHLY_TYPE_BY_TEXT[normalized_label], "text"

    for label_key in sorted(MONTHLY_TYPE_BY_TEXT.keys(), key=len, reverse=True):
        if label_key and label_key in raw_text:
            return MONTHLY_TYPE_BY_TEXT[label_key], "text"

    bg = colors.get("background_color")
    if bg in MONTHLY_TYPE_BY_COLOR:
        return MONTHLY_TYPE_BY_COLOR[bg], "background_color"

    accent = colors.get("accent_color")
    if accent in MONTHLY_TYPE_BY_COLOR:
        return MONTHLY_TYPE_BY_COLOR[accent], "accent_color"

    return "기타", "unknown"


def _parse_monthly_cell_blocks(cell) -> list[dict]:
    items = []
    if not cell:
        return items

    for block in cell.select(".c-fQnhbx"):
        item = _build_monthly_item(block)
        if item:
            items.append(item)

    return items


def _get_direct_monthly_day_cells(row) -> list:
    direct_cells = [
        child
        for child in row.find_all(recursive=False)
        if _has_class_contains(child, "period-monthly")
        and _has_class_contains(child, "c-wIHID")
    ]
    if direct_cells:
        return direct_cells

    return [
        cell
        for cell in row.select('[class*="period-monthly"]')
        if _has_class_contains(cell, "c-wIHID")
    ]


def _summarize_day_items(items: list[dict]) -> dict:
    total_minutes = sum(
        item.get("duration_minutes") or 0
        for item in items
        if isinstance(item.get("duration_minutes"), int)
    )

    primary_type = None
    if items:
        primary = max(items, key=lambda item: item.get("duration_minutes") or -1)
        primary_type = primary.get("type")

    return {
        "primary_type": primary_type,
        "total_minutes": total_minutes if total_minutes > 0 else None,
        "total_duration": _format_duration_minutes(total_minutes) if total_minutes > 0 else None,
    }


def parse_monthly_employee_row(row, headers: list[dict]) -> dict:
    user = _extract_user_from_monthly_row(row)
    summary_time = _extract_summary_time_from_row(row)
    day_cells = _get_direct_monthly_day_cells(row)

    days = []
    max_len = max(len(headers), len(day_cells))

    for idx in range(max_len):
        header = headers[idx] if idx < len(headers) else {
            "index": idx,
            "date": None,
            "day": None,
            "weekday": None,
            "is_weekend": None,
        }

        cell = day_cells[idx] if idx < len(day_cells) else None
        items = _parse_monthly_cell_blocks(cell) if cell else []
        summary = _summarize_day_items(items)

        day_record = {
            "index": header.get("index", idx),
            "date": header.get("date"),
            "day": header.get("day"),
            "weekday": header.get("weekday"),
            "is_weekend": header.get("is_weekend"),
            "items": items,
            **summary,
        }
        day_record = {
            k: v for k, v in day_record.items() if v is not None or k in {"items"}
        }
        days.append(day_record)

    return {
        "user": user,
        "summary_time": summary_time,
        "days": days,
    }


def member_dedupe_key(member: dict) -> str:
    """월간 직원 row 중복 제거 키."""
    user = member.get("user") or {}
    if user.get("record_id"):
        return f"id:{user['record_id']}"
    if user.get("user_profile_uid"):
        return f"uid:{user['user_profile_uid']}"
    return f"name:{user.get('name') or ''}"


def _monthly_item_key(item: dict) -> tuple:
    return (
        item.get("type"),
        item.get("label"),
        item.get("duration"),
        item.get("start_time"),
        item.get("end_time"),
        item.get("raw_text"),
    )


def merge_monthly_member(base: dict, other: dict) -> dict:
    """같은 직원의 월간 row 두 개를 병합한다."""
    if other.get("summary_time") and not base.get("summary_time"):
        base["summary_time"] = other["summary_time"]

    base_user = base.get("user") or {}
    other_user = other.get("user") or {}
    for field in ("name", "role", "record_id", "user_profile_uid"):
        if not base_user.get(field) and other_user.get(field):
            base_user[field] = other_user[field]
    base["user"] = base_user

    base_days: dict = {}
    for day in base.get("days") or []:
        key = day.get("date") if day.get("date") is not None else day.get("index")
        base_days[key] = day

    for day in other.get("days") or []:
        key = day.get("date") if day.get("date") is not None else day.get("index")
        if key not in base_days:
            base_days[key] = day
            continue

        existing = base_days[key]
        existing_items = list(existing.get("items") or [])
        if not existing_items and day.get("items"):
            base_days[key] = day
            continue

        seen = {_monthly_item_key(item) for item in existing_items}
        for item in day.get("items") or []:
            item_key = _monthly_item_key(item)
            if item_key in seen:
                continue
            existing_items.append(item)
            seen.add(item_key)

        existing["items"] = existing_items
        existing.update(_summarize_day_items(existing_items))

    base["days"] = sorted(
        base_days.values(),
        key=lambda d: d.get("index") if d.get("index") is not None else 0,
    )
    return base


def merge_monthly_members(members: list[dict]) -> list[dict]:
    """월간 직원 목록을 record_id/name 기준으로 중복 제거·병합한다."""
    merged: dict[str, dict] = {}
    order: list[str] = []

    for member in members:
        key = member_dedupe_key(member)
        if key not in merged:
            merged[key] = member
            order.append(key)
            continue
        merged[key] = merge_monthly_member(merged[key], member)

    return [merged[key] for key in order]


def parse_visible_monthly_rows(
    html_content: str,
    headers: list[dict] | None = None,
) -> tuple[list[dict], list[dict], int | None, int | None]:
    """현재 HTML에 렌더링된 직원 row만 파싱한다."""
    soup = BeautifulSoup(html_content, "html.parser")
    year, month = _extract_month_from_html(html_content, soup)
    if headers is None:
        headers = _extract_monthly_headers(soup, year, month)

    rows = [
        row
        for row in soup.select(".c-bZNrxE")
        if row.select_one('[data-role="header-column-cell"]')
    ]
    members = [parse_monthly_employee_row(row, headers) for row in rows]
    return members, headers, year, month


def build_monthly_result(
    members: list[dict],
    headers: list[dict],
    year: int | None,
    month: int | None,
) -> dict:
    """병합된 월간 파싱 결과 dict를 만든다."""
    year_month = f"{year:04d}-{month:02d}" if year and month else None
    return {
        "period": "monthly",
        "year_month": year_month,
        "year": year,
        "month": month,
        "days": headers,
        "members": members,
        "meta": {
            "member_count": len(members),
            "day_count": len(headers),
        },
    }


def parse_flex_hr_monthly_html(html_content: str) -> dict:
    """Flex 구성원 근무 월간 HTML을 파싱한다."""
    soup = BeautifulSoup(html_content, "html.parser")

    year, month = _extract_month_from_html(html_content, soup)
    headers = _extract_monthly_headers(soup, year, month)

    rows = [
        row
        for row in soup.select(".c-bZNrxE")
        if row.select_one('[data-role="header-column-cell"]')
    ]

    members = [parse_monthly_employee_row(row, headers) for row in rows]
    year_month = f"{year:04d}-{month:02d}" if year and month else None

    return {
        "period": "monthly",
        "year_month": year_month,
        "year": year,
        "month": month,
        "days": headers,
        "members": members,
        "meta": {
            "member_count": len(members),
            "day_count": len(headers),
        },
    }


def flatten_flex_hr_monthly(result: dict) -> list[dict]:
    """월간 파싱 결과를 검색용 row 리스트로 평탄화한다."""
    rows = []

    for member in result.get("members", []):
        user = member.get("user") or {}

        for day in member.get("days", []):
            items = day.get("items") or []

            if not items:
                rows.append(
                    {
                        "date": day.get("date"),
                        "day": day.get("day"),
                        "weekday": day.get("weekday"),
                        "is_weekend": day.get("is_weekend"),
                        "name": user.get("name"),
                        "role": user.get("role"),
                        "record_id": user.get("record_id"),
                        "summary_time": member.get("summary_time"),
                        "type": None,
                        "duration": None,
                        "duration_minutes": None,
                        "raw_text": None,
                    }
                )
                continue

            for item in items:
                rows.append(
                    {
                        "date": day.get("date"),
                        "day": day.get("day"),
                        "weekday": day.get("weekday"),
                        "is_weekend": day.get("is_weekend"),
                        "name": user.get("name"),
                        "role": user.get("role"),
                        "record_id": user.get("record_id"),
                        "summary_time": member.get("summary_time"),
                        "type": item.get("type"),
                        "label": item.get("label"),
                        "duration": item.get("duration"),
                        "duration_minutes": item.get("duration_minutes"),
                        "start_time": item.get("start_time"),
                        "end_time": item.get("end_time"),
                        "background_color": item.get("background_color"),
                        "accent_color": item.get("accent_color"),
                        "raw_text": item.get("raw_text"),
                    }
                )

    return rows


def is_monthly_html(html_content: str) -> bool:
    """월간 그리드 HTML 여부."""
    if re.search(r"period=monthly", html_content):
        return True
    if "c-fQnhbx" in html_content and "c-gNZGXI" not in html_content:
        return True
    soup = BeautifulSoup(html_content, "html.parser")
    return bool(soup.select(".c-bZNrxE .c-fQnhbx, [data-role='header-row-cell']"))
