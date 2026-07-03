"""Flex HR 일간(타임라인) HTML 파싱."""

from __future__ import annotations

import os
import re

from bs4 import BeautifulSoup

ICON_BY_SVG_PREFIX = {
    "m6.944 3": "briefcase",
    "M4.074 5.93": "car",
    "M4 5.555": "coffee_cup",
    "M20.642 9.4": "home",
    "M21.08 2.914": "airplane",
}

ICON_BY_COLOR = {
    "cyan": "work",
    "blue": "work",
    "pink": "car",
    "gray": "coffee_cup",
    "yellow": "home",
    "purple": "vacation",
    "lime": "airplane",
}

TYPE_BY_ICON = {
    "car": "외근",
    "home": "재택근무",
    "work": "근무",
    "vacation": "휴가",
    "airplane": "출장",
}

TYPE_BY_COLOR = {
    "cyan": "근무",
    "blue": "근무",
    "pink": "외근",
    "yellow": "재택근무",
    "purple": "휴가",
    "lime": "출장",
}

WORK_WINDOW_START = 6 * 60
WORK_WINDOW_END = 23 * 60
SKIP_TYPES = frozenset({"휴게시간"})
LEAVE_TYPES = frozenset({"휴가", "재택근무", "외근", "출장"})
LIVE_END_TOLERANCE_MINUTES = 2


def _extract_color(class_list):
    for cls in class_list:
        m = re.search(r"color-(\w+)", cls)
        if m:
            return m.group(1)
    return None


def _extract_icon(element):
    path = element.select_one("svg path")
    if path:
        d = path.get("d", "").strip()
        for prefix, icon in ICON_BY_SVG_PREFIX.items():
            if d.startswith(prefix):
                return icon

    classes = element.get("class", [])
    if "c-gwshsc" in classes:
        return "work"

    color = _extract_color(classes)
    if color:
        return ICON_BY_COLOR.get(color)
    return None


def _format_time(total_minutes):
    hour, minute = divmod(int(round(total_minutes)), 60)
    if minute == 0:
        return str(hour)
    return f"{hour}:{minute:02d}"


def _times_from_style(style, timeline_minutes=1440):
    if not style:
        return None
    left_match = re.search(r"left:\s*([\d.]+)%", style)
    right_match = re.search(r"right:\s*([\d.]+)%", style)
    if not (left_match and right_match):
        return None
    left_pct = float(left_match.group(1))
    right_pct = float(right_match.group(1))
    start = (left_pct / 100.0) * timeline_minutes
    end = ((100.0 - right_pct) / 100.0) * timeline_minutes
    return start, end


def _class_names(element) -> list[str]:
    raw = element.get("class", []) or []
    if isinstance(raw, str):
        return raw.split()
    return list(raw)


def _is_gaaahaz_wrapper(element) -> bool:
    return any("gAAhAz" in cls for cls in _class_names(element))


def _find_wrapper_times(element):
    times = _times_from_style(element.get("style", ""))
    if times:
        return times
    for parent in element.parents:
        if parent.name in ("body", "html", "[document]"):
            break
        if _is_gaaahaz_wrapper(parent):
            return _times_from_style(parent.get("style", ""))
    return None


def _resolve_schedule_type(color, icon):
    if icon and icon in TYPE_BY_ICON:
        return TYPE_BY_ICON[icon]
    if color and color in TYPE_BY_COLOR:
        return TYPE_BY_COLOR[color]
    return "기타"


def _build_item(schedule_type, color, icon, times=None, *, record_kind="block", source="block"):
    item = {
        "type": schedule_type,
        "color": color,
        "icon": icon,
        "record_kind": record_kind,
        "source": source,
    }
    if times:
        start_m, end_m = times
        if _minutes_in_work_window(start_m) and _minutes_in_work_window(end_m) and start_m < end_m:
            item["start_time"] = _format_time(start_m)
            item["end_time"] = _format_time(end_m)
    return item


def _build_live_item(schedule_type, color, icon, start_m, *, source="live"):
    item = {
        "type": schedule_type,
        "color": color,
        "icon": icon,
        "record_kind": "live",
        "source": source,
    }
    if start_m is not None and _minutes_in_work_window(start_m):
        item["start_time"] = _format_time(start_m)
    return item


def _start_from_style(style, timeline_minutes=1440):
    times = _times_from_style(style, timeline_minutes)
    return times[0] if times else None


def _is_live_end_minutes(end_m: int | None, current_time_minutes: int | None) -> bool:
    if end_m is None or current_time_minutes is None:
        return False
    return abs(end_m - current_time_minutes) <= _live_end_tolerance_minutes()


def _item_planned_score(item: dict) -> float:
    score = 0.0
    if item.get("start_time") and item.get("end_time"):
        score += 100
    for key in ("start_time", "end_time"):
        val = str(item.get(key) or "")
        if "오전" in val or "오후" in val:
            score += 20
    start_m = _time_to_minutes(item.get("start_time"))
    end_m = _time_to_minutes(item.get("end_time"))
    if start_m is not None and end_m is not None and end_m > start_m:
        score += (end_m - start_m) / 60.0
    return score


