"""
【模块说明】
- 主要作用：提供后端 HTTP/SSE 接口，承载课程管理、文件上传、索引构建与对话服务。
- 核心对象：FastAPI app、OrchestrationRunner、workspace 注册表。
- 核心接口：/workspaces、/upload、/build-index、/chat、/chat/stream。
- 阅读建议：先看模块说明，再看类/函数头部注释和关键步骤注释。
- 注释策略：每个相对独立代码块都使用“目的 + 实现方式”进行说明。
"""
import os
import logging
import shutil
import uuid
import contextvars
from queue import Empty, Queue
from threading import Thread
from typing import List, Optional
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime
from time import perf_counter
from core.metrics import add_event, trace_scope

"""basicConfig: 配置全局日志记录，设置日志级别、格式和时间格式,方便后续在代码中使用 logging 模块记录日志。"""
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("backend.api")

from backend.schemas import (
    CourseWorkspace, ChatRequest, ChatResponse, ChatMessage
)
from rag.ingest import DocumentParser
from rag.chunk import chunk_documents
from rag.store_faiss import build_index
from core.orchestration.runner import OrchestrationRunner

app = FastAPI(title="Course Learning Agent API")

"""CORS 中间件配置,允许所有来源的跨域请求，适用于开发阶段。生产环境建议根据实际情况限制 allow_origins。"""
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 全局编排器实例
runner = OrchestrationRunner()

# 内存工作区注册表（生产环境建议换数据库）
workspaces = {}

"""启动时从磁盘扫描已有 workspace 目录恢复数据.
    - 目的：在 API 启动时自动加载之前创建的课程工作区，确保重启后数据不丢失。
    - 实现方式：扫描 data/ 目录下的子目录，每个子目录代表一个课程工作区，读取其 uploads/ 目录中的文件列表，
        并构建 CourseWorkspace 对象注册到内存中。同时确保所有必要的子目录存在。"""
def load_workspaces_from_disk():
    data_dir = os.path.abspath(runner.data_dir)
    if not os.path.exists(data_dir):
        return
    for course_name in os.listdir(data_dir):
        course_path = os.path.join(data_dir, course_name)
        if not os.path.isdir(course_path):
            continue
        uploads_dir = os.path.join(course_path, "uploads")
        documents = []
        if os.path.exists(uploads_dir):
            documents = [f for f in os.listdir(uploads_dir)
                         if os.path.isfile(os.path.join(uploads_dir, f))]
        workspaces[course_name] = CourseWorkspace(
            course_name=course_name,
            subject="",
            created_at=datetime.now(),
            documents=documents,
            index_path=os.path.join(course_path, "index", "faiss_index"),
            notes_path=os.path.join(course_path, "notes"),
            mistakes_path=os.path.join(course_path, "mistakes"),
            exams_path=os.path.join(course_path, "exams"),
        )
        # 确保所有子目录存在
        for subdir in ["uploads", "index", "notes", "mistakes", "exams", "practices"]:
            os.makedirs(os.path.join(course_path, subdir), exist_ok=True)


# 启动时恢复
load_workspaces_from_disk()


class CreateWorkspaceRequest(BaseModel):
    """创建课程工作区的请求体。"""
    course_name: str
    subject: str


class MessageRequest(BaseModel):
    """通用消息请求体（当前保留扩展位）。"""
    message: str


@app.get("/")
async def root():
    """健康检查入口。"""
    return {
        "message": "Course Learning Agent API",
        "version": "0.1.0"
    }


@app.post("/workspaces", response_model=CourseWorkspace)
async def create_workspace(request: CreateWorkspaceRequest):
    """创建课程工作区。"""
    if request.course_name in workspaces:
        raise HTTPException(status_code=400, detail="Workspace already exists")
    
    workspace_path = runner.get_workspace_path(request.course_name)
    os.makedirs(workspace_path, exist_ok=True)
    os.makedirs(os.path.join(workspace_path, "uploads"), exist_ok=True)
    os.makedirs(os.path.join(workspace_path, "index"), exist_ok=True)
    os.makedirs(os.path.join(workspace_path, "notes"), exist_ok=True)
    os.makedirs(os.path.join(workspace_path, "mistakes"), exist_ok=True)
    os.makedirs(os.path.join(workspace_path, "exams"), exist_ok=True)
    os.makedirs(os.path.join(workspace_path, "practices"), exist_ok=True)
    
    workspace = CourseWorkspace(
        course_name=request.course_name,
        subject=request.subject,
        created_at=datetime.now(),
        index_path=os.path.join(workspace_path, "index", "faiss_index"),
        notes_path=os.path.join(workspace_path, "notes"),
        mistakes_path=os.path.join(workspace_path, "mistakes"),
        exams_path=os.path.join(workspace_path, "exams")
    )
    
    workspaces[request.course_name] = workspace
    return workspace


