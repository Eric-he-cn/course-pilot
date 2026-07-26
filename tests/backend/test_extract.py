"""教材解析：docx / pptx 走标准库拆 OOXML，页码口径各格式不同。"""
from __future__ import annotations

import shutil
import subprocess
import zipfile

import pytest

from modules.knowledge.extract import SUPPORTED_SUFFIXES, extract_pages

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_A = ('xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
      'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"')


def _docx(tmp_path, body: str):
    path = tmp_path / "material.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {_W}><w:body>{body}</w:body></w:document>')
    return path


def _paragraph(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _pptx(tmp_path, slides: list[str], notes: dict[int, str] | None = None):
    path = tmp_path / "material.pptx"
    with zipfile.ZipFile(path, "w") as archive:
        for number, text in enumerate(slides, start=1):
            archive.writestr(
                f"ppt/slides/slide{number}.xml",
                f'<?xml version="1.0"?><p:sld {_A}><a:t>{text}</a:t></p:sld>',
            )
        for number, text in (notes or {}).items():
            archive.writestr(
                f"ppt/notesSlides/notesSlide{number}.xml",
                f'<?xml version="1.0"?><p:notes {_A}><a:t>{text}</a:t></p:notes>',
            )
    return path


def test_docx_paragraphs_carry_no_page_number(tmp_path):
    """Word 不存真实分页，页码留空——错的 p.N 比没有页码更糟。"""
    path = _docx(tmp_path, _paragraph("CPU 调度概览") + _paragraph("时间片过长退化成 FCFS。"))
    assert extract_pages(path, "material.docx") == [(None, "CPU 调度概览\n\n时间片过长退化成 FCFS。")]


def test_docx_tables_keep_their_rows_and_columns(tmp_path):
    """表格转 markdown：检索命中一行时还能看出它属于哪一列。"""
    cells = "".join(f"<w:tc><w:p><w:r><w:t>{value}</w:t></w:r></w:p></w:tc>" for value in ("RR", "10ms"))
    header = "".join(f"<w:tc><w:p><w:r><w:t>{value}</w:t></w:r></w:p></w:tc>" for value in ("算法", "时间片"))
    path = _docx(tmp_path, f"<w:tbl><w:tr>{header}</w:tr><w:tr>{cells}</w:tr></w:tbl>")
    text = extract_pages(path, "material.docx")[0][1]
    assert "| 算法 | 时间片 |" in text
    assert "| RR | 10ms |" in text


def test_pptx_uses_slide_numbers_as_pages(tmp_path):
    path = _pptx(tmp_path, ["第一页：调度目标", "第二页：Round Robin"])
    assert extract_pages(path, "material.pptx") == [(1, "第一页：调度目标"), (2, "第二页：Round Robin")]


def test_pptx_slides_are_ordered_numerically_not_lexically(tmp_path):
    """slide10 排在 slide2 后面：按字符串排会把页码和内容错配。"""
    path = _pptx(tmp_path, [f"第 {number} 页" for number in range(1, 12)])
    pages = extract_pages(path, "material.pptx")
    assert [page for page, _ in pages] == list(range(1, 12))
    assert pages[9] == (10, "第 10 页")


def test_pptx_notes_join_their_slide(tmp_path):
    path = _pptx(tmp_path, ["标题页"], notes={1: "讲稿：先讲吞吐再讲公平"})
    assert extract_pages(path, "material.pptx") == [(1, "标题页\n讲稿：先讲吞吐再讲公平")]


def test_legacy_formats_are_declared_supported():
    assert {".doc", ".ppt", ".docx", ".pptx", ".pdf", ".txt", ".md"} == SUPPORTED_SUFFIXES


def test_legacy_doc_explains_what_is_missing_when_no_converter(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    path = tmp_path / "material.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0legacy ole2")
    with pytest.raises(ValueError, match="LibreOffice"):
        extract_pages(path, "material.doc")


@pytest.mark.skipif(shutil.which("textutil") is None, reason="需要 macOS 自带的 textutil")
def test_real_word_xml_from_a_converter_parses(tmp_path):
    """手写的 XML 只能验证自己的假设，这条拿真实转换器产出的 docx 走一遍。"""
    source = tmp_path / "src.txt"
    source.write_text("CPU 调度概览\n\n时间片过长退化成 FCFS。\n", encoding="utf-8")
    target = tmp_path / "real.docx"
    subprocess.run(["textutil", "-convert", "docx", "-output", str(target), str(source)], check=True, capture_output=True)
    text = extract_pages(target, "real.docx")[0][1]
    assert "CPU 调度概览" in text and "FCFS" in text
