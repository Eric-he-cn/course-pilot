"""
【模块说明】
- 主要作用：提供统一记忆管理接口，封装情景记忆读写与用户画像维护。
- 核心类：MemoryManager。
- 核心函数：get_memory_manager（全局单例获取）、save/search/get_profile_context。
- 阅读建议：先看模块说明，再看类/函数头部注释和关键步骤注释。
- 注释策略：每个相对独立代码块都使用“目的 + 实现方式”进行说明。
"""
import os
from typing import List, Dict, Any, Optional
from memory.store import SQLiteMemoryStore


# ── 单例：避免每次请求重新建立 SQLite 连接 ────────────────────────────────
_store: Optional[SQLiteMemoryStore] = None


def _get_store() -> SQLiteMemoryStore:
    global _store
    if _store is None:
        _store = SQLiteMemoryStore()
    return _store

"""MemoryManager: 统一记忆管理接口，供 Runner、Grader 与 MCP 工具调用。
    目的：提供一个统一的接口来管理情景记忆和用户画像，供不同组件调用，避免重复代码和耦合。
    实现方式：MemoryManager 类封装了情景记忆的保存、检索和格式化，以及用户画像的获取和更新。
        通过 get_memory_manager 函数提供全局单例实例，确保在不同组件中共享同一记忆库连接。"""
