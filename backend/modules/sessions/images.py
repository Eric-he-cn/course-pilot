from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

# 主流 OCR 服务普遍支持的常见格式；其余 MIME 直接拒绝。
_ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}
# 允许解码的像素上限相对 max_pixels 的倍数：再大就不值得为了缩放先解进内存。
_DECODE_HEADROOM = 8
_FORMAT_BY_MIME = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}


@dataclass(frozen=True)
class ProcessedImage:
    content: bytes
    mime_type: str
    width: int
    height: int


def process_image(*, content: bytes, mime_type: str, max_bytes: int, max_pixels: int) -> ProcessedImage:
    """校验附件图片并重编码：超像素先等比缩放，重编码同时去除 EXIF 等元数据。"""
    if mime_type not in _ALLOWED_MIME:
        raise ValueError("只支持 JPEG、PNG、WebP 图片")
    if len(content) > max_bytes:
        raise ValueError(f"图片超过大小上限 {max_bytes // (1024 * 1024)} MiB")
    try:
        image = Image.open(io.BytesIO(content))
        # 先按 header 尺寸判：几百 KB 的图也能解压成几亿像素，load 之后再判就已经进内存了。
        if image.width * image.height > max_pixels * _DECODE_HEADROOM:
            raise ValueError(f"图片分辨率过高（{image.width}×{image.height}），请压缩后再上传")
        image.load()
    except Image.DecompressionBombError as error:
        # PIL 在 open 阶段就拦下的超大图，文案要说清是尺寸问题而不是格式问题。
        raise ValueError("图片分辨率过高，请压缩后再上传") from error
    except UnidentifiedImageError as error:
        raise ValueError("无法识别的图片内容") from error
    if image.width * image.height > max_pixels:
        scale = (max_pixels / (image.width * image.height)) ** 0.5
        image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))))
    output_format = _FORMAT_BY_MIME[mime_type]
    if output_format == "JPEG" and image.mode not in {"RGB", "L"}:
        image = image.convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format=output_format)
    return ProcessedImage(buffer.getvalue(), mime_type, image.width, image.height)
