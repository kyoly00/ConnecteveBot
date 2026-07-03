"""Flex 월간 근태 — 가상 스크롤 대응 점진 수집 + tooltip enrich."""

from __future__ import annotations

import os
import time

from app.services.flex_hr.flex_parser.month_parser import (
    build_monthly_result,
    member_dedupe_key,
    merge_monthly_member,
    parse_month_nav_label,
    parse_tooltip_time,
    parse_visible_monthly_rows,
)

MONTH_NAV = "nav.c-cOeAxU"
MONTH_LABEL_SELECTOR = f'{MONTH_NAV} [class*="periodType-monthly"]'
MONTH_PREV_SELECTOR = (
    f'{MONTH_NAV} .c-cOSqbm > button[data-scope="icon-button"]:first-child'
)
MONTH_NEXT_SELECTOR = (
    f'{MONTH_NAV} .c-cOSqbm > button[data-scope="icon-button"]:last-child'
)

TRIGGER_SELECTOR = (
    '.c-wIHID-eBcQxc-period-monthly [data-scope="tooltip"][data-part="trigger"]'
)
CARD_SELECTOR = ".c-fQnhbx"
TOOLTIP_SELECTOR = (
    '[data-scope="tooltip"][data-part="definition-list-description"]:visible'
)
SCROLL_CONTAINER = ".c-gCFtop"

_SCROLL_HORIZONTAL_JS = """
() => {
  for (const el of document.querySelectorAll(".c-kdPqYH, .c-fozXlO, .c-jrjMcV")) {
    el.scrollLeft = el.scrollWidth;
  }
}
"""

_SCROLL_DOWN_JS = """
() => {
  const scrollEl = document.querySelector(".c-gCFtop");
  if (!scrollEl) {
    return { found: false, moved: false, atBottom: true };
  }

  const before = scrollEl.scrollTop;
  const stepPx = window.__flexMonthlyScrollStepPx || 300;
  scrollEl.scrollTop = Math.min(scrollEl.scrollTop + stepPx, scrollEl.scrollHeight);

  const atBottom =
    scrollEl.scrollTop + scrollEl.clientHeight >= scrollEl.scrollHeight - 2;

  return {
    found: true,
    moved: scrollEl.scrollTop > before,
    atBottom,
    scrollTop: scrollEl.scrollTop,
    scrollHeight: scrollEl.scrollHeight,
  };
}
"""


def _timing_debug() -> bool:
    return os.getenv("FLEX_MONTHLY_TIMING_DEBUG", "true").lower() in (
        "1",
        "true",
        "yes",
    )


