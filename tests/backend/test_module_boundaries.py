from __future__ import annotations

import re
from pathlib import Path

from contracts.knowledge import KnowledgeSearchPort

BACKEND = Path(__file__).resolve().parents[2] / "backend"


def test_feature_modules_do_not_import_other_feature_internals():
    modules = BACKEND / "modules"
    for feature_dir in (path for path in modules.iterdir() if path.is_dir()):
        for source in feature_dir.glob("*.py"):
            text = source.read_text(encoding="utf-8")
            for other in (path.name for path in modules.iterdir() if path.is_dir() and path.name != feature_dir.name):
                assert f"modules.{other}.repository" not in text, f"{source} crosses into {other} repository"
                assert f"modules.{other}.service" not in text, f"{source} crosses into {other} service"


def test_lower_layers_do_not_import_upward():
    # 依赖方向：app → modules/adapters → contracts/core。下层反向 import 上层即违规。
    upward = {
        "modules": re.compile(r"^\s*(from|import)\s+app\b", re.MULTILINE),
        "contracts": re.compile(r"^\s*(from|import)\s+(app|modules|adapters)\b", re.MULTILINE),
        "core": re.compile(r"^\s*(from|import)\s+(app|modules|adapters|contracts)\b", re.MULTILINE),
    }
    for package, pattern in upward.items():
        for source in (BACKEND / package).rglob("*.py"):
            match = pattern.search(source.read_text(encoding="utf-8"))
            assert match is None, f"{source} imports upward: {match.group(0).strip()}"


def test_agent_knowledge_port_requires_resolved_scope_not_a_naked_course_id():
    annotations = KnowledgeSearchPort.search.__annotations__
    assert "scope" in annotations
    assert "course_id" not in annotations
