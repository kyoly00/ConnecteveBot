"""Turn3 XML answer 파싱 테스트."""

import sys
from pathlib import Path

_CONN_BOT = Path(__file__).resolve().parents[1]
_ROOT = Path(__file__).resolve().parents[2]
for _path in (_ROOT, _CONN_BOT):
    _s = str(_path)
    if _s not in sys.path:
        sys.path.insert(0, _s)

from app.agent.router import parse_rag_structured_response


def test_merge_multiple_answer_tags():
    raw = (
        "<answer>도입문입니다.</answer>\n"
        "<answer>\n"
        "| 항목 | 값 |\n"
        "| --- | --- |\n"
        "| A | 1 |\n"
        "</answer>\n"
        "<sources_used>1</sources_used>\n"
        "<attachments_used>none</attachments_used>\n"
        "<links_used>none</links_used>"
    )
    answer, sources, _, _ = parse_rag_structured_response(raw, max_docs=5)
    assert "도입문입니다." in answer
    assert "| 항목 |" in answer
    assert sources == [1]
