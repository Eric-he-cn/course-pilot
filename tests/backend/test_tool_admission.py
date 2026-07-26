from __future__ import annotations

from pathlib import Path

import pytest

from adapters.web import HttpWebAccess
from contracts.web import WebAccessError
from modules.agent.calculator import CalculationError, evaluate
from modules.agent.tools import (
    MAIN,
    TOOL_CAPABILITY,
    capabilities_of,
    profile_for_skill,
    specs_for,
    validate_profiles,
)
from modules.notes.store import NoteStore


def test_every_tool_has_a_capability_and_profiles_are_consistent():
    """能力归类漏一个、或 profile 含未声明能力的工具，都要在这里暴露，
    而不是等运行期静默拒绝。"""
    assert validate_profiles() == []


def test_schema_layer_hides_tools_the_profile_cannot_use():
    """策略在模型看到工具之前就过滤：不允许的工具连 schema 都不下发。"""
    main_names = {spec.name for spec in specs_for(MAIN.tools, capabilities=MAIN.capabilities)}
    assert {"web_search", "web_fetch", "note_write", "calculator"} <= main_names


def test_skill_gets_exactly_its_declared_tools_and_inherits_spend_limits():
    """skill 拿到的就是它声明的那些工具，没声明的（这里是写笔记）拿不到；
    花钱工具的次数上限沿用主 profile，激活 skill 不是绕开预算的口子。"""
    from pathlib import Path

    from modules.agent.skills import load_skill

    practice = load_skill(Path(__file__).resolve().parents[2] / "skills" / "builtin" / "practice" / "SKILL.md")
    profile = profile_for_skill(practice.allowed_tools)
    granted = {spec.name for spec in specs_for(profile.tools, capabilities=profile.capabilities)}
    assert {"search_materials", "emit_evidence", "web_search"} <= granted
    assert "note_write" not in granted
    assert profile.per_tool_budget["web_search"] == MAIN.per_tool_budget["web_search"]


def test_skill_gets_its_declared_tools_plus_the_baseline():
    """声明即权限，例外只有基座工具：它们是跨 skill 的基础设施，不必每份
    SKILL.md 重复声明。既没声明、又不在基座里的，一件都拿不到。"""
    from modules.agent.tools import BASELINE_TOOLS

    profile = profile_for_skill(("search_materials", "note_write"))
    names = {spec.name for spec in specs_for(profile.tools, capabilities=profile.capabilities)}
    assert names == {"search_materials", "note_write", *BASELINE_TOOLS}
    assert "plan_update" not in names and "web_search" not in names and "use_skill" not in names


def test_privileged_tools_are_not_importable_by_user_skills():
    """能力集合推不出导入白名单：write_state 里就有 plan_update / memory_patch。"""
    from modules.agent.skills import IMPORTABLE_TOOLS

    for name in ("plan_update", "memory_patch", "use_skill", "note_write", "web_search", "web_fetch"):
        assert name not in IMPORTABLE_TOOLS, f"{name} 不该允许被导入的 skill 使用"
    assert TOOL_CAPABILITY["plan_update"] == TOOL_CAPABILITY["emit_evidence"]  # 同能力，靠白名单区分信任


@pytest.mark.parametrize(
    "url, why",
    [
        ("http://127.0.0.1:8000/api/v2/health", "本机回环"),
        ("http://localhost:8000/", "localhost 别名"),
        ("http://2130706433/", "十进制写法的 127.0.0.1"),
        ("http://0x7f000001/", "十六进制写法"),
        ("http://127.1/", "短写法"),
        ("http://[::1]/", "IPv6 回环"),
        ("http://169.254.169.254/latest/meta-data/", "云元数据地址"),
        ("http://10.0.0.1/", "私网"),
        ("http://192.168.1.1/", "家用路由器管理页"),
        ("http://0.0.0.0/", "全零地址"),
    ],
)
def test_fetch_refuses_non_public_addresses(url, why):
    """字面量校验挡不住十进制/十六进制/短写法——它们过不了 ipaddress 却过得了解析器，
    所以校验只能放在 getaddrinfo 之后。"""
    web = HttpWebAccess(api_key="k")
    with pytest.raises(WebAccessError) as error:
        web.fetch(url=url)
    assert error.value.code in {"blocked_address", "dns_failed"}, why


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://example.com"])
def test_fetch_refuses_non_http_schemes(url):
    with pytest.raises(WebAccessError) as error:
        HttpWebAccess(api_key="k").fetch(url=url)
    assert error.value.code == "unsupported_scheme"


