from __future__ import annotations

import re
from pathlib import Path

from contracts.knowledge import KnowledgeSearchPort

BACKEND = Path(__file__).resolve().parents[2] / "backend"


# 模块之间只能通过 api 子模块往来，那里放的是 Port。默认拒绝，例外必须写在这里，
# 这样新增的越界会被挡下，而不是像按名字列黑名单那样只挡住恰好想到的两个。
# 下面四条是历史遗留：agent 直接拿了别的模块的具体存储类，其中前两个内部是裸 SQL。
# 它们应当收敛成 Port，在那之前先冻在这里，不让同类越界再增加。
LEGACY_CROSSINGS = {
    ("agent", "sessions", "artifacts"),
    ("agent", "sessions", "compactions"),
    ("agent", "memory", "store"),
    ("agent", "notes", "store"),
}
# 三种写法都要认：from modules.X.Y import Z、from modules.X import Y、import modules.X.Y
CROSS_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+modules\.([a-z_]+)(?:\.([a-z_]+))?(?:\s+import\s+(.+))?$", re.MULTILINE)


def test_feature_modules_only_reach_each_other_through_api():
    modules = BACKEND / "modules"
    for source in modules.rglob("*.py"):
        own = source.relative_to(modules).parts[0]
        for other, submodule, imported in CROSS_IMPORT.findall(source.read_text(encoding="utf-8")):
            reached = submodule or imported.split(",")[0].strip()
            if other == own or reached == "api":
                continue
            assert (own, other, reached) in LEGACY_CROSSINGS, (
                f"{source.relative_to(BACKEND)} 越过 modules.{other}.api 直接 import 了 "
                f"modules.{other}.{submodule}；请在 {other}/api.py 里补 Port"
            )


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
