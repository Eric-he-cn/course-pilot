"""LLM-as-judge runner for benchmark raw outputs."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional, Tuple

from openai import OpenAI
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")


JUDGE_DIMENSIONS = (
    "correctness",
    "groundedness",
    "completeness",
    "pedagogy_clarity",
    "instruction_following",
)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _heuristic_dimensions(case: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, float]:
    response_text = str(row.get("response_text", "") or row.get("response_preview", "") or "").strip()
    response_chars = len(response_text)
    citations = row.get("citations") if isinstance(row.get("citations"), list) else []
    has_citations = bool(citations)
    rag_hit = _clamp01(float(row.get("rag_hit", 0.0) or 0.0))
    case_error = bool(row.get("case_error"))

    if case_error or not response_text:
        return {name: 0.0 for name in JUDGE_DIMENSIONS}

    correctness = _clamp01(0.45 + 0.35 * rag_hit + (0.10 if response_chars >= 350 else 0.0))
    groundedness = _clamp01(0.30 + (0.35 if has_citations else 0.0) + 0.25 * rag_hit)
    completeness = _clamp01(0.35 + min(0.45, float(response_chars) / 1800.0))
    pedagogy_clarity = _clamp01(0.45 + (0.20 if ("###" in response_text or "1." in response_text) else 0.0))
    instruction_following = _clamp01(0.40 + (0.25 if response_chars >= 240 else 0.0) + (0.10 if has_citations else 0.0))

    return {
        "correctness": correctness,
        "groundedness": groundedness,
        "completeness": completeness,
        "pedagogy_clarity": pedagogy_clarity,
        "instruction_following": instruction_following,
    }


def _heuristic_pairwise_winner(candidate_row: Dict[str, Any], baseline_row: Dict[str, Any]) -> Tuple[str, float, str]:
    cand_rag = _clamp01(float(candidate_row.get("rag_hit", 0.0) or 0.0))
    base_rag = _clamp01(float(baseline_row.get("rag_hit", 0.0) or 0.0))
    if cand_rag > base_rag + 0.05:
        return "candidate", 0.45, "heuristic_rag_hit_better"
    if base_rag > cand_rag + 0.05:
        return "baseline", 0.45, "heuristic_rag_hit_worse"

    cand_len = len(str(candidate_row.get("response_text", "") or ""))
    base_len = len(str(baseline_row.get("response_text", "") or ""))
    if cand_len > base_len * 1.2:
        return "candidate", 0.30, "heuristic_response_richer"
    if base_len > cand_len * 1.2:
        return "baseline", 0.30, "heuristic_response_shorter"
    return "tie", 0.20, "heuristic_tie"


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            rows.append(json.loads(s))
    return rows


def append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _index_by_case_id(rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        case_id = str(row.get("case_id", "")).strip()
        if case_id:
            out[case_id] = row
    return out


def _judge_client_config() -> Dict[str, str]:
    api_key = str(os.getenv("EVAL_JUDGE_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")).strip()
    base_url = str(os.getenv("EVAL_JUDGE_BASE_URL", "") or os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com")).strip()
    model = str(os.getenv("EVAL_JUDGE_MODEL", "") or os.getenv("DEFAULT_MODEL", "deepseek-chat")).strip()
    return {"api_key": api_key, "base_url": base_url, "model": model}


def _judge_prompt(case: Dict[str, Any], row: Dict[str, Any]) -> List[Dict[str, str]]:
    mode = str(case.get("mode", row.get("mode", "learn"))).strip()
    rubric = {
        "learn": "重点看事实正确性、是否基于引用材料、解释是否清晰易学、是否完整回答用户问题。",
        "practice": "重点看题型是否符合要求、题目是否可作答可判分、讲评是否有教学价值。",
        "exam": "重点看试卷结构、覆盖度、评分规则完整性、交卷反馈质量。",
    }.get(mode, "重点看正确性、清晰度、完整性和指令遵循。")
    reference_answer = str(case.get("reference_answer", "") or "").strip()
    citations = row.get("citations") or []
    citations_text = "\n".join(
        f"- {c.get('doc_id', '')} p{c.get('page', '')}: {str(c.get('text', '')).strip()[:220]}"
        for c in citations
        if isinstance(c, dict)
    )
    user_message = str(case.get("message", row.get("message", "")) or "")
    response_text = str(row.get("response_text", "") or row.get("response_preview", "") or "")
    schema_hint = {
        "dimensions": {name: "0~1 float" for name in JUDGE_DIMENSIONS},
        "overall_score": "0~1 float",
        "label": "pass|warn|fail",
        "confidence": "0~1 float",
        "reasoning": "short string",
    }
    user_content = (
        f"模式: {mode}\n"
        f"评估重点: {rubric}\n\n"
        f"用户问题:\n{user_message}\n\n"
        f"候选回答:\n{response_text}\n\n"
        f"引用证据:\n{citations_text or '无'}\n\n"
        f"参考答案:\n{reference_answer or '无'}\n\n"
        f"请仅输出 JSON，结构参考:\n{json.dumps(schema_hint, ensure_ascii=False)}"
    )
    return [
        {
            "role": "system",
            "content": (
                "你是 CoursePilot v3 的回答质量评审器。"
                "请严格依据用户问题、候选回答、引用证据和参考答案评分。"
                "所有分数均在 0 到 1 之间，只输出 JSON。"
            ),
        },
        {"role": "user", "content": user_content},
    ]


def _pairwise_prompt(case: Dict[str, Any], candidate_row: Dict[str, Any], baseline_row: Dict[str, Any]) -> List[Dict[str, str]]:
    mode = str(case.get("mode", candidate_row.get("mode", "learn"))).strip()
    user_content = (
        f"模式: {mode}\n"
        f"用户问题:\n{str(case.get('message', candidate_row.get('message', '')))}\n\n"
        f"候选版本回答:\n{str(candidate_row.get('response_text', '') or '')}\n\n"
        f"基线版本回答:\n{str(baseline_row.get('response_text', '') or '')}\n\n"
        '请仅输出 JSON，结构为 {"winner":"candidate|baseline|tie","confidence":0~1,"reasoning":"..."}'
    )
    return [
        {
            "role": "system",
            "content": "你是版本回归评审器。只比较两版回答哪一个整体更好，并输出 JSON。",
        },
        {"role": "user", "content": user_content},
    ]


def _parse_json_content(raw: str) -> Dict[str, Any]:
    text = str(raw or "").strip()
    if "```" in text:
        parts = [segment.strip() for segment in text.split("```") if segment.strip()]
        if parts:
            for part in parts:
                if part.startswith("json"):
                    text = part[4:].strip()
                    break
                if part.startswith("{"):
                    text = part
                    break
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    return json.loads(text)


def _call_model_json(
    client: OpenAI,
    *,
    model: str,
    messages: List[Dict[str, str]],
    temperature: float,
    timeout_ms: int,
) -> Dict[str, Any]:
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        timeout=max(1.0, float(timeout_ms) / 1000.0),
    )
    content = response.choices[0].message.content or ""
    return _parse_json_content(content)


def _overall_from_dimensions(dimensions: Dict[str, Any]) -> float:
    vals = [float(dimensions.get(name, 0.0) or 0.0) for name in JUDGE_DIMENSIONS]
    vals = [max(0.0, min(1.0, v)) for v in vals]
    return mean(vals) if vals else 0.0


def _label_from_score(score: float) -> str:
    if score >= 0.8:
        return "pass"
    if score >= 0.6:
        return "warn"
    return "fail"


def judge_row(
    *,
    client: Optional[OpenAI],
    model: str,
    case: Dict[str, Any],
    row: Dict[str, Any],
    baseline_row: Optional[Dict[str, Any]],
    temperature: float,
    timeout_ms: int,
    allow_heuristic_fallback: bool,
) -> Dict[str, Any]:
    case_id = str(row.get("case_id", case.get("case_id", "")) or "").strip()
    mode = str(row.get("mode", case.get("mode", "learn")) or "learn")
    result: Dict[str, Any] = {
        "case_id": case_id,
        "mode": mode,
        "judge_profile": str(case.get("judge_profile", "default") or "default"),
        "judge_model": model,
        "rubric_version": "v1",
        "judge_skipped": False,
        "judge_fallback": False,
        "pairwise_winner": "",
        "pairwise_confidence": 0.0,
        "pairwise_reasoning": "",
    }
    if row.get("case_error"):
        result.update(
            {
                "judge_skipped": True,
                "judge_skip_reason": "case_error",
                "overall_score": 0.0,
                "judge_score": 0.0,
                "label": "fail",
                "confidence": 0.0,
                "reasoning": "candidate_case_error",
                "dimensions": {name: 0.0 for name in JUDGE_DIMENSIONS},
            }
        )
        return result
    if client is None:
        if allow_heuristic_fallback:
            dimensions = _heuristic_dimensions(case, row)
            overall_score = _overall_from_dimensions(dimensions)
            label = _label_from_score(overall_score)
            result.update(
                {
                    "judge_skipped": False,
                    "judge_fallback": True,
                    "judge_skip_reason": "",
                    "overall_score": overall_score,
                    "judge_score": overall_score,
                    "label": label,
                    "confidence": 0.35,
                    "reasoning": "heuristic_fallback_missing_judge_config",
                    "dimensions": dimensions,
                }
            )
            if baseline_row and baseline_row.get("response_text") and row.get("response_text"):
                winner, confidence, reasoning = _heuristic_pairwise_winner(row, baseline_row)
                result.update(
                    {
                        "pairwise_winner": winner,
                        "pairwise_confidence": confidence,
                        "pairwise_reasoning": reasoning,
                    }
                )
            return result
        result.update(
            {
                "judge_skipped": True,
                "judge_fallback": False,
                "judge_skip_reason": "missing_judge_config",
                "overall_score": 0.0,
                "judge_score": 0.0,
                "label": "warn",
                "confidence": 0.0,
                "reasoning": "judge_not_configured",
                "dimensions": {name: 0.0 for name in JUDGE_DIMENSIONS},
            }
        )
        return result

    payload = _call_model_json(
        client,
        model=model,
        messages=_judge_prompt(case, row),
        temperature=temperature,
        timeout_ms=timeout_ms,
    )
    dimensions_raw = payload.get("dimensions", {})
    dimensions = {
        name: max(0.0, min(1.0, float(dimensions_raw.get(name, 0.0) or 0.0)))
        for name in JUDGE_DIMENSIONS
    }
    overall_score = payload.get("overall_score")
    if not isinstance(overall_score, (int, float)):
        overall_score = _overall_from_dimensions(dimensions)
    overall_score = max(0.0, min(1.0, float(overall_score)))
    label = str(payload.get("label", "") or _label_from_score(overall_score)).strip().lower()
    if label not in {"pass", "warn", "fail"}:
        label = _label_from_score(overall_score)
    confidence = max(0.0, min(1.0, float(payload.get("confidence", 0.0) or 0.0)))
    result.update(
        {
            "overall_score": overall_score,
            "judge_score": overall_score,
            "label": label,
            "confidence": confidence,
            "reasoning": str(payload.get("reasoning", "") or "").strip(),
            "dimensions": dimensions,
        }
    )

    if baseline_row and baseline_row.get("response_text") and row.get("response_text"):
        pairwise = _call_model_json(
            client,
            model=model,
            messages=_pairwise_prompt(case, row, baseline_row),
            temperature=temperature,
            timeout_ms=timeout_ms,
        )
        winner = str(pairwise.get("winner", "") or "").strip().lower()
        if winner not in {"candidate", "baseline", "tie"}:
            winner = "tie"
        result.update(
            {
                "pairwise_winner": winner,
                "pairwise_confidence": max(0.0, min(1.0, float(pairwise.get("confidence", 0.0) or 0.0))),
                "pairwise_reasoning": str(pairwise.get("reasoning", "") or "").strip(),
            }
        )
    return result


def summarize_judge_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    judged = [row for row in rows if not row.get("judge_skipped")]
    fallback_judged = [row for row in judged if bool(row.get("judge_fallback"))]
    by_mode: Dict[str, Dict[str, Any]] = {}
    for mode in ("learn", "practice", "exam"):
        mode_rows = [row for row in judged if str(row.get("mode", "")) == mode]
        if not mode_rows:
            continue
        dims = {
            name: mean(float((row.get("dimensions") or {}).get(name, 0.0) or 0.0) for row in mode_rows)
            for name in JUDGE_DIMENSIONS
        }
        by_mode[mode] = {
            "num": len(mode_rows),
            "avg_overall_score": mean(float(row.get("overall_score", 0.0) or 0.0) for row in mode_rows),
            "avg_confidence": mean(float(row.get("confidence", 0.0) or 0.0) for row in mode_rows),
            "dimensions": dims,
        }
    candidate_wins = [row for row in judged if row.get("pairwise_winner") == "candidate"]
    baseline_wins = [row for row in judged if row.get("pairwise_winner") == "baseline"]
    pairwise_total = len([row for row in judged if row.get("pairwise_winner") in {"candidate", "baseline", "tie"}])
    return {
        "num_rows": len(rows),
        "num_judged": len(judged),
        "num_fallback_judged": len(fallback_judged),
        "judge_skipped": len(judged) == 0,
        "avg_overall_score": mean(float(row.get("overall_score", 0.0) or 0.0) for row in judged) if judged else 0.0,
        "avg_confidence": mean(float(row.get("confidence", 0.0) or 0.0) for row in judged) if judged else 0.0,
        "label_counts": {
            "pass": sum(1 for row in judged if row.get("label") == "pass"),
            "warn": sum(1 for row in judged if row.get("label") == "warn"),
            "fail": sum(1 for row in judged if row.get("label") == "fail"),
        },
        "dimensions": {
            name: mean(float((row.get("dimensions") or {}).get(name, 0.0) or 0.0) for row in judged) if judged else 0.0
            for name in JUDGE_DIMENSIONS
        },
        "pairwise_candidate_win_rate": float(len(candidate_wins)) / float(pairwise_total) if pairwise_total else 0.0,
        "pairwise_baseline_win_rate": float(len(baseline_wins)) / float(pairwise_total) if pairwise_total else 0.0,
        "by_mode": by_mode,
    }


def _write_markdown(path: Path, summary: Dict[str, Any]) -> None:
    lines = [
        "# Judge Summary",
        "",
        f"- num_rows: {summary.get('num_rows', 0)}",
        f"- num_judged: {summary.get('num_judged', 0)}",
        f"- num_fallback_judged: {summary.get('num_fallback_judged', 0)}",
        f"- judge_skipped: {summary.get('judge_skipped', False)}",
        f"- avg_overall_score: {summary.get('avg_overall_score', 0.0):.4f}",
        f"- avg_confidence: {summary.get('avg_confidence', 0.0):.4f}",
        "",
        "## Dimensions",
        "",
    ]
    for name, value in (summary.get("dimensions") or {}).items():
        lines.append(f"- {name}: {float(value):.4f}")
    lines.extend(["", "## Labels", ""])
    for name, value in (summary.get("label_counts") or {}).items():
        lines.append(f"- {name}: {value}")
    lines.extend(["", "## Pairwise", ""])
    lines.append(f"- candidate_win_rate: {float(summary.get('pairwise_candidate_win_rate', 0.0)):.4f}")
    lines.append(f"- baseline_win_rate: {float(summary.get('pairwise_baseline_win_rate', 0.0)):.4f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run LLM-as-judge on benchmark raw outputs.")
    parser.add_argument("--raw", default=str(ROOT / "data" / "perf_runs" / "baseline_v1" / "baseline_raw.jsonl"))
    parser.add_argument("--cases", default=str(ROOT / "benchmarks" / "cases_v1.jsonl"))
    parser.add_argument("--baseline-raw", default="")
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "perf_runs" / "judge_default"))
    parser.add_argument("--profile", default="judge_default")
    parser.add_argument("--max-cases", type=int, default=0)
    parser.add_argument("--temperature", type=float, default=float(os.getenv("EVAL_JUDGE_TEMPERATURE", "0")))
    parser.add_argument("--timeout-ms", type=int, default=int(os.getenv("EVAL_JUDGE_TIMEOUT_MS", "20000")))
    parser.add_argument(
        "--allow-heuristic-fallback",
        type=int,
        default=int(os.getenv("EVAL_JUDGE_ALLOW_HEURISTIC_FALLBACK", "1")),
    )
    args = parser.parse_args()

    raw_rows = load_jsonl(Path(args.raw))
    if not raw_rows:
        print(f"[judge] no raw rows found: {args.raw}")
        return 1
    case_map = _index_by_case_id(load_jsonl(Path(args.cases)))
    baseline_map = _index_by_case_id(load_jsonl(Path(args.baseline_raw))) if args.baseline_raw else {}
    if args.max_cases > 0:
        raw_rows = raw_rows[: int(args.max_cases)]

    cfg = _judge_client_config()
    client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"]) if cfg["api_key"] else None
    if client is None and int(args.allow_heuristic_fallback) > 0:
        print("[judge] missing API key; using heuristic fallback scoring")
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / "judge_raw.jsonl"
    summary_path = out_dir / "judge_summary.json"
    summary_md_path = out_dir / "judge_summary.md"
    if raw_path.exists():
        raw_path.unlink()

    rows: List[Dict[str, Any]] = []
    for row in raw_rows:
        case_id = str(row.get("case_id", "")).strip()
        case = case_map.get(case_id, row)
        judged = judge_row(
            client=client,
            model=cfg["model"],
            case=case,
            row=row,
            baseline_row=baseline_map.get(case_id),
            temperature=float(args.temperature),
            timeout_ms=int(args.timeout_ms),
            allow_heuristic_fallback=bool(int(args.allow_heuristic_fallback)),
        )
        append_jsonl(raw_path, judged)
        rows.append(judged)
        print(f"[judge] {case_id} skipped={int(bool(judged.get('judge_skipped')))} label={judged.get('label', '')}")

    summary = summarize_judge_rows(rows)
    summary.update(
        {
            "profile": args.profile,
            "judge_model": cfg["model"],
            "judge_base_url": cfg["base_url"],
        }
    )
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(summary_md_path, summary)
    print(f"[judge] done rows={len(rows)} judged={summary.get('num_judged', 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
