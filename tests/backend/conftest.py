from __future__ import annotations

from fastapi.testclient import TestClient


def workspace(client: TestClient):
    """测试里取默认用户的工作区。工作区按用户懒建，所以先摸一次接口再取。"""
    client.get("/api/v2/health")
    return client.app.state.workspaces.default()
