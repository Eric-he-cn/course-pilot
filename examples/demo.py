"""
【模块说明】
- 主要作用：提供命令行版项目使用演示，帮助快速了解端到端流程。
- 核心函数：demo_workflow、show_api_examples、show_architecture。
- 使用方式：python examples/demo.py [--api] [--arch]
- 阅读建议：先看模块说明，再看类/函数头部注释和关键步骤注释。
- 注释策略：每个相对独立代码块都使用“目的 + 实现方式”进行说明。
"""

import sys


def demo_workflow() -> None:
    """打印项目完整使用流程（创建课程、上传、建索引、对话）。"""
    print("=" * 68)
    print("CoursePilot 演示流程")
    print("=" * 68)
    print("1. 启动后端：python -m backend.api")
    print("2. 启动前端：streamlit run frontend/streamlit_app.py")
    print("3. 在前端创建课程并上传教材")
    print("4. 点击“构建索引”完成 RAG 准备")
    print("5. 选择学习/练习/考试模式开始对话")
    print("6. 查看自动保存的 notes/practices/exams/memory 记录")
    print("=" * 68)


def show_api_examples() -> None:
    """打印常用 API 示例请求。"""
    print("\n[API 示例] 创建课程")
    print("POST /workspaces")
    print('{"course_name":"线性代数","subject":"数学"}')

    print("\n[API 示例] 上传文件")
    print("POST /workspaces/{course_name}/upload")

    print("\n[API 示例] 构建索引")
    print("POST /workspaces/{course_name}/build-index")

    print("\n[API 示例] 对话")
    print("POST /chat 或 /chat/stream")
    print('{"course_name":"线性代数","mode":"learn","message":"什么是矩阵的秩？"}')


def show_architecture() -> None:
    """打印简版系统架构说明。"""
    print("\n[架构概览]")
    print("前端 Streamlit -> 后端 FastAPI -> OrchestrationRunner")
    print("Runner -> Router/Tutor/Grader + RAG + Memory + MCP Tools")
    print("工具调用路径：LLM tool call -> MCPTools.call_tool -> stdio MCP server")


if __name__ == "__main__":
    demo_workflow()
    if "--api" in sys.argv:
        show_api_examples()
    if "--arch" in sys.argv:
        show_architecture()
