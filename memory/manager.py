"""
【模块说明】
- 主要作用：提供统一记忆管理接口，封装情景记忆读写与用户画像维护。
- 核心类：MemoryManager。
- 核心函数：get_memory_manager（全局单例获取）、save/search/get_profile_context。
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


class MemoryManager:
    """统一记忆管理接口，供 Runner、Grader 与 MCP 工具调用。"""

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
    ) -> List[Dict[str, Any]]:
        """按关键词检索历史情景记忆，返回 list[dict]。"""
        return self._store.search_episodes(
            query=query,
            course_name=course_name,
            user_id=self.user_id,
            event_types=event_types,
            top_k=top_k,
            min_importance=min_importance,
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

    def get_profile_context(self, course_name: str) -> str:
        """生成注入 prompt 用的用户画像摘要（一段话）。"""
        p = self.get_profile(course_name)
        parts = []
        if p["weak_points"]:
            weak_str = "、".join(p["weak_points"][:8])  # 最多展示 8 个
            parts.append(f"该用户的薄弱知识点：{weak_str}，讲解时请重点关注。")
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

        # 移动到最前（最近错的最重要），去重
        merged = []
        seen = set()
        for tag in new_tags + existing:
            tag = tag.strip()
            if tag and tag not in seen:
                merged.append(tag)
                seen.add(tag)
        merged = merged[:20]  # 最多保留 20 条

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