def _ms_since(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000


def _fmt_ms(ms: float) -> str:
    if ms >= 1000:
        return f"{ms / 1000:.1f}s"
    return f"{ms:.0f}ms"


def _monthly_tooltip_enrich_enabled() -> bool:
    return os.getenv("FLEX_MONTHLY_TOOLTIP_ENRICH", "true").lower() in (
        "1",
        "true",
        "yes",
    )


def _hover_settle_ms() -> int:
    return int(os.getenv("FLEX_MONTHLY_HOVER_SETTLE_MS", "80") or "80")


def _hover_wait_ms() -> int:
    return int(os.getenv("FLEX_MONTHLY_HOVER_MS", "200") or "200")


def _tooltip_timeout_ms() -> int:
    return int(os.getenv("FLEX_MONTHLY_TOOLTIP_TIMEOUT_MS", "800") or "800")


def _scroll_step_ms() -> int:
    return int(os.getenv("FLEX_MONTHLY_SCROLL_STEP_MS", "200") or "200")


def _scroll_step_px() -> int:
    return int(os.getenv("FLEX_MONTHLY_SCROLL_STEP_PX", "250") or "250")


def _load_settle_ms() -> int:
    return int(os.getenv("FLEX_MONTHLY_LOAD_SETTLE_MS", "400") or "400")


def _horiz_settle_ms() -> int:
    return int(os.getenv("FLEX_MONTHLY_HORIZ_SETTLE_MS", "120") or "120")


def _hover_clear_ms() -> int:
    return int(os.getenv("FLEX_MONTHLY_HOVER_CLEAR_MS", "30") or "30")


def _max_scroll_rounds() -> int:
    return int(os.getenv("FLEX_MONTHLY_SCROLL_MAX_ROUNDS", "300") or "300")


def _nav_settle_ms() -> int:
    return int(os.getenv("FLEX_MONTHLY_NAV_SETTLE_MS", "400") or "400")


def _max_month_nav_clicks() -> int:
    return int(os.getenv("FLEX_MONTHLY_NAV_MAX_CLICKS", "36") or "36")


def read_displayed_year_month(page) -> tuple[int, int] | None:
    """월간 네비게이션에 표시된 연·월을 읽는다."""
    loc = page.locator(MONTH_LABEL_SELECTOR).first
    if loc.count() == 0:
        return None
    return parse_month_nav_label(loc.inner_text(timeout=5000))


def navigate_to_target_month(page, target_year: int, target_month: int) -> tuple[int, int]:
    """
    이전/다음 월 버튼으로 목표 연·월까지 이동하고 화면 라벨을 검증한다.
    URL date 매개변수 대신 UI 네비게이션을 사용한다.
    """
    page.wait_for_selector(MONTH_NAV, timeout=120000)
    settle_ms = _nav_settle_ms()
    label_loc = page.locator(MONTH_LABEL_SELECTOR).first

    for step in range(_max_month_nav_clicks() + 1):
        current = read_displayed_year_month(page)
        if current is None:
            raise RuntimeError("월간 네비게이션 라벨을 읽을 수 없습니다.")

        cy, cm = current
        if cy == target_year and cm == target_month:
            print(f"  목표 월 확인: {target_year}. {target_month}")
            if settle_ms > 0:
                page.wait_for_timeout(settle_ms)
            page.wait_for_selector(
                f"{SCROLL_CONTAINER}, .c-bZNrxE, .c-fQnhbx", timeout=120000
            )
            return current

        current_val = cy * 12 + cm
        target_val = target_year * 12 + target_month
        if current_val < target_val:
            btn = page.locator(MONTH_NEXT_SELECTOR).first
            direction = "다음"
        else:
            btn = page.locator(MONTH_PREV_SELECTOR).first
            direction = "이전"

        if btn.get_attribute("aria-disabled") == "true" or btn.is_disabled():
            raise RuntimeError(
                f"월 이동 한계 — 현재 {cy}.{cm}, 목표 {target_year}.{target_month}"
            )

        prev_text = label_loc.inner_text()
        print(f"  월 이동({direction}): {cy}.{cm} → {target_year}.{target_month}")
        btn.click()

        changed = False
        for _ in range(30):
            wait_ms = max(settle_ms // 5, 80)
            page.wait_for_timeout(wait_ms)
            if label_loc.inner_text() != prev_text:
                changed = True
                break
        if not changed and settle_ms > 0:
            page.wait_for_timeout(settle_ms)

    raise RuntimeError(
        f"월 네비게이션 실패(최대 {step + 1}회 시도) — "
        f"목표 {target_year}.{target_month}"
    )


def inject_monthly_card_tooltips(page, *, only_new: bool = True) -> dict:
    """
    화면에 보이는 월간 카드에 hover → 툴팁 시각을 .c-fQnhbx DOM 속성으로 기록.

    only_new=True이면 이미 처리한 카드(data-flex-tooltip-done)는 대기 없이 skip.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    t0 = time.perf_counter()
    timing = {
        "scroll_into_view": 0.0,
        "settle": 0.0,
        "hover": 0.0,
        "read": 0.0,
        "clear": 0.0,
    }

    if not _monthly_tooltip_enrich_enabled():
        return {"enabled": False, "total": 0, "enriched": 0, "errors": 0, "skipped": 0}

    triggers = page.locator(TRIGGER_SELECTOR)
    count = triggers.count()
    stats = {
        "enabled": True,
        "total": count,
        "enriched": 0,
        "errors": 0,
        "skipped": 0,
        "already_done": 0,
        "processed": 0,
    }

    if count == 0:
        stats["timing_ms"] = {**timing, "total": 0.0}
        return stats

    settle_ms = _hover_settle_ms()
    hover_ms = _hover_wait_ms()
    read_timeout = _tooltip_timeout_ms()
    clear_ms = _hover_clear_ms()

    for i in range(count):
        trigger = triggers.nth(i)
        try:
            card_loc = trigger.locator(CARD_SELECTOR)
            if card_loc.count() == 0:
                stats["skipped"] += 1
                continue

            card = card_loc.first

            if only_new and card.get_attribute("data-flex-tooltip-done"):
                stats["already_done"] += 1
                continue

            stats["processed"] += 1

            t_view = time.perf_counter()
            trigger.scroll_into_view_if_needed(timeout=2000)
            timing["scroll_into_view"] += _ms_since(t_view)

            if settle_ms > 0:
                page.wait_for_timeout(settle_ms)
                timing["settle"] += settle_ms

            trigger.hover()
            if hover_ms > 0:
                page.wait_for_timeout(hover_ms)
                timing["hover"] += hover_ms

            tooltip = ""
            t_read = time.perf_counter()
            try:
                tooltip = page.locator(TOOLTIP_SELECTOR).last.inner_text(timeout=read_timeout)
            except PlaywrightTimeoutError:
                tooltip = ""
            timing["read"] += _ms_since(t_read)

            parsed = parse_tooltip_time(tooltip)
            start_24h = parsed.get("start_24h") or ""
            end_24h = parsed.get("end_24h") or ""

            if start_24h and end_24h:
                card.evaluate(
                    """
                    (el, data) => {
                      el.setAttribute('data-flex-start-time', data.start);
                      el.setAttribute('data-flex-end-time', data.end);
                      if (data.tooltip) {
                        el.setAttribute('data-flex-tooltip', data.tooltip);
                      }
                      el.setAttribute('data-flex-tooltip-done', '1');
                    }
                    """,
                    {"start": start_24h, "end": end_24h, "tooltip": tooltip.strip()},
                )
                stats["enriched"] += 1
            else:
                card.evaluate("(el) => el.setAttribute('data-flex-tooltip-done', '1')")
                stats["skipped"] += 1

            page.mouse.move(0, 0)
            if clear_ms > 0:
                page.wait_for_timeout(clear_ms)
                timing["clear"] += clear_ms

        except Exception:
            stats["errors"] += 1

    stats["timing_ms"] = {**timing, "total": _ms_since(t0)}
    return stats


def _log_tooltip_timing(round_no: int, tip_stats: dict) -> None:
    if not _timing_debug():
        return
    tm = tip_stats.get("timing_ms") or {}
    print(
        f"    tooltip {_fmt_ms(tm.get('total', 0))}: "
        f"cards={tip_stats.get('total', 0)} "
        f"new={tip_stats.get('processed', 0)} "
        f"skip_done={tip_stats.get('already_done', 0)} "
        f"| view={_fmt_ms(tm.get('scroll_into_view', 0))} "
        f"settle={_fmt_ms(tm.get('settle', 0))} "
        f"hover={_fmt_ms(tm.get('hover', 0))} "
        f"read={_fmt_ms(tm.get('read', 0))}"
    )


def crawl_flex_hr_monthly(page, *, target_url: str = "") -> dict:
    """
    가상 스크롤 월간 그리드를 점진 수집한다.

    1. 로딩 대기
    2. 보이는 row 파싱 + tooltip enrich
    3. .c-gCFtop 아래로 조금 스크롤
    4. 끝까지 반복
    5. 중복 제거·병합 후 결과 반환
    """
    crawl_t0 = time.perf_counter()
    total_timing = {
        "tooltip": 0.0,
        "parse": 0.0,
        "scroll": 0.0,
        "settle": 0.0,
        "rounds": 0,
    }

    page.wait_for_selector(f"{SCROLL_CONTAINER}, .c-bZNrxE, .c-fQnhbx", timeout=120000)
    load_ms = _load_settle_ms()
    if load_ms > 0:
        page.wait_for_timeout(load_ms)
        total_timing["settle"] += load_ms

    page.evaluate(
        f"() => {{ window.__flexMonthlyScrollStepPx = {_scroll_step_px()}; }}"
    )
    page.evaluate(_SCROLL_HORIZONTAL_JS)
    horiz_ms = _horiz_settle_ms()
    if horiz_ms > 0:
        page.wait_for_timeout(horiz_ms)
        total_timing["settle"] += horiz_ms

    html = page.content()
    _, headers, year, month = parse_visible_monthly_rows(html)

    members_by_key: dict[str, dict] = {}
    member_order: list[str] = []
    tooltip_total = {"enriched": 0, "skipped": 0, "errors": 0, "already_done": 0}
    stable_bottom_rounds = 0
    round_idx = 0
    scroll_step_ms = _scroll_step_ms()

    print(
        f"  월간 가상 스크롤 수집 시작 "
        f"(step={_scroll_step_px()}px, scroll_wait={scroll_step_ms}ms, "
        f"hover={_hover_settle_ms()}+{_hover_wait_ms()}ms)"
    )

    for round_idx in range(_max_scroll_rounds()):
        round_t0 = time.perf_counter()

        t_tip = time.perf_counter()
        tip_stats = inject_monthly_card_tooltips(page, only_new=True)
        tip_ms = _ms_since(t_tip)
        total_timing["tooltip"] += tip_ms
        for key in ("enriched", "skipped", "errors", "already_done"):
            tooltip_total[key] = tooltip_total.get(key, 0) + tip_stats.get(key, 0)

        t_parse = time.perf_counter()
        chunk, headers, year, month = parse_visible_monthly_rows(page.content(), headers)
        parse_ms = _ms_since(t_parse)
        total_timing["parse"] += parse_ms

        new_members = 0
        for member in chunk:
            key = member_dedupe_key(member)
            if not key or key == "name:":
                continue
            if key not in members_by_key:
                members_by_key[key] = member
                member_order.append(key)
                new_members += 1
            else:
                members_by_key[key] = merge_monthly_member(members_by_key[key], member)

        t_scroll = time.perf_counter()
        scroll_state = page.evaluate(_SCROLL_DOWN_JS)
        scroll_ms = _ms_since(t_scroll)
        total_timing["scroll"] += scroll_ms

        if not scroll_state.get("found"):
            print("  경고: .c-gCFtop 스크롤 컨테이너를 찾지 못했습니다.")
            break

        at_bottom = bool(scroll_state.get("atBottom"))
        moved = bool(scroll_state.get("moved"))

        wait_ms = 0.0
        if moved and scroll_step_ms > 0 and not at_bottom:
            page.wait_for_timeout(scroll_step_ms)
            wait_ms = scroll_step_ms
            total_timing["settle"] += wait_ms

        round_ms = _ms_since(round_t0)
        total_timing["rounds"] += 1

        if _timing_debug():
            print(
                f"  라운드 {round_idx + 1}: +{new_members}명 "
                f"(누적 {len(members_by_key)}명) "
                f"| total {_fmt_ms(round_ms)} "
                f"(tip {_fmt_ms(tip_ms)}, parse {_fmt_ms(parse_ms)}, "
                f"scroll {_fmt_ms(scroll_ms)}, wait {_fmt_ms(wait_ms)})"
            )
            _log_tooltip_timing(round_idx + 1, tip_stats)
        elif new_members:
            print(
                f"  라운드 {round_idx + 1}: +{new_members}명 "
                f"(누적 {len(members_by_key)}명, tooltip +{tip_stats.get('enriched', 0)})"
            )

        if new_members:
            stable_bottom_rounds = 0
        elif at_bottom and not moved:
            stable_bottom_rounds += 1
            if stable_bottom_rounds >= 2:
                break
        elif at_bottom:
            stable_bottom_rounds += 1
            if stable_bottom_rounds >= 2:
                break

    members = [members_by_key[key] for key in member_order]
    result = build_monthly_result(members, headers, year, month)
    crawl_ms = _ms_since(crawl_t0)

    print(
        f"  월간 가상 스크롤 수집 완료: {len(members)}명 / {len(headers)}일 "
        f"(라운드 {round_idx + 1}, tooltip enriched={tooltip_total['enriched']}, "
        f"skipped={tooltip_total['skipped']}, errors={tooltip_total['errors']})"
    )
    if _timing_debug():
        print(
            f"  [timing] 총 {_fmt_ms(crawl_ms)} | "
            f"tooltip {_fmt_ms(total_timing['tooltip'])} "
            f"parse {_fmt_ms(total_timing['parse'])} "
            f"scroll {_fmt_ms(total_timing['scroll'])} "
            f"wait {_fmt_ms(total_timing['settle'])} "
            f"({total_timing['rounds']}라운드)"
        )

    return result
