"""
【模块说明】
- 主要作用：实现 Streamlit 前端界面，提供课程管理、文件管理与对话交互。
- 核心函数：stream_chat、render_mermaid、load_workspaces。
- 关键特性：SSE 流式渲染、引用展示、Mermaid 导图下载。
- 阅读建议：先看模块说明，再看类/函数头部注释和关键步骤注释。
- 注释策略：每个相对独立代码块都使用“目的 + 实现方式”进行说明。
"""
import re
import streamlit as st
import requests
import json
import os
from datetime import datetime


def fix_latex(text: str) -> str:
    """将 LLM 输出的 LaTeX 定界符转换为 Streamlit KaTeX 可识别的格式。
    \\[...\\]  →  $$...$$  （块公式）
    \\(...\\)  →  $...$    （行内公式）
    """
    if not text:
        return text
    # 块公式：\[ ... \]  →  $$...$$
    text = re.sub(r'\\\[\s*(.*?)\s*\\\]', r'$$\1$$', text, flags=re.DOTALL)
    # 行内公式：\( ... \)  →  $...$
    text = re.sub(r'\\\(\s*(.*?)\s*\\\)', r'$\1$', text, flags=re.DOTALL)
    return text


def extract_mermaid_blocks(text: str):
    """从回复文本中提取 ```mermaid``` 代码块，返回 (cleaned_text, [code_str, ...])。"""
    blocks: list[str] = []

    def _repl(m: re.Match) -> str:
        blocks.append(m.group(1).strip())
        return "\n> 📊 *[思维导图已在下方渲染]*\n"

    cleaned = re.sub(r"```mermaid\s*(.*?)```", _repl, text, flags=re.DOTALL)
    return cleaned, blocks


def strip_source_markers(text: str) -> str:
    """移除历史消息中的 [来源N] 标记，避免旧引用编号干扰当前回答。"""
    if not text:
        return text
    return re.sub(r"\[来源\d+\]", "", text)


def render_mermaid(mermaid_code: str, idx: int = 0, height: int = 520) -> None:
    """使用 Mermaid CDN + components.html 渲染思维导图，并提供 SVG/PNG 下载按钮。"""
    import streamlit.components.v1 as components

    svg_id = f"mm{idx}"
    html_code = f"""<!DOCTYPE html>
<html><head>
<style>
  body{{margin:0;padding:8px;background:#fff;font-family:sans-serif;}}
  .tb{{display:flex;gap:8px;margin-bottom:8px;flex-wrap:wrap;}}
  button{{padding:5px 14px;border:1px solid #ced4da;border-radius:4px;cursor:pointer;
          background:#f8f9fa;font-size:13px;}}
  button:hover{{background:#e2e6ea;}}
  #mc{{overflow:auto;text-align:center;}}
  .mermaid{{display:inline-block;}}
</style>
</head><body>
<div class="tb">
  <button onclick="dlSVG()">⬇ 下载 SVG</button>
  <button onclick="dlPNG()">🖼 下载 PNG</button>
</div>
<div id="mc"><div class="mermaid" id="{svg_id}">{mermaid_code}</div></div>
<script type="module">
  import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs';
  mermaid.initialize({{startOnLoad:true,theme:'default',securityLevel:'loose'}});
</script>
<script>
function dlSVG(){{
  var el=document.querySelector('#{svg_id} svg');
  if(!el){{alert('图表尚未渲染，请稍等片刻');return;}}
  var d=new XMLSerializer().serializeToString(el);
  var b=new Blob([d],{{type:'image/svg+xml;charset=utf-8'}});
  var u=URL.createObjectURL(b);
  var a=document.createElement('a');a.href=u;a.download='mindmap.svg';a.click();
  URL.revokeObjectURL(u);
}}
function dlPNG(){{
  var el=document.querySelector('#{svg_id} svg');
  if(!el){{alert('图表尚未渲染，请稍等片刻');return;}}
  // \u4ece viewBox \u8bfb\u53d6\u81ea\u7136\u5206\u8fa8\u7387\uff08Mermaid \u8f93\u51fa\u7684\u771f\u5b9e SVG \u5c3a\u5bf8\uff09
  var natW=0,natH=0;
  var vb=el.getAttribute('viewBox');
  if(vb){{
    var pts=vb.trim().split(/[\\s,]+/);
    if(pts.length>=4){{natW=parseFloat(pts[2]);natH=parseFloat(pts[3]);}}
  }}
  if(!natW){{natW=parseFloat(el.getAttribute('width'))||1600;}}
  if(!natH){{natH=parseFloat(el.getAttribute('height'))||900;}}
  // 3\u00d7 \u8d85\u91c7\u6837\uff0c\u8f93\u51fa\u9ad8\u6e05 PNG
  var scale=3;
  var c=document.createElement('canvas');
  c.width=Math.round(natW*scale);
  c.height=Math.round(natH*scale);
  var ctx=c.getContext('2d');
  // \u514b\u9686 SVG \u5e76\u663e\u5f0f\u8bbe\u7f6e width/height \u4ee5\u786e\u4fdd\u6b63\u786e\u62c9\u4f38
  var clone=el.cloneNode(true);
  clone.setAttribute('width',natW);
  clone.setAttribute('height',natH);
  var sd=new XMLSerializer().serializeToString(clone);
  var img=new Image();
  img.onload=function(){{
    ctx.fillStyle='white';ctx.fillRect(0,0,c.width,c.height);
    ctx.scale(scale,scale);
    ctx.drawImage(img,0,0,natW,natH);
    var a=document.createElement('a');a.href=c.toDataURL('image/png',1.0);
    a.download='mindmap.png';a.click();
  }};
  img.src='data:image/svg+xml;base64,'+btoa(unescape(encodeURIComponent(sd)));
}}
</script>
</body></html>"""
    components.html(html_code, height=height, scrolling=True)


