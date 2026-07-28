"""按用户隔离的工作区：一个用户一份数据库与一套目录。

隔离是结构性的（不同文件），不依赖每条查询都记得带过滤条件——现有二十多张表、
上百处查询，漏一处就串数据，而目录方式漏不了。
"""
from __future__ import annotations

import threading

from core.identity import LEGACY_MARKERS, workspace_id
from core.settings import Settings

from .bootstrap import Application, SharedRuntime, build_application, build_shared_runtime


class Workspaces:
    def __init__(self, settings: Settings, shared: SharedRuntime | None = None) -> None:
        self._settings = settings
        self._shared = shared or build_shared_runtime(settings)
        self._root = settings.data_dir / "users"
        self._cache: dict[str, Application] = {}
        # migration 里有非幂等的 ALTER TABLE ADD COLUMN，而前端首屏并发打好几个请求：
        # 不加锁的话新用户第一次登录会双建工作区、并发跑 migration 撞 duplicate column。
        self._lock = threading.Lock()

    @property
    def shared(self) -> SharedRuntime:
        return self._shared

    def for_username(self, username: str) -> Application:
        return self.for_id(workspace_id(username))

    def for_id(self, uid: str) -> Application:
        with self._lock:
            existing = self._cache.get(uid)
            if existing is not None:
                return existing
            application = build_application(self._settings.for_workspace(self._root / uid), self._shared)
            # 这两件事原本在进程启动时做；改成懒建后时机变成该用户首次请求。
            # 不调 start() 的话 submit 会走 rejected 分支，用户点索引拿到的报错跟真实原因无关。
            application.sessions.recover_stale_turns()
            application.knowledge_jobs.start()
            self._cache[uid] = application
            return application

    def default(self) -> Application:
        return self.for_username(self._settings.default_user)

    def legacy_data_pending(self) -> bool:
        """旧布局还在 data/ 根下、且还没有任何用户工作区。"""
        root = self._settings.data_dir
        has_legacy = any((root / marker).exists() for marker in LEGACY_MARKERS)
        return has_legacy and not any(self._root.glob("user_*"))

    def close_all(self) -> None:
        with self._lock:
            for application in self._cache.values():
                try: application.knowledge_jobs.shutdown()
                except Exception: pass
            self._cache.clear()
        self._shared.close()
