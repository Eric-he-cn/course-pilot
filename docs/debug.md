以下是整个对话从头到尾所有调试过程的梳理，按时间顺序排列：

## 1. ModuleNotFoundError — 模块找不到
- 发现问题：直接运行 `python backend/api.py` 时找不到 `backend` 包。
- 解决思路：使用模块方式运行，确保项目根目录在 `sys.path`。
- 解决步骤：启动命令改为 `python -m backend.api`。
- 解决结果：后端正常启动，导入错误消失。

## 2. API Key 未加载
- 发现问题：LLM 调用拿到的 API Key 为 `None`。
- 解决思路：入口处调用 `load_dotenv()`，读取 `.env`。
- 解决步骤：确认 `openai_compat.py` 顶部调用 `load_dotenv()`，并检查 `.env` 配置格式。
- 解决结果：API Key 正常读取，LLM 调用成功。

## 3. openai 与 httpx 版本不兼容
- 发现问题：`openai` 导入/运行期指向 `httpx` 参数变化的错误。
- 解决思路：对齐版本，避免破坏性变更。
- 解决步骤：固定 `openai==2.21.0`、`httpx==0.28.1`，重新安装。
- 解决结果：SDK 正常工作，HTTP 请求无报错。

## 4. FAISS 在 Windows 中文路径下崩溃
- 发现问题：含中文路径的课程索引读写 `faiss.write_index/read_index` 失败。
- 解决思路：先 `os.chdir()` 到索引目录，用 ASCII 相对路径操作。
- 解决步骤：在 `store_faiss.py` 的 `save()/load()` 中用 `try/finally` 包裹 `chdir`，操作完恢复。
- 解决结果：中文路径课程可正常保存/加载索引。

## 5. DOCX 格式不支持
- 发现问题：上传 `.docx` 显示不支持，无法入库。
- 解决思路：增加 `python-docx` 解析分支。
- 解决步骤：`ingest.py` 添加 `parse_docx()`，分发处支持 `.docx`；`requirements.txt` 增加依赖。
- 解决结果：Word 文档可解析并索引。

## 6. SSE 流式输出被换行截断
- 发现问题：回答含换行时 SSE 帧被截断，前端 JSON 解析失败。
- 解决思路：每个 chunk 先 `json.dumps()`，前端 `json.loads()` 还原。
- 解决步骤：后端 SSE 输出改为单行 JSON 字符串；前端逐帧解码后拼接。
- 解决结果：含 Markdown/公式的流式输出稳定。

## 7. 练习模式多轮上下文丢失
- 发现问题：第二轮提交答案时 AI 重新出题或答非所问。
- 解决思路：用完整对话 `history` 驱动 Grader/Tutor，不依赖单独状态。
- 解决步骤：`runner.py` 练习流程传递全量 `history`，移除全局 quiz 状态。
- 解决结果：练习模式多轮对话连贯，评分准确。

## 8. LaTeX 公式不渲染
- 发现问题：`\[...]` / `\(...)` 公式在 Streamlit 中原样显示。
- 解决思路：转为 MathJax 支持的 `$$...$$` / `$...$`。
- 解决步骤：前端添加 `fix_latex()`，显示前和保存 history 前调用。
- 解决结果：公式正常渲染。

## 9. 练习/考试记录用户答案提取错误
- 发现问题：记录中的 `user_answer` 取到上一轮消息。
- 解决思路：显式传入本次 `user_message`，不再从 history 反查。
- 解决步骤：`_save_practice_record/_save_exam_record` 新增 `user_message` 参数，4 个调用点同步更新。
- 解决结果：记录中的用户答案与本次提交一致。

## 10. 当前消息重复发送给 LLM
- 发现问题：history 切片包含刚追加的当前消息，LLM 收到两份相同输入。
- 解决思路：切片排除最后一条，即用 `[-21:-1]` 而非 `[-20:]`。
- 解决步骤：前端 `send_message()/stream_chat()` 改为 `chat_history[-21:-1]`。
- 解决结果：LLM 不再重复接收当前消息。

## 11. 文件上传路径穿越
- 发现问题：上传文件名可构造 `../../evil.py` 写出工作区。
- 解决思路：`os.path.basename()` 净化文件名 + 扩展名白名单。
- 解决步骤：`api.py` 上传端点使用 `safe_filename = basename(...)`，校验后缀仅允许 `.pdf/.txt/.md/.docx`。
- 解决结果：路径穿越与非法类型上传被拦截。

## 12. 课程名路径穿越
- 发现问题：`get_workspace_path(course_name)` 直接 join，`../..` 可穿越。
- 解决思路：对 `course_name` 做 `basename` 净化并校验。
- 解决步骤：`runner.py` 中 `get_workspace_path` 校验空名、`.`、`..`，非法则抛错。
- 解决结果：课程名构路径安全，穿越面消除。

## 13. chunk.py 可能死循环
- 发现问题：`overlap >= chunk_size` 时 `start` 不前进，循环不终止。
- 解决思路：入口收敛参数并加兜底前进。
- 解决步骤：当 `overlap >= chunk_size` 时自动设置为 `chunk_size // 2`；循环内若 `next_start <= start` 强制前进一个 chunk。
- 解决结果：分块循环必定终止，不再卡死。