# 后端 API 地址：默认连接本机服务，可通过环境变量覆盖。
API_BASE = os.getenv("API_BASE", "http://localhost:8000")

# ── 模式主题色 ───────────────────────────────────────────────────────────────
MODE_THEME = {
    "learn":    {"bg": "#EBF5FB", "accent": "#2471A3", "pill": "#D6EAF8", "label": "📖 学习模式"},
    "practice": {"bg": "#EAFAF1", "accent": "#1E8449", "pill": "#D5F5E3", "label": "✍️ 练习模式"},
    "exam":     {"bg": "#FEF9E7", "accent": "#9A7D0A", "pill": "#FCF3CF", "label": "📝 考试模式"},
}

def inject_mode_css(mode: str) -> None:
    """注入全局样式（不改主背景色，保持灰白协调）。"""
    c = MODE_THEME.get(mode, MODE_THEME["learn"])
    st.markdown(f"""<style>
/* 侧边栏保持浅灰 */
[data-testid="stSidebar"] {{
    background-color: #F4F6F8 !important;
}}
/* 模式标签胶囊 */
.mode-pill {{
    display:inline-block; padding:4px 14px; border-radius:20px;
    background:{c["pill"]}; color:{c["accent"]}; font-weight:700;
    font-size:0.88rem; border:1px solid {c["accent"]}66;
    vertical-align:middle;
}}
/* 对话区左侧模式指示条 */
.mode-bar {{
    border-left: 5px solid {c["accent"]};
    background: {c["pill"]}66;
    border-radius: 0 8px 8px 0;
    padding: 8px 16px;
    margin-bottom: 10px;
    display: flex;
    align-items: center;
    gap: 10px;
    color: {c["accent"]};
    font-weight: 600;
    font-size: 0.95rem;
}}
/* 帮助面板 */
.help-section {{
    background:#fff; border:1px solid #DEE2E6; border-radius:12px;
    padding:22px 24px; line-height:1.75; margin-bottom:12px;
}}
.help-section h3 {{ color:{c["accent"]}; margin-top:1rem; }}
</style>""", unsafe_allow_html=True)

