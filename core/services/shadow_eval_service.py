"""Asynchronous online shadow-eval queue + worker."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Dict, List


class OnlineShadowEvalService:
    """Persist online eval queue records and process them asynchronously."""

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = self.base_dir / "_worker_state.json"
        self._state_lock = threading.Lock()
        self._worker_started = False

    @staticmethod
    def _env_bool(name: str, default: bool = False) -> bool:
        raw = str(os.getenv(name, "1" if default else "0")).strip().lower()
        return raw in {"1", "true", "yes", "on"}

    @staticmethod
    def _poll_sec() -> float:
        try:
            return max(5.0, float(os.getenv("ONLINE_EVAL_POLL_SEC", "30")))
        except Exception:
            return 30.0

    def _state_load(self) -> Dict[str, int]:
        if not self._state_path.exists():
            return {}
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        out: Dict[str, int] = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                try:
                    out[str(k)] = int(v)
                except Exception:
                    continue
        return out

    def _state_save(self, state: Dict[str, int]) -> None:
        serialized = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)
        if self._state_path.exists():
            try:
                current = self._state_path.read_text(encoding="utf-8")
                if current == serialized:
                    return
            except Exception:
                pass
        self._state_path.write_text(serialized, encoding="utf-8")

    def enqueue(self, payload: Dict[str, Any]) -> str:
        day = datetime.now().strftime("%Y-%m-%d")
        target_dir = self.base_dir / day
        target_dir.mkdir(parents=True, exist_ok=True)
        queue_path = target_dir / "eval_queue.jsonl"
        row = dict(payload or {})
        row.setdefault("queued_at", datetime.now().isoformat())
        with queue_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return str(queue_path)

    @staticmethod
    def _iter_new_lines(path: Path, offset: int) -> tuple[List[Dict[str, Any]], int]:
        rows: List[Dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as f:
            f.seek(max(0, int(offset or 0)))
            while True:
                line = f.readline()
                if not line:
                    break
                text = line.strip()
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
            new_offset = f.tell()
        return rows, new_offset

    @staticmethod
    def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        if not path.exists():
            return rows
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                text = line.strip()
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
        return rows

    @staticmethod
    def _append_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        with path.open("a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    @staticmethod
    def _to_benchmark_row(item: Dict[str, Any]) -> Dict[str, Any]:
        citations = item.get("citations") if isinstance(item.get("citations"), list) else []
        response_text = str(item.get("response_text", "") or "")
        e2e_latency = float(item.get("e2e_latency_ms", 0.0) or 0.0)
        first_token = item.get("first_token_latency_ms")
        try:
            first_token_latency = float(first_token) if first_token is not None else 0.0
        except Exception:
            first_token_latency = 0.0
        return {
            "case_id": str(item.get("case_id", "") or "").strip(),
            "mode": str(item.get("mode", "learn") or "learn"),
            "course_name": str(item.get("course_name", "") or ""),
            "message": str(item.get("message", "") or ""),
            "response_text": response_text,
            "response_preview": response_text[:500],
            "citations": citations,
            "rag_hit": 0.0,
            "rag_top1": 0.0,
            "rag_precision": 0.0,
            "rag_has_gold": 0.0,
            "rag_match_strategy": "",
            "rag_match_signal": "",
            "need_rag": bool(item.get("need_rag", False)),
            "trace_contract_error": False,
            "fallback_rate_case": 1.0 if bool(item.get("fallback", False)) else 0.0,
            "resolved_mode_override_count_case": 1.0 if bool(item.get("mode_overridden", False)) else 0.0,
            "session_store_hit_case": 1.0 if bool(item.get("session_store_hit", True)) else 0.0,
            "taskgraph_step_status_coverage_case": 1.0,
            "first_token_latency_ms": first_token_latency,
            "e2e_latency_ms": e2e_latency,
            "latency_budget_met_case": 1.0,
            "case_error": bool(item.get("case_error", False)),
        }

    @staticmethod
    def _to_case_row(item: Dict[str, Any]) -> Dict[str, Any]:
        history = item.get("history") if isinstance(item.get("history"), list) else []
        return {
            "case_id": str(item.get("case_id", "") or "").strip(),
            "mode": str(item.get("mode", "learn") or "learn"),
            "course_name": str(item.get("course_name", "") or ""),
            "message": str(item.get("message", "") or ""),
            "history": history,
            "judge_profile": "online_shadow",
            "tags": ["online_shadow"],
        }

    @staticmethod
    def _summary_from_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not rows:
            return {"total_cases": 0}
        e2e = [float(r.get("e2e_latency_ms", 0.0) or 0.0) for r in rows]
        ttft = [float(r.get("first_token_latency_ms", 0.0) or 0.0) for r in rows]

        def _p95(vals: List[float]) -> float:
            if not vals:
                return 0.0
            sorted_vals = sorted(vals)
            idx = int(0.95 * max(0, len(sorted_vals) - 1))
            return float(sorted_vals[idx])

        return {
            "total_cases": len(rows),
            "p50_e2e_latency_ms": float(median(e2e)),
            "p95_e2e_latency_ms": _p95(e2e),
            "p50_first_token_latency_ms": float(median(ttft)),
            "p95_first_token_latency_ms": _p95(ttft),
            "fallback_rate": float(sum(1 for r in rows if float(r.get("fallback_rate_case", 0.0) or 0.0) > 0.0)) / float(len(rows)),
            "trace_contract_error_rate": 0.0,
            "taskgraph_step_status_coverage": 1.0,
        }

    def _process_date_dir(self, date_dir: Path, state: Dict[str, int]) -> None:
        queue_path = date_dir / "eval_queue.jsonl"
        if not queue_path.exists():
            return
        queue_key = str(queue_path.resolve())
        offset = int(state.get(queue_key, 0) or 0)
        new_rows, new_offset = self._iter_new_lines(queue_path, offset)
        if not new_rows:
            state[queue_key] = new_offset
            return

        bench_raw = date_dir / "benchmark_raw_online.jsonl"
        bench_summary = date_dir / "benchmark_summary_online.json"
        cases_path = date_dir / "cases_online.jsonl"

        existing_cases = {
            str(row.get("case_id", "")).strip()
            for row in self._load_jsonl(cases_path)
            if str(row.get("case_id", "")).strip()
        }
        bench_rows = [self._to_benchmark_row(row) for row in new_rows]
        case_rows: List[Dict[str, Any]] = []
        for row in new_rows:
            case_id = str(row.get("case_id", "")).strip()
            if not case_id or case_id in existing_cases:
                continue
            case_rows.append(self._to_case_row(row))
            existing_cases.add(case_id)
        self._append_jsonl(bench_raw, bench_rows)
        self._append_jsonl(cases_path, case_rows)

        all_rows = self._load_jsonl(bench_raw)
        bench_summary.write_text(
            json.dumps(self._summary_from_rows(all_rows), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        state[queue_key] = new_offset

        if not self._env_bool("ONLINE_EVAL_RUN_JUDGE_REVIEW", True):
            return
        python_bin = os.getenv("ONLINE_EVAL_PYTHON_BIN", "").strip() or os.getenv("MCP_PYTHON_BIN", "").strip() or os.sys.executable
        judge_out = date_dir / "judge"
        review_out = date_dir / "review"
        judge_out.mkdir(parents=True, exist_ok=True)
        review_out.mkdir(parents=True, exist_ok=True)
        root = Path(__file__).resolve().parents[2]
        judge_cmd = [
            python_bin,
            str(root / "scripts" / "eval" / "judge_runner.py"),
            "--raw",
            str(bench_raw),
            "--cases",
            str(cases_path),
            "--output-dir",
            str(judge_out),
        ]
        review_cmd = [
            python_bin,
            str(root / "scripts" / "eval" / "review_runner.py"),
            "--benchmark-summary",
            str(bench_summary),
            "--benchmark-raw",
            str(bench_raw),
            "--judge-summary",
            str(judge_out / "judge_summary.json"),
            "--judge-raw",
            str(judge_out / "judge_raw.jsonl"),
            "--output-dir",
            str(review_out),
        ]
        try:
            subprocess.run(judge_cmd, check=False, timeout=max(30, int(os.getenv("ONLINE_EVAL_JUDGE_TIMEOUT_SEC", "1800"))))
            subprocess.run(review_cmd, check=False, timeout=max(30, int(os.getenv("ONLINE_EVAL_REVIEW_TIMEOUT_SEC", "900"))))
        except Exception:
            return

    def _worker_loop(self) -> None:
        while True:
            try:
                with self._state_lock:
                    state = self._state_load()
                    for date_dir in sorted(self.base_dir.iterdir()):
                        if not date_dir.is_dir():
                            continue
                        self._process_date_dir(date_dir, state)
                    self._state_save(state)
            except Exception:
                pass
            time.sleep(self._poll_sec())

    def start_worker(self) -> None:
        if self._worker_started:
            return
        if not self._env_bool("ONLINE_EVAL_WORKER_ENABLED", False):
            return
        thread = threading.Thread(target=self._worker_loop, daemon=True, name="online-shadow-eval-worker")
        thread.start()
        self._worker_started = True


_DEFAULT_ONLINE_SHADOW_EVAL = OnlineShadowEvalService(base_dir="./data/perf_runs/online_eval")


def get_default_online_shadow_eval() -> OnlineShadowEvalService:
    return _DEFAULT_ONLINE_SHADOW_EVAL