@app.get("/workspaces", response_model=List[CourseWorkspace])
async def list_workspaces():
    """列出全部课程工作区。"""
    return list(workspaces.values())


@app.get("/workspaces/{course_name}", response_model=CourseWorkspace)
async def get_workspace(course_name: str):
    """获取单个课程工作区。"""
    if course_name not in workspaces:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspaces[course_name]


@app.post("/workspaces/{course_name}/upload")
async def upload_document(
    course_name: str,
    file: UploadFile = File(...)
):
    """上传单个教材文件到指定课程。"""
    if course_name not in workspaces:
        raise HTTPException(status_code=404, detail="Workspace not found")
    
    workspace = workspaces[course_name]
    workspace_path = runner.get_workspace_path(course_name)

    # 安全校验：只取文件名部分，防止路径穿越
    safe_filename = os.path.basename(file.filename or "")
    if not safe_filename:
        raise HTTPException(status_code=400, detail="无效的文件名")

    # 文件类型白名单校验
    allowed_exts = {".pdf", ".txt", ".md", ".docx", ".pptx", ".ppt"}
    ext = os.path.splitext(safe_filename)[1].lower()
    if ext not in allowed_exts:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型: {ext}，仅支持 pdf/txt/md/docx/pptx/ppt")

    upload_path = os.path.join(workspace_path, "uploads", safe_filename)

    # 保存文件到磁盘
    with open(upload_path, "wb") as f:
        content = await file.read()
        f.write(content)
    
    # 同步到内存文档列表
    if safe_filename not in workspace.documents:
        workspace.documents.append(safe_filename)
    
    return {
        "message": f"File {safe_filename} uploaded successfully",
        "filename": safe_filename
    }


