#!/usr/bin/env python3
"""准备端到端浏览器测试的教材 fixture：下载开源教材、切出章节、光栅化一页图片。

只用公开发布的开源教材，不自造内容；每份教材切成十几页的章节，让上传和索引在
测试里几十秒内可完成。源文件缓存在 source/，重复执行不会重新下载。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Source:
    key: str
    url: str
    min_bytes: int
    note: str


@dataclass(frozen=True)
class Slice:
    out: str
    source: str
    first_page: int  # 原书 PDF 页码，1 起，闭区间
    last_page: int
    course: str
    anchors: tuple[str, ...] = field(default=())


SOURCES = (
    Source("fudan-llm-tap", "https://intro-llm.github.io/chapter/LLM-TAP.pdf", 20_000_000,
           "复旦《大规模语言模型：从理论到实践》张奇等，intro-llm.github.io"),
    Source("d2l-zh", "https://zh.d2l.ai/d2l-zh.pdf", 30_000_000,
           "《动手学深度学习》李沐等，zh.d2l.ai"),
    Source("ostep-cpu-sched", "https://pages.cs.wisc.edu/~remzi/OSTEP/cpu-sched.pdf", 100_000,
           "OSTEP 第 7 章 CPU Scheduling，Remzi & Andrea Arpaci-Dusseau"),
    Source("mml-book", "https://mml-book.github.io/book/mml-book.pdf", 17_000_000,
           "Mathematics for Machine Learning，Deisenroth 等，mml-book.github.io"),
)

SLICES = (
    Slice("llm-微调-LoRA.pdf", "fudan-llm-tap", 137, 145, "大语言模型",
          ("QLoRA", "AdaLoRA", "4-bit")),
    Slice("llm-指令数据集.pdf", "fudan-llm-tap", 146, 156, "大语言模型",
          ("Super-NaturalInstructions", "指令集")),
    Slice("深度学习-批量规范化.pdf", "d2l-zh", 294, 303, "深度学习",
          ("批量规范化", "内部协变量偏移", "Ali Rahimi")),
    Slice("os-cpu-scheduling.pdf", "ostep-cpu-sched", 1, 13, "操作系统",
          ("Round Robin", "turnaround", "response time")),
    Slice("math-gaussian.pdf", "mml-book", 200, 212, "机器学习数学",
          ("Gaussian", "Product of Gaussian Densities")),
)

# 大切片：整整一章，几十页、上百条书签，用来压知识页构建的节点上限与树深度。
# 上面那几份只有十来页，任何上限都碰不到，「超出上限会丢内容」这类 bug 在它们身上走不到。
#
# 单独一个元组，不并进 SLICES：eval_dataset.py 与 example_setup.py 会把 SLICES 里
# 同课程的切片全部装进课程，多一份几十页的教材会改掉评测的检索基线，也会拖慢示例数据准备。
BIG_SLICES = (
    # d2l 第 4 章「多层感知机」。选它的理由：自成体系（从模型讲到过拟合、正则、
    # 数值稳定性，最后落到一场 Kaggle 实战），四层书签，同名标题（章与 4.1 同叫
    # 「多层感知机」）也在里面，概念去重那条路顺带走得到。
    Slice("深度学习-多层感知机.pdf", "d2l-zh", 147, 212, "深度学习",
          ("K折交叉验证", "暂退法", "Xavier", "协变量偏移")),
)

# OCR 提问用的图片：从这份切片里取一页光栅化，内容仍是真实教材页。
IMAGE_FROM = ("os-cpu-scheduling.pdf", 7)


def fetch(source: Source, directory: Path) -> Path:
    path = directory / f"{source.key}.pdf"
    if path.is_file() and path.stat().st_size >= source.min_bytes:
        return path
    print(f"下载 {source.key}：{source.url}")
    result = subprocess.run(["curl", "-fL", "--retry", "3", "-C", "-", "-o", str(path), source.url])
    if result.returncode != 0 or path.stat().st_size < source.min_bytes:
        raise SystemExit(f"{source.key} 下载不完整（{path.stat().st_size if path.is_file() else 0} bytes），重跑本脚本会断点续传。")
    return path


def carry_outline(reader: PdfReader, writer: PdfWriter, first: int, last: int) -> int:
    """把原书目录里落在 [first, last] 的书签按原层级搬进切片，页码换算成切片内的序号。

    pypdf 的 outline 是嵌套列表：紧跟某一条之后的子列表就是它的下级。只 add_page 不会
    带走书签，而概念的层级要靠它还原。
    """
    carried = 0

    def walk(node, parent) -> None:
        nonlocal carried
        latest = parent
        for item in node:
            if isinstance(item, list):
                walk(item, latest)
                continue
            try:
                page = reader.get_destination_page_number(item) + 1
            except Exception:  # noqa: BLE001 - 指不到页的书签跳过就行
                continue
            if not first <= page <= last:
                latest = parent  # 范围外的条目不入切片，它的下级挂回上一层
                continue
            latest = writer.add_outline_item(item.title, page - first, parent=parent)
            carried += 1

    walk(reader.outline, None)
    return carried


def cut(source_pdf: Path, spec: Slice, out_dir: Path) -> tuple[Path, list[str]]:
    reader = PdfReader(source_pdf)
    writer = PdfWriter()
    for number in range(spec.first_page, spec.last_page + 1):
        writer.add_page(reader.pages[number - 1])
    carried = carry_outline(reader, writer, spec.first_page, spec.last_page)
    target = out_dir / spec.out
    with target.open("wb") as stream:
        writer.write(stream)
    print(f"  {spec.out}：{spec.last_page - spec.first_page + 1} 页，书签 {carried} 条")
    pages = [(page.extract_text() or "").replace("\n", " ") for page in PdfReader(target).pages]
    return target, pages


def rasterize(pdf: Path, page_number: int, target: Path) -> None:
    """sips 只认单页 PDF，先把目标页单独导出再转 PNG。"""
    writer = PdfWriter()
    writer.add_page(PdfReader(pdf).pages[page_number - 1])
    single = target.with_suffix(".page.pdf")
    with single.open("wb") as stream:
        writer.write(stream)
    subprocess.run(["sips", "-s", "format", "png", str(single), "--out", str(target)],
                   check=True, capture_output=True)
    single.unlink()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "testdata" / "fixtures"), help="fixture 输出目录")
    parser.add_argument("--data-dir", default=str(ROOT / "testdata" / "e2e"), help="e2e 后端数据目录，会被清空")
    parser.add_argument("--only", action="append", metavar="文件名",
                        help="只重切指定的切片，可重复。教材的 sha256 是评测的硬门，别无谓重切。")
    args = parser.parse_args()

    out = Path(args.out)
    source_dir = out / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    wanted = [spec for spec in SLICES + BIG_SLICES if not args.only or spec.out in args.only]
    if args.only and (unknown := set(args.only) - {spec.out for spec in wanted}):
        raise SystemExit(f"没有这几份切片：{sorted(unknown)}")
    downloads = {source.key: fetch(source, source_dir)
                 for source in SOURCES if source.key in {spec.source for spec in wanted}}

    print("\n课程与教材（页码为切片内页码，可直接对照引用）：")
    for spec in wanted:
        target, pages = cut(downloads[spec.source], spec, out)
        joined = "\n".join(pages)
        missing = [anchor for anchor in spec.anchors if anchor not in joined]
        assert not missing, f"{spec.out} 缺少锚点 {missing}：教材改版了，需要重新选页"
        located = {anchor: next((i + 1 for i, text in enumerate(pages) if anchor in text), None) for anchor in spec.anchors}
        print(f"  [{spec.course}] {target.name}  原书 p{spec.first_page}-{spec.last_page} → {len(pages)} 页 · "
              + "，".join(f"{anchor}@p{page}" for anchor, page in located.items()))

    if any(spec.out == IMAGE_FROM[0] for spec in wanted):
        image = out / "教材页-提问.png"
        rasterize(out / IMAGE_FROM[0], IMAGE_FROM[1], image)
        assert image.stat().st_size > 20_000, "图片过小，OCR 可能读不出文字"
        print(f"\nOCR 图片：{image.name}（{IMAGE_FROM[0]} 第 {IMAGE_FROM[1]} 页，{image.stat().st_size // 1024} KB）")

    # 浏览器里没有文件选择器可用，改由页面 fetch 再注入 input；
    # vite.config.ts 的 server.fs.allow 已放开这个目录。
    print(f"\n页面取文件用：/@fs{out}/<文件名>")

    data_dir = Path(args.data_dir)
    shutil.rmtree(data_dir, ignore_errors=True)
    print(f"已清空 e2e 数据目录：{data_dir}")
    print("启动被测实例：STORAGE_DATA_DIR=testdata/e2e ./scripts/dev.sh")
    print("\n教材来源：")
    for source in SOURCES:
        print(f"  {source.key} — {source.note}")


if __name__ == "__main__":
    main()