class MemoryManager:

    def __init__(self, user_id: str = "default", store: Optional[SQLiteMemoryStore] = None):
        self.user_id = user_id
        self._store = store or _get_store()

    # ── 情景记忆 ──────────────────────────────────────────────────────────────

    def save_episode(
        self,
        course_name: str,
        event_type: str,
        content: str,
        importance: float = 0.5,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """写入一条情景记忆。

        event_type 建议值：
          'qa'        — 学习模式问答
          'mistake'   — 练习/考试错题（importance 建议 0.9）
          'practice'  — 练习做题（非错题）
          'exam'      — 考试完成事件
        """
        eid = self._store.save_episode(
            course_name=course_name,
            event_type=event_type,
            content=content,
            importance=importance,
            metadata=metadata,
            user_id=self.user_id,
        )
        print(f"[Memory] 保存情景记忆 [{event_type}] eid={eid[:8]}... course={course_name}")
        return eid

    def search_episodes(
        self,
        query: str,
        course_name: str,
        event_types: Optional[List[str]] = None,
        top_k: int = 3,
        min_importance: float = 0.0,
        mode: Optional[str] = None,
        agent: Optional[str] = None,
        phase: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """按关键词检索历史情景记忆，返回 list[dict]。"""
        return self._store.search_episodes(
            query=query,
            course_name=course_name,
            user_id=self.user_id,
            event_types=event_types,
            top_k=top_k,
            min_importance=min_importance,
            mode=mode,
            agent=agent,
            phase=phase,
        )

    def get_recent_episodes(
        self,
        course_name: str,
        event_types: Optional[List[str]] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """取最近情景记忆，用于生成学习报告。"""
        return self._store.get_recent_episodes(
            course_name=course_name,
            user_id=self.user_id,
            event_types=event_types,
            limit=limit,
        )

    def format_episodes_context(self, episodes: List[Dict[str, Any]]) -> str:
        """将检索到的情景记忆格式化为 LLM 可读的上下文字符串。"""
        if not episodes:
            return ""
        lines = ["【相关历史记录】"]
        for ep in episodes:
            date_str = ep.get("created_at", "")[:10]
            etype = {"qa": "问答", "mistake": "错题", "practice": "练习", "exam": "考试"}.get(
                ep.get("event_type", ""), ep.get("event_type", "")
            )
            importance_flag = "⚠️" if ep.get("importance", 0) >= 0.8 else ""
            lines.append(f"[{date_str} {etype}]{importance_flag} {ep['content'][:150]}")
        return "\n".join(lines)

    # ── 用户画像 ──────────────────────────────────────────────────────────────

    def get_profile(self, course_name: str) -> Dict[str, Any]:
        """获取当前课程的用户画像。"""
        return self._store.get_profile(self.user_id, course_name)

    @staticmethod
    def _normalize_concepts(concepts: Optional[List[str]], limit: int = 12) -> List[str]:
        """标准化知识点列表：去空、去重、裁剪长度与数量。"""
        if not concepts:
            return []
        out: List[str] = []
        seen = set()
        for c in concepts:
            name = str(c).strip()
            if not name:
                continue
            if len(name) > 40:
                name = name[:40]
            if name not in seen:
                out.append(name)
                seen.add(name)
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _merge_weak_points(existing: List[str], new_tags: List[str], limit: int = 20) -> List[str]:
        """合并薄弱点：新标签前置、去重、限长。"""
        merged: List[str] = []
        seen = set()
        for tag in (new_tags or []) + (existing or []):
            t = str(tag).strip()
            if not t:
                continue
            if t not in seen:
                merged.append(t)
                seen.add(t)
            if len(merged) >= limit:
                break
        return merged

    @staticmethod
    def _update_concept_mastery_map(
        concept_mastery: Dict[str, Any],
        concepts: List[str],
        score: float,
    ) -> Dict[str, Any]:
        """根据分数更新知识点掌握度（EMA + 累计统计）。"""
        updated: Dict[str, Any] = dict(concept_mastery or {})
        safe_score = max(0.0, min(100.0, float(score)))
        target = safe_score / 100.0

        for concept in concepts:
            current = updated.get(concept)
            if not isinstance(current, dict):
                current = {}

            attempts_old = int(current.get("attempts", 0) or 0)
            avg_old = float(current.get("avg_score", 0.0) or 0.0)
            if attempts_old > 0:
                mastery_default = avg_old / 100.0
            else:
                mastery_default = 0.5
            mastery_old = float(current.get("mastery", mastery_default) or mastery_default)
            mastery_old = max(0.0, min(1.0, mastery_old))

            attempts_new = attempts_old + 1
            avg_new = (avg_old * attempts_old + safe_score) / attempts_new

            # 前几次学习更新更敏感，后期逐渐稳定，避免短期波动过大。
            alpha = max(0.12, 0.30 - min(attempts_old, 9) * 0.02)
            mastery_new = (1.0 - alpha) * mastery_old + alpha * target
            mastery_new = max(0.0, min(1.0, mastery_new))

            updated[concept] = {
                "mastery": round(mastery_new, 4),
                "attempts": attempts_new,
                "avg_score": round(avg_new, 2),
            }
        return updated

    def record_event(
        self,
        course_name: str,
        event_type: str,
        content: str,
        importance: float = 0.5,
        metadata: Optional[Dict[str, Any]] = None,
        score: Optional[float] = None,
        concepts: Optional[List[str]] = None,
        update_weak_points: bool = False,
        increment_qa: bool = False,
        increment_practice: bool = False,
        mode: Optional[str] = None,
        agent: Optional[str] = None,
        phase: Optional[str] = None,
    ) -> str:
        """统一记忆写入口：一次调用同时写情景记忆与用户画像。"""
        concept_list = self._normalize_concepts(concepts)
        meta = dict(metadata or {})
        if score is not None and "score" not in meta:
            meta["score"] = score
        if concept_list and "concepts" not in meta:
            meta["concepts"] = concept_list
        if mode and "mode" not in meta:
            meta["mode"] = mode
        if agent and "agent" not in meta:
            meta["agent"] = agent
        if phase and "phase" not in meta:
            meta["phase"] = phase

        eid = self.save_episode(
            course_name=course_name,
            event_type=event_type,
            content=content,
            importance=importance,
            metadata=meta,
        )

        profile = self.get_profile(course_name)
        updates: Dict[str, Any] = {}

        if increment_qa:
            updates["total_qa"] = int(profile.get("total_qa", 0)) + 1

        if increment_practice and score is not None:
            total_old = int(profile.get("total_practice", 0))
            avg_old = float(profile.get("avg_score", 0.0))
            total_new = total_old + 1
            avg_new = (avg_old * total_old + float(score)) / total_new
            updates["total_practice"] = total_new
            updates["avg_score"] = round(avg_new, 1)

        if update_weak_points and concept_list:
            existing_weak = profile.get("weak_points", [])
            updates["weak_points"] = self._merge_weak_points(existing_weak, concept_list)

        if score is not None and concept_list:
            concept_mastery = profile.get("concept_mastery", {})
            updates["concept_mastery"] = self._update_concept_mastery_map(
                concept_mastery=concept_mastery if isinstance(concept_mastery, dict) else {},
                concepts=concept_list,
                score=float(score),
            )

        if updates:
            self._store.upsert_profile(self.user_id, course_name, **updates)

        return eid

    def get_profile_context(self, course_name: str) -> str:
        """生成注入 prompt 用的用户画像摘要（一段话）。"""
        p = self.get_profile(course_name)
        parts = []
        if p["weak_points"]:
            weak_str = "、".join(p["weak_points"][:8])  # 最多展示 8 个
            parts.append(f"该用户的薄弱知识点：{weak_str}，讲解时请重点关注。")
        concept_mastery = p.get("concept_mastery", {})
        if isinstance(concept_mastery, dict) and concept_mastery:
            weak_candidates = []
            for name, info in concept_mastery.items():
                if not isinstance(info, dict):
                    continue
                attempts = int(info.get("attempts", 0) or 0)
                mastery = float(info.get("mastery", 0.5) or 0.5)
                if attempts >= 2:
                    weak_candidates.append((name, mastery, attempts))
            weak_candidates.sort(key=lambda x: (x[1], -x[2], x[0]))
            weakest = weak_candidates[:3]
            if weakest:
                weak_text = "、".join(f"{n}(掌握度{m:.0%})" for n, m, _ in weakest)
                parts.append(f"近期掌握较弱知识点：{weak_text}。")
        if p["total_practice"] > 0:
            parts.append(
                f"已做 {p['total_practice']} 道练习题，平均得分 {p['avg_score']:.0f} 分。"
            )
        return " ".join(parts) if parts else ""

    def update_weak_points(self, course_name: str, new_tags: List[str]) -> None:
        """合并错题标签到薄弱知识点列表（去重，最多保留 20 条）。"""
        if not new_tags:
            return
        p = self.get_profile(course_name)
        existing: List[str] = p.get("weak_points", [])
        merged = self._merge_weak_points(existing, self._normalize_concepts(new_tags), limit=20)

        self._store.upsert_profile(self.user_id, course_name, weak_points=merged)
        print(f"[Memory] 更新薄弱知识点：{merged[:5]}...")

    def record_practice_result(
        self, course_name: str, score: float, is_mistake: bool = False
    ) -> None:
        """更新用户画像中的练习统计（滑动平均分）。"""
        p = self.get_profile(course_name)
        total = p["total_practice"] + 1
        old_avg = p["avg_score"]
        new_avg = (old_avg * (total - 1) + score) / total  # 累计平均
        self._store.upsert_profile(
            self.user_id,
            course_name,
            total_practice=total,
            avg_score=round(new_avg, 1),
        )

    def increment_qa_count(self, course_name: str) -> None:
        """每次 learn 模式问答后 +1。"""
        p = self.get_profile(course_name)
        self._store.upsert_profile(
            self.user_id, course_name, total_qa=p["total_qa"] + 1
        )

    # ── 统计 ──────────────────────────────────────────────────────────────────

    def get_stats(self, course_name: str = None) -> Dict[str, Any]:
        """返回记忆库统计信息。"""
        stats = self._store.get_stats(self.user_id, course_name)
        if course_name:
            profile = self.get_profile(course_name)
            stats["weak_points"] = profile["weak_points"]
            stats["concept_mastery"] = profile.get("concept_mastery", {})
            stats["total_qa"] = profile["total_qa"]
            stats["total_practice"] = profile["total_practice"]
            stats["avg_score"] = profile["avg_score"]
        return stats


# ── 全局默认实例（runner / grader 直接 import 使用）──────────────────────────
_default_manager: Optional[MemoryManager] = None


def get_memory_manager(user_id: str = "default") -> MemoryManager:
    """获取全局 MemoryManager 实例（按 user_id 区分）。"""
    global _default_manager
    # 简易版：单用户场景直接复用同一实例
    if _default_manager is None or _default_manager.user_id != user_id:
        _default_manager = MemoryManager(user_id=user_id)
    return _default_manager
