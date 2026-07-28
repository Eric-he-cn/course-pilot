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


_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_NOTES_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide"


def _pptx(tmp_path, slides: list[str], notes: dict[int, str] | None = None, numbers: list[int] | None = None):
    """numbers 指定 slide 的文件编号，默认 1..N；给断号就能模拟删过幻灯片的 pptx。
    备注页按真实结构挂在 slide 的 _rels 上，PowerPoint 就是这么存的。"""
    path = tmp_path / "material.pptx"
    numbers = numbers or list(range(1, len(slides) + 1))
    with zipfile.ZipFile(path, "w") as archive:
        for number, text in zip(numbers, slides):
            archive.writestr(
                f"ppt/slides/slide{number}.xml",
                f'<?xml version="1.0"?><p:sld {_A}><a:t>{text}</a:t></p:sld>',
            )
            if number in (notes or {}):
                archive.writestr(
                    f"ppt/slides/_rels/slide{number}.xml.rels",
                    f'<?xml version="1.0"?><Relationships xmlns="{_RELS_NS}">'
                    f'<Relationship Id="rId1" Type="{_NOTES_REL}" '
                    f'Target="../notesSlides/notesSlide{number}.xml"/></Relationships>',
                )
        for number, text in (notes or {}).items():
            archive.writestr(
                f"ppt/notesSlides/notesSlide{number}.xml",
                f'<?xml version="1.0"?><p:notes {_A}><a:t>{text}</a:t></p:notes>',
            )
    return path


_RENDERED_BREAK = "<w:r><w:lastRenderedPageBreak/></w:r>"
_MANUAL_BREAK = '<w:r><w:br w:type="page"/></w:r>'


def test_docx_without_pagination_info_leaves_the_page_empty(tmp_path):
    """两个分页信号都没有时（不渲染的产出器）页码留空——错的 p.N 比没有页码更糟。"""
    path = _docx(tmp_path, _paragraph("CPU 调度概览") + _paragraph("时间片过长退化成 FCFS。"))
    assert extract_pages(path, "material.docx") == [(None, "CPU 调度概览\n\n时间片过长退化成 FCFS。")]


def test_docx_uses_the_last_rendered_pagination(tmp_path):
    """ECMA-376 §17.3.3.13：这个元素记的就是上次分页保存时页在哪断开。Word 与 LibreOffice 都写。"""
    body = _paragraph("第一页") + "<w:p>" + _RENDERED_BREAK + "<w:r><w:t>第二页</w:t></w:r></w:p>"
    assert extract_pages(_docx(tmp_path, body), "material.docx") == [(1, "第一页"), (2, "第二页")]


def test_docx_counts_author_inserted_page_breaks(tmp_path):
    body = _paragraph("前言") + "<w:p>" + _MANUAL_BREAK + "<w:r><w:t>正文</w:t></w:r></w:p>"
    assert extract_pages(_docx(tmp_path, body), "material.docx") == [(1, "前言"), (2, "正文")]


def test_docx_splits_a_paragraph_that_straddles_a_page_break(tmp_path):
    """分页可以落在段落中间，那半句该归下一页，不能整段算起始页。"""
    body = "<w:p><w:r><w:t>上页结尾</w:t></w:r>" + _RENDERED_BREAK + "<w:r><w:t>下页开头</w:t></w:r></w:p>"
    assert extract_pages(_docx(tmp_path, body), "material.docx") == [(1, "上页结尾"), (2, "下页开头")]


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


def test_pdf_extraction_falls_through_when_a_reader_fails(tmp_path, monkeypatch):
    """pdfium 首选、pypdf 兜底。任一层抛异常都不该让整份教材提不出文字。"""
    from modules.knowledge import extract as module

    path = tmp_path / "book.pdf"
    path.write_bytes(b"%PDF-1.4\n(fallback text) Tj\n%%EOF\n")

    monkeypatch.setattr(module, "_pdfium_pages", lambda _p: (_ for _ in ()).throw(RuntimeError("pdfium 挂了")))
    monkeypatch.setattr(module, "_pypdf_pages", lambda _p: [(1, "pypdf 兜底成功")])
    assert extract_pages(path, "book.pdf") == [(1, "pypdf 兜底成功")]

    # 两层都失败时走无依赖兜底，而不是抛异常
    monkeypatch.setattr(module, "_pypdf_pages", lambda _p: (_ for _ in ()).throw(RuntimeError("pypdf 也挂了")))
    assert "fallback text" in extract_pages(path, "book.pdf")[0][1]


