"""整份重写的 markdown 崩在半路时，原文件必须还在。

记忆与知识页手写区都是先读后写：崩在 truncate 之后就不只丢这一次改动，是整份没了，
而这两处都没有第二份副本。trace payload 不在这里——它是可清理的旁路记录，读侧已经
把坏 JSON 归成 invalid 并在界面上标出来。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.common import write_text_atomic
from modules.knowledge.wiki import HANDWRITTEN_MARKER, WikiStore
from modules.memory.store import MemoryStore


def crash_midway(monkeypatch) -> None:
    """模拟写到一半进程没了：内容只落一半，然后抛错。"""
    real = Path.write_text

    def half(self: Path, data: str, *args, **kwargs):
        real(self, data[: len(data) // 2], *args, **kwargs)
        raise OSError("模拟写到一半崩了")

    monkeypatch.setattr(Path, "write_text", half)


def tmp_leftovers(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.tmp"))


def test_memory_patch_crash_keeps_the_previous_file(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path)
    store.patch(scope="user", section="profile", content="数学系本科，讲原理直接上公式")
    before = (tmp_path / "user.md").read_text(encoding="utf-8")
    assert "数学系本科" in before

    crash_midway(monkeypatch)
    with pytest.raises(OSError):
        store.patch(scope="user", section="profile", content="换成完全不同的一段内容")

    assert (tmp_path / "user.md").read_text(encoding="utf-8") == before
    assert tmp_leftovers(tmp_path) == []


def test_memory_whole_rewrite_crash_keeps_the_previous_file(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path)
    store.write_whole(scope="user", content="# 用户画像\n\n偏好公式推导。")
    before = (tmp_path / "user.md").read_text(encoding="utf-8")

    crash_midway(monkeypatch)
    with pytest.raises(OSError):
        store.write_whole(scope="user", content="# 用户画像\n\n这一次写到一半就崩了。")

    assert (tmp_path / "user.md").read_text(encoding="utf-8") == before
    assert tmp_leftovers(tmp_path) == []


def test_wiki_rewrite_crash_keeps_the_handwritten_section(tmp_path, monkeypatch):
    """手写区重新构建不会覆盖，所以它和记忆一样没有第二份副本。"""
    store = WikiStore(tmp_path)
    page = store.write(course_id="course_a", concept_id="c1", concept_name="调度",
                       body="第一版正文", source_hash="h1", source_refs=["p.1"],
                       updated_at="2026-08-04T00:00:00Z")
    path = next((tmp_path / "wiki").rglob("*.md"))
    path.write_text(path.read_text(encoding="utf-8") + "我自己补的一句，别弄丢。\n", encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    assert HANDWRITTEN_MARKER in before and page.concept_id == "c1"

    crash_midway(monkeypatch)
    with pytest.raises(OSError):
        store.write(course_id="course_a", concept_id="c1", concept_name="调度",
                    body="第二版正文", source_hash="h2", source_refs=["p.2"],
                    updated_at="2026-08-04T01:00:00Z")

    assert path.read_text(encoding="utf-8") == before
    assert tmp_leftovers(path.parent) == []


def test_listing_ignores_a_leftover_temp_file(tmp_path):
    """改名之外的路径也可能留下 .tmp（比如进程被 kill），它不该被当成一页。"""
    store = WikiStore(tmp_path)
    store.write(course_id="course_a", concept_id="c1", concept_name="调度", body="正文",
                source_hash="h1", source_refs=[], updated_at="2026-08-04T00:00:00Z")
    directory = (tmp_path / "wiki").iterdir().__next__()
    (directory / f"c1.md.{os.getpid()}.tmp").write_text("半份内容", encoding="utf-8")

    assert [item.concept_id for item in store.list_pages(course_id="course_a")] == ["c1"]


def test_replace_failure_leaves_the_original_and_no_temp(tmp_path, monkeypatch):
    """改名这一步失败时也要收干净，不然目录里会攒一堆 .tmp。"""
    target = tmp_path / "user.md"
    target.write_text("原内容\n", encoding="utf-8")
    monkeypatch.setattr("core.common.os.replace", lambda *_: (_ for _ in ()).throw(OSError("改名失败")))

    with pytest.raises(OSError):
        write_text_atomic(target, "新内容\n")

    assert target.read_text(encoding="utf-8") == "原内容\n"
    assert tmp_leftovers(tmp_path) == []


def test_normal_write_replaces_content_and_leaves_no_temp(tmp_path):
    target = tmp_path / "user.md"
    write_text_atomic(target, "第一版\n")
    write_text_atomic(target, "第二版\n")

    assert target.read_text(encoding="utf-8") == "第二版\n"
    assert tmp_leftovers(tmp_path) == []
