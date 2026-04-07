以下是整个对话从头到尾所有调试过程的梳理，按时间顺序排列。

> 当前状态说明（2026-04）：本文档仍以“历史问题档案”为主，部分条目对应旧架构或旧口径。当前实现请以 `README.md` 与 `docs/guides/architecture.md` 为准。最近阶段已经完成的关键变化包括：rolling summary、`history_summary_state`、Router query rewrite、learn 模式默认不写普通 `qa`、`qa_summary` 异步归档。

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

## 22. 流式状态停在“检索历史记忆”
- 发现问题：前端长期显示 `memory_search` 状态，之后突然直接出最终答案，用户感知为“卡住”。
- 解决思路：工具执行后必须有“继续推理”状态，且后端长等待期间要有心跳事件。
- 解决步骤：
  - `openai_compat.py::chat_stream_with_tools` 在每轮工具执行后发送 `工具调用完成，继续推理中...`；
  - `backend/api.py::/chat/stream` 增加 SSE 心跳状态（默认每 8 秒）。
- 解决结果：前端状态链路闭环，长耗时时仍可见“正在处理”反馈。

## 23. API 入口缺少请求级日志，跨层定位困难
- 发现问题：日志分散在 runner/llm，缺少统一请求入口和耗时汇总。
- 解决思路：在 API 层增加 request_id + trace_id 结构化日志，并记录首包/总耗时。
- 解决步骤：
  - `/chat`、`/chat/stream` 增加 `request.start/request.done/request.error`；
  - 注入 `trace_scope`，将 `request_id` 写入 trace meta，便于串联下游日志。
- 解决结果：可按同一 `request_id` 追踪 API→Runner→LLM/tool 全链路。

## 24. 根目录和 perf 目录历史临时文件堆积
- 发现问题：根目录残留调试脚本，`data/perf_runs` 混入 smoke 目录，影响可读性。
- 解决思路：按“归档优先，不硬删除”清理。
- 解决步骤：
  - 调试文件移动到 `tools/debug_archive/20260315_v2/`；
  - 非 canonical perf 结果移动到 `data/perf_runs/_archive/20260315_v2_cleanup/`；
  - 两处均生成 `ARCHIVE_INDEX.md`。
- 解决结果：主目录只保留 canonical baseline/after 和正式运行入口。

## 25. 上下文预算角标长期显示 0%
- 发现问题：前端预算角标偶发长期显示 `0%`，但后端实际已做预算裁剪。
- 解决思路：明确“预算事件只在流式链路发出”，并确保 learn/practice/exam 三模式统一发事件；前端加超时提示兜底。
- 解决步骤：
  - `runner.py`：在三种 `run_*_mode_stream` 中统一发送 `__context_budget__`，非流式路径不再混入该事件；
  - `streamlit_app.py`：增加“预算事件超时未到达”提示，避免用户误判卡死。
- 解决结果：角标随请求更新，若事件延迟也有明确提示，不再固定 `0%`。

## 26. 练习多题出题与评卷链路错配
- 发现问题：练习请求多题时仍按单题 schema 解析，导致选择题形态漂移、解析失败、评卷错路由。
- 解决思路：practice 多题统一走试卷生成链路，并依据元数据决定评卷入口。
- 解决步骤：
  - `runner.py`：`num_questions > 1` 时走 `QuizMaster.generate_exam_paper()`，写入 `exam_meta`；
  - 练习提交答案时：若历史含 `exam_meta`，走 `Grader.grade_exam_stream()`，否则走 `grade_practice_stream()`；
  - `quizmaster.py`：增加题型锁定、选择题形态校验+单次重试、答题卡总分归一到 100。
- 解决结果：练习多题链路稳定性提升，题型漂移与评分口径错配显著减少。

## 27. rolling summary 上线后 `history_len` 被误读成“轮数”
- 发现问题：前端预算窗口里的 `history_len` 实际是“当前发送给后端的 message 窗口大小”，不是 `user + assistant` 对话轮数；rolling summary 生效后，这个数字封顶在 `20`，容易被误判为“摘要没触发”。
- 解决思路：明确区分 `message window` 与 `turns`，并在预算窗口显示 rolling summary 状态，而不是继续沿用旧字段语义。
- 解决步骤：
  - 前端上下文窗口改为显示 `X msgs (~Y turns)`；
  - 当窗口达到前端发送上限时，显式提示“前端发送窗口已封顶”；
  - 预算卡片增加 rolling summary 指标，避免继续用旧 `history_llm_compress` 字段描述新机制。
- 解决结果：长对话时可以正确判断 rolling summary 是否生效，不再把 `history_len=20` 误读为“只有 20 轮”。

## 28. 预算窗口继续显示旧 `history_llm_compress`，与 rolling summary 不一致
- 发现问题：历史压缩主路径已经从“每次请求重算 old history 摘要”切到 rolling summary block，但前端预算窗口仍显示旧字段 `history_llm_compress`，造成“明明已经 summary_source=state，界面却显示未触发压缩”的错觉。
- 解决思路：把展示字段切换到 rolling summary 相关指标，保留旧字段仅作 fallback 兼容。
- 解决步骤：
  - 前端预算窗口改为优先展示 `summary_source=state`、`history_summary_block_count`、`history_block_compress_ms`；
  - 旧 `history_llm_compress` 字段仅在 fallback 路径下显示。
- 解决结果：预算窗口展示和当前代码路径对齐，能够正确反映 block-based summary 的实际状态。

## 29. learn 模式普通问答写入过多 `qa`，长期记忆噪声累积
- 发现问题：早期 learn 模式普通问答默认写入 `qa`，随着聊天轮次增加，长期记忆里会堆积大量低价值问答，影响 `memory_search` 相关性，也增加后续归档成本。
- 解决思路：把 learn 记忆写入收敛成“显式长期记忆请求才写”，普通问答默认不入库。
- 解决步骤：
  - `runner.py` 增加 `_should_persist_learn_episode()`；
  - 当前通过规则匹配识别“记住/下次提醒/以后按这个偏好”等表达；
  - 命中时写入 `qa`，并打上 `explicit_memory_request=True`。
- 解决结果：learn 主链路不再自动堆积低价值 `qa`，长期记忆噪声显著下降。

## 30. 旧 `qa` 只做首行拼接归档，摘要质量不足
- 发现问题：早期 `qa` 归档只是抽首行做规则拼接，虽然便宜，但表达能力有限，难以保留用户偏好、薄弱点和稳定事实。
- 解决思路：把旧 `qa` 归档升级为“异步 LLM 摘要优先，规则摘要回退”。
- 解决步骤：
  - `memory/manager.py` 新增 LLM 版 `qa_summary` 生成逻辑；
  - 最近 `50` 条原始 `qa` 保留，每批 `20` 条低价值旧 `qa` 归档成一条 `qa_summary`；
  - 仅 `importance <= 0.55` 且非 `explicit_memory_request` 的 `qa` 参与归档；
  - LLM 失败时回退到规则摘要，保证归档链路不中断。
- 解决结果：长期记忆检索优先命中更稳定、可读性更好的 `qa_summary`，同时保留异步和低侵入特性。