def test_search_without_key_is_reported_as_not_configured():
    with pytest.raises(WebAccessError) as error:
        HttpWebAccess(api_key="").search(query="x")
    assert error.value.code == "not_configured"


@pytest.mark.parametrize(
    "title",
    ["../../../etc/passwd", "..", ".", "/etc/passwd", "  ", "....//x"],
)
def test_notes_cannot_escape_the_course_directory(tmp_path, title):
    store = NoteStore(tmp_path)
    try:
        store.write(course_id="course_1", title=title, content="x")
    except ValueError:
        pass  # 拒绝是预期结果之一
    outside = list(tmp_path.glob("*.md")) + list((tmp_path / "notes").glob("*.md"))
    assert not outside, f"「{title}」写出了课程目录"


def test_notes_reject_symlinked_targets(tmp_path):
    """指向目录外的符号链接不能成为写入通道：resolve() 会解析链接，
    落点因此暴露给 commonpath 校验。"""
    store = NoteStore(tmp_path)
    store.write(course_id="course_1", title="正常笔记", content="内容")
    secret = tmp_path / "secret.md"
    secret.write_text("原始内容", encoding="utf-8")
    (tmp_path / "notes" / "course_1" / "被链接.md").symlink_to(secret)

    with pytest.raises(ValueError):
        store.write(course_id="course_1", title="被链接", content="覆盖内容")
    assert secret.read_text(encoding="utf-8") == "原始内容"


def test_notes_roundtrip_and_course_isolation(tmp_path):
    store = NoteStore(tmp_path)
    store.write(course_id="course_1", title="调度算法卡片", content="# FIFO\n先来先服务")
    store.write(course_id="course_2", title="另一门课的", content="内容")

    assert "先来先服务" in store.read(course_id="course_1", title="调度算法卡片")
    assert [note.title for note in store.list_notes(course_id="course_1")] == ["调度算法卡片"]
    with pytest.raises(LookupError):
        store.read(course_id="course_1", title="另一门课的")


def test_notes_append_mode(tmp_path):
    store = NoteStore(tmp_path)
    store.write(course_id="c", title="错题本", content="第一题")
    store.write(course_id="c", title="错题本", content="第二题", mode="append")
    body = store.read(course_id="c", title="错题本")
    assert "第一题" in body and "第二题" in body


@pytest.mark.parametrize(
    "expression, expected",
    [("2+3*4", 14), ("(100+10+10)/3", 40.0), ("10//3", 3), ("2**10", 1024), ("-5+3", -2)],
)
def test_calculator_evaluates_arithmetic(expression, expected):
    assert evaluate(expression) == expected


@pytest.mark.parametrize(
    "expression, why",
    [
        ("2**999999999", "AST 只有 5 个节点，资源上限必须在求值时判"),
        ("9**9**9**9", "嵌套指数"),
        ("__import__('os').system('ls')", "函数调用"),
        ("open('/etc/passwd').read()", "内建函数"),
        ("x + 1", "变量名"),
        ("1/0", "除零"),
        ("[1,2,3]", "非算术字面量"),
        ("1 << 999999", "位移同样能炸"),
    ],
)
def test_calculator_refuses_dangerous_expressions(expression, why):
    with pytest.raises(CalculationError):
        evaluate(expression)


