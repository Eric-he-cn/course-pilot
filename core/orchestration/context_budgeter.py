"""Context budgeter for history/RAG/memory trimming before agent calls."""

from __future__ import annotations

import json
import os
import re
from time import perf_counter
from typing import Any, Dict, List, Optional, Tuple

from core.metrics import add_event, estimate_text_tokens
from core.orchestration.prompts import (
    CONTEXT_COMPRESSOR_SYSTEM_PROMPT,
    CONTEXT_COMPRESSOR_USER_PROMPT,
    HISTORY_BLOCK_COMPRESSOR_SYSTEM_PROMPT,
    HISTORY_BLOCK_COMPRESSOR_USER_PROMPT,
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "1" if default else "0")).strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def _trim_by_chars(text: str, max_chars: int) -> str:
    s = (text or "").strip()
    if max_chars <= 0 or len(s) <= max_chars:
        return s
    return s[:max_chars].rstrip()


class ContextBudgeter:
    """Trim history/RAG/memory with a fixed budget order."""

    def __init__(self) -> None:
        self.ctx_total_tokens = _env_int("CTX_TOTAL_TOKENS", 8192)
        self.ctx_safety_margin = _env_int("CTX_SAFETY_MARGIN", 256)
        self.history_recent_turns = max(1, _env_int("CB_HISTORY_RECENT_TURNS", 5))
        self.recent_raw_turns = max(1, _env_int("CB_RECENT_RAW_TURNS", self.history_recent_turns))
        self.history_summary_max_tokens = _env_int("CB_HISTORY_SUMMARY_MAX_TOKENS", 2000)
        self.history_block_turns = max(1, _env_int("CB_HISTORY_SUMMARY_BLOCK_TURNS", 5))
        self.history_max_blocks = max(1, _env_int("CB_HISTORY_SUMMARY_MAX_BLOCKS", 10))
        self.history_block_target_tokens = max(80, _env_int("CB_HISTORY_BLOCK_COMPRESS_TARGET_TOKENS", 160))
        self.history_block_max_tokens = max(
            self.history_block_target_tokens,
            _env_int("CB_HISTORY_BLOCK_COMPRESS_MAX_TOKENS", 220),
        )
        self.history_block_timeout_ms = max(200, _env_int("CB_HISTORY_BLOCK_COMPRESS_TIMEOUT_MS", 1500))
        self.history_block_model = str(os.getenv("CB_HISTORY_BLOCK_COMPRESS_MODEL", "")).strip()
        self.history_block_temperature = _env_float("CB_HISTORY_BLOCK_COMPRESS_TEMPERATURE", 0.1)
        self.rag_max_tokens = _env_int("CB_RAG_MAX_TOKENS", 1800)
        self.memory_max_tokens = _env_int("CB_MEMORY_MAX_TOKENS", 450)
        self.rag_compress_owner = str(os.getenv("RAG_COMPRESS_OWNER", "retriever")).strip().lower() or "retriever"

        self.enable_llm_history_compress = _env_bool("CB_ENABLE_LLM_HISTORY_COMPRESS", True)
        self.llm_compress_trigger_tokens = max(120, _env_int("CB_LLM_COMPRESS_TRIGGER_TOKENS", 600))
        self.llm_compress_target_tokens = max(80, _env_int("CB_LLM_COMPRESS_TARGET_TOKENS", 260))
        self.llm_compress_timeout_ms = max(200, _env_int("CB_LLM_COMPRESS_TIMEOUT_MS", 1200))
        self.llm_compress_max_retries = max(0, _env_int("CB_LLM_COMPRESS_MAX_RETRIES", 0))
        self.llm_compress_model = str(os.getenv("CB_LLM_COMPRESS_MODEL", "")).strip()
        self.llm_compress_temperature = _env_float("CB_LLM_COMPRESS_TEMPERATURE", 0.1)

    @staticmethod
    def _history_recent_text(history: List[Dict[str, Any]], recent_turns: int) -> str:
        if not history:
            return ""
        keep = max(0, recent_turns * 2)
        recent = history[-keep:] if keep > 0 else []
        lines: List[str] = []
        for msg in recent:
            role = str(msg.get("role", "user"))
            content = str(msg.get("content", "")).strip()
            if not content:
                continue
            lines.append(f"[{role}] {content}")
        if not lines:
            return ""
        return "【最近对话】\n" + "\n".join(lines)

    @staticmethod
    def _history_older_lines(
        history: List[Dict[str, Any]],
        recent_turns: int,
        max_items: int = 24,
    ) -> List[Tuple[str, str]]:
        if not history:
            return []
        keep = max(0, recent_turns * 2)
        older = history[:-keep] if keep > 0 else history
        out: List[Tuple[str, str]] = []
        for msg in older[-max(1, max_items):]:
            role = str(msg.get("role", "user"))
            content = str(msg.get("content", "")).strip()
            if not content:
                continue
            out.append((role, _trim_by_chars(content, 220)))
        return out

    @staticmethod
    def _dedup_keep_order(items: List[str], limit: int) -> List[str]:
        out: List[str] = []
        seen = set()
        for item in items:
            t = item.strip()
            if not t:
                continue
            key = t.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(t)
            if len(out) >= limit:
                break
        return out

    @classmethod
    def _format_summary_card(cls, fields: Dict[str, List[str]]) -> str:
        parts = ["【历史摘要卡片】"]
        for key in ("facts", "constraints", "unresolved", "preferences", "next_steps"):
            vals = cls._dedup_keep_order(list(fields.get(key, []) or []), 4)
            parts.append(f"{key}:")
            if vals:
                parts.extend([f"- {v}" for v in vals])
            else:
                parts.append("- （无）")
        return "\n".join(parts).strip()

    @classmethod
    def _heuristic_summary_card(cls, lines: List[Tuple[str, str]]) -> str:
        facts: List[str] = []
        constraints: List[str] = []
        unresolved: List[str] = []
        preferences: List[str] = []
        constraint_keywords = ("必须", "不要", "不得", "仅", "要求", "格式", "限制", "截止", "必须要")
        unresolved_keywords = ("?", "？", "请", "希望", "需要", "怎么", "如何", "为什么")
        preference_keywords = ("喜欢", "偏好", "先", "以后", "习惯", "更想", "优先")

        for role, content in lines:
            text = content.strip()
            if not text:
                continue
            if role == "assistant":
                facts.append(_trim_by_chars(text, 120))
            if any(k in text for k in constraint_keywords):
                constraints.append(_trim_by_chars(text, 120))
            if role == "user" and any(k in text for k in unresolved_keywords):
                unresolved.append(_trim_by_chars(text, 120))
            if role == "user" and any(k in text for k in preference_keywords):
                preferences.append(_trim_by_chars(text, 120))

        next_steps: List[str] = []
        if unresolved:
            next_steps.append("优先回答未解决问题，并给出可执行步骤。")
        if constraints:
            next_steps.append("保持格式与约束不变，避免偏离用户要求。")
        if not next_steps and facts:
            next_steps.append("基于既有结论继续推进，不重复展开已确认内容。")
        if not next_steps:
            next_steps.append("继续保持上下文一致性并直接回答当前问题。")

        return cls._format_summary_card(
            {
                "facts": facts,
                "constraints": constraints,
                "unresolved": unresolved,
                "preferences": preferences,
                "next_steps": next_steps,
            }
        )

    @staticmethod
    def _parse_summary_json(raw: str) -> Dict[str, List[str]]:
        text = str(raw or "").strip()
        if not text:
            return {}
        if "```" in text:
            m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text, flags=re.IGNORECASE)
            if m:
                text = m.group(1).strip()
        try:
            obj = json.loads(text)
        except Exception:
            return {}
        if not isinstance(obj, dict):
            return {}

        out: Dict[str, List[str]] = {}
        for key in ("facts", "constraints", "unresolved", "preferences", "next_steps"):
            val = obj.get(key, [])
            if isinstance(val, list):
                out[key] = [str(x).strip() for x in val if str(x).strip()]
            elif isinstance(val, str) and val.strip():
                out[key] = [val.strip()]
            else:
                out[key] = []
        return out

    @staticmethod
    def _history_lines_to_text(lines: List[Tuple[str, str]]) -> str:
        return "\n".join(f"[{r}] {c}" for r, c in lines if c).strip()

    @staticmethod
    def _history_messages_to_text(history: List[Dict[str, Any]]) -> str:
        if not history:
            return ""
        lines = []
        for msg in history:
            role = str(msg.get("role", "user"))
            content = str(msg.get("content", "")).strip()
            if content:
                lines.append(f"[{role}] {content}")
        return "\n".join(lines).strip()

    def _llm_summary_card(self, lines: List[Tuple[str, str]]) -> Tuple[str, Optional[float], str]:
        source_text = self._history_lines_to_text(lines)
        source_tokens = estimate_text_tokens(source_text)
        if (
            not source_text
            or not self.enable_llm_history_compress
            or source_tokens < self.llm_compress_trigger_tokens
        ):
            return "", None, "skip"

        prompt = CONTEXT_COMPRESSOR_USER_PROMPT.format(source_text=source_text)
        messages = [
            {"role": "system", "content": CONTEXT_COMPRESSOR_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        timeout_s = max(0.4, self.llm_compress_timeout_ms / 1000.0)
        max_tokens = max(120, min(600, self.llm_compress_target_tokens + 80))
        retries = max(0, self.llm_compress_max_retries)
        t0 = perf_counter()
        last_err = ""
        for _ in range(retries + 1):
            try:
                from core.llm.openai_compat import get_llm_client

                llm = get_llm_client()
                model = self.llm_compress_model or llm.model
                resp = llm.client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=self.llm_compress_temperature,
                    max_tokens=max_tokens,
                    timeout=timeout_s,
                )
                content = resp.choices[0].message.content or ""
                fields = self._parse_summary_json(content)
                if not fields:
                    last_err = "invalid_json"
                    continue
                card = self._format_summary_card(fields)
                card = self._trim_to_tokens(card, self.llm_compress_target_tokens)
                elapsed_ms = (perf_counter() - t0) * 1000.0
                add_event(
                    "history_llm_compress",
                    success=True,
                    source_tokens_est=source_tokens,
                    target_tokens_est=self.llm_compress_target_tokens,
                    elapsed_ms=elapsed_ms,
                )
                return card, elapsed_ms, "llm"
            except Exception as ex:
                last_err = str(ex)
                continue

        elapsed_ms = (perf_counter() - t0) * 1000.0
        add_event(
            "history_llm_compress",
            success=False,
            source_tokens_est=source_tokens,
            target_tokens_est=self.llm_compress_target_tokens,
            elapsed_ms=elapsed_ms,
            error=last_err or "llm_compress_failed",
        )
        return "", elapsed_ms, "llm_failed"

    def compress_history_block(
        self,
        turns: List[List[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        messages_flat: List[Dict[str, Any]] = []
        for turn in turns:
            for msg in turn:
                if isinstance(msg, dict):
                    messages_flat.append(msg)
        source_text = self._history_messages_to_text(messages_flat)
        source_tokens = estimate_text_tokens(source_text)
        if not source_text:
            return {
                "summary_text": "",
                "source": "none",
                "tokens_est": 0,
                "elapsed_ms": None,
                "source_tokens_est": 0,
            }

        prompt = HISTORY_BLOCK_COMPRESSOR_USER_PROMPT.format(source_text=source_text)
        messages = [
            {"role": "system", "content": HISTORY_BLOCK_COMPRESSOR_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        timeout_s = max(0.4, self.history_block_timeout_ms / 1000.0)
        max_tokens = max(120, min(600, self.history_block_max_tokens))
        t0 = perf_counter()
        source = "heuristic"
        summary_text = ""
        last_err = ""
        try:
            from core.llm.openai_compat import get_llm_client

            llm = get_llm_client()
            model = self.history_block_model or llm.model
            resp = llm.client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=self.history_block_temperature,
                max_tokens=max_tokens,
                timeout=timeout_s,
            )
            content = resp.choices[0].message.content or ""
            fields = self._parse_summary_json(content)
            if fields:
                summary_text = self._format_summary_card(fields)
                source = "llm"
            else:
                last_err = "invalid_json"
        except Exception as ex:
            last_err = str(ex)

        if not summary_text:
            history_lines = [(str(m.get("role", "user")), str(m.get("content", ""))) for m in messages_flat]
            summary_text = self._heuristic_summary_card(history_lines)
            source = "heuristic"

        summary_text = self._trim_to_tokens(summary_text, self.history_block_target_tokens)
        elapsed_ms = (perf_counter() - t0) * 1000.0
        add_event(
            "history_block_compress",
            success=bool(summary_text),
            source=source,
            source_tokens_est=source_tokens,
            target_tokens_est=self.history_block_target_tokens,
            elapsed_ms=elapsed_ms,
            error=last_err or None,
        )
        return {
            "summary_text": summary_text,
            "source": source,
            "tokens_est": estimate_text_tokens(summary_text),
            "elapsed_ms": elapsed_ms,
            "source_tokens_est": source_tokens,
        }

    @staticmethod
    def _keywords(query: str) -> List[str]:
        q = (query or "").lower()
        kws = re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", q)
        return list(dict.fromkeys(kws))[:12]

    @staticmethod
    def _split_sentences(text: str) -> List[str]:
        out = re.split(r"(?<=[。！？!?\.])\s+|\n+", text or "")
        return [s.strip() for s in out if s.strip()]

    def compress_rag_text(
        self,
        query: str,
        rag_text: str,
        sent_per_chunk: int,
        sent_max_chars: int,
    ) -> str:
        text = (rag_text or "").strip()
        if not text:
            return ""
        blocks = re.split(r"(?=\[来源\d+:[^\]]+\])", text)
        kws = self._keywords(query)
        kept_blocks: List[str] = []
        for block in blocks:
            b = block.strip()
            if not b:
                continue
            lines = b.splitlines()
            head = lines[0] if lines else ""
            body = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
            sents = self._split_sentences(body) if body else []
            if not sents:
                kept_blocks.append(b)
                continue

            scored = []
            for s in sents:
                low = s.lower()
                overlap = sum(1 for k in kws if k in low)
                scored.append((overlap, len(s), s))
            scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

            top_n = max(1, int(sent_per_chunk))
            selected = [x[2] for x in scored[:top_n]]
            selected = [_trim_by_chars(s, sent_max_chars) for s in selected]
            if not any(selected):
                selected = [_trim_by_chars(sents[0], sent_max_chars)]
            kept_blocks.append(head + "\n" + " ".join([s for s in selected if s]))

        return "\n\n".join(kept_blocks).strip()

    @staticmethod
    def _trim_to_tokens(text: str, max_tokens: int) -> str:
        s = (text or "").strip()
        if not s or max_tokens <= 0:
            return ""
        est = estimate_text_tokens(s)
        if est <= max_tokens:
            return s
        ratio = max_tokens / max(1, est)
        target_chars = max(80, int(len(s) * ratio))
        return _trim_by_chars(s, target_chars)

    def build_context(
        self,
        query: str,
        history: List[Dict[str, Any]],
        rag_text: str,
        memory_text: str,
        rag_sent_per_chunk: int,
        rag_sent_max_chars: int,
        history_summary_state: Optional[Dict[str, Any]] = None,
        pending_history: Optional[List[Dict[str, Any]]] = None,
        recent_history: Optional[List[Dict[str, Any]]] = None,
        history_state_metrics: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        metrics = dict(history_state_metrics or {})
        if history_summary_state is not None or pending_history is not None or recent_history is not None:
            blocks = []
            if isinstance(history_summary_state, dict):
                blocks = history_summary_state.get("blocks", []) or []
            block_texts: List[str] = []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                turn_range = str(block.get("turn_range", "")).strip()
                summary = str(block.get("summary_text", "") or "").strip()
                if not summary:
                    continue
                if turn_range:
                    block_texts.append(f"【历史摘要块 {turn_range}】\n{summary}")
                else:
                    block_texts.append(summary)
            summary_text = "\n\n".join(block_texts).strip()
            summary_source = "state" if summary_text else "none"
            llm_compress_ms: Optional[float] = metrics.get("history_block_compress_ms")
            pending_text = self._history_messages_to_text(pending_history or [])
            if pending_text:
                pending_text = "【待归档历史】\n" + pending_text
            recent_text = self._history_messages_to_text(recent_history or [])
            if recent_text:
                recent_text = "【最近对话】\n" + recent_text
            summary_text = self._trim_to_tokens(summary_text, self.history_summary_max_tokens)
            history_sections = [x for x in [summary_text, pending_text, recent_text] if x]
            history_summary_tokens = estimate_text_tokens(summary_text)
            history_recent_tokens = estimate_text_tokens(recent_text)
            history_pending_tokens = estimate_text_tokens(pending_text)
        else:
            recent_turns = max(1, min(self.history_recent_turns, self.recent_raw_turns))
            recent_text = self._history_recent_text(history, recent_turns)
            older_lines = self._history_older_lines(history, recent_turns)

            summary_text = ""
            summary_source = "none"
            llm_compress_ms = None
            if older_lines:
                summary_text, llm_compress_ms, summary_source = self._llm_summary_card(older_lines)
                if not summary_text:
                    summary_text = self._heuristic_summary_card(older_lines)
                    summary_source = "heuristic"

            summary_budget = (
                self.llm_compress_target_tokens
                if summary_source == "llm"
                else self.history_summary_max_tokens
            )
            summary_text = self._trim_to_tokens(summary_text, summary_budget)
            history_sections = [x for x in [summary_text, recent_text] if x]
            history_summary_tokens = estimate_text_tokens(summary_text)
            history_recent_tokens = estimate_text_tokens(recent_text)
            history_pending_tokens = 0

        hist_text = "\n\n".join(history_sections).strip()
        recent_turns_for_budget = max(1, min(self.history_recent_turns, self.recent_raw_turns))
        history_budget = self.history_summary_max_tokens + max(120, recent_turns_for_budget * 120) + max(0, history_pending_tokens)
        hist_text = self._trim_to_tokens(hist_text, history_budget)

        rag_budgeter_compress_applied = False
        if self.rag_compress_owner == "budgeter":
            rag_comp = self.compress_rag_text(
                query=query,
                rag_text=rag_text,
                sent_per_chunk=rag_sent_per_chunk,
                sent_max_chars=rag_sent_max_chars,
            )
            rag_budgeter_compress_applied = True
        else:
            # 默认由 Retriever 负责句级压缩；Budgeter 只做 token 预算裁切，避免重复压缩。
            rag_comp = str(rag_text or "").strip()
        rag_comp = self._trim_to_tokens(rag_comp, self.rag_max_tokens)

        mem_comp = self._trim_to_tokens(memory_text, self.memory_max_tokens)

        sections = []
        if hist_text:
            sections.append(hist_text)
        if rag_comp:
            sections.append("【教材参考】\n" + rag_comp)
        if mem_comp:
            sections.append(mem_comp)
        final = "\n\n".join(sections).strip()

        hard_budget = max(256, self.ctx_total_tokens - self.ctx_safety_margin)
        final_before_hard_trim_tokens = estimate_text_tokens(final)
        final = self._trim_to_tokens(final, hard_budget)
        hard_truncated = final_before_hard_trim_tokens > hard_budget
        history_tokens = estimate_text_tokens(hist_text)
        rag_tokens = estimate_text_tokens(rag_comp)
        memory_tokens = estimate_text_tokens(mem_comp)
        final_tokens = estimate_text_tokens(final)
        llm_applied = summary_source == "llm"
        history_summary_block_count = int(metrics.get("history_summary_block_count", 0) or 0)
        history_summary_state_hit = bool(metrics.get("history_summary_state_hit", False))
        add_event(
            "context_budget",
            history_tokens_est=history_tokens,
            history_recent_tokens_est=history_recent_tokens,
            history_pending_tokens_est=history_pending_tokens,
            history_summary_tokens_est=history_summary_tokens,
            history_summary_source=summary_source,
            history_llm_compress_applied=llm_applied,
            history_llm_compress_ms=llm_compress_ms,
            history_summary_block_count=history_summary_block_count,
            history_summary_state_hit=history_summary_state_hit,
            history_block_compress_ms=metrics.get("history_block_compress_ms"),
            rag_compress_owner=self.rag_compress_owner,
            rag_budgeter_compress_applied=rag_budgeter_compress_applied,
            rag_tokens_est=rag_tokens,
            memory_tokens_est=memory_tokens,
            final_tokens_before_hard_trim_est=final_before_hard_trim_tokens,
            final_tokens_est=final_tokens,
            budget_tokens_est=hard_budget,
            hard_truncated=hard_truncated,
        )
        return {
            "history_text": hist_text,
            "history_recent_text": recent_text,
            "history_summary_text": summary_text,
            "history_summary_source": summary_source,
            "history_llm_compress_applied": llm_applied,
            "history_llm_compress_ms": llm_compress_ms,
            "rag_compress_owner": self.rag_compress_owner,
            "rag_budgeter_compress_applied": rag_budgeter_compress_applied,
            "rag_text": rag_comp,
            "memory_text": mem_comp,
            "final_text": final,
            "history_tokens_est": history_tokens,
            "history_recent_tokens_est": history_recent_tokens,
            "history_pending_tokens_est": history_pending_tokens,
            "history_summary_tokens_est": history_summary_tokens,
            "history_summary_block_count": history_summary_block_count,
            "history_summary_state_hit": history_summary_state_hit,
            "rag_tokens_est": rag_tokens,
            "memory_tokens_est": memory_tokens,
            "final_tokens_before_hard_trim_est": final_before_hard_trim_tokens,
            "final_tokens_est": final_tokens,
            "budget_tokens_est": hard_budget,
            "hard_truncated": hard_truncated,
        }
