"""
【模块说明】
- 主要作用：批量重建全部课程的 FAISS 索引。
- 核心函数：rebuild_course、main。
- 适用场景：更换嵌入模型或调整分块参数后，需重新向量化全部课程。
- 阅读建议：先看模块说明，再看类/函数头部注释和关键步骤注释。
- 注释策略：每个相对独立代码块都使用“目的 + 实现方式”进行说明。
"""

import os
import sys

# 支持从 scripts/ 目录直接运行，同时显式把仓库根目录加入导入路径。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, ".env"))

# 与 api.py 使用完全相同的函数
from rag.ingest import DocumentParser
from rag.chunk import chunk_documents
from rag.store_faiss import build_index

WORKSPACES_DIR = os.path.join(ROOT, "data", "workspaces")
ALLOWED_EXTS = {".pdf", ".txt", ".md", ".docx", ".pptx", ".ppt"}


def rebuild_course(course_name: str) -> bool:
    workspace = os.path.join(WORKSPACES_DIR, course_name)
    uploads_dir = os.path.join(workspace, "uploads")
    index_path = os.path.join(workspace, "index", "faiss_index")

    if not os.path.isdir(uploads_dir):
        print(f"  [跳过] {course_name}: 无 uploads/ 目录")
        return False

    disk_files = [
        f for f in os.listdir(uploads_dir)
        if os.path.isfile(os.path.join(uploads_dir, f))
        and os.path.splitext(f)[1].lower() in ALLOWED_EXTS
    ]
    if not disk_files:
        print(f"  [跳过] {course_name}: uploads/ 为空")
        return False

    print(f"\n{'='*52}")
    print(f"  课程: {course_name}  ({len(disk_files)} 个文件)")
    print(f"{'='*52}")

    all_pages = []
    failed = []
    for fname in disk_files:
        fpath = os.path.join(uploads_dir, fname)
        try:
            pages = DocumentParser.parse_document(fpath)
            if pages:
                all_pages.extend(pages)
                print(f"    ✓ {fname}: {len(pages)} 页")
            else:
                failed.append(fname)
                print(f"    ⚠ {fname}: 解析结果为空")
        except Exception as e:
            failed.append(fname)
            print(f"    ✗ {fname}: {e}")

    if not all_pages:
        print(f"  [跳过] {course_name}: 所有文件解析失败")
        return False

    # 分块（自动读取 .env 中的 CHUNK_SIZE / CHUNK_OVERLAP）
    chunks = chunk_documents(all_pages)
    print(f"  共 {len(chunks)} 个文本块，开始向量化…")

    # 构建索引（自动读取 EMBEDDING_MODEL，维度自动检测）
    store = build_index(chunks)

    # 保存
    os.makedirs(os.path.dirname(index_path), exist_ok=True)
    store.save(index_path)
    print(f"  ✅ 索引已保存 → {index_path}.*")
    if failed:
        print(f"  ⚠ 以下文件跳过: {', '.join(failed)}")
    return True


def main():
    if not os.path.isdir(WORKSPACES_DIR):
        print("未找到 data/workspaces 目录，无需重建。")
        return

    courses = sorted([
        d for d in os.listdir(WORKSPACES_DIR)
        if os.path.isdir(os.path.join(WORKSPACES_DIR, d))
    ])

    if not courses:
        print("没有任何课程工作空间。")
        return

    print(f"发现 {len(courses)} 个课程: {courses}")
    print(f"参数: CHUNK_SIZE={os.getenv('CHUNK_SIZE')}  "
          f"CHUNK_OVERLAP={os.getenv('CHUNK_OVERLAP')}  "
          f"TOP_K={os.getenv('TOP_K_RESULTS')}")
    print(f"模型: {os.getenv('EMBEDDING_MODEL')}")

    success = 0
    for course in courses:
        if rebuild_course(course):
            success += 1

    print(f"\n完成！成功重建 {success}/{len(courses)} 个课程的索引。")


if __name__ == "__main__":
    main()
