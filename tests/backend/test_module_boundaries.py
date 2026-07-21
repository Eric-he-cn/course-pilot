from __future__ import annotations

from pathlib import Path

from contracts.knowledge import KnowledgeSearchPort


def test_feature_modules_do_not_import_other_feature_internals():
    modules = Path(__file__).resolve().parents[2] / "backend" / "modules"
    for feature_dir in (path for path in modules.iterdir() if path.is_dir()):
        for source in feature_dir.glob("*.py"):
            text = source.read_text(encoding="utf-8")
            for other in (path.name for path in modules.iterdir() if path.is_dir() and path.name != feature_dir.name):
                assert f"modules.{other}.repository" not in text, f"{source} crosses into {other} repository"
                assert f"modules.{other}.service" not in text, f"{source} crosses into {other} service"


def test_agent_knowledge_port_requires_resolved_scope_not_a_naked_course_id():
    annotations = KnowledgeSearchPort.search.__annotations__
    assert "scope" in annotations
    assert "course_id" not in annotations
