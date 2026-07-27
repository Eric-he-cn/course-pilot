"""从上传的教材里取出带页码的文本。

每种格式一个 `_from_*`，统一返回 `[(页码, 文本)]`；页码取不到就是 None，引用只显示文件名。
Office 的新格式（docx / pptx）是 zip 包着 XML，用标准库拆就够，不引第三方依赖。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree

TEXT_SUFFIXES = {".txt", ".md"}
# 老的二进制格式（.doc / .ppt）只有装了转换器才收，见 _convert_legacy
LEGACY_SUFFIXES = {".doc": ".docx", ".ppt": ".pptx"}
SUPPORTED_SUFFIXES = {".pdf", ".txt", ".md", ".docx", ".pptx", *LEGACY_SUFFIXES}

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def extract_pages(path: Path, filename: str) -> list[tuple[int | None, str]]:
    suffix = Path(filename).suffix.lower()
    if suffix in LEGACY_SUFFIXES:
        path, suffix = _convert_legacy(path, suffix), LEGACY_SUFFIXES[suffix]
    if suffix in TEXT_SUFFIXES:
        return [(None, path.read_bytes().decode("utf-8", errors="replace").strip())]
    if suffix == ".docx":
        return _from_docx(path)
    if suffix == ".pptx":
        return _from_pptx(path)
    return _from_pdf(path)


def _convert_legacy(path: Path, suffix: str) -> Path:
    """.doc / .ppt 是 OLE2 二进制，自己解析不现实。有系统转换器就转，没有就说清楚缺什么。"""
    target = Path(tempfile.mkdtemp()) / f"converted{LEGACY_SUFFIXES[suffix]}"
    if shutil.which("soffice"):
        subprocess.run(
            ["soffice", "--headless", "--convert-to", LEGACY_SUFFIXES[suffix].lstrip("."),
             "--outdir", str(target.parent), str(path)],
            check=True, capture_output=True, timeout=300,
        )
        converted = next(target.parent.glob(f"*{LEGACY_SUFFIXES[suffix]}"), None)
        if converted is not None:
            return converted
    if suffix == ".doc" and shutil.which("textutil"):  # macOS 自带
        subprocess.run(["textutil", "-convert", "docx", "-output", str(target), str(path)],
                       check=True, capture_output=True, timeout=300)
        return target
    raise ValueError(f"解析 {suffix} 需要本机装有 LibreOffice（命令 soffice）；也可以把文件另存为 {LEGACY_SUFFIXES[suffix]} 再上传")


def _from_docx(path: Path) -> list[tuple[int | None, str]]:
    """Word 文档的页码来自两个信号，都没有就留空。

    `w:lastRenderedPageBreak` 是规范里专门用来记「上次被会分页的程序保存时页在哪断开」的
    元素（ECMA-376 §17.3.3.13），Word 与 LibreOffice 都会写；`w:br w:type="page"` 是作者
    手动插的分页符。两者都没有时（textutil、python-docx 这类不渲染的产出器）页码留空，
    不去猜——一个对不上的 p.N 比没有页码更糟。
    """
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    pages: dict[int, list[str]] = {}
    page, paginated = 1, False

    def add(text: str) -> None:
        if text.strip():
            pages.setdefault(page, []).append(text.strip())

    for node in root.iter():
        if node.tag == f"{_W}p":
            buffer = ""
            for item in node.iter():
                if item.tag == f"{_W}t":
                    buffer += item.text or ""
                elif item.tag == f"{_W}lastRenderedPageBreak" or (
                    item.tag == f"{_W}br" and item.get(f"{_W}type") == "page"
                ):
                    # 分页可能落在段落中间，所以要就地切开而不是整段归给起始页
                    add(buffer)
                    buffer = ""
                    page += 1
                    paginated = True
            add(buffer)
        elif node.tag == f"{_W}tbl":
            add(_markdown_table([
                [" ".join(cell.itertext()).strip() for cell in row.findall(f"{_W}tc")]
                for row in node.findall(f"{_W}tr")
            ]))
    # 表格里的段落会被上面的 w:p 分支再收一次，重复无害：检索多命中一次，不丢内容
    if not paginated:
        return [(None, "\n\n".join(block for blocks in pages.values() for block in blocks).strip())]
    return [(number, "\n\n".join(blocks).strip()) for number, blocks in sorted(pages.items())]


def _from_pptx(path: Path) -> list[tuple[int | None, str]]:
    """幻灯片编号就是可靠的页码，讲义类教材靠它定位。备注页一并收进同一页。"""
    with zipfile.ZipFile(path) as archive:
        slides = sorted(
            (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
            key=lambda name: int(re.search(r"\d+", Path(name).stem).group()),
        )
        pages: list[tuple[int | None, str]] = []
        for number, name in enumerate(slides, start=1):
            parts = [ElementTree.fromstring(archive.read(name))]
            notes = f"ppt/notesSlides/notesSlide{number}.xml"
            if notes in archive.namelist():
                parts.append(ElementTree.fromstring(archive.read(notes)))
            lines = [text.strip() for part in parts for node in part.iter(f"{_A}t") if (text := node.text or "")]
            pages.append((number, "\n".join(line for line in lines if line)))
    return pages


def _from_pdf(path: Path) -> list[tuple[int | None, str]]:
    """三级：pdfium → pypdf → 无依赖兜底。都拿不到文字的走 OCR 通道，见 scanned.py。"""
    raw = path.read_bytes()
    # 明显截断的数据不必交给解析库：只会刷一堆告警，结果不会比兜底更好。
    if b"%%EOF" in raw:
        for reader in (_pdfium_pages, _pypdf_pages):
            try:
                pages = reader(path)
            except Exception:
                continue
            if any(text for _page, text in pages):
                return pages
    # 不依赖任何库的兜底：认最常见的字面量文本算子。
    fragments = re.findall(rb"\(([^()]*)\)\s*(?:Tj|TJ)", raw)
    return [(None, "\n".join(fragment.decode("latin-1", errors="replace") for fragment in fragments).strip())]


def _pdfium_pages(path: Path) -> list[tuple[int | None, str]]:
    """首选 pdfium：中文字体解码更稳，而且会在中英与数字之间补空格。

    实测 fudan-llm-tap.pdf 前 12 页——pypdf 抽出 14082 字、55 个私用区乱码字符，
    pdfium 抽出 25706 字、0 个；正文里 pypdf 的「根据2016 年Google」在 pdfium 是
    「根据 2016 年 Google」。粘在一起的词 FTS 分不开，也就检索不到。速度还快 2-4 倍。
    """
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    try:
        return [(number, document[number - 1].get_textpage().get_text_range().strip())
                for number in range(1, len(document) + 1)]
    finally:
        document.close()


def pdf_outline(path: Path) -> list[tuple[int, str, int | None]]:
    """读 PDF 自带的目录书签，返回 (层级, 标题, 页码)。没有书签就返回空。

    教材的目录是作者写的，比从正文刮标题准得多：不会把代码注释、表格行当成标题，
    页码指向正文而不是目录页。实测 fudan 125 条、d2l-zh 832 条，读一次 0.05 秒。
    """
    import pypdfium2 as pdfium

    try:
        document = pdfium.PdfDocument(str(path))
    except Exception:
        return []
    try:
        rows = []
        for bookmark in document.get_toc():
            destination = bookmark.get_dest()
            page = destination.get_index() if destination is not None else None
            rows.append((bookmark.level, bookmark.get_title() or "", (page + 1) if page is not None else None))
        return rows
    except Exception:
        return []
    finally:
        document.close()


def _pypdf_pages(path: Path) -> list[tuple[int | None, str]]:
    from pypdf import PdfReader

    return [(number, (page.extract_text() or "").strip())
            for number, page in enumerate(PdfReader(str(path)).pages, start=1)]


def _markdown_table(rows: list[list[str]]) -> str:
    """表格转成 markdown：行列关系留在文本里，检索到一行还能看出它属于哪一列。"""
    rows = [row for row in rows if any(cell for cell in row)]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    body = ["| " + " | ".join((row + [""] * width)[:width]) + " |" for row in rows]
    return "\n".join([body[0], "| " + " | ".join(["---"] * width) + " |", *body[1:]])
