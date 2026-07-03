"""RAG Turn3 이웃 parent 확장 단위 테스트."""

from app.rag.vectordb import expand_parent_ids_by_page_neighbors


def _catalog(page_id: str, sections: list[tuple[str, str]]) -> list[dict]:
    return [
        {
            "id": pid,
            "page_id": page_id,
            "section_id": sid,
            "section_title": title,
        }
        for pid, sid, title in (
            (f"p{i}", f"s{i:03d}", f"sec{i}") for i, (pid, sid, title) in enumerate(sections)
        )
    ]


def test_expand_neighbors_radius_two():
    catalog = [
        {"id": "a", "page_id": "pg1", "section_id": "001", "section_title": "s0"},
        {"id": "b", "page_id": "pg1", "section_id": "002", "section_title": "s1"},
        {"id": "c", "page_id": "pg1", "section_id": "003", "section_title": "s2"},
        {"id": "d", "page_id": "pg1", "section_id": "004", "section_title": "s3"},
        {"id": "e", "page_id": "pg1", "section_id": "005", "section_title": "s4"},
    ]
    expanded = expand_parent_ids_by_page_neighbors(
        ["c"],
        catalog,
        neighbor_radius=2,
        max_total=12,
        max_per_page=6,
    )
    assert expanded == ["c", "a", "b", "d", "e"]


def test_expand_respects_max_total():
    catalog = [
        {"id": f"id{i}", "page_id": "pg", "section_id": f"{i:03d}", "section_title": f"s{i}"}
        for i in range(10)
    ]
    expanded = expand_parent_ids_by_page_neighbors(
        ["id5"],
        catalog,
        neighbor_radius=4,
        max_total=3,
        max_per_page=6,
    )
    assert len(expanded) == 3
    assert expanded[0] == "id5"