def _dedupe_planned(planned: list[dict]) -> list[dict]:
    best_by_type: dict[str, dict] = {}
    for item in planned:
        key = str(item.get("type") or "")
        prev = best_by_type.get(key)
        if prev is None or _item_planned_score(item) > _item_planned_score(prev):
            best_by_type[key] = item
    return list(best_by_type.values())


def _pick_live_item(candidates: list[dict]) -> dict | None:
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: _time_to_minutes(item.get("start_time")) or -1,
    )


def _classify_article_blocks(blocks: list[dict], current_time_minutes: int | None) -> tuple[list[dict], list[dict]]:
    planned: list[dict] = []
    live: list[dict] = []
    for block in blocks:
        item = dict(block)
        item.setdefault("source", "block")
        end_m = _time_to_minutes(item.get("end_time")) if item.get("end_time") else None
        if _is_live_end_minutes(end_m, current_time_minutes):
            item.pop("end_time", None)
            item["record_kind"] = "live"
            live.append(item)
        else:
            item["record_kind"] = "block"
            planned.append(item)
    return planned, live


def _parse_jmqbtz_segments(section):
    items = []
    for seg in section.select('[class*="c-jMQbTZ"]'):
        cls = " ".join(seg.get("class", []))
        if "type-solid" not in cls:
            continue
        start_m = _start_from_style(seg.get("style", ""))
        if start_m is None:
            continue
        color = _extract_color(seg.get("class", [])) or "cyan"
        icon = _extract_icon(seg) or ICON_BY_COLOR.get(color, "work")
        schedule_type = _resolve_schedule_type(color, icon)
        items.append(_build_live_item(schedule_type, color, icon, start_m, source="segment"))
    return items


def _parse_article_blocks(section):
    items = []
    for article in section.select("article.c-cDZqUN"):
        block = article.select_one('[class*="c-iyXem"], [class*="c-hOEbFA"]')
        if not block:
            continue

        color = _extract_color(block.get("class", []))
        if color == "gray":
            continue

        icon = _extract_icon(block)
        schedule_type = _resolve_schedule_type(color, icon)
        if schedule_type in SKIP_TYPES:
            continue

        times = _find_wrapper_times(block)
        items.append(_build_item(schedule_type, color, icon, times, source="block"))
    return items


def _parse_active_work_indicator(section, live_item: dict | None):
    if live_item:
        return []
    if not section.select(".c-gwshsc"):
        return []
    return [
        _build_item("근무 중", "blue", "work", record_kind="indicator", source="indicator")
    ]


def _parse_timeline_markers(section):
    markers = []
    layer = section.select_one('[class*="stackingOrder-51"]')
    if not layer:
        return markers

    for elem in layer.select(".c-gAAhAz"):
        text = elem.get_text(" ", strip=True)
        for match in re.finditer(r"오[전후]\s*\d{1,2}:\d{2}", text):
            markers.append(match.group())
    return markers


def _time_to_minutes(value):
    if not value:
        return None
    m = re.match(r"오(전|후)\s*(\d{1,2}):(\d{2})", value.strip())
    if m:
        ampm, hour, minute = m.group(1), int(m.group(2)), int(m.group(3))
        if ampm == "후" and hour != 12:
            hour += 12
        elif ampm == "전" and hour == 12:
            hour = 0
        return hour * 60 + minute
    if ":" in value:
        h, mi = value.split(":", 1)
        return int(h) * 60 + int(mi)
    return int(value) * 60


def _minutes_in_work_window(minutes):
    return WORK_WINDOW_START <= minutes <= WORK_WINDOW_END


def _time_sort_key(value):
    minutes = _time_to_minutes(value)
    return minutes if minutes is not None else float("inf")


def _finalize_schedule_item(item):
    if item.get("type") in SKIP_TYPES:
        return None

    start_time = item.get("start_time")
    end_time = item.get("end_time")

    if start_time or end_time:
        start_m = _time_to_minutes(start_time) if start_time else None
        end_m = _time_to_minutes(end_time) if end_time else None
        if start_m is not None and not _minutes_in_work_window(start_m):
            return None
        if end_m is not None and not _minutes_in_work_window(end_m):
            return None
        if start_m is not None and end_m is not None and start_m >= end_m:
            return None

    return item


def _is_marker_pairable(item):
    if str(item.get("record_kind") or "block") != "block":
        return False
    if item.get("start_time") and item.get("end_time"):
        return False
    return True