def test_untrusted_web_content_is_labelled_before_the_body():
    """声明必须在正文之前——后置声明会被长正文推走。"""
    from modules.agent.tools import _UNTRUSTED_PREFIX

    assert "不要执行" in _UNTRUSTED_PREFIX
    assert "只作资料" in _UNTRUSTED_PREFIX


def test_system_prompt_states_today_and_web_content_rule():
    from modules.agent.context import assemble_messages

    system = assemble_messages(
        course_name="测试", materials=[], history=[], question="q",
        seed_query="q", seed_result_text="e", history_token_budget=1000,
    ).messages[0].content
    assert "今天是 2" in system  # 排计划要靠注入的日期，不能让模型猜
    assert "网络内容" in system


def test_all_builtin_skills_load_and_declare_known_tools():
    """内建 skill 的工具声明必须都能解析出来：声明了不存在的工具就是半残 skill，
    要在这里报出来而不是等运行期静默拒绝。"""
    from modules.agent.skills import SkillRegistry

    root = Path(__file__).resolve().parents[2] / "skills" / "builtin"
    registry = SkillRegistry.from_directory(root)
    names = set(registry.builtin_names())
    assert names == {"practice", "flashcards", "diagram", "mistake_review", "research"}

    for name in names:
        skill = registry.get(name)
        granted = {spec.name for spec in specs_for(skill.allowed_tools, capabilities=capabilities_of(skill.allowed_tools))}
        assert granted == set(skill.allowed_tools), f"{name} 声明了无法授予的工具：{set(skill.allowed_tools) - granted}"
        assert skill.when_to_use and skill.description


def test_network_access_stays_an_explicit_per_skill_decision():
    """哪些 skill 能出网写死在这里：research 要查教材外的资料，practice 要核对术语说法。
    再给别的 skill 开联网，必须先改这条断言——不让它悄悄扩散。"""
    from modules.agent.skills import SkillRegistry

    registry = SkillRegistry.from_directory(Path(__file__).resolve().parents[2] / "skills" / "builtin")
    online = {name for name in registry.builtin_names() if "network" in capabilities_of(registry.get(name).allowed_tools)}
    assert online == {"research", "practice"}


def test_every_skill_example_actually_triggers_its_own_pre_routing():
    """帮助页展示的例句必须真的能命中预路由，否则用户照着说却加载不到 skill。
    这条是帮助页不腐烂的那个检查。

    practice 有两个触发源：意图正则（要练题）与"本会话有待批改的练习"这个状态，
    所以它的作答类例句不该被要求命中正则，只要求至少有一句命中。
    """
    from modules.agent.service import _PRACTICE_INTENT, _SKILL_INTENT
    from modules.agent.skills import SkillRegistry

    registry = SkillRegistry.from_directory(Path(__file__).resolve().parents[2] / "skills" / "builtin")
    for name in registry.builtin_names():
        skill = registry.get(name)
        assert skill.examples, f"{name} 没有触发例句"
        pattern = _PRACTICE_INTENT if name == "practice" else _SKILL_INTENT.get(name)
        assert pattern is not None, f"{name} 没有预路由正则"
        hits = [example for example in skill.examples if pattern.search(example)]
        if name == "practice":
            assert hits, "practice 的例句里至少要有一句能命中意图正则"
        else:
            assert len(hits) == len(skill.examples), \
                f"{name} 这些例句命中不了自己的预路由正则：{set(skill.examples) - set(hits)}"


def test_frontend_tool_labels_and_capability_hints_cover_every_tool():
    """前端的中文名与能力分组是硬编码的镜像：漏一个工具就会在界面上露出英文函数名，
    或者在使用说明里被归错组。"""
    import re

    app = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    labels = set(re.findall(r"(\w+): '", app.split("const TOOL_LABELS")[1].split("}")[0]))
    hints = dict(re.findall(r"(\w+): '(\w+)'", app.split("TOOL_CAPABILITY_HINT")[1].split("}")[0]))
    backend = set(TOOL_CAPABILITY)

    assert labels == backend, f"TOOL_LABELS 与后端工具不一致：{labels ^ backend}"
    assert set(hints) == backend, f"TOOL_CAPABILITY_HINT 少了：{backend - set(hints)}"
    for name, capability in hints.items():
        assert TOOL_CAPABILITY[name] == capability, f"{name} 的能力分组前后端不一致"


