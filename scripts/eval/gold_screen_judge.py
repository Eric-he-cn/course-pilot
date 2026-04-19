"""Dedicated first-screen judge for human-verified gold candidate generation."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from openai import OpenAI


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")


DEFAULT_THRESHOLD = 0.85


def _clamp01(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except Exception:
        return 0.0


def _parse_json_content(raw: str) -> Dict[str, Any]:
    text = str(raw or "").strip()
    if "```" in text:
        parts = [segment.strip() for segment in text.split("```") if segment.strip()]
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


def build_client_from_env() -> tuple[Optional[OpenAI], Dict[str, str]]:
    api_key = str(os.getenv("GOLD_SCREEN_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")).strip()
    base_url = str(os.getenv("GOLD_SCREEN_BASE_URL", "") or os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com")).strip()
    model = str(
        os.getenv("GOLD_SCREEN_MODEL", "")
        or os.getenv("DEFAULT_MODEL_THINKING", "")
        or os.getenv("DEFAULT_MODEL", "deepseek-chat")
    ).strip()
    cfg = {"api_key": api_key, "base_url": base_url, "model": model}
    if not api_key:
        return None, cfg
    return OpenAI(api_key=api_key, base_url=base_url), cfg


def _judge_prompt(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    citations = payload.get("citations") if isinstance(payload.get("citations"), list) else []
    citations_text = "\n".join(
        (
            f"[{idx}] doc={c.get('doc_id', '')} page={c.get('page', '')} chunk={c.get('chunk_id', '')}\n"
            f"preview={str(c.get('text_preview', '') or c.get('text', '') or '').strip()[:240]}\n"
            f"scores: score={c.get('score')} dense={c.get('dense_score')} bm25={c.get('bm25_score')} "
            f"rrf={c.get('rrf_score')} rerank={c.get('rerank_score')}"
        )
        for idx, c in enumerate(citations)
        if isinstance(c, dict)
    )
    plan_summary = payload.get("plan_summary") if isinstance(payload.get("plan_summary"), dict) else {}
    trace_summary = payload.get("trace_summary") if isinstance(payload.get("trace_summary"), dict) else {}
    schema_hint = {
        "decision": "candidate|manual_fix|reject",
        "confidence": "0~1 float",
        "reasoning": "short string",
        "selected_citation_indexes": [0],
        "citation_quality_score": "0~1 float",
        "answer_grounded_score": "0~1 float",
        "coverage_score": "0~1 float",
    }
    user_content = (
        "请判断这条样本能否进入 CoursePilot 的 gold 候选池。\n\n"
        f"用户问题:\n{str(payload.get('message', '') or '')}\n\n"
        f"模型回答:\n{str(payload.get('response_text', '') or '')}\n\n"
        f"plan 摘要:\n{json.dumps(plan_summary, ensure_ascii=False)}\n\n"
        f"trace 摘要:\n{json.dumps(trace_summary, ensure_ascii=False)}\n\n"
        f"citations:\n{citations_text or '无'}\n\n"
        "评价重点:\n"
        "1. 回答是否明显受教材证据支撑，而不是凭空扩写。\n"
        "2. 哪些 citation 真的能作为 gold 候选，索引必须使用 0-based。\n"
        "3. 如果引用明显偏题、证据不足、错引或无引文，不能判为 candidate。\n"
        "4. 不要因为回答流畅就给高分，优先看证据质量与可复查性。\n"
        "5. 分数请使用 0.00~1.00 的两位小数，不要默认输出 0.85 或 0.90 这类阈值附近常数。\n"
        "6. `citation_quality_score` 看引文质量，`answer_grounded_score` 看回答是否被证据支撑，"
        "`coverage_score` 看被选 citation 对问题关键点的覆盖度。\n\n"
        "校准要求：\n"
        "- 把 0.85 视为严格门槛，不是默认及格分。\n"
        "- 只有证据充分、引用准确、覆盖关键点的样本，才应该达到或超过 0.85。\n"
        "- 常见的“回答还不错但证据不够硬”的情况，应该落在 0.55~0.84，并进入 manual_fix，而不是 candidate。\n"
        "- 目标是让候选池保持明显区分度，避免大多数样本都挤在 0.85 附近；理想上 candidate 不应超过一半样本。\n\n"
        "建议参考分段：\n"
        "- 0.90~1.00：证据非常强，可直接进入 candidate。\n"
        "- 0.85~0.89：边界通过，只有当关键论点都被引用覆盖时才可进入 candidate。\n"
        "- 0.55~0.84：部分可用，但证据覆盖不够，进入 manual_fix。\n"
        "- 0.00~0.54：证据严重不足、错引或扩写明显，进入 reject。\n\n"
        f"只输出 JSON，结构参考:\n{json.dumps(schema_hint, ensure_ascii=False)}"
    )
    return [
        {
            "role": "system",
            "content": (
                "你是 CoursePilot 的 gold-screen judge。"
                "你的任务不是判断回答是否好看，而是判断它是否已经具备进入人工复查候选池的证据质量。"
                "只输出 JSON，不要输出 markdown。"
            ),
        },
        {"role": "user", "content": user_content},
    ]


def _heuristic_gold_screen(payload: Dict[str, Any]) -> Dict[str, Any]:
    citations = payload.get("citations") if isinstance(payload.get("citations"), list) else []
    response_text = str(payload.get("response_text", "") or "").strip()
    trace_summary = payload.get("trace_summary") if isinstance(payload.get("trace_summary"), dict) else {}
    has_signal = bool(citations) and len(response_text) >= 160 and not bool(trace_summary.get("fallback"))
    if has_signal:
        return {
            "overall_score": 0.68,
            "decision": "manual_fix",
            "confidence": 0.30,
            "reasoning": "judge_unavailable_use_manual_fix_fallback",
            "selected_citation_indexes": [0],
            "citation_quality_score": 0.60,
            "answer_grounded_score": 0.70,
        }
    return {
        "overall_score": 0.20,
        "decision": "reject",
        "confidence": 0.20,
        "reasoning": "judge_unavailable_and_evidence_weak",
        "selected_citation_indexes": [],
        "citation_quality_score": 0.10,
        "answer_grounded_score": 0.20,
    }


def _sanitize_indexes(values: Any, citation_count: int) -> List[int]:
    out: List[int] = []
    if not isinstance(values, list):
        return out
    for item in values:
        if not isinstance(item, int):
            continue
        if 0 <= item < citation_count and item not in out:
            out.append(item)
    return out


def normalize_judge_result(
    *,
    payload: Dict[str, Any],
    raw_result: Dict[str, Any],
    threshold: float = DEFAULT_THRESHOLD,
    fallback_used: bool = False,
    judge_error: str = "",
) -> Dict[str, Any]:
    citations = payload.get("citations") if isinstance(payload.get("citations"), list) else []
    citation_quality_score = _clamp01(raw_result.get("citation_quality_score", 0.0))
    answer_grounded_score = _clamp01(raw_result.get("answer_grounded_score", 0.0))
    coverage_score = _clamp01(raw_result.get("coverage_score", 0.0))
    model_confidence = _clamp01(raw_result.get("confidence", 0.0))
    selected_citation_indexes = _sanitize_indexes(raw_result.get("selected_citation_indexes"), len(citations))
    trace_summary = payload.get("trace_summary") if isinstance(payload.get("trace_summary"), dict) else {}
    citation_count = len(citations)
    selected_count = len(selected_citation_indexes)
    selected_ratio = float(selected_count) / float(citation_count) if citation_count > 0 else 0.0
    fallback_penalty = 0.10 if bool(trace_summary.get("fallback")) else 0.0
    retrieval_penalty = 0.15 if bool(trace_summary.get("retrieval_empty")) else 0.0
    missing_selection_penalty = 0.20 if selected_count <= 0 else 0.0
    overall_score = _clamp01(
        round(
            0.50 * answer_grounded_score
            + 0.35 * citation_quality_score
            + 0.15 * max(coverage_score, selected_ratio)
            - fallback_penalty
            - retrieval_penalty
            - missing_selection_penalty,
            2,
        )
    )
    confidence = _clamp01(
        round(
            0.60 * model_confidence
            + 0.25 * max(coverage_score, selected_ratio)
            + 0.15 * (0.0 if bool(trace_summary.get("fallback")) else 1.0),
            2,
        )
    )
    strict_candidate_ready = (
        overall_score >= float(threshold)
        and selected_count >= 1
        and citation_quality_score >= 0.78
        and answer_grounded_score >= 0.80
        and coverage_score >= 0.70
    )
    hard_reject = (
        overall_score < 0.55
        or (
            selected_count <= 0
            and citation_quality_score < 0.40
            and answer_grounded_score < 0.50
        )
    )
    if strict_candidate_ready:
        decision = "candidate"
    elif hard_reject:
        decision = "reject"
    elif overall_score >= 0.45 or selected_citation_indexes or str(payload.get("response_text", "")).strip():
        decision = "manual_fix"
    else:
        decision = "reject"
    return {
        "overall_score": overall_score,
        "decision": decision,
        "confidence": confidence,
        "reasoning": str(raw_result.get("reasoning", "") or ""),
        "selected_citation_indexes": selected_citation_indexes,
        "citation_quality_score": citation_quality_score,
        "answer_grounded_score": answer_grounded_score,
        "coverage_score": coverage_score,
        "judge_threshold": float(threshold),
        "judge_fallback": bool(fallback_used),
        "judge_error": str(judge_error or ""),
        "model_decision": str(raw_result.get("decision", "") or ""),
    }


def judge_gold_sample(
    *,
    payload: Dict[str, Any],
    client: Optional[OpenAI],
    model: str,
    threshold: float = DEFAULT_THRESHOLD,
    temperature: float = 0.0,
    timeout_ms: int = 120000,
    allow_heuristic_fallback: bool = True,
) -> Dict[str, Any]:
    if client is None:
        fallback = _heuristic_gold_screen(payload)
        return normalize_judge_result(
            payload=payload,
            raw_result=fallback,
            threshold=threshold,
            fallback_used=True,
            judge_error="missing_api_key",
        )
    try:
        response = client.chat.completions.create(
            model=model,
            messages=_judge_prompt(payload),
            temperature=float(temperature),
            timeout=max(1.0, float(timeout_ms) / 1000.0),
        )
        content = response.choices[0].message.content or ""
        parsed = _parse_json_content(content)
        return normalize_judge_result(payload=payload, raw_result=parsed, threshold=threshold)
    except Exception as ex:
        if not allow_heuristic_fallback:
            raise
        fallback = _heuristic_gold_screen(payload)
        return normalize_judge_result(
            payload=payload,
            raw_result=fallback,
            threshold=threshold,
            fallback_used=True,
            judge_error=f"{type(ex).__name__}: {ex}",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run dedicated gold-screen judge on one sampled payload.")
    parser.add_argument("--input", help="JSON file containing one sampled payload. If omitted, read from stdin.")
    parser.add_argument("--output", help="Optional output JSON path.")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--timeout-ms", type=int, default=120000)
    args = parser.parse_args()

    if args.input:
        payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    else:
        payload = json.loads(sys.stdin.read())
    client, cfg = build_client_from_env()
    result = judge_gold_sample(
        payload=payload,
        client=client,
        model=cfg["model"],
        threshold=float(args.threshold),
        temperature=float(args.temperature),
        timeout_ms=max(1000, int(args.timeout_ms)),
    )
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