@app.get("/workspaces/{course_name}/files")
async def list_workspace_files(course_name: str):
    """列出课程的已上传文件及索引状态。"""
    if course_name not in workspaces:
        raise HTTPException(status_code=404, detail="Workspace not found")
    workspace_path = runner.get_workspace_path(course_name)
    uploads_dir = os.path.join(workspace_path, "uploads")
    index_path = os.path.abspath(workspaces[course_name].index_path)

    files = []
    if os.path.exists(uploads_dir):
        for fname in sorted(os.listdir(uploads_dir)):
            fpath = os.path.join(uploads_dir, fname)
            if os.path.isfile(fpath):
                stat = os.stat(fpath)
                files.append({
                    "name": fname,
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                })

    # 索引状态：FAISS 实际存储为 faiss_index.faiss + faiss_index.pkl 两个平文件
    index_built = os.path.exists(f"{index_path}.faiss")
    index_mtime = None
    if index_built:
        try:
            mtimes = [os.stat(f).st_mtime for f in [f"{index_path}.faiss", f"{index_path}.pkl"]
                      if os.path.exists(f)]
            if mtimes:
                index_mtime = datetime.fromtimestamp(max(mtimes)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass

    return {"files": files, "index_built": index_built, "index_mtime": index_mtime}


@app.delete("/workspaces/{course_name}/files/{filename}")
async def delete_workspace_file(course_name: str, filename: str):
    """删除课程中某个已上传的原始文件。"""
    if course_name not in workspaces:
        raise HTTPException(status_code=404, detail="Workspace not found")
    safe_filename = os.path.basename(filename)
    workspace_path = runner.get_workspace_path(course_name)
    fpath = os.path.join(workspace_path, "uploads", safe_filename)
    if not os.path.isfile(fpath):
        raise HTTPException(status_code=404, detail=f"文件 {safe_filename} 不存在")
    os.remove(fpath)
    # 同步内存
    ws = workspaces[course_name]
    if safe_filename in ws.documents:
        ws.documents.remove(safe_filename)
    return {"message": f"文件 {safe_filename} 已删除"}


@app.delete("/workspaces/{course_name}/index")
async def delete_workspace_index(course_name: str):
    """删除课程的 FAISS 索引（不影响已上传的原始文件）。"""
    if course_name not in workspaces:
        raise HTTPException(status_code=404, detail="Workspace not found")
    index_path = os.path.abspath(workspaces[course_name].index_path)
    faiss_file = f"{index_path}.faiss"
    pkl_file = f"{index_path}.pkl"
    if not os.path.exists(faiss_file):
        raise HTTPException(status_code=404, detail="索引不存在")
    for f in [faiss_file, pkl_file]:
        if os.path.exists(f):
            os.remove(f)
    return {"message": "索引已删除"}



"""为课程构建 RAG 向量索引。
    - 目的：根据课程已上传的教材文件，解析文本内容，分块处理，并构建 FAISS 向量索引，供后续对话检索使用。
    - 实现方式：首先验证课程工作区存在，然后扫描 uploads/ 目录获取文件列表，解析每个文件提取文本内容，进行分块处理，
        最后调用 build_index 构建 FAISS 索引，并保存到磁盘。"""
@app.post("/workspaces/{course_name}/build-index")
async def build_workspace_index(course_name: str):
    """为课程构建 RAG 向量索引。"""
    if course_name not in workspaces:
        raise HTTPException(status_code=404, detail="Workspace not found")
    
    workspace = workspaces[course_name]
    workspace_path = runner.get_workspace_path(course_name)
    uploads_dir = os.path.join(workspace_path, "uploads")

    try:
        # 直接扫描 uploads/ 目录，避免内存列表与磁盘不同步导致漏文件
        allowed_exts = {".pdf", ".txt", ".md", ".docx", ".pptx", ".ppt"}
        disk_files = []
        if os.path.exists(uploads_dir):
            disk_files = [
                f for f in os.listdir(uploads_dir)
                if os.path.isfile(os.path.join(uploads_dir, f))
                and os.path.splitext(f)[1].lower() in allowed_exts
            ]
        # 回写内存，保持一致
        workspace.documents = disk_files

        if not disk_files:
            raise HTTPException(status_code=400, detail="uploads/ 目录中没有可用文件，请先上传教材")

        # 解析全部教材文件
        all_pages = []
        failed = []
        for doc_name in disk_files:
            doc_path = os.path.join(uploads_dir, doc_name)
            pages = DocumentParser.parse_document(doc_path)
            if pages:
                all_pages.extend(pages)
            else:
                failed.append(doc_name)

        if not all_pages:
            detail = "所有文件解析均未提取到文本。"
            if failed:
                detail += f" 解析失败的文件：{', '.join(failed)}（PDF 请确认非扫描版；PPTX 请确认文件未损坏）"
            raise HTTPException(status_code=400, detail=detail)

        # 文档分块
        chunks = chunk_documents(all_pages)

        # 构建向量索引
        store = build_index(chunks)

        # 保存索引到磁盘
        index_path = os.path.abspath(workspace.index_path)
        os.makedirs(os.path.dirname(index_path), exist_ok=True)
        store.save(index_path)

        return {
            "message": "Index built successfully",
            "num_chunks": len(chunks),
            "num_documents": len(workspace.documents)
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"构建索引时发生错误: {str(e)}")

"""同步对话接口。
    - 目的：提供一个 HTTP POST 接口，接受用户消息和对话历史
        进行处理，并返回生成的回复消息和执行计划。"""
@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """同步对话接口。"""
    if request.course_name not in workspaces:
        raise HTTPException(status_code=404, detail="Workspace not found")
    request_id = uuid.uuid4().hex[:12]
    history = [m.model_dump() for m in request.history] if request.history else []
    t0 = perf_counter()
    logger.info(
        "[chat] request.start request_id=%s course=%s mode=%s history_len=%d",
        request_id,
        request.course_name,
        request.mode,
        len(history),
    )
    try:
        with trace_scope(
            {
                "request_id": request_id,
                "course_name": request.course_name,
                "mode": request.mode,
                "api": "/chat",
            }
        ) as trace:
            add_event(
                "api_request_start",
                request_id=request_id,
                api="/chat",
                mode=request.mode,
                course_name=request.course_name,
                history_len=len(history),
            )
            # 执行编排主流程
            response_message, plan = runner.run(
                course_name=request.course_name,
                mode=request.mode,
                user_message=request.message,
                state={},
                history=history,
            )
            elapsed_ms = (perf_counter() - t0) * 1000.0
            add_event(
                "api_request_end",
                request_id=request_id,
                api="/chat",
                success=True,
                elapsed_ms=elapsed_ms,
            )
            logger.info(
                "[chat] request.done request_id=%s trace_id=%s elapsed_ms=%.1f",
                request_id,
                trace.trace_id,
                elapsed_ms,
            )
    except Exception as e:
        elapsed_ms = (perf_counter() - t0) * 1000.0
        logger.exception(
            "[chat] request.error request_id=%s elapsed_ms=%.1f err=%s",
            request_id,
            elapsed_ms,
            str(e),
        )
        raise
    
    return ChatResponse(
        message=response_message,
        plan=plan
    )


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """流式聊天接口，SSE 格式逐 token 输出。chunk 用 JSON 编码防止换行符破坏 SSE 协议。"""
    import json as _json
    if request.course_name not in workspaces:
        raise HTTPException(status_code=404, detail="Workspace not found")

    request_id = uuid.uuid4().hex[:12]
    history = [m.model_dump() for m in request.history] if request.history else []
    heartbeat_sec = float(os.getenv("SSE_HEARTBEAT_SEC", "8"))
    t0 = perf_counter()
    logger.info(
        "[chat.stream] request.start request_id=%s course=%s mode=%s history_len=%d",
        request_id,
        request.course_name,
        request.mode,
        len(history),
    )

    def event_generator():
        first_chunk_latency_ms = None
        emitted_chunks = 0
        q: Queue = Queue()

        def _emit(payload):
            nonlocal first_chunk_latency_ms, emitted_chunks
            if first_chunk_latency_ms is None:
                first_chunk_latency_ms = (perf_counter() - t0) * 1000.0
            emitted_chunks += 1
            return f"data: {_json.dumps(payload, ensure_ascii=False)}\n\n"

        def _runner_worker():
            try:
                for chunk in runner.run_stream(
                    course_name=request.course_name,
                    mode=request.mode,
                    user_message=request.message,
                    state={},
                    history=history,
                ):
                    q.put(("chunk", chunk))
                q.put(("done", None))
            except Exception as ex:
                q.put(("error", ex))

        try:
            with trace_scope(
                {
                    "request_id": request_id,
                    "course_name": request.course_name,
                    "mode": request.mode,
                    "api": "/chat/stream",
                }
            ) as trace:
                add_event(
                    "api_request_start",
                    request_id=request_id,
                    api="/chat/stream",
                    mode=request.mode,
                    course_name=request.course_name,
                    history_len=len(history),
                )
                worker_ctx = contextvars.copy_context()
                worker = Thread(target=lambda: worker_ctx.run(_runner_worker), daemon=True)
                worker.start()
                while True:
                    try:
                        kind, payload = q.get(timeout=heartbeat_sec)
                    except Empty:
                        yield _emit({"__status__": "后端仍在处理，正在继续推理..."})
                        continue
                    if kind == "chunk":
                        if payload:
                            # 用 JSON 序列化 chunk，换行符等特殊字符会被转义，不会破坏 SSE 行格式
                            yield _emit(payload)
                        continue
                    if kind == "error":
                        raise payload
                    if kind == "done":
                        break
                elapsed_ms = (perf_counter() - t0) * 1000.0
                add_event(
                    "api_request_end",
                    request_id=request_id,
                    api="/chat/stream",
                    success=True,
                    elapsed_ms=elapsed_ms,
                    first_chunk_latency_ms=first_chunk_latency_ms,
                    emitted_chunks=emitted_chunks,
                )
                logger.info(
                    "[chat.stream] request.done request_id=%s trace_id=%s first_chunk_ms=%.1f elapsed_ms=%.1f emitted_chunks=%d",
                    request_id,
                    trace.trace_id,
                    float(first_chunk_latency_ms or -1.0),
                    elapsed_ms,
                    emitted_chunks,
                )
        except Exception as e:
            import traceback
            traceback.print_exc()
            elapsed_ms = (perf_counter() - t0) * 1000.0
            add_event(
                "api_request_end",
                request_id=request_id,
                api="/chat/stream",
                success=False,
                elapsed_ms=elapsed_ms,
                first_chunk_latency_ms=first_chunk_latency_ms,
                emitted_chunks=emitted_chunks,
                error=str(e),
            )
            logger.exception(
                "[chat.stream] request.error request_id=%s first_chunk_ms=%.1f elapsed_ms=%.1f emitted_chunks=%d err=%s",
                request_id,
                float(first_chunk_latency_ms or -1.0),
                elapsed_ms,
                emitted_chunks,
                str(e),
            )
            yield _emit(f"（生成回答时出错：{e}）")
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "backend.api:app",
        host=os.getenv("API_HOST", "0.0.0.0"),
        port=int(os.getenv("API_PORT", "8000")),
        reload=True
    )