# ── 帮助面板内容 ──────────────────────────────────────────────────────────────
HELP_CONTENT = """
<div class="help-section">
<h3>🚀 快速开始</h3>
<ol>
  <li><b>创建课程</b>：侧边栏 → 「➕ 创建新课程」，填写课程名与学科标签</li>
  <li><b>上传资料</b>：选择课程后，上传 PDF / TXT / MD / DOCX / PPTX 等教材文件</li>
  <li><b>构建索引</b>：点击「🔨 构建索引」，系统将对教材进行向量化，首次需下载嵌入模型（约1GB，仅下载一次）</li>
  <li><b>开始对话</b>：选择学习模式后，在底部输入框提问即可</li>
</ol>

<h3>📖 学习模式</h3>
<ul>
  <li>向 AI 提问任何教材相关内容，获得基于教材的精准讲解</li>
  <li>每条回答附带<b>引用来源</b>，点击可查看原始段落</li>
  <li>可要求"生成 XX 的思维导图"，AI 将自动绘制 Mermaid 思维导图并支持下载</li>
  <li>可直接搜索互联网补充教材未覆盖的内容</li>
  <li>AI 会记录你的学习历史，自动关注薄弱知识点</li>
</ul>

<h3>✍️ 练习模式</h3>
<ul>
  <li>告诉 AI 你想练习的知识点与题型（选择题 / 判断题 / 简答题 / 计算题等）</li>
  <li>AI 出题后，直接在对话框回答，系统将自动评分并给出详细解析</li>
  <li>评分采用<b>逐题对照</b>机制，确保结果准确</li>
  <li>错题将自动记录到记忆库，下次练习时 AI 会优先强化薄弱点</li>
</ul>

<h3>📝 考试模式</h3>
<ul>
  <li>首先告诉 AI 考试配置（范围、题型、题数、难度）</li>
  <li>AI 生成完整试卷后，将所有答案<b>一次性提交</b></li>
  <li>AI 出具逐题批改报告和总得分，并分析薄弱知识点</li>
  <li>考试模式禁用联网搜索，模拟真实考场</li>
</ul>

<h3>🛠️ 实用技巧</h3>
<ul>
  <li><b>思维导图</b>：输入"帮我生成【主题】的思维导图"，可下载 SVG / PNG / Mermaid 源码</li>
  <li><b>笔记保存</b>：输入"把这段内容保存为笔记"，AI 会自动写入课程目录</li>
  <li><b>切换课程</b>：切换后对话历史自动清空，互不干扰</li>
  <li><b>文件管理</b>：侧边栏「📁 文件与索引」区可查看已上传文件、索引状态，并支持单独删除</li>
</ul>
</div>
"""


st.set_page_config(
    page_title="CoursePilot",
    page_icon="📚",
    layout="wide"
)

# 初始化会话状态：前端交互中的临时状态统一放在 session_state。
if "current_course" not in st.session_state:
    st.session_state.current_course = None
if "current_mode" not in st.session_state:
    st.session_state.current_mode = "learn"
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "workspaces" not in st.session_state:
    st.session_state.workspaces = []
if "show_help" not in st.session_state:
    st.session_state.show_help = False


@st.cache_data(ttl=30, show_spinner=False)
def fetch_workspaces_cached(api_base: str):
    """获取课程列表缓存，避免页面每次 rerun 都阻塞等待接口。"""
    try:
        response = requests.get(f"{api_base}/workspaces", timeout=5)
        if response.status_code == 200:
            return response.json()
    except Exception:
        pass
    return []


@st.cache_data(ttl=30, show_spinner=False)
def fetch_workspace_files_cached(api_base: str, course_name: str):
    """获取课程文件与索引状态缓存，减少重复请求。"""
    fallback = {"files": [], "index_built": False, "index_mtime": None}
    try:
        resp = requests.get(f"{api_base}/workspaces/{course_name}/files", timeout=5)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return fallback


def invalidate_api_cache():
    """在上传/删除/重建后清空缓存，避免前端读到旧状态。"""
    fetch_workspaces_cached.clear()
    fetch_workspace_files_cached.clear()


