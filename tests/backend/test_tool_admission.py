from __future__ import annotations

from pathlib import Path

import pytest

from adapters.web import HttpWebAccess
from contracts.web import WebAccessError
from modules.agent.calculator import CalculationError, evaluate
from modules.agent.tools import (
    MAIN,
    PRACTICE_CAPABILITIES,
    TOOL_CAPABILITY,
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

    practice_names = {spec.name for spec in specs_for(MAIN.tools, capabilities=PRACTICE_CAPABILITIES)}
    # 练习态不出网也不写笔记：让用户做题时联网等于让他查答案。
    assert "web_search" not in practice_names and "web_fetch" not in practice_names
    assert "note_write" not in practice_names
    assert "search_materials" in practice_names and "emit_evidence" in practice_names


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