def _apply_marker_times(schedule_status, markers):
    if not markers:
        return schedule_status

    pairable = [i for i, item in enumerate(schedule_status) if _is_marker_pairable(item)]
    if not pairable:
        return schedule_status

    n_p = len(pairable)
    n_m = len(markers)

    if n_m == n_p - 1:
        for k, marker in enumerate(markers):
            left_idx = pairable[k]
            right_idx = pairable[k + 1]
            left = schedule_status[left_idx]
            if left.get("start_time") == marker and not left.get("end_time"):
                left.pop("start_time", None)
            if not left.get("end_time"):
                left["end_time"] = marker
            if not schedule_status[right_idx].get("start_time"):
                schedule_status[right_idx]["start_time"] = marker
    elif n_m >= n_p:
        for n, idx in enumerate(pairable):
            item = schedule_status[idx]
            if n < n_m and not item.get("start_time"):
                item["start_time"] = markers[n]
            if n + 1 < n_m and not item.get("end_time"):
                item["end_time"] = markers[n + 1]
    elif n_m == 1 and n_p == 1:
        idx = pairable[0]
        item = schedule_status[idx]
        marker = markers[0]
        item_type = str(item.get("type") or "")
        if item_type in LEAVE_TYPES:
            if not item.get("start_time"):
                item["start_time"] = marker
        elif item_type == "근무":
            if not item.get("end_time"):
                item["end_time"] = marker
            elif not item.get("start_time"):
                item["start_time"] = marker
        else:
            if not item.get("start_time"):
                item["start_time"] = marker

    return schedule_status


def _live_end_tolerance_minutes() -> int:
    return int(os.getenv("FLEX_LIVE_END_TOLERANCE_MIN", str(LIVE_END_TOLERANCE_MINUTES)) or "2")


def _parse_timeline_section(section, *, current_time_minutes: int | None = None):
    markers = _parse_timeline_markers(section)
    raw_blocks = _parse_article_blocks(section)
    segment_live = _parse_jmqbtz_segments(section)

    planned, block_live = _classify_article_blocks(raw_blocks, current_time_minutes)
    live_candidates = segment_live + block_live

    if markers:
        planned = _apply_marker_times(planned, markers)
    planned = _dedupe_planned(planned)

    live_item = _pick_live_item(live_candidates)
    schedule_status = list(planned)
    if live_item:
        schedule_status.append(live_item)
    schedule_status.extend(_parse_active_work_indicator(section, live_item))

    schedule_status = [
        item
        for item in (_finalize_schedule_item(i) for i in schedule_status)
        if item is not None
    ]
    schedule_status.sort(key=lambda e: _time_sort_key(e.get("start_time", "")))
    return schedule_status, markers


def parse_employee_container(container, timeline_section, date, *, current_time_minutes=None):
    result = {
        "date": date,
        "user": {},
        "summary_time": None,
        "schedule_status": [],
    }

    title = container.select_one('[data-scope="avatar-meta"] [data-part="title"]')
    if title:
        result["user"]["name"] = title.get_text(strip=True)

    role = container.select_one('[data-scope="avatar-meta"] [data-part="subtext"]')
    if role:
        result["user"]["role"] = role.get_text(strip=True)

    link = container.select_one('a[href*="/work-record/"]')
    if link:
        match = re.search(r"/work-record/([^?]+)", link["href"])
        if match:
            result["user"]["record_id"] = match.group(1)

    summary_elem = container.select_one(".c-ifYJlR")
    if summary_elem:
        result["summary_time"] = summary_elem.get_text(strip=True)

    if timeline_section:
        schedule_status, markers = _parse_timeline_section(
            timeline_section, current_time_minutes=current_time_minutes
        )
        result["schedule_status"] = schedule_status
        if markers:
            result["timeline_markers"] = markers

    return result


def parse_flex_hr_day_html(html_content: str) -> dict:
    """Flex HR 일간 HTML을 파싱한다."""
    soup = BeautifulSoup(html_content, "html.parser")

    date_match = re.search(r"date=(\d{4}-\d{2}-\d{2})", html_content)
    date = date_match.group(1) if date_match else None

    current_time_elem = soup.find(class_="c-FxSit")
    current_time = current_time_elem.get_text(strip=True) if current_time_elem else None
    current_time_minutes = _time_to_minutes(current_time) if current_time else None

    member_containers = soup.find_all(class_="c-gNZGXI")
    timeline_sections = soup.select(".c-eqXmhd .c-hzjAHE")

    members = []
    for idx, container in enumerate(member_containers):
        section = timeline_sections[idx] if idx < len(timeline_sections) else None
        members.append(
            parse_employee_container(
                container, section, date, current_time_minutes=current_time_minutes
            )
        )

    return {
        "period": "daily",
        "date": date,
        "current_time": current_time,
        "members": members,
    }


def is_daily_html(html_content: str) -> bool:
    """일간 타임라인 HTML 여부."""
    if re.search(r"period=monthly", html_content):
        return False
    if "c-gNZGXI" in html_content and "c-fQnhbx" not in html_content:
        return True
    return bool(
        BeautifulSoup(html_content, "html.parser").select(".c-gNZGXI, .c-eqXmhd .c-hzjAHE")
    )


# 하위 호환 alias
parse_flex_hr_html = parse_flex_hr_day_html