def load_workspaces():
    """加载可用课程工作区列表。"""
    try:
        st.session_state.workspaces = fetch_workspaces_cached(API_BASE)
    except Exception as e:
        st.error(f"加载课程失败: {e}")


def create_workspace(course_name: str, subject: str):
    """创建新课程工作区。"""
    try:
        response = requests.post(
            f"{API_BASE}/workspaces",
            json={"course_name": course_name, "subject": subject}
        )
        if response.status_code == 200:
            st.success(f"课程 '{course_name}' 创建成功！")
            invalidate_api_cache()
            load_workspaces()
            return True
        else:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text or f"HTTP {response.status_code}"
            st.error(f"创建失败: {detail}")
    except Exception as e:
        st.error(f"创建课程失败: {e}")
    return False


def upload_file(course_name: str, file):
    """上传课程资料文件到后端工作区。"""
    try:
        files = {"file": (file.name, file, file.type)}
        response = requests.post(
            f"{API_BASE}/workspaces/{course_name}/upload",
            files=files
        )
        if response.status_code == 200:
            return True
    except Exception as e:
        st.error(f"上传文件失败: {e}")
    return False


def build_index(course_name: str):
    """为课程构建 RAG 索引。"""
    try:
        response = requests.post(
            f"{API_BASE}/workspaces/{course_name}/build-index",
            timeout=300  # 最长等待5分钟（首次需下载嵌入模型）
        )
        if response.status_code == 200:
            data = response.json()
            fetch_workspace_files_cached.clear()
            st.success(f"索引构建成功！共 {data['num_chunks']} 个文本块")
            return True
        else:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text or f"HTTP {response.status_code}"
            st.error(f"构建失败: {detail}")
    except requests.exceptions.Timeout:
        st.error("构建超时，请检查后端是否在下载嵌入模型，稍后重试")
    except Exception as e:
        st.error(f"构建索引失败: {e}")
    return False


def send_message(course_name: str, mode: str, message: str):
    """发送非流式对话请求，并携带裁剪后的历史。"""
    try:
        # 取当前消息之前的最多 20 条历史（[-21:-1] 排除最后一条刚 append 的用户消息，避免重复）
        history = st.session_state.chat_history[-21:-1] if st.session_state.chat_history else []
        # 只保留 role 和 content 字段
        history_payload = []
        for m in history:
            role = m["role"]
            content = m["content"]
            if role == "assistant":
                content = strip_source_markers(content)
            payload = {"role": role, "content": content}
            if role == "assistant" and m.get("tool_calls"):
                payload["tool_calls"] = m.get("tool_calls")
            history_payload.append(payload)
        response = requests.post(
            f"{API_BASE}/chat",
            json={
                "course_name": course_name,
                "mode": mode,
                "message": message,
                "history": history_payload
            },
            timeout=120
        )
        if response.status_code == 200:
            return response.json()
        else:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text or f"HTTP {response.status_code}"
            st.error(f"请求失败: {detail}")
    except requests.exceptions.Timeout:
        st.error("请求超时，请稍后重试")
    except Exception as e:
        st.error(f"发送消息失败: {e}")
    return None


def stream_chat(course_name: str, mode: str, message: str):
    """流式发送消息，返回文本 chunk 生成器（供 st.write_stream 使用）。"""
    import json as _json
    # 取当前消息之前的最多 20 条历史（[-21:-1] 排除最后一条刚 append 的用户消息，避免重复）
    history = st.session_state.chat_history[-21:-1] if st.session_state.chat_history else []
    history_payload = []
    for m in history:
        role = m["role"]
        content = m["content"]
        if role == "assistant":
            content = strip_source_markers(content)
        payload_item = {"role": role, "content": content}
        if role == "assistant" and m.get("tool_calls"):
            payload_item["tool_calls"] = m.get("tool_calls")
        history_payload.append(payload_item)
    payload = {
        "course_name": course_name,
        "mode": mode,
        "message": message,
        "history": history_payload,
    }
    try:
        with requests.post(
            f"{API_BASE}/chat/stream",
            json=payload,
            stream=True,
            timeout=180,
        ) as resp:
            if resp.status_code != 200:
                try:
                    detail = resp.json().get("detail", resp.text)
                except Exception:
                    detail = resp.text or f"HTTP {resp.status_code}"
                yield f"（请求失败：{detail}）"
                return
            for raw_line in resp.iter_lines():
                if raw_line:
                    line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        # JSON 解码，还原换行符等特殊字符
                        try:
                            yield _json.loads(data)
                        except _json.JSONDecodeError:
                            yield data
    except requests.exceptions.Timeout:
        yield "（请求超时，请稍后重试）"
    except Exception as e:
        yield f"（流式输出失败：{e}）"


