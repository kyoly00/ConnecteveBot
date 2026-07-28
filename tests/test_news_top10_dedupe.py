"""news top10 hard dedupe — URL/제목/전날/회사명."""
from __future__ import annotations

from app.services.news.news_summary import (
    ArticleCandidate,
    dedupe_final_articles,
    previous_top10_exclusion_keys,
)


def _art(title: str, url: str, **kwargs) -> ArticleCandidate:
    return ArticleCandidate(
        source_key="t",
        source_name="t",
        source_priority="A",
        title=title,
        url=url,
        body_100="",
        primary_score=50,
        llm_title_score=70,
        final_score=kwargs.get("final_score", 80),
        category=kwargs.get("category", "의료AI/영상분석"),
        summary=kwargs.get("summary", "요약"),
    )


def test_dedupe_same_url_and_title():
    items = [
        _art("의료 AI 규제", "https://ex.com/a", final_score=90),
        _art("의료 AI 규제", "https://ex.com/a", final_score=90),
        _art("다른 기사", "https://ex.com/b", final_score=70),
    ]
    out = dedupe_final_articles(items, top_n=10)
    assert len(out) == 2
    assert out[0].url.endswith("/a")
    assert out[1].url.endswith("/b")


def test_dedupe_previous_top10_url_and_title():
    prev_urls, prev_titles = previous_top10_exclusion_keys(
        [
            {
                "title": "어제 기사",
                "url": "https://ex.com/yesterday",
            }
        ]
    )
    items = [
        _art("어제 기사", "https://ex.com/new-url", final_score=99),
        _art("새 기사", "https://ex.com/yesterday", final_score=98),
        _art("오늘 신규", "https://ex.com/today", final_score=80),
    ]
    out = dedupe_final_articles(
        items,
        top_n=10,
        previous_urls=prev_urls,
        previous_titles=prev_titles,
    )
    assert len(out) == 1
    assert out[0].url.endswith("/today")


def test_dedupe_company_keyword_once():
    items = [
        _art("뷰노, 병원 AI 도입 확대", "https://ex.com/1", final_score=90),
        _art("뷰노 실적 관련 후속", "https://ex.com/2", final_score=85),
        _art("루닛 신규 계약", "https://ex.com/3", final_score=80),
    ]
    out = dedupe_final_articles(items, top_n=10)
    assert len(out) == 2
    assert "뷰노" in out[0].title
    assert "루닛" in out[1].title
