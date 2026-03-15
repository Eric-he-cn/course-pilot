"""
【模块说明】
- 主要作用：把解析后的文档文本切分为可检索的小块，并保留元数据。
- 核心函数：simple_chunk_text、chunk_documents。
- 设计要点：支持 overlap，并防止 overlap 配置异常导致死循环。
- 阅读建议：先看模块说明，再看类/函数头部注释和关键步骤注释。
- 注释策略：每个相对独立代码块都使用“目的 + 实现方式”进行说明。
"""
import os
import re
from typing import List, Dict, Any


def simple_chunk_text(
    text: str,
    chunk_size: int = 512,
    overlap: int = 50
) -> List[str]:
    """按字符长度进行基础分块并支持重叠。"""
    # 防止 overlap >= chunk_size 导致 start 永不前进而死循环
    if chunk_size <= 0:
        chunk_size = 512
    if overlap >= chunk_size:
        overlap = chunk_size // 2

    chunks = []
    start = 0
    text_len = len(text)
    
    while start < text_len:
        end = start + chunk_size
        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk)
        next_start = end - overlap
        if next_start <= start:          # 额外安全兜底
            next_start = start + chunk_size
        start = next_start
        
    return chunks


def chunk_documents(
    pages: List[Dict[str, Any]],
    chunk_size: int = None,
    overlap: int = None
) -> List[Dict[str, Any]]:
    """对文档页进行分块，同时保留 doc_id/page/chunk_id 元数据。"""
    if chunk_size is None:
        chunk_size = int(os.getenv("CHUNK_SIZE", "512"))
    if overlap is None:
        overlap = int(os.getenv("CHUNK_OVERLAP", "50"))
    
    strategy = os.getenv("CHUNK_STRATEGY", "chapter_hybrid").strip().lower()
    if strategy not in {"fixed", "chapter_hybrid"}:
        strategy = "chapter_hybrid"

    all_chunks = []

    def _fixed_chunking() -> List[Dict[str, Any]]:
        out = []
        for page in pages:
            text = page["text"]
            page_num = page.get("page")
            doc_id = page["doc_id"]
            chunks = simple_chunk_text(text, chunk_size, overlap)
            for i, chunk_text in enumerate(chunks):
                out.append({
                    "text": chunk_text,
                    "doc_id": doc_id,
                    "page": page_num,
                    "chunk_id": f"{doc_id}_p{page_num}_c{i}" if page_num else f"{doc_id}_c{i}",
                    "chapter": None,
                    "section": None,
                })
        return out

    if strategy == "fixed":
        return _fixed_chunking()

    try:
        # chapter_hybrid：按章节/标题优先切分，失败时自动回退 fixed。
        for page in pages:
            text = page["text"]
            page_num = page.get("page")
            doc_id = page["doc_id"]
            lines = text.splitlines()
            current_chapter = "未知章节"
            current_section = "正文"
            section_buf: List[str] = []
            sections: List[Dict[str, str]] = []

            def flush_section() -> None:
                body = "\n".join(section_buf).strip()
                if body:
                    sections.append(
                        {
                            "chapter": current_chapter,
                            "section": current_section,
                            "text": body,
                        }
                    )

            for line in lines:
                s = line.strip()
                if not s:
                    section_buf.append(line)
                    continue
                chapter_hit = re.match(r"^(第[一二三四五六七八九十百零\d]+章)\s*(.*)$", s)
                chapter_en_hit = re.match(r"^(chapter\s+\d+)\b[:：\s-]*(.*)$", s, re.IGNORECASE)
                md_head_hit = re.match(r"^(#{1,6})\s+(.+)$", s)

                if chapter_hit:
                    flush_section()
                    section_buf = []
                    current_chapter = chapter_hit.group(1)
                    current_section = chapter_hit.group(2).strip() or "正文"
                    continue
                if chapter_en_hit:
                    flush_section()
                    section_buf = []
                    current_chapter = chapter_en_hit.group(1).strip()
                    current_section = chapter_en_hit.group(2).strip() or "正文"
                    continue
                if md_head_hit:
                    flush_section()
                    section_buf = []
                    level = len(md_head_hit.group(1))
                    title = md_head_hit.group(2).strip()
                    if level <= 2:
                        current_chapter = title
                        current_section = "正文"
                    else:
                        current_section = title
                    continue
                section_buf.append(line)

            flush_section()
            if not sections:
                sections = [{"chapter": None, "section": None, "text": text}]

            chunk_idx = 0
            for sec in sections:
                chunks = simple_chunk_text(sec["text"], chunk_size, overlap)
                for chunk_text in chunks:
                    all_chunks.append({
                        "text": chunk_text,
                        "doc_id": doc_id,
                        "page": page_num,
                        "chunk_id": f"{doc_id}_p{page_num}_c{chunk_idx}" if page_num else f"{doc_id}_c{chunk_idx}",
                        "chapter": sec.get("chapter"),
                        "section": sec.get("section"),
                    })
                    chunk_idx += 1

        if not all_chunks:
            return _fixed_chunking()
        return all_chunks
    except Exception:
        # 章节识别失败时自动回退 fixed。
        return _fixed_chunking()