# 页面主 UI：先渲染顶栏和侧边栏，再渲染主内容区。
st.markdown("""
<div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:0.2rem;">
  <h1 style="margin:0; font-size:2rem;">📚 CoursePilot — 你的课程学习 AI 助手</h1>
  <a href="https://github.com/Eric-he-cn/your_AI_study_agent" target="_blank"
     style="display:flex; align-items:center; gap:6px; text-decoration:none;
            color:#24292f; background:#f6f8fa; border:1px solid #d0d7de;
            border-radius:6px; padding:6px 12px; font-size:0.85rem; white-space:nowrap;">
    <svg height="18" viewBox="0 0 16 16" width="18" fill="currentColor">
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38
               0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13
               -.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66
               .07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15
               -.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27
               .68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12
               .51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48
               0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/>
    </svg>
    Eric He / CoursePilot
  </a>
</div>
<p style="color:#666; margin:0.2rem 0 0.6rem 0; font-size:0.97rem;">
  将课程教材接入 RAG 知识库，三种模式闭环（学习→练习→考试），
  多 Agent 协同 + 多 个 MCP 工具支撑，跨会话记忆追踪薄弱知识点，让大学生更高效地掌握课程内容。
</p>
""", unsafe_allow_html=True)
st.markdown("---")

# 侧边栏：课程管理、模式切换、文件上传与索引管理入口。
with st.sidebar:
    st.header("⚙️ 设置")
    
    # 步骤1：刷新课程列表，确保前后端状态一致。
    if st.button("🔄 刷新课程列表"):
        fetch_workspaces_cached.clear()
        load_workspaces()
    
    # 步骤2：创建课程工作区。
    if "expander_open" not in st.session_state:
        st.session_state.expander_open = False
    with st.expander("➕ 创建新课程", expanded=st.session_state.expander_open):
        # 用 form 批量提交，避免输入每个字符都触发整页 rerun
        with st.form("create_workspace_form", clear_on_submit=False):
            new_course_name = st.text_input("课程名称", key="new_course_name")
            new_subject = st.text_input(
                "学科标签",
                key="new_subject",
                placeholder="例如：线性代数、通信原理",
            )
            create_submitted = st.form_submit_button("创建")

        if create_submitted:
            st.session_state.expander_open = True
            if new_course_name and new_subject:
                create_workspace(new_course_name, new_subject)
            else:
                st.warning("请填写课程名称和学科标签")
    
    # 步骤3：选择当前课程。
    st.markdown("### 📖 选择课程")
    if not st.session_state.workspaces:
        load_workspaces()
    if st.session_state.workspaces:
        course_names = [w["course_name"] for w in st.session_state.workspaces]
        selected = st.selectbox(
            "当前课程",
            course_names,
            key="course_selector"
        )
        if selected != st.session_state.current_course:
            st.session_state.current_course = selected
            st.session_state.chat_history = []
    else:
        st.info("暂无课程，请创建新课程")
    
    # 步骤4：切换学习模式（learn/practice/exam）。
    st.markdown("### 🎯 学习模式")
    mode = st.radio(
        "选择模式",
        ["learn", "practice", "exam"],
        format_func=lambda x: {
            "learn": "📖 学习模式",
            "practice": "✍️ 练习模式",
            "exam": "📝 考试模式"
        }[x],
        key="mode_selector"
    )
    if mode != st.session_state.current_mode:
        st.session_state.current_mode = mode
    
    # 步骤5：管理知识库文件与索引。
    if st.session_state.current_course:
        st.markdown("### 📁 文件与索引")

        # ── 上传区 ──────────────────────────────────
        with st.expander("📤 上传资料", expanded=False):
            uploaded_file = st.file_uploader(
                "选择文件",
                type=["pdf", "txt", "md", "docx", "pptx", "ppt"],
                key="file_uploader",
                label_visibility="collapsed",
            )
            if uploaded_file and st.button("⬆ 上传"):
                if upload_file(st.session_state.current_course, uploaded_file):
                    fetch_workspace_files_cached.clear()
                    st.success(f"✅ {uploaded_file.name} 上传成功")

        # ── 文件列表 ─────────────────────────────────
        course = st.session_state.current_course
        fdata = fetch_workspace_files_cached(API_BASE, course)

        files = fdata.get("files", [])
        index_built = fdata.get("index_built", False)
        index_mtime = fdata.get("index_mtime")

        if files:
            with st.expander(f"📂 已上传文件 ({len(files)})", expanded=True):
                for f in files:
                    size_kb = f["size"] / 1024
                    size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb/1024:.2f} MB"
                    col_f, col_del = st.columns([5, 1])
                    with col_f:
                        st.caption(f"📄 **{f['name']}**  \n{size_str} · {f['modified']}")
                    with col_del:
                        safe_key = re.sub(r"\W", "_", f["name"])
                        if st.button("🗑", key=f"del_file_{safe_key}", help=f"删除 {f['name']}"):
                            try:
                                dr = requests.delete(
                                    f"{API_BASE}/workspaces/{course}/files/{f['name']}", timeout=10)
                                if dr.status_code == 200:
                                    fetch_workspace_files_cached.clear()
                                    st.success(f"已删除 {f['name']}")
                                    st.rerun()
                                else:
                                    st.error(dr.json().get("detail", "删除失败"))
                            except Exception as ex:
                                st.error(str(ex))
        else:
            st.caption("暂无已上传文件")

        # ── 索引状态 ─────────────────────────────────
        st.markdown("**🗂 索引状态**")
        if index_built:
            st.markdown(
                f"<span style='color:#555; font-size:0.88rem;'>✅ 索引已建立（{index_mtime or '时间未知'}）</span>",
                unsafe_allow_html=True
            )
            col_b, col_d = st.columns(2)
            with col_b:
                if st.button("🔨 重建索引", use_container_width=True):
                    with st.spinner("构建中…"):
                        build_index(course)
                    st.rerun()
            with col_d:
                if st.button("🗑 删除索引", use_container_width=True):
                    try:
                        dr = requests.delete(f"{API_BASE}/workspaces/{course}/index", timeout=10)
                        if dr.status_code == 200:
                            fetch_workspace_files_cached.clear()
                            st.warning("索引已删除")
                            st.rerun()
                        else:
                            st.error(dr.json().get("detail", "删除失败"))
                    except Exception as ex:
                        st.error(str(ex))
        else:
            st.warning("索引尚未建立")
            if st.button("🔨 构建索引", use_container_width=True):
                with st.spinner("正在构建索引，首次需下载嵌入模型，请耐心等待…"):
                    build_index(course)
                st.rerun()


