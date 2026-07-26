"""扫描版（图片版）PDF：识别、估算、逐页 OCR。

图片版 PDF 没有文字层，普通提取只能拿到空字符串。走 OCR 要花真金白银，所以流程是
「识别 → 拿两页真的 OCR 一遍量出成本 → 用户确认 → 全量跑」。

估算刻意不内置任何价格表：不同模型、不同渠道的计价差很多，硬编码的表迟早失准。
按真实样本外推，换模型也不用改这里。
"""
from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

# 每页文字少于这个数就当没有文字层。扫描件偶尔带页眉页码的少量可选文字，
# 一个纯粹的 ">0 就算有文字" 判据会把它们误判成文字版。
TEXT_LAYER_MIN_CHARS = 60
# 渲染倍率。实测 1.5 与 2.0 的 OCR 文字完全一致，而图片 token 少 44%——没有理由渲更大。
RENDER_SCALE = 1.5
# 估算取样页数：太少不稳，太多本身就是花钱
SAMPLE_PAGES = 2
OCR_WORKERS = 4


@dataclass(frozen=True)
class TextLayer:
    pages: int
    median_chars: int

    @property
    def is_scanned(self) -> bool:
        return self.pages > 0 and self.median_chars < TEXT_LAYER_MIN_CHARS


@dataclass(frozen=True)
class OcrEstimate:
    pages: int
    sampled_pages: int
    prompt_tokens: int          # 取样实测
    completion_tokens: int
    seconds: float
    projected_prompt_tokens: int
    projected_completion_tokens: int
    projected_seconds: float

    def as_dict(self) -> dict[str, object]:
        return {
            "pages": self.pages, "sampled_pages": self.sampled_pages,
            "sample_prompt_tokens": self.prompt_tokens, "sample_completion_tokens": self.completion_tokens,
            "sample_seconds": round(self.seconds, 1),
            "projected_prompt_tokens": self.projected_prompt_tokens,
            "projected_completion_tokens": self.projected_completion_tokens,
            "projected_total_tokens": self.projected_prompt_tokens + self.projected_completion_tokens,
            "projected_minutes": round(self.projected_seconds / 60, 1),
        }


def page_count(path: Path) -> int:
    import pypdfium2 as pdfium

    try:
        document = pdfium.PdfDocument(str(path))
    except Exception:
        return 0
    try:
        return len(document)
    finally:
        document.close()


def probe_text_layer(path: Path) -> TextLayer:
    """只看有没有文字层，不做提取。中位数比均值稳：扫描件里夹几页文字版不该翻案。

    页数用 pdfium 数（它比 pypdf 宽容），文字层用 pypdf 看。pypdf 整个读不出来时按
    「没有文字层」算——那种情况下 OCR 正好是唯一的出路。
    """
    pages = page_count(path)
    if pages == 0:
        return TextLayer(pages=0, median_chars=0)
    try:
        from pypdf import PdfReader

        counts = sorted(len((page.extract_text() or "").strip()) for page in PdfReader(str(path)).pages)
    except Exception:
        return TextLayer(pages=pages, median_chars=0)
    if not counts:
        return TextLayer(pages=pages, median_chars=0)
    return TextLayer(pages=pages, median_chars=counts[len(counts) // 2])


def render_page(path: Path, index: int, *, scale: float = RENDER_SCALE) -> bytes:
    """渲成 JPEG。PNG 对扫描件大一倍多，而 OCR 认字看不出差别。"""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    try:
        image = document[index].render(scale=scale).to_pil()
    finally:
        document.close()
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "JPEG", quality=85)
    return buffer.getvalue()


def estimate(path: Path, transcribe: Callable[[bytes], dict[str, int]]) -> OcrEstimate:
    """真 OCR 前几页，再按页数线性外推。取样本身的产出丢掉——它只用来量成本。"""
    total = page_count(path)
    sampled = min(SAMPLE_PAGES, total)
    if sampled == 0:
        raise ValueError("这份 PDF 没有可渲染的页面")
    import time

    prompt, completion, started = 0, 0, time.monotonic()
    for index in range(sampled):
        usage = transcribe(render_page(path, index))
        prompt += usage.get("prompt_tokens", 0)
        completion += usage.get("completion_tokens", 0)
    seconds = time.monotonic() - started
    ratio = total / sampled
    return OcrEstimate(
        pages=total, sampled_pages=sampled, prompt_tokens=prompt, completion_tokens=completion, seconds=seconds,
        projected_prompt_tokens=int(prompt * ratio), projected_completion_tokens=int(completion * ratio),
        # 全量跑是并发的，按线性外推再按并发度折算
        projected_seconds=seconds * ratio / OCR_WORKERS,
    )


def transcribe_pages(
    path: Path, transcribe: Callable[[bytes], str], *, on_progress: Callable[[int, int], None] | None = None,
) -> Iterator[tuple[int, str]]:
    """并发逐页 OCR，按页号顺序交出结果。一页失败不放弃整本，那页留空。"""
    total = page_count(path)

    def one(index: int) -> tuple[int, str]:
        try:
            return index + 1, transcribe(render_page(path, index))
        except Exception:
            return index + 1, ""

    done = 0
    with ThreadPoolExecutor(max_workers=OCR_WORKERS, thread_name_prefix="coursepilot-ocr") as pool:
        for page, text in pool.map(one, range(total)):
            done += 1
            if on_progress is not None:
                on_progress(done, total)
            yield page, text
