"""벤치마크 지표 집계."""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class JobTiming:
    index: int
    session_id: str
    question: str
    inference_sec: float
    queue_wait_sec: float = 0.0
    enqueue_sec: float = 0.0
    intent: str = ""
    error: str = ""


@dataclass
class StrategyReport:
    strategy: str
    worker_count: int
    question_count: int
    wall_sec: float
    enqueue_sec: float
    throughput_qps: float
    inference_sec: dict[str, float]
    queue_wait_sec: dict[str, float]
    errors: int
    jobs: list[JobTiming] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "jobs": [asdict(j) for j in self.jobs],
        }


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = (len(ordered) - 1) * (pct / 100.0)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def summarize_timings(values: list[float]) -> dict[str, float]:
    if not values:
        return {"min": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0, "mean": 0.0, "total": 0.0}
    return {
        "min": round(min(values), 3),
        "p50": round(percentile(values, 50), 3),
        "p95": round(percentile(values, 95), 3),
        "max": round(max(values), 3),
        "mean": round(statistics.mean(values), 3),
        "total": round(sum(values), 3),
    }


def build_report(
    *,
    strategy: str,
    worker_count: int,
    jobs: list[JobTiming],
    wall_sec: float,
    enqueue_sec: float = 0.0,
    notes: str = "",
) -> StrategyReport:
    inference_values = [j.inference_sec for j in jobs if not j.error]
    wait_values = [j.queue_wait_sec for j in jobs if j.queue_wait_sec > 0]
    errors = sum(1 for j in jobs if j.error)
    qps = len(jobs) / wall_sec if wall_sec > 0 else 0.0
    return StrategyReport(
        strategy=strategy,
        worker_count=worker_count,
        question_count=len(jobs),
        wall_sec=round(wall_sec, 3),
        enqueue_sec=round(enqueue_sec, 3),
        throughput_qps=round(qps, 4),
        inference_sec=summarize_timings(inference_values),
        queue_wait_sec=summarize_timings(wait_values),
        errors=errors,
        jobs=jobs,
        notes=notes,
    )


def format_comparison_table(reports: list[StrategyReport]) -> str:
    header = (
        f"{'strategy':<18} {'workers':>7} {'wall(s)':>8} {'enqueue':>8} "
        f"{'qps':>7} {'inf_p50':>8} {'inf_p95':>8} {'wait_p50':>9} {'err':>4}"
    )
    lines = [header, "-" * len(header)]
    for r in reports:
        lines.append(
            f"{r.strategy:<18} {r.worker_count:>7} {r.wall_sec:>8.3f} {r.enqueue_sec:>8.3f} "
            f"{r.throughput_qps:>7.3f} {r.inference_sec.get('p50', 0):>8.3f} "
            f"{r.inference_sec.get('p95', 0):>8.3f} {r.queue_wait_sec.get('p50', 0):>9.3f} "
            f"{r.errors:>4}"
        )
    return "\n".join(lines)