def test_a_reader_returning_only_blank_pages_is_not_accepted(tmp_path, monkeypatch):
    """图片版 PDF 会让上层解析器返回一堆空串。这时要继续往下试，不能当成解析成功。"""
    from modules.knowledge import extract as module

    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    monkeypatch.setattr(module, "_pdfium_pages", lambda _p: [(1, ""), (2, "")])
    monkeypatch.setattr(module, "_pypdf_pages", lambda _p: [(1, "pypdf 找到了字")])
    assert extract_pages(path, "scan.pdf") == [(1, "pypdf 找到了字")]


@pytest.mark.skipif(shutil.which("textutil") is None, reason="需要 macOS 自带的 textutil")
def test_real_word_xml_from_a_converter_parses(tmp_path):
    """手写的 XML 只能验证自己的假设，这条拿真实转换器产出的 docx 走一遍。"""
    source = tmp_path / "src.txt"
    source.write_text("CPU 调度概览\n\n时间片过长退化成 FCFS。\n", encoding="utf-8")
    target = tmp_path / "real.docx"
    subprocess.run(["textutil", "-convert", "docx", "-output", str(target), str(source)], check=True, capture_output=True)
    text = extract_pages(target, "real.docx")[0][1]
    assert "CPU 调度概览" in text and "FCFS" in text


def test_legacy_conversion_leaves_nothing_in_tmp(monkeypatch):
    """.doc / .ppt 的转换产物是用户教材的完整副本，读完必须删——
    留在 /tmp 里等于把别人的教材摊在共享目录下，而且永不回收。

    盯住转换用的那个目录本身，不比对整个 gettempdir()：后者会被同机其他进程干扰，
    报错还会指向无关文件。"""
    import tempfile
    from pathlib import Path

    from modules.knowledge import extract as module

    if not (shutil.which("soffice") or shutil.which("textutil")):
        pytest.skip("本机没有 .doc 转换器")
    seen: list[Path] = []
    original = module._convert_legacy
    monkeypatch.setattr(module, "_convert_legacy",
                        lambda path, suffix, workdir: (seen.append(workdir), original(path, suffix, workdir))[1])
    with tempfile.TemporaryDirectory() as holder:
        source = Path(holder) / "讲义.doc"
        source.write_text("第一节 导数的定义。", encoding="utf-8")
        pages = extract_pages(source, "讲义.doc")
    assert pages and pages[0][1], f"转换后没取到文本：{pages}"
    assert seen, "没走到 legacy 转换分支，测试本身有问题"
    assert not seen[0].exists(), f"转换产物残留：{seen[0]}"


def test_pptx_notes_follow_rels_not_file_numbering(tmp_path):
    """删过幻灯片的 pptx 文件名会断号（slide1/slide2/slide4）。备注按 enumerate 序号
    拼 notesSlideN.xml 的话，第三页会去取属于别人的备注，或者干脆丢掉自己的。"""
    path = _pptx(tmp_path, ["第一页", "第二页", "第三页"], numbers=[1, 2, 4],
                 notes={1: "讲稿一", 4: "讲稿三"})
    assert extract_pages(path, "material.pptx") == [
        (1, "第一页\n讲稿一"), (2, "第二页"), (3, "第三页\n讲稿三"),
    ]


def test_pptx_notes_found_when_rels_use_an_absolute_part_name(tmp_path):
    """OPC 的 Target 可以是绝对 part name（带前导斜杠），部分生成器就这么写。
    不剥掉斜杠就永远匹配不上 zip 成员名，讲稿整段丢失且不报错。"""
    path = tmp_path / "material.pptx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ppt/slides/slide1.xml", f'<?xml version="1.0"?><p:sld {_A}><a:t>正文页</a:t></p:sld>')
        archive.writestr(
            "ppt/slides/_rels/slide1.xml.rels",
            f'<?xml version="1.0"?><Relationships xmlns="{_RELS_NS}">'
            f'<Relationship Id="rId1" Type="{_NOTES_REL}" Target="/ppt/notesSlides/notesSlide1.xml"/></Relationships>',
        )
        archive.writestr("ppt/notesSlides/notesSlide1.xml", f'<?xml version="1.0"?><p:notes {_A}><a:t>讲稿内容</a:t></p:notes>')
    assert extract_pages(path, "material.pptx") == [(1, "正文页\n讲稿内容")]