## 14. TXT 仅支持 UTF-8
- 发现问题：GBK/GB2312 TXT 上传解析失败，静默返回空内容。
- 解决思路：多编码回退，覆盖主流中文编码。
- 解决步骤：按 `utf-8-sig → utf-8 → gbk → latin-1` 顺序尝试读取，全部失败才报错。
- 解决结果：GBK/GB2312 文件可解析，UTF-8-BOM 也被去除 BOM。

## 15. os.chdir() 线程不安全
- 发现问题：FAISS 读写用 `os.chdir()`，并发请求互相干扰进程 CWD。
- 解决思路：用全局 `threading.Lock` 串行化 `chdir → 操作 → 恢复`。
- 解决步骤：`store_faiss.py` 顶部定义锁，在 `save()/load()` 的 `chdir` 区域加锁。
- 解决结果：并发场景索引读写稳定，无目录争用。

## 16. Mermaid 思维导图 PNG 导出分辨率低
- 发现问题：点击"下载 PNG"导出的图片模糊，实际尺寸与屏幕渲染框一致，远低于预期。
- 解决思路：`getBoundingClientRect()` 只取 CSS 像素尺寸，受页面缩放影响；应改为读取 SVG 原生 `viewBox`，再以 3× 倍率进行超采样。
- 解决步骤：前端 Mermaid 下载 JS 改为解析 SVG `viewBox` 取自然宽高，Canvas 以 `width×3`/`height×3` 创建，`drawImage` 填满后按原始尺寸导出 PNG。
- 解决结果：导出 PNG 清晰度提升约 3 倍，边缘锐利，适合打印。

## 17. 练习模式评分结果未写入记忆库
- 发现问题：练习结束后 `memory_search` 工具查不到最近错题；对话保存到 `practices/` 文件，但 SQLite 记忆库无新记录。
- 解决思路：`run_practice_mode_stream` 通过内联 LLM 调用评分，未经过 `GraderAgent`，因此从未调用记忆写入逻辑。
- 解决步骤：在 `runner.py` 新增 `_save_grading_to_memory()`，用正则提取评分结果中的得分，保存 `practice`/`mistake` episode，并调用 `update_weak_points()` 及 `record_practice_result()`；在 `_is_practice_grading()` 判断为真后调用。
- 解决结果：练习评分完成后记忆库同步更新，`memory_search` 可检索历史错题。

## 18. 考试模式评分结果未写入记忆库
- 发现问题：考试模式同样使用内联批改，`exams/` 文件正常保存但记忆库无记录，薄弱知识点不被追踪。
- 解决思路：与 Bug 17 同源，考试批改结果也绕过了记忆写入。
- 解决步骤：新增 `_save_exam_to_memory()`，从考试报告文本中提取总分和薄弱知识点，以 `exam` 类型写入 `episodes` 表；在 `_is_exam_grading()` 判定后调用。
- 解决结果：考试结束后记忆库更新，支持后续针对薄弱点出题。

## 19. runner.py 语法错误导致服务启动失败
- 发现问题：修改 `runner.py` 后服务无法启动，报 `SyntaxError: unexpected EOF`；`def _is_exam_grading` 方法头部丢失。
- 解决思路：`multi_replace_string_in_file` 的某次替换匹配范围过大，意外删除了方法定义行。
- 解决步骤：在 `_is_exam_grading` 方法体上方补回 `def _is_exam_grading(self, text: str) -> bool:` 一行。
- 解决结果：服务正常启动，语法错误消除。

## 20. 练习模式评分结果不准确（未逐题对照）
- 发现问题：LLM 以"印象式"对比压缩后的答案字符串，出现同样答案被判错、或错误答案被判对的情况；多选题尤为突出。
- 解决思路：要求 LLM 在给出评分前必须先逐题输出对照表，再按公式计算得分，避免跳步直接打分。
- 解决步骤：更新 `prompts.py` 中的 `PRACTICE_PROMPT`，增加"Step-1 强制输出对照表 `| 题号 | 标准答案 | 学生答案 | 结果 |`"规则，在对照表完成后才允许计算总分。
- 解决结果：评分准确率显著提升，对照表可供用户直观核查。

## 21. FAISS 索引检测逻辑错误（始终认为无索引）
- 发现问题：侧边栏"文件与索引"面板始终显示"索引未构建"，即使已成功 build-index；"删除索引"按钮无效。
- 解决思路：FAISS 以平铺文件形式保存为 `{path}.faiss` + `{path}.pkl`，而非目录；原代码用 `os.path.isdir()` 检测必然为 False。
- 解决步骤：`api.py` 中检测逻辑改为 `os.path.exists(f"{index_path}.faiss")`；删除逻辑改为 `os.remove()` 分别删除两个文件，替换原来的 `shutil.rmtree()`。
- 解决结果：索引状态正确显示，构建/重建/删除操作均生效。
