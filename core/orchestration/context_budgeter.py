"""Context budgeter for history/RAG/memory trimming before agent calls."""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List

from core.metrics import estimate_text_tokens


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


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
        self.history_recent_turns = _env_int("CB_HISTORY_RECENT_TURNS", 6)
        self.history_summary_max_tokens = _env_int("CB_HISTORY_SUMMARY_MAX_TOKENS", 700)
        self.rag_max_tokens = _env_int("CB_RAG_MAX_TOKENS", 1800)
        self.memory_max_tokens = _env_int("CB_MEMORY_MAX_TOKENS", 450)

    @staticmethod
    def _history_recent_text(history: List[Dict[str, Any]], recent_turns: int) -> str:
        if not history:
            return ""
        # One turn approximated as one user + one assistant message.
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
    def _history_summary_text(history: List[Dict[str, Any]], recent_turns: int) -> str:
        if not history:
            return ""
        keep = max(0, recent_turns * 2)
        older = history[:-keep] if keep > 0 else history
        if not older:
            return ""
        snippets: List[str] = []
        for msg in older[-12:]:
            role = str(msg.get("role", "user"))
            content = str(msg.get("content", "")).strip()
            if not content:
                continue
            snippets.append(f"{role}:{content[:80]}")
        if not snippets:
            return ""
        return "【较早历史摘要】" + " | ".join(snippets)

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
        # Split by source citation blocks to preserve provenance.
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
        if estimate_text_tokens(s) <= max_tokens:
            return s
        ratio = max_tokens / max(1, estimate_text_tokens(s))
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
    ) -> Dict[str, Any]:
        # 1) History: recent raw + summary.
        recent = self._history_recent_text(history, self.history_recent_turns)
        summary = self._history_summary_text(history, self.history_recent_turns)
        hist_text = "\n".join([x for x in [summary, recent] if x]).strip()
        hist_text = self._trim_to_tokens(hist_text, self.history_summary_max_tokens + 400)

        # 2) RAG: sentence-level compression.
        rag_comp = self.compress_rag_text(
            query=query,
            rag_text=rag_text,
            sent_per_chunk=rag_sent_per_chunk,
            sent_max_chars=rag_sent_max_chars,
        )
        rag_comp = self._trim_to_tokens(rag_comp, self.rag_max_tokens)

        # 3) Memory: selected short snippets.
        mem_comp = self._trim_to_tokens(memory_text, self.memory_max_tokens)

        sections = []
        if hist_text:
            sections.append(hist_text)
        if rag_comp:
            sections.append("【教材参考】\n" + rag_comp)
        if mem_comp:
            sections.append(mem_comp)
        final = "\n\n".join(sections).strip()

        # 4) Final hard cut.
        hard_budget = max(256, self.ctx_total_tokens - self.ctx_safety_margin)
        final = self._trim_to_tokens(final, hard_budget)
        history_tokens = estimate_text_tokens(hist_text)
        rag_tokens = estimate_text_tokens(rag_comp)
        memory_tokens = estimate_text_tokens(mem_comp)
        final_tokens = estimate_text_tokens(final)
        return {
            "history_text": hist_text,
            "rag_text": rag_comp,
            "memory_text": mem_comp,
            "final_text": final,
            "history_tokens_est": history_tokens,
            "rag_tokens_est": rag_tokens,
            "memory_tokens_est": memory_tokens,
            "final_tokens_est": final_tokens,
            "budget_tokens_est": hard_budget,
        }
