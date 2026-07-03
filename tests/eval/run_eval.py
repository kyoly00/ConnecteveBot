"""
ConnBot 평가 실행기 — QA 스냅샷 + debug 로그 → 레이어별 점수.

사용 (conn_eval_env):
  cd ConnBot/tests
  python -m eval.run_eval

  # 옵션 없이 실행 시: 최신 QA 스냅샷 + 매칭 debug run_ts 자동 선택
  # 기본 layers=rules,ragas,deepeval, judge=gpt-4o-mini, max_llm_cases=5
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent.parent
CONN_BOT_ROOT = TESTS_DIR.parent
REPO_ROOT = CONN_BOT_ROOT.parent
QA_RESULTS_DIR = TESTS_DIR / "qa_results"

DEFAULT_LAYERS = "rules,ragas,deepeval"
DEFAULT_JUDGE_MODEL = "gpt-4o-mini"
DEFAULT_MAX_LLM_CASES = 5
if str(CONN_BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(CONN_BOT_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

from eval.models import CaseResult, EvalCase, LayerScore
from eval.postprocess import build_eval_cases, collect_run_manifest
from eval.loaders import find_latest_qa_snapshot, load_qa_snapshot, resolve_debug_run_ts
from eval.scorers import score_all_rules, score_deepeval_batch, score_ragas_batch

KST = timezone(timedelta(hours=9))


def _kst_stamp() -> str:
    return datetime.now(KST).strftime("%Y%m%d_%H%M%S")


def _aggregate_pass(layers: list[LayerScore]) -> tuple[bool, list[str]]:
    actionable = [
        layer
        for layer in layers
        if layer.details.get("skipped") is None
    ]
    if not actionable:
        return False, ["all_layers_skipped"]

    failures: list[str] = []
    for layer in actionable:
        if not layer.passed:
            failures.append(layer.layer)
    return (not failures, failures)


def evaluate_cases(
    cases: list[EvalCase],
    *,
    layers: set[str],
    judge_model: str,
    max_llm_cases: int | None,
) -> list[CaseResult]:
    results: list[CaseResult] = []

    ragas_scores: dict[int, LayerScore] = {}
    deepeval_scores: dict[int, list[LayerScore]] = {}

    llm_targets = cases
    if max_llm_cases is not None:
        llm_targets = cases[:max_llm_cases]

    if "ragas" in layers:
        print(f"[eval] RAGAS 평가 중 ({len(llm_targets)}건)...")
        ragas_scores = score_ragas_batch(llm_targets, judge_model=judge_model)

    if "deepeval" in layers:
        print(f"[eval] DeepEval 평가 중 ({len(llm_targets)}건)...")
        deepeval_scores = score_deepeval_batch(
            llm_targets,
            judge_model=judge_model,
        )

    llm_target_ids = {c.index for c in llm_targets}

    for case in cases:
        layer_scores: list[LayerScore] = []
        llm_requested = bool(layers & {"ragas", "deepeval"})
        in_llm_batch = case.index in llm_target_ids

        if llm_requested and max_llm_cases is not None and not in_llm_batch and "rules" not in layers:
            results.append(
                CaseResult(
                    case=case,
                    layers=[
                        LayerScore(
                            layer="skipped",
                            score=None,
                            passed=True,
                            details={"skipped": "max_llm_cases"},
                        )
                    ],
                    overall_passed=False,
                    failure_reasons=["llm_not_in_batch"],
                )
            )
            continue

        if "rules" in layers:
            layer_scores.extend(score_all_rules(case))

        if "ragas" in layers and in_llm_batch:
            if case.index in ragas_scores:
                layer_scores.append(ragas_scores[case.index])
            else:
                from eval.scorers.ragas_scorers import score_ragas_case

                layer_scores.append(score_ragas_case(case, judge_model=judge_model))
        elif "ragas" in layers:
            layer_scores.append(
                LayerScore(
                    layer="L3_L5_ragas",
                    score=None,
                    passed=True,
                    details={"skipped": "max_llm_cases"},
                )
            )

        if "deepeval" in layers and in_llm_batch and case.index in deepeval_scores:
            layer_scores.extend(deepeval_scores[case.index])
        elif "deepeval" in layers and not in_llm_batch:
            layer_scores.append(
                LayerScore(
                    layer="L2_deepeval",
                    score=None,
                    passed=True,
                    details={"skipped": "max_llm_cases"},
                )
            )

        passed, failures = _aggregate_pass(layer_scores)
        results.append(
            CaseResult(
                case=case,
                layers=layer_scores,
                overall_passed=passed,
                failure_reasons=failures,
            )
        )

    return results


def _layer_summary(results: list[CaseResult]) -> dict[str, dict]:
    buckets: dict[str, list[float]] = {}
    pass_counts: dict[str, int] = {}
    total_counts: dict[str, int] = {}

    for result in results:
        for layer in result.layers:
            name = layer.layer
            total_counts[name] = total_counts.get(name, 0) + 1
            if layer.passed:
                pass_counts[name] = pass_counts.get(name, 0) + 1
            if layer.score is not None:
                buckets.setdefault(name, []).append(layer.score)

    summary: dict[str, dict] = {}
    for name, scores in buckets.items():
        summary[name] = {
            "count": total_counts.get(name, 0),
            "pass_count": pass_counts.get(name, 0),
            "pass_rate": round(
                pass_counts.get(name, 0) / max(total_counts.get(name, 1), 1),
                4,
            ),
            "avg_score": round(sum(scores) / len(scores), 4) if scores else None,
        }
    return summary


def write_outputs(
    results: list[CaseResult],
    *,
    output_dir: Path,
    manifest: dict | None,
    run_meta: dict,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    cases_dir = output_dir / "cases"
    failures_dir = output_dir / "failures"
    cases_dir.mkdir(exist_ok=True)
    failures_dir.mkdir(exist_ok=True)

    if manifest:
        (output_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    summary = {
        "run": run_meta,
        "total_cases": len(results),
        "passed_cases": sum(1 for r in results if r.overall_passed),
        "failed_cases": sum(1 for r in results if not r.overall_passed),
        "pass_rate": round(
            sum(1 for r in results if r.overall_passed) / max(len(results), 1),
            4,
        ),
        "layers": _layer_summary(results),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    failure_lines: list[str] = [
        "# Eval Failures",
        "",
        f"- run: {run_meta.get('run_id')}",
        f"- pass_rate: {summary['pass_rate']}",
        "",
    ]

    for result in results:
        case_path = cases_dir / f"case_{result.case.index:04d}.json"
        case_path.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        if result.overall_passed:
            continue

        failure_lines.extend(
            [
                f"## [{result.case.index:04d}] {result.case.question_type}",
                "",
                f"- **question**: {result.case.question}",
                f"- **expected_intent**: {result.case.expected_intent} → **actual**: {result.case.intent}",
                f"- **pass_hint**: `{result.case.pass_hint}`",
                f"- **failed_layers**: {', '.join(result.failure_reasons)}",
                "",
                "<details><summary>answer preview</summary>",
                "",
                "```",
                (result.case.answer or "")[:800],
                "```",
                "",
                "</details>",
                "",
            ]
        )

    (failures_dir / "summary.md").write_text(
        "\n".join(failure_lines),
        encoding="utf-8",
    )


def _resolve_qa_snapshot_path(arg: str) -> Path:
    if arg.strip():
        for candidate in (
            (TESTS_DIR / arg).resolve(),
            Path(arg).resolve(),
        ):
            if candidate.is_file():
                return candidate
        return Path(arg).resolve()

    latest = find_latest_qa_snapshot(QA_RESULTS_DIR)
    if latest is None:
        print(
            f"QA 스냅샷 없음. 먼저 run_qa_weak_questions.py를 실행하거나 "
            f"--qa-snapshot을 지정하세요. (검색: {QA_RESULTS_DIR})"
        )
        sys.exit(1)
    return latest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ConnBot RAGAS+DeepEval 평가 실행 (인자 생략 시 최신 QA·debug 자동)"
    )
    parser.add_argument(
        "--qa-snapshot",
        default="",
        help="QA JSONL (기본: qa_results/weak_question_answers_*.jsonl 최신)",
    )
    parser.add_argument(
        "--debug-root",
        default=str(REPO_ROOT / "debug"),
        help="debug/{run_ts}/{session_id} 루트",
    )
    parser.add_argument(
        "--debug-run-ts",
        default="",
        help="debug run 타임스탬프 (기본: QA 스냅샷 session_id와 매칭되는 최신 run)",
    )
    parser.add_argument(
        "--output",
        default="",
        help="출력 디렉토리 (기본: eval_runs/eval_YYYYMMDD_HHMMSS)",
    )
    parser.add_argument(
        "--layers",
        default=DEFAULT_LAYERS,
        help=f"평가 레이어 (기본: {DEFAULT_LAYERS})",
    )
    parser.add_argument(
        "--judge-model",
        default=DEFAULT_JUDGE_MODEL,
        help=f"RAGAS/DeepEval judge LLM (기본: {DEFAULT_JUDGE_MODEL})",
    )
    parser.add_argument(
        "--max-llm-cases",
        type=int,
        default=DEFAULT_MAX_LLM_CASES,
        help=f"LLM 평가 최대 케이스 수, 0=전체 (기본: {DEFAULT_MAX_LLM_CASES})",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    layers = {x.strip() for x in args.layers.split(",") if x.strip()}
    needs_llm = bool(layers & {"ragas", "deepeval"})
    if needs_llm and not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY가 없습니다. .env 또는 환경변수를 설정하세요.")
        sys.exit(1)

    qa_path = _resolve_qa_snapshot_path(args.qa_snapshot)
    if not qa_path.is_file():
        print(f"QA 스냅샷 없음: {qa_path}")
        sys.exit(1)

    debug_root = Path(args.debug_root).resolve()
    qa_rows = load_qa_snapshot(qa_path)
    if not qa_rows:
        print("QA 스냅샷이 비어 있습니다.")
        sys.exit(1)

    debug_run_ts = resolve_debug_run_ts(
        debug_root,
        qa_rows,
        prefer_run_ts=args.debug_run_ts,
    )
    if not debug_run_ts:
        print(f"debug run을 찾지 못했습니다: {debug_root}")
        sys.exit(1)

    print(f"QA 스냅샷: {qa_path} ({len(qa_rows)}건)")
    print(f"debug root: {debug_root}")
    print(f"debug run_ts: {debug_run_ts}" + (" (자동)" if not args.debug_run_ts else ""))

    cases = build_eval_cases(
        qa_rows,
        debug_root=debug_root,
        prefer_run_ts=debug_run_ts,
    )
    linked = sum(1 for c in cases if c.eval_summary)
    print(f"eval_summary 조인: {linked}/{len(cases)}")

    max_llm = args.max_llm_cases if args.max_llm_cases > 0 else None
    results = evaluate_cases(
        cases,
        layers=layers,
        judge_model=args.judge_model,
        max_llm_cases=max_llm,
    )

    run_id = f"eval_{_kst_stamp()}"
    output_dir = (
        Path(args.output).resolve()
        if args.output
        else (TESTS_DIR / "eval_runs" / run_id)
    )

    manifest = collect_run_manifest(cases, debug_root)
    run_meta = {
        "run_id": run_id,
        "qa_snapshot": str(qa_path),
        "debug_root": str(debug_root),
        "debug_run_ts": debug_run_ts,
        "layers": sorted(layers),
        "judge_model": args.judge_model,
        "max_llm_cases": max_llm,
    }

    write_outputs(results, output_dir=output_dir, manifest=manifest, run_meta=run_meta)

    passed = sum(1 for r in results if r.overall_passed)
    print(f"\n완료 → {output_dir}")
    print(f"통과: {passed}/{len(results)} ({passed / max(len(results), 1):.1%})")


if __name__ == "__main__":
    main()