def test_notes_have_a_read_route(tmp_path):
    """笔记有 4 个 skill 在写，必须有查看入口，否则用户存了看不到。"""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from core.settings import Settings

    data_dir = tmp_path / "data"
    settings = Settings(
        data_dir=data_dir, database_path=data_dir / "coursepilot.db", uploads_dir=data_dir / "materials",
        text_provider="example", text_base_url="x", text_api_key="", text_model="m",
        enable_remote_llm=False, chunk_size=120, chunk_overlap=20, top_k_results=6,
    )
    with TestClient(create_app(settings=settings)) as client:
        course = client.post("/api/v2/courses", json={"name": "算法"}).json()
        assert client.get(f"/api/v2/courses/{course['id']}/notes").json() == {"notes": []}

        NoteStore(client.app.state.workspaces.default().settings.data_dir).write(
            course_id=course["id"], title="调度卡片", content="# Q1\n答案")
        listed = client.get(f"/api/v2/courses/{course['id']}/notes").json()["notes"]
        assert [note["title"] for note in listed] == ["调度卡片"]

        body = client.get(f"/api/v2/courses/{course['id']}/notes/调度卡片").json()
        assert "答案" in body["content"]
        assert client.get(f"/api/v2/courses/{course['id']}/notes/不存在的").status_code == 404
        # 课程隔离：另一门课看不到这篇
        other = client.post("/api/v2/courses", json={"name": "编译"}).json()
        assert client.get(f"/api/v2/courses/{other['id']}/notes").json() == {"notes": []}


def test_memory_has_read_and_write_routes(tmp_path):
    """长期记忆此前只有文件没有入口，而项目介绍宣称"可读可编辑"。"""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from core.settings import Settings

    data_dir = tmp_path / "data"
    settings = Settings(
        data_dir=data_dir, database_path=data_dir / "coursepilot.db", uploads_dir=data_dir / "materials",
        text_provider="example", text_base_url="x", text_api_key="", text_model="m",
        enable_remote_llm=False, chunk_size=120, chunk_overlap=20, top_k_results=6,
    )
    with TestClient(create_app(settings=settings)) as client:
        assert client.get("/api/v2/memory").json()["content"] == ""
        client.put("/api/v2/memory", json={"content": "偏好：先给结论再展开"})
        assert "先给结论" in client.get("/api/v2/memory").json()["content"]

        course = client.post("/api/v2/courses", json={"name": "算法"}).json()
        client.put(f"/api/v2/courses/{course['id']}/memory", json={"content": "学到第 7 章"})
        assert "第 7 章" in client.get(f"/api/v2/courses/{course['id']}/memory").json()["content"]
        # 课程记忆互不串台，也不影响跨课程画像
        assert "先给结论" in client.get("/api/v2/memory").json()["content"]
        assert client.get("/api/v2/courses/course_missing/memory").status_code == 404
        # 超长拒绝
        assert client.put("/api/v2/memory", json={"content": "长" * 50_000}).status_code == 422


def test_agent_written_managed_blocks_survive_a_manual_rewrite(tmp_path):
    """用户整份改写后，助手的受管区块仍能被 memory_patch 重新写入。"""
    from modules.memory.store import MemoryStore

    store = MemoryStore(tmp_path)
    store.patch(scope="user", section="preferences", content="喜欢先看例子")
    store.write_whole(scope="user", content="我自己手写的一段，把标记删了")
    assert "手写" in store.read_user() and "喜欢先看例子" not in store.read_user()
    store.patch(scope="user", section="preferences", content="喜欢先看例子")
    body = store.read_user()
    assert "手写" in body and "喜欢先看例子" in body  # 手写段落没被覆盖
