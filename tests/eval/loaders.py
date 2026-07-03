"""weak_question·QA 스냅샷·디버그 JSONL 로더."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterator


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def load_weak_questions(txt_path: Path) -> list[dict[str, str]]:
    if not txt_path.exists():
        raise FileNotFoundError(f"질문 파일 없음: {txt_path}")

    dataset: list[dict[str, str]] = []
    for raw in txt_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "||" not in line:
            dataset.append({"question": line, "type": "Unknown"})
            continue
        question, meta = line.split("||", 1)
        parts = [p.strip() for p in meta.strip().split("|")]
        item: dict[str, str] = {
            "question": question.strip(),
            "type": parts[0] if parts else meta.strip(),
        }
        if len(parts) > 1:
            item["expected_intent"] = parts[1]
        if len(parts) > 2:
            item["pass_hint"] = parts[2]
        dataset.append(item)
    return dataset


def load_qa_snapshot(path: Path) -> list[dict[str, Any]]:
    return load_jsonl(path)


_RUN_TS_RE = re.compile(r"^\d{8}T\d{6}Z$")


def iter_debug_runs(debug_root: Path) -> Iterator[Path]:
    if not debug_root.is_dir():
        return
    for child in sorted(debug_root.iterdir(), reverse=True):
        if child.is_dir() and _RUN_TS_RE.match(child.name):
            yield child


def find_session_debug_dir(
    debug_root: Path,
    session_id: str,
    *,
    prefer_run_ts: str = "",
) -> tuple[str, Path] | None:
    """session_id에 해당하는 debug/{run_ts}/{session_id} 폴더 탐색."""
    candidates: list[tuple[str, Path]] = []
    if prefer_run_ts:
        preferred = debug_root / prefer_run_ts / session_id
        if (preferred / "09_llm_io.jsonl").exists():
            return prefer_run_ts, preferred

    for run_dir in iter_debug_runs(debug_root):
        session_dir = run_dir / session_id
        if (session_dir / "09_llm_io.jsonl").exists():
            candidates.append((run_dir.name, session_dir))

    return candidates[0] if candidates else None


def load_llm_io_rows(session_dir: Path) -> list[dict[str, Any]]:
    return load_jsonl(session_dir / "09_llm_io.jsonl")


def load_search_pipeline_rows(session_dir: Path) -> list[dict[str, Any]]:
    return load_jsonl(session_dir / "06_search_pipeline.jsonl")


def load_run_manifest(debug_root: Path, run_ts: str) -> dict[str, Any] | None:
    path = debug_root / run_ts / "00_run_manifest.jsonl"
    rows = load_jsonl(path)
    return rows[0] if rows else None


def extract_eval_summary(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in reversed(rows):
        if row.get("phase") == "eval_summary":
            return row
    return None


def extract_rag_research(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in reversed(rows):
        if row.get("phase") == "rag_research":
            return row
    return None


def extract_pipeline_final(
    search_rows: list[dict[str, Any]],
    *,
    search_kind: str = "primary",
) -> list[dict[str, Any]]:
    finals: list[dict[str, Any]] = []
    for row in search_rows:
        if row.get("step") != "pipeline_final":
            continue
        if search_kind and row.get("search_kind") not in ("", search_kind):
            continue
        chunk = row.get("final_for_llm") or []
        if isinstance(chunk, list):
            finals.extend(chunk)
    return finals


def contexts_from_pipeline_final(rows: list[dict[str, Any]]) -> list[str]:
    contexts: list[str] = []
    for item in rows:
        title = str(item.get("page_title") or "").strip()
        section = str(item.get("section_title") or "").strip()
        summary = str(item.get("page_summary") or "").strip()
        parts = [p for p in (title, section, summary) if p]
        if parts:
            contexts.append(" | ".join(parts))
    return contexts


def find_latest_qa_snapshot(qa_results_dir: Path) -> Path | None:
    """qa_results/weak_question_answers_*.jsonl 중 최신 파일."""
    if not qa_results_dir.is_dir():
        return None
    files = [
        p
        for p in qa_results_dir.glob("weak_question_answers_*.jsonl")
        if p.is_file()
    ]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def resolve_debug_run_ts(
    debug_root: Path,
    qa_rows: list[dict[str, Any]],
    *,
    prefer_run_ts: str = "",
) -> str:
    """QA 스냅샷 session_id와 매칭되는 debug run_ts (미지정 시 최신 QA 기준)."""
    explicit = (prefer_run_ts or "").strip()
    if explicit:
        return explicit

    session_ids = [
        str(row.get("session_id") or "").strip()
        for row in qa_rows
        if row.get("session_id")
    ]

    best_ts = ""
    best_count = -1
    for run_dir in iter_debug_runs(debug_root):
        if not session_ids:
            return run_dir.name
        count = sum(
            1
            for sid in session_ids
            if (run_dir / sid / "09_llm_io.jsonl").exists()
        )
        if count > best_count:
            best_count = count
            best_ts = run_dir.name
        if count == len(session_ids):
            return run_dir.name

    if best_ts:
        return best_ts

    for run_dir in iter_debug_runs(debug_root):
        return run_dir.name
    return ""
