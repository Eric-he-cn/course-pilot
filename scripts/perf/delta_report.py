"""Compare baseline vs after benchmark summaries and emit delta report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict


KEYS = [
    "avg_prompt_tokens",
    "p95_prompt_tokens",
    "p50_first_token_latency_ms",
    "p95_first_token_latency_ms",
    "p50_e2e_latency_ms",
    "p95_e2e_latency_ms",
    "avg_retrieval_ms",
    "p95_retrieval_ms",
    "hit_at_k",
    "top1_acc",
    "precision_at_k",
    "tool_call_rate",
    "tool_success_rate",
    "error_rate",
    "replan_trigger_rate",
]


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _delta(before: float, after: float) -> Dict[str, float]:
    abs_delta = float(after) - float(before)
    rel = 0.0 if float(before) == 0.0 else abs_delta / float(before)
    return {"before": float(before), "after": float(after), "abs_delta": abs_delta, "rel_delta": rel}


def _build_block(base: Dict[str, Any], aft: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k in KEYS:
        out[k] = _delta(float(base.get(k, 0.0) or 0.0), float(aft.get(k, 0.0) or 0.0))
    return out


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def _write_md(path: Path, report: Dict[str, Any]) -> None:
    lines = [
        f"# Delta Report - {report.get('profile', 'baseline_vs_after')}",
        "",
        "| metric | baseline | after | abs_delta | rel_delta |",
        "|---|---:|---:|---:|---:|",
    ]
    for k in KEYS:
        b = report["overall"][k]
        lines.append(
            f"| `{k}` | {_fmt(b['before'])} | {_fmt(b['after'])} | {_fmt(b['abs_delta'])} | {_fmt(b['rel_delta'])} |"
        )
    for mode in ("learn", "practice", "exam"):
        blk = report.get("by_mode", {}).get(mode)
        if not blk:
            continue
        lines.extend(
            [
                "",
                f"## {mode}",
                "",
                "| metric | baseline | after | abs_delta | rel_delta |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for k in KEYS:
            b = blk[k]
            lines.append(
                f"| `{k}` | {_fmt(b['before'])} | {_fmt(b['after'])} | {_fmt(b['abs_delta'])} | {_fmt(b['rel_delta'])} |"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build baseline vs after delta report.")
    parser.add_argument("--baseline", required=True, help="baseline_summary.json")
    parser.add_argument("--after", required=True, help="after_summary.json")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--profile", default="baseline_vs_after")
    args = parser.parse_args()

    b = _load_json(Path(args.baseline))
    a = _load_json(Path(args.after))
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    report = {
        "profile": args.profile,
        "overall": _build_block(b, a),
        "by_mode": {},
    }
    for mode in ("learn", "practice", "exam"):
        bb = (b.get("by_mode") or {}).get(mode)
        aa = (a.get("by_mode") or {}).get(mode)
        if bb and aa:
            report["by_mode"][mode] = _build_block(bb, aa)

    json_path = out_dir / "delta_report.json"
    md_path = out_dir / "delta_report.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_md(md_path, report)
    print(f"[delta] json={json_path}")
    print(f"[delta] md={md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