# 主内容区：展示状态栏、历史消息和流式回复。
if st.session_state.current_course:
    # 注入模式主题色
    inject_mode_css(st.session_state.current_mode)

    # ── 顶栏：课程/模式信息 + 帮助 + 清空历史 ────────────────────────────────
    col_info, col_btns = st.columns([6, 2])
    with col_info:
        c = MODE_THEME[st.session_state.current_mode]
        st.markdown(
            f"**当前课程**：{st.session_state.current_course} &nbsp;&nbsp;"
            f'<span class="mode-pill">{c["label"]}</span>',
            unsafe_allow_html=True,
        )
    with col_btns:
        btn_col1, btn_col2 = st.columns(2)
        with btn_col1:
            if st.button("❓ 帮助", use_container_width=True):
                st.session_state.show_help = not st.session_state.show_help
        with btn_col2:
            if st.button("🗑 清空", use_container_width=True, help="清空当前对话历史"):
                st.session_state.chat_history = []

    # ── 帮助面板（可折叠） ───────────────────────────────────────────────────
    if st.session_state.show_help:
        st.markdown(HELP_CONTENT, unsafe_allow_html=True)

    st.markdown("---")

    # ── 对话区模式指示条 ──────────────────────────────────────────────────────
    mode_bar_info = {
        "learn":    ("📖", "学习模式", "提问知识点 · 生成思维导图 · 保存笔记"),
        "practice": ("✍️", "练习模式", "指定题型和知识点 · 提交答案后自动评分"),
        "exam":     ("📝", "考试模式", "配置考试 → 收到试卷 → 一次性提交全部答案"),
    }
    icon, label, tip = mode_bar_info[st.session_state.current_mode]
    st.markdown(
        f'<div class="mode-bar">{icon} <span>{label}</span>'
        f'<span style="font-weight:400;font-size:0.82rem;opacity:0.8;margin-left:8px">· {tip}</span></div>',
        unsafe_allow_html=True,
    )

    # 渲染历史消息：包含正文、引用、工具调用记录与导图。
    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"]):
            st.markdown(fix_latex(msg["content"]))
            
            # 渲染引用来源（仅显示该条消息自己的 citations）。
            if msg.get("citations"):
                with st.expander(f"📑 查看引用来源（共 {len(msg['citations'])} 条）"):
                    for i, citation in enumerate(msg["citations"]):
                        page_str = f"  第 {citation['page']} 页" if citation.get("page") else ""
                        score_str = f"  相关度 {citation['score']:.2f}" if citation.get("score") is not None else ""
                        st.markdown(
                            f"**[来源{i+1}]** `{citation['doc_id']}`{page_str}{score_str}"
                        )
                        preview = citation["text"][:300].replace("\n", " ").strip()
                        if len(citation["text"]) > 300:
                            preview += "…"
                        st.caption(preview)
                        if i < len(msg["citations"]) - 1:
                            st.divider()
            
            # 渲染工具调用记录（便于排查工具链路）。
            visible_tool_calls = [
                tc for tc in (msg.get("tool_calls") or [])
                if not (isinstance(tc, dict) and tc.get("type") == "internal_meta")
            ]
            if visible_tool_calls:
                with st.expander("🔧 工具调用"):
                    for tool_call in visible_tool_calls:
                        st.json(tool_call)

            # 渲染 Mermaid 思维导图代码块。
            for m_idx, mb in enumerate(msg.get("mermaid_blocks") or []):
                render_mermaid(mb["code"], idx=abs(hash(mb["code"])) % 100000, height=520)
                with st.expander("📄 下载 Mermaid 源码"):
                    safe_title = re.sub(r"[^\w\-]", "_", mb.get("title", "mindmap"))
                    st.download_button(
                        label="⬇ 下载 .md 文件",
                        data=f"```mermaid\n{mb['code']}\n```",
                        file_name=f"{safe_title}.md",
                        mime="text/markdown",
                        key=f"dl_md_{abs(hash(mb['code'])) % 100000}_{m_idx}",
                    )

    # 输入区：提交后写入历史并触发流式请求。
    user_input = st.chat_input("输入你的问题...")
    
    if user_input:
        # 步骤1：先把用户消息写入历史，保证刷新后可回放。
        st.session_state.chat_history.append({
            "role": "user",
            "content": user_input
        })
        
        # 步骤2：立即回显用户消息，降低等待感。
        with st.chat_message("user"):
            st.markdown(user_input)
        
        # 流式输出助手回答
        # 单独收集文本，避免依赖 st.write_stream 返回类型（新版 Streamlit 返回 StreamingOutput 而非 str）
        collected_chunks: list[str] = []
        st.session_state._pending_citations = []  # 在流开始前初始化
        st.session_state._pending_tool_calls = []  # 保存内部 tool_calls 元数据
        assistant_payload = None

        with st.chat_message("assistant"):
            progress_placeholder = st.empty()

            def _collecting_stream():
                for chunk in stream_chat(
                    st.session_state.current_course,
                    st.session_state.current_mode,
                    user_input,
                ):
                    # 拦截 citations 元数据事件，不渲染到气泡，仅存于 session_state
                    if isinstance(chunk, dict) and "__citations__" in chunk:
                        st.session_state._pending_citations = chunk["__citations__"]
                        continue  # 跳过 yield，防止 st.write_stream 把 dict 渲染成乱码

                    # 工具元数据事件：用于下一轮评分，不在当前对话中显示
                    if isinstance(chunk, dict) and "__tool_calls__" in chunk:
                        st.session_state._pending_tool_calls = chunk["__tool_calls__"] or []
                        continue

                    # 进度事件：显示当前正在执行的阶段，避免“卡死感”
                    if isinstance(chunk, dict) and "__status__" in chunk:
                        status_text = str(chunk.get("__status__", "")).strip()
                        if status_text:
                            progress_placeholder.caption(f"⏳ {status_text}")
                        continue

                    if isinstance(chunk, str):
                        collected_chunks.append(chunk)
                    yield chunk

            st.write_stream(_collecting_stream())
            progress_placeholder.empty()

            full_response = "".join(collected_chunks)
            # 捕获流式过程中拦截到的 citations
            citations = st.session_state.pop("_pending_citations", None) or None
            tool_calls = st.session_state.pop("_pending_tool_calls", None) or None

            if full_response:
                # 提取 mermaid 代码块，避免 markdown 渲染失败
                cleaned_response, mermaid_codes = extract_mermaid_blocks(full_response)
                mermaid_blocks = [{"code": c, "title": "思维导图"} for c in mermaid_codes]

                # 关键修复：当前轮流式结束后，立即在当前气泡里展示引用来源
                if citations:
                    with st.expander(f"📑 查看引用来源（共 {len(citations)} 条）"):
                        for i, citation in enumerate(citations):
                            page_str = f"  第 {citation['page']} 页" if citation.get("page") else ""
                            score_str = f"  相关度 {citation['score']:.2f}" if citation.get("score") is not None else ""
                            st.markdown(
                                f"**[来源{i+1}]** `{citation['doc_id']}`{page_str}{score_str}"
                            )
                            preview = citation["text"][:300].replace("\n", " ").strip()
                            if len(citation["text"]) > 300:
                                preview += "…"
                            st.caption(preview)
                            if i < len(citations) - 1:
                                st.divider()

                # 同一轮直接渲染 mermaid，避免必须下一轮才看到图
                for m_idx, mb in enumerate(mermaid_blocks):
                    render_mermaid(mb["code"], idx=abs(hash(mb["code"])) % 100000, height=520)
                    with st.expander("📄 下载 Mermaid 源码"):
                        safe_title = re.sub(r"[^\w\-]", "_", mb.get("title", "mindmap"))
                        st.download_button(
                            label="⬇ 下载 .md 文件",
                            data=f"```mermaid\n{mb['code']}\n```",
                            file_name=f"{safe_title}.md",
                            mime="text/markdown",
                            key=f"dl_md_now_{abs(hash(mb['code'])) % 100000}_{m_idx}",
                        )

                assistant_payload = {
                    "role": "assistant",
                    "content": fix_latex(cleaned_response),
                    "citations": citations,
                    "tool_calls": tool_calls,
                    "mermaid_blocks": mermaid_blocks,
                }

        if assistant_payload:
            # 把完整回答加入对话历史，保证刷新后仍能复现引用与导图
            st.session_state.chat_history.append(assistant_payload)

else:
    inject_mode_css("learn")
    st.info("👈 请先在侧边栏选择或创建一个课程")
    st.markdown(HELP_CONTENT, unsafe_allow_html=True)
