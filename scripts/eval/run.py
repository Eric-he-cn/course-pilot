"""Unified entrypoint for dataset lint / benchmark / judge / review pipelines."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BENCHMARKS = ROOT / "benchmarks"
LEGACY_BASELINE = BENCHMARKS / "archive" / "20260415_legacy_reset"


def _python_bin() -> str:
    return os.getenv("EVAL_PYTHON_BIN", "").strip() or sys.executable


def _run(cmd: list[str]) -> int:
    print(f"[eval.run] {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=str(ROOT), check=False).returncode


def _nonempty(path: Path) -> bool:
    return path.exists() and path.is_file() and path.stat().st_size > 0


def _first_nonempty(*paths: Path) -> Path:
    for path in paths:
        if _nonempty(path):
            return path
    return paths[0]


def _canonical_cases() -> Path:
    return _first_nonempty(BENCHMARKS / "cases_v1.jsonl", LEGACY_BASELINE / "cases_v1.jsonl")


def _canonical_gold() -> Path:
    return _first_nonempty(BENCHMARKS / "rag_gold_v1.jsonl", LEGACY_BASELINE / "rag_gold_v1.jsonl")


def _smoke_cases() -> Path:
    return _first_nonempty(BENCHMARKS / "cases_v1_smoke3.jsonl", LEGACY_BASELINE / "cases_v1_smoke3.jsonl")


def _lint_dataset_path() -> Path:
    return _first_nonempty(BENCHMARKS / "v3_expanded_84.jsonl", LEGACY_BASELINE / "v3_expanded_84.jsonl", BENCHMARKS)


def _smoke_args() -> tuple[list[str], list[str]]:
    out_dir = ROOT / "data" / "perf_runs" / "smoke_eval"
    bench_out = out_dir / "benchmark"
    bench_args = [
        _python_bin(),
        "-m",
        "scripts.perf.bench_runner",
        "--cases",
        str(_smoke_cases()),
        "--gold",
        str(_canonical_gold()),
        "--output-dir",
        str(bench_out),
        "--profile",
        "smoke_eval",
        "--repeats",
        "1",
        "--gold-mismatch-policy",
        "warn",
        "--gate-policy",
        "fail",
    ]
    lint_args = [
        _python_bin(),
        "-m",
        "scripts.eval.dataset_lint",
        "--path",
        str(_lint_dataset_path()),
        "--output-dir",
        str(out_dir / "dataset_lint"),
    ]
    return lint_args, bench_args


def _full_args() -> tuple[list[str], list[str], list[str], list[str]]:
    out_dir = ROOT / "data" / "perf_runs" / "nightly_eval"
    bench_out = out_dir / "benchmark"
    judge_out = out_dir / "judge"
    review_out = out_dir / "review"
    bench_args = [
        _python_bin(),
        "-m",
        "scripts.perf.bench_runner",
        "--cases",
        str(_canonical_cases()),
        "--gold",
        str(_canonical_gold()),
        "--output-dir",
        str(bench_out),
        "--profile",
        "nightly_eval",
        "--repeats",
        "1",
        "--gold-mismatch-policy",
        "warn",
        "--gate-policy",
        "warn",
    ]
    judge_args = [
        _python_bin(),
        "-m",
        "scripts.eval.judge_runner",
        "--raw",
        str(bench_out / "baseline_raw.jsonl"),
        "--cases",
        str(_canonical_cases()),
        "--output-dir",
        str(judge_out),
        "--profile",
        "nightly_eval",
    ]
    review_args = [
        _python_bin(),
        "-m",
        "scripts.eval.review_runner",
        "--benchmark-summary",
        str(bench_out / "baseline_summary.json"),
        "--benchmark-raw",
        str(bench_out / "baseline_raw.jsonl"),
        "--judge-summary",
        str(judge_out / "judge_summary.json"),
        "--judge-raw",
        str(judge_out / "judge_raw.jsonl"),
        "--output-dir",
        str(review_out),
    ]
    lint_args = [
        _python_bin(),
        "-m",
        "scripts.eval.dataset_lint",
        "--path",
        str(_lint_dataset_path()),
        "--output-dir",
        str(out_dir / "dataset_lint"),
    ]
    return lint_args, bench_args, judge_args, review_args


def main() -> int:
    parser = argparse.ArgumentParser(description="Unified eval orchestration entrypoint.")
    parser.add_argument("target", choices=["smoke", "full", "review"])
    parser.add_argument("--benchmark-dir", default="", help="Used by review target to point at an existing benchmark dir.")
    parser.add_argument("--judge-dir", default="", help="Used by review target to point at an existing judge dir.")
    parser.add_argument("--output-dir", default="", help="Used by review target to override review output dir.")
    args = parser.parse_args()

    if args.target == "smoke":
        lint_args, bench_args = _smoke_args()
        return _run(lint_args) or _run(bench_args)

    if args.target == "full":
        lint_args, bench_args, judge_args, review_args = _full_args()
        rc = _run(lint_args)
        if rc != 0:
            return rc
        rc = _run(bench_args)
        if rc != 0:
            return rc
        rc = _run(judge_args)
        if rc != 0:
            return rc
        return _run(review_args)

    benchmark_dir = Path(args.benchmark_dir or (ROOT / "data" / "perf_runs" / "nightly_eval" / "benchmark"))
    judge_dir = Path(args.judge_dir or (ROOT / "data" / "perf_runs" / "nightly_eval" / "judge"))
    output_dir = Path(args.output_dir or (ROOT / "data" / "perf_runs" / "nightly_eval" / "review"))
    review_args = [
        _python_bin(),
        "-m",
        "scripts.eval.review_runner",
        "--benchmark-summary",
        str(benchmark_dir / "baseline_summary.json"),
        "--benchmark-raw",
        str(benchmark_dir / "baseline_raw.jsonl"),
        "--judge-summary",
        str(judge_dir / "judge_summary.json"),
        "--judge-raw",
        str(judge_dir / "judge_raw.jsonl"),
        "--output-dir",
        str(output_dir),
    ]
    return _run(review_args)


if __name__ == "__main__":
    raise SystemExit(main())
