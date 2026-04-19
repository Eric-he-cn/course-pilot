"""Utilities shared by the benchmark/gold candidate pipeline."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set


ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_DIR = ROOT / "benchmarks"
DEFAULT_DATA_DIR = ROOT / "data" / "workspaces"

ACTIVE_BENCHMARK_FILES = {
    "cases_v1.jsonl",
    "rag_gold_v1.jsonl",
}
PIPELINE_TRANSIENT_FILES = {
    "gold_candidates.jsonl",
    "gold_manual_fix.jsonl",
    "gold_rejected.jsonl",
    "gold_label_sessions.jsonl",
}
PIPELINE_ALL_FILES = ACTIVE_BENCHMARK_FILES | PIPELINE_TRANSIENT_FILES

DEFAULT_INCLUDED_COURSES = [
    "深度学习",
    "通信网络",
    "新中特",
    "LLM基础",
]
DEFAULT_EXCLUDED_COURSES = {"course", "perf_demo"}

COURSE_QUESTION_SEEDS: Dict[str, List[str]] = {
    "矩阵理论": [
        "请结合教材解释矩阵的秩的定义、几何意义，以及它和线性无关性的关系，并给出教材依据。",
        "请根据教材讲清相似对角化的条件、判断方法和常见误区，并引用对应教材内容。",
        "请结合教材说明 Jordan 标准形要解决什么问题，它和特征值、特征向量之间是什么关系。",
        "请根据教材解释正定矩阵的判定方法，并比较主子式法和特征值法的适用场景。",
        "请结合教材讲解矩阵分解在本课程里的作用，并举一个教材中的典型例子。",
        "请根据教材说明特征值与特征向量在矩阵理论中的核心作用，并结合一个章节上下文解释。",
    ],
    "深度学习": [
        "请根据教材解释反向传播算法的基本思路、链式法则的作用，以及训练时最容易混淆的地方。",
        "请结合教材讲清梯度消失和梯度爆炸为什么会发生，常见缓解方法有哪些，并给出教材依据。",
        "请根据教材解释 Batch Normalization 的动机、工作机制和训练收益，并引用教材内容。",
        "请结合教材说明卷积神经网络的局部连接和参数共享为什么有效，并给出教材中的解释。",
        "请根据教材讲解注意力机制的核心思想，以及它相比传统序列模型解决了什么问题。",
        "请结合教材解释正则化在深度学习中的作用，并比较至少两种常见方法。",
    ],
    "通信网络": [
        "请根据教材比较 OSI 七层模型和 TCP/IP 分层思想，说明它们在课程中的作用与差异。",
        "请结合教材解释差错控制和流量控制的区别，并给出一个典型协议场景作为依据。",
        "请根据教材讲清拥塞控制为什么必要，常见触发信号和控制思路是什么。",
        "请结合教材说明路由算法的基本目标，并比较至少两类典型算法的差异。",
        "请根据教材解释 HTTP、TCP、UDP 在网络栈中的关系和职责边界，并引用教材内容。",
        "请结合教材说明电路交换、报文交换和分组交换的区别，以及各自适用场景。",
    ],
    "新中特": [
        "请根据教材解释新时代中国特色社会主义思想的核心要义，并结合教材结构给出依据。",
        "请结合教材讲清中国式现代化的主要特征，以及它与西方现代化叙事的差异。",
        "请根据教材解释新发展理念的内涵，并说明它在高质量发展中的作用。",
        "请结合教材讲解共同富裕的含义、实现逻辑与常见误解，并引用教材内容。",
        "请根据教材说明坚持党的全面领导在本课程叙事中的位置和作用。",
        "请结合教材解释高质量发展这一概念的提出背景、核心要求与现实意义。",
    ],
    "LLM基础": [
        "请根据教材解释 Transformer 的核心结构，以及自注意力为什么能提升建模能力。",
        "请结合教材讲清 Tokenization 在大语言模型中的作用，以及它对上下文窗口的影响。",
        "请根据教材解释 Prompt Engineering 的基本原则，并说明它如何影响模型输出质量。",
        "请结合教材讲解 RAG 的基本流程，以及它相对纯生成范式解决了什么问题。",
        "请根据教材解释 SFT 和 RLHF 的分工，以及它们在模型对齐中的作用。",
        "请结合教材说明 KV Cache 为什么能降低推理开销，以及它的边界条件是什么。",
    ],
}


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
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(dict(row), ensure_ascii=False) + "\n")


def upsert_jsonl_by_case_id(path: Path, row: Dict[str, Any], *, key: str = "case_id") -> None:
    key_value = str(row.get(key, "") or "").strip()
    rows = load_jsonl(path)
    replaced = False
    out: List[Dict[str, Any]] = []
    for item in rows:
        item_key = str(item.get(key, "") or "").strip()
        if key_value and item_key == key_value:
            out.append(dict(row))
            replaced = True
        else:
            out.append(item)
    if not replaced:
        out.append(dict(row))
    write_jsonl(path, out)


def ensure_pipeline_files(bench_dir: Path) -> None:
    bench_dir.mkdir(parents=True, exist_ok=True)
    for filename in sorted(PIPELINE_ALL_FILES):
        path = bench_dir / filename
        if not path.exists():
            path.touch()


def load_processed_case_ids(bench_dir: Path) -> Set[str]:
    processed: Set[str] = set()
    for filename in [
        "gold_candidates.jsonl",
        "gold_manual_fix.jsonl",
        "gold_rejected.jsonl",
        "cases_v1.jsonl",
    ]:
        path = bench_dir / filename
        for row in load_jsonl(path):
            case_id = str(row.get("case_id", "") or "").strip()
            if case_id:
                processed.add(case_id)
    return processed


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def safe_slug(text: str) -> str:
    value = re.sub(r"\W+", "_", str(text or "").strip().lower(), flags=re.UNICODE).strip("_")
    return value or "course"


def build_case_id(course_name: str, message: str = "") -> str:
    course_slug = safe_slug(course_name)
    seed = f"{str(course_name or '').strip()}||{str(message or '').strip()}".encode("utf-8")
    digest = hashlib.sha1(seed).hexdigest()[:12]
    return f"human_{course_slug}_{digest}"


def scan_indexed_courses(
    data_dir: Path | str | None = None,
    *,
    included_courses: Optional[Sequence[str]] = None,
    excluded_courses: Optional[Sequence[str]] = None,
) -> List[str]:
    base = Path(data_dir or DEFAULT_DATA_DIR)
    if not base.exists():
        return []
    included = list(included_courses or DEFAULT_INCLUDED_COURSES)
    included_set = {str(name).strip() for name in included if str(name).strip()}
    excluded = {str(name).strip() for name in (excluded_courses or DEFAULT_EXCLUDED_COURSES)}
    indexed: List[str] = []
    for child in sorted(base.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        course_name = child.name
        if course_name in excluded:
            continue
        if included_set and course_name not in included_set:
            continue
        faiss_path = child / "index" / "faiss_index.faiss"
        pkl_path = child / "index" / "faiss_index.pkl"
        if faiss_path.exists() and pkl_path.exists():
            indexed.append(course_name)
    priority = {name: idx for idx, name in enumerate(included)}
    indexed.sort(key=lambda name: (priority.get(name, 9999), name))
    return indexed


def _generic_questions(course_name: str) -> List[str]:
    return [
        f"请根据《{course_name}》教材，解释一个本课程最核心的基础概念，并引用教材依据。",
        f"请结合《{course_name}》教材，讲清一个容易混淆的关键概念及其常见误区。",
        f"请根据《{course_name}》教材，说明一个重要定理、公式或原则的适用条件。",
        f"请结合《{course_name}》教材，概括一个章节的核心知识结构，并引用教材依据。",
        f"请根据《{course_name}》教材，比较两个容易混淆的概念，并说明它们之间的联系与区别。",
        f"请结合《{course_name}》教材，说明一个典型知识点在实际问题中的意义或应用。",
    ]


def generate_question_suggestions(courses: Sequence[str], total: int = 30) -> List[Dict[str, Any]]:
    course_list = [str(course).strip() for course in courses if str(course).strip()]
    if not course_list or total <= 0:
        return []
    per_course = max(1, total // len(course_list))
    extra = max(0, total % len(course_list))
    out: List[Dict[str, Any]] = []
    for index, course_name in enumerate(course_list):
        seeds = list(COURSE_QUESTION_SEEDS.get(course_name) or _generic_questions(course_name))
        target = per_course + (1 if index < extra else 0)
        if len(seeds) < target:
            seeds.extend(_generic_questions(course_name))
        for seq, message in enumerate(seeds[:target], start=1):
            out.append(
                {
                    "suggestion_id": f"{safe_slug(course_name)}_{seq:02d}",
                    "course_name": course_name,
                    "message": message,
                }
            )
    return out[:total]


def citation_to_dict(citation: Any, *, index: Optional[int] = None) -> Dict[str, Any]:
    payload = citation.model_dump() if hasattr(citation, "model_dump") else dict(citation or {})
    text = str(payload.get("text", "") or "").strip()
    result = {
        "text": text,
        "text_preview": text[:220],
        "doc_id": str(payload.get("doc_id", "") or ""),
        "page": payload.get("page"),
        "chunk_id": payload.get("chunk_id"),
        "score": payload.get("score"),
        "dense_score": payload.get("dense_score"),
        "bm25_score": payload.get("bm25_score"),
        "rrf_score": payload.get("rrf_score"),
        "rerank_score": payload.get("rerank_score"),
        "evidence_passed": payload.get("evidence_passed"),
    }
    if index is not None:
        result["index"] = int(index)
    return result


def citations_to_dicts(citations: Sequence[Any]) -> List[Dict[str, Any]]:
    return [citation_to_dict(citation, index=idx) for idx, citation in enumerate(list(citations or []))]


def extract_internal_meta(tool_calls: Any, name: str) -> Optional[Dict[str, Any]]:
    if not isinstance(tool_calls, list):
        return None
    for item in reversed(tool_calls):
        if not isinstance(item, dict):
            continue
        if str(item.get("type", "") or "").strip() != "internal_meta":
            continue
        if str(item.get("name", "") or "").strip() != str(name or "").strip():
            continue
        payload = item.get("payload")
        return dict(payload) if isinstance(payload, dict) else None
    return None


def plan_to_dict(plan: Any) -> Dict[str, Any]:
    if plan is None:
        return {}
    if hasattr(plan, "model_dump"):
        return dict(plan.model_dump())
    if isinstance(plan, dict):
        return dict(plan)
    return {"repr": str(plan)}


def plan_summary(plan: Any) -> Dict[str, Any]:
    payload = plan_to_dict(plan)
    return {
        "resolved_mode": str(payload.get("resolved_mode", "") or ""),
        "workflow_template": str(payload.get("workflow_template", "") or ""),
        "action_kind": str(payload.get("action_kind", "") or ""),
        "question_raw": str(payload.get("question_raw", "") or ""),
        "retrieval_query": str(payload.get("retrieval_query", "") or ""),
        "memory_query": str(payload.get("memory_query", "") or ""),
        "route_reason": str(payload.get("route_reason", "") or ""),
        "need_rag": bool(payload.get("need_rag", False)),
    }


def summarize_trace_events(
    events: Sequence[Dict[str, Any]],
    *,
    e2e_latency_ms: float = 0.0,
    replan_triggered: bool = False,
) -> Dict[str, Any]:
    rows = [dict(event) for event in list(events or []) if isinstance(event, dict)]
    llm_events = [event for event in rows if event.get("type") == "llm_call"]
    retrieval_events = [event for event in rows if event.get("type") == "retrieval"]
    retrieval_missing_index_events = [event for event in rows if event.get("type") == "retrieval_missing_index"]
    retrieval_skipped_events = [event for event in rows if event.get("type") == "retrieval_skipped"]
    retrieval_unmatched_events = [event for event in rows if event.get("type") == "retrieval_unmatched"]
    tool_events = [event for event in rows if event.get("type") == "tool_call"]
    fallback_events = [event for event in rows if event.get("type") == "runtime_fallback"]
    taskgraph_events = [event for event in rows if event.get("type") == "taskgraph_compiled"]
    llm_ms_values = [float(event["llm_ms"]) for event in llm_events if isinstance(event.get("llm_ms"), (int, float))]
    first_token_vals = [
        float(event["first_token_latency_ms"])
        for event in llm_events
        if isinstance(event.get("first_token_latency_ms"), (int, float))
    ]
    retrieval_ms_values = [
        float(event["retrieval_ms"])
        for event in retrieval_events
        if isinstance(event.get("retrieval_ms"), (int, float))
    ]
    taskgraph_route = ""
    if taskgraph_events:
        taskgraph_route = str(taskgraph_events[-1].get("route", "") or "")
    retrieval_empty = bool(retrieval_missing_index_events or retrieval_unmatched_events)
    return {
        "event_count": len(rows),
        "trace_event_types": sorted({str(event.get("type", "") or "") for event in rows if event.get("type")}),
        "retrieval_empty": retrieval_empty,
        "retrieval_missing_index": bool(retrieval_missing_index_events),
        "retrieval_skipped": bool(retrieval_skipped_events),
        "fallback": bool(fallback_events),
        "replan": bool(replan_triggered),
        "taskgraph_route": taskgraph_route,
        "tool_call_count": len(tool_events),
        "llm_call_count": len(llm_events),
        "retrieval_call_count": len(retrieval_events),
        "avg_llm_ms": mean(llm_ms_values) if llm_ms_values else 0.0,
        "retrieval_ms": mean(retrieval_ms_values) if retrieval_ms_values else 0.0,
        "first_token_latency_ms": mean(first_token_vals) if first_token_vals else 0.0,
        "e2e_ms": float(e2e_latency_ms),
    }


def parse_citation_indexes(raw: str, citation_count: int) -> List[int]:
    text = str(raw or "").strip()
    if not text:
        return []
    indexes: List[int] = []
    for part in re.split(r"[,\s]+", text):
        if not part or not part.isdigit():
            continue
        idx = int(part)
        if 0 <= idx < citation_count and idx not in indexes:
            indexes.append(idx)
    return indexes


def select_citations(citations: Sequence[Dict[str, Any]], indexes: Sequence[int]) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    rows = [dict(item) for item in list(citations or []) if isinstance(item, dict)]
    for idx in indexes:
        if isinstance(idx, int) and 0 <= idx < len(rows):
            selected.append(dict(rows[idx]))
    return selected


def derive_gold_keywords(question: str, retrieval_query: str = "") -> List[str]:
    combined = " ".join(part for part in [str(question or "").strip(), str(retrieval_query or "").strip()] if part)
    if not combined:
        return []
    tokens = re.split(r"[\s,，。；;：:、（）()【】\[\]!?！？]+", combined)
    keywords: List[str] = []
    for token in tokens:
        text = str(token or "").strip()
        if len(text) < 2:
            continue
        if text not in keywords:
            keywords.append(text)
        if len(keywords) >= 5:
            break
    return keywords


def build_official_case_row(candidate: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "case_id": str(candidate.get("case_id", "") or ""),
        "mode": "learn",
        "course_name": str(candidate.get("course_name", "") or ""),
        "message": str(candidate.get("message", "") or ""),
        "history": [],
        "need_rag": True,
        "requires_citations": True,
        "reference_answer": str(candidate.get("reference_answer", "") or candidate.get("response_text", "") or ""),
        "judge_profile": "learn_default",
        "should_use_tools": False,
        "tags": ["human_gold", "citation"],
    }


def build_official_gold_row(
    candidate: Dict[str, Any],
    *,
    selected_indexes: Sequence[int],
    verified_at: Optional[str] = None,
) -> Dict[str, Any]:
    citations = [dict(item) for item in list(candidate.get("citations") or []) if isinstance(item, dict)]
    selected = select_citations(citations, selected_indexes)
    plan = dict(candidate.get("plan_summary") or {})
    doc_ids: List[str] = []
    pages: List[int] = []
    chunk_ids: List[str] = []
    for citation in selected:
        doc_id = str(citation.get("doc_id", "") or "").strip()
        if doc_id and doc_id not in doc_ids:
            doc_ids.append(doc_id)
        page = citation.get("page")
        if isinstance(page, int) and page not in pages:
            pages.append(page)
        elif isinstance(page, float) and int(page) not in pages:
            pages.append(int(page))
        elif isinstance(page, str) and page.strip().isdigit():
            value = int(page.strip())
            if value not in pages:
                pages.append(value)
        chunk_id = str(citation.get("chunk_id", "") or "").strip()
        if chunk_id and chunk_id not in chunk_ids:
            chunk_ids.append(chunk_id)
    gold_keywords = derive_gold_keywords(
        str(candidate.get("message", "") or ""),
        str(plan.get("retrieval_query", "") or ""),
    )
    return {
        "case_id": str(candidate.get("case_id", "") or ""),
        "gold_doc_ids": doc_ids,
        "gold_pages": pages,
        "gold_chunk_ids": chunk_ids,
        "gold_keywords": gold_keywords,
        "should_retrieve": True,
        "label_source": "human_verified",
        "verified_at": verified_at or now_iso(),
    }
