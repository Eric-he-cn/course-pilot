# 多教材 Wiki e2e 评测

一门课上传 4 份互有重合的教材切片，对比**知识页开 / 关**两臂的回答质量。
判据全确定性，不用 LLM judge。

脚本只通过 HTTP 打后端，不 import 仓库代码；python 一律用仓库的 `.venv/bin/python`。

| 文件 | 干什么 |
| --- | --- |
| `common.py` | HTTP 客户端、SSE 解析、文本归一化（判据口径的唯一实现） |
| `run_arm.py` | 跑一臂：建课 → 上传索引 4 份切片 →（W 臂）建知识页 → 逐题提问 → 落 JSONL |
| `judge.py` | 离线判 JSONL，出汇总表与对比表；`--self-test` 是判据自己的 A/B |
| `probe.py` | W 臂构建后的三条前置检查，全绿了才值得把 20 题 × 3 轮跑完 |

语料（教材切片 + 题目集）不在仓库里——原书 PDF 合计约 92 MB。下面的路径示例
统一假设语料放在 `testdata/e2e-wiki/`（`testdata/` 在 `.gitignore` 里）。

---

## 语料准备

### 1. 下载原书（5 份开源 PDF，全部有书签、有文字层）

| 文件 | 页数 | 来源 |
| --- | --- | --- |
| `ml-notes-fengdu78.pdf` | 336 | `https://raw.githubusercontent.com/fengdu78/Coursera-ML-AndrewNg-Notes/master/机器学习个人笔记完整版v5.52-A4打印版.pdf` |
| `dl-notes-fengdu78.pdf` | 799 | `https://raw.githubusercontent.com/fengdu78/deeplearning_ai_books/master/Deeplearning深度学习笔记v5.72.pdf` |
| `d2l-zh-pytorch.pdf` | 827 | `https://github.com/d2l-ai/d2l-zh/releases/download/v2.0.0/d2l-zh-pytorch-2.0.0.pdf` |
| `llm-cookbook.pdf` | 373 | `https://github.com/datawhalechina/llm-cookbook/releases/download/v1%2C0%2C0/LLM-v1.0.0.pdf` |
| `happy-llm.pdf` | 171 | `https://github.com/datawhalechina/happy-llm/releases/download/v1.0.2/Happy-LLM-0727.pdf` |

均为作者自己 release / 维护的开源教材或笔记。下载后用 pypdf 抽两页正文确认能抽出成句中文
（有的开源 PDF 文字层是乱码，索引不可用）。llm-cookbook 是备用重合源，当前切片方案没用到它。

### 2. 切片（`slices/*.pdf`，4 份，主题互有重合）

用 pypdf 按下表页码区间切，**必须用 `writer.append(reader, pages=(start, stop),
import_outline=True)`**——逐页 `add_page` 会把书签丢光，而 W 臂的目录质量全靠书签：

| 源文件 | 切片名 | 0-indexed 半开区间 | 页数 |
| --- | --- | --- | --- |
| ml-notes-fengdu78.pdf | `ml-notes-slice.pdf` | (22,37) (43,55) (116,154) | 65 |
| dl-notes-fengdu78.pdf | `dl-notes-slice.pdf` | (116,136) (149,169) (185,194) (358,387) (546,557) | 89 |
| d2l-zh-pytorch.pdf | `d2l-slice.pdf` | (146,159) (168,182) (238,261) (336,350) (432,456) | 88 |
| happy-llm.pdf | `happy-llm-slice.pdf` | (6,57) | 51 |

区间选的是重合主题：梯度下降 / 神经网络基础（4 份重合）、正则化与 dropout、CNN、
RNN / 序列模型、注意力 / Transformer（各 2–3 份重合）。切完核对页数与书签条数。

### 3. 题目集（`e2e_wiki_dataset.yaml`，20 条，手工标注）

题目集是对着上面的切片**手工标定**的，不能机械重新生成；切片区间一变，所有页码锚点作废。
结构约定（判据脚本按这个读，写在题目集头部注释里，与 `common.py` 的归一化口径逐条对应）：

- `samples[]`：`id` / `kind`（`cross_source` 12 条、`single_source` 8 条）/ `topic` / `question`；
- `must_contain[]`：`pattern`（对归一化后回答做 `re.search`）+ `note`；
- `attribution[]`：`document` + `pages`（**切片内页位置**，1 起；逐页用 pypdfium2 抽文字确认，
  不按书签页码或常识推断填）；
- `conflate_pairs[]`：`a`/`a_doc`/`b`/`b_doc`/`both_ok_if`，两本书记号互斥的点。

---

## 起两臂实例

**开发实例（默认 8000 / 5173）不要碰。** 评测另起端口与数据目录。

评测不需要界面，可以直接起 uvicorn（参数与 `scripts/dev.sh` 里那行一致，不带 `--reload`）：

```bash
# R 臂（关知识页）
STORAGE_DATA_DIR=testdata/e2e-wiki/data-R \
  .venv/bin/python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8002

# W 臂（开知识页），另开一个终端
STORAGE_DATA_DIR=testdata/e2e-wiki/data-W \
  .venv/bin/python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8003
```

要用 `dev.sh` 也行（会顺带起前端，多占一个端口）：

```bash
CP_PORT_OFFSET=2 STORAGE_DATA_DIR=testdata/e2e-wiki/data-R ./scripts/dev.sh   # 8002 + 5175
CP_PORT_OFFSET=3 STORAGE_DATA_DIR=testdata/e2e-wiki/data-W ./scripts/dev.sh   # 8003 + 5176
```

**两臂必须是两个数据目录。** 同一个目录里 R 臂会读到 W 臂建的知识页。`run_arm.py --arm R`
有一道闸：课程上开着 `wiki_enabled` 就直接退出。

真模型走仓库 `.env` 里已有的配置，不用另外传 key。确认接的是远端而不是演示模式：

```bash
curl -s http://127.0.0.1:8002/api/v2/health | python3 -m json.tool | grep -A3 '"llm"'
# mode 应该是 openai_compatible 之类，不是 demo_fallback
```

---

## 跑的顺序

```bash
CORPUS=testdata/e2e-wiki

# 0. 判据先自检，别拿没验过的判据去判一次几十块钱的跑批
.venv/bin/python scripts/e2e_wiki/judge.py --self-test                     # 37/37

# 1. R 臂（关知识页）
.venv/bin/python scripts/e2e_wiki/run_arm.py --arm R --base http://127.0.0.1:8002 \
    --data-dir $CORPUS/data-R --dataset $CORPUS/e2e_wiki_dataset.yaml \
    --slices $CORPUS/slices --repeat 3 --out $CORPUS/out/R.jsonl

# 2. W 臂先只建知识页，跑三条 probe，绿了再往下
.venv/bin/python scripts/e2e_wiki/run_arm.py --arm W --base http://127.0.0.1:8003 \
    --data-dir $CORPUS/data-W --dataset $CORPUS/e2e_wiki_dataset.yaml \
    --slices $CORPUS/slices --repeat 3 --out $CORPUS/out/W.jsonl --setup-only
.venv/bin/python scripts/e2e_wiki/run_arm.py --arm W --base http://127.0.0.1:8003 \
    --data-dir $CORPUS/data-W --dataset $CORPUS/e2e_wiki_dataset.yaml \
    --slices $CORPUS/slices --repeat 3 --out $CORPUS/out/W.jsonl --resume --limit 3
.venv/bin/python scripts/e2e_wiki/probe.py --wiki-json $CORPUS/out/W-wiki.json \
    --jsonl $CORPUS/out/W.jsonl --dataset $CORPUS/e2e_wiki_dataset.yaml \
    --data-dir $CORPUS/data-W --base http://127.0.0.1:8003

# 3. probe 绿了再把 W 臂跑满
.venv/bin/python scripts/e2e_wiki/run_arm.py --arm W --base http://127.0.0.1:8003 \
    --data-dir $CORPUS/data-W --dataset $CORPUS/e2e_wiki_dataset.yaml \
    --slices $CORPUS/slices --repeat 3 --out $CORPUS/out/W.jsonl --resume

# 4. 判定
.venv/bin/python scripts/e2e_wiki/judge.py --dataset $CORPUS/e2e_wiki_dataset.yaml \
    --jsonl R=$CORPUS/out/R.jsonl --jsonl W=$CORPUS/out/W.jsonl \
    --detail --json $CORPUS/out/judge.json
```

**账单**：20 题 × 3 轮 × 2 臂 = 120 轮真模型对话。W 臂建知识页另算，四份切片预估在
150–250 次模型调用之间，`run_arm.py` 会先把估算打出来，超过 `--wiki-budget`（默认 400）
就直接退出不建。

---

## 注意事项

- **`--resume` 按 `(sample_id, run)` 跳过，只跳过 `ok: true` 的那些**，失败的会重试。
  不加 `--resume` 而 out 文件已存在时脚本直接拒绝，不覆盖。
- **每题一个新会话**，避免后面的题读到前面的回答。
- 建课与上传索引都是幂等的（按课程名、按文件名），中断重跑不会重复上传。
- **知识页构建也幂等**：`wiki.py` 按 `source_hash` 逐节比对，内容没变的页一次模型调用都不发。
  但 `run_arm.py` 在 `<out>-wiki.json` 已存在时**默认不再走一遍**——重跑会把「这一批实际写了
  多少页」换成一排 `skipped`，probe 的页数对账就对的不是真账了。真要重建加 `--force-wiki`。
- **SSE 有帧解析不出来时原文 dump 到 `<out 同目录>/<out 名>-raw/<tag>.sse`**，不吞。
- `judge.py --scope` 两个口径：`cited`（默认，正文里真标了 `[n]` 的引用）和 `retrieved`
  （本轮登记过的全部）。模型不标编号时 `cited` 会空，那时要看 `retrieved` 才分得清
  「没检索到」和「检索到了没标」。两个都看一遍。
- 判据改动后先跑 `judge.py --self-test`（带条数闸，跳过分支会被抓出来）。
- 用 demo provider（`COURSEPILOT_ENABLE_REMOTE_LLM=0`）可以先冒烟验链路，但 demo 回答是
  固定模板，知识页正文会被 `rag_min_rerank_score` 滤掉（wiki 引用 0 条是预期），
  验的是链路与判据代码，不是回答质量。

---

## 席位可调性（R+ 对照臂靠它）

**教材席位可以用环境变量调大，知识页席位不行。**

| | 常量 | 活路径 | 能不能用环境变量改 |
| --- | --- | --- | --- |
| 教材 | `agent/tools.py` `SEARCH_LIMIT = 6` | `settings.top_k_results` → `TurnService(search_limit=)` → `ToolExecutor(search_limit=)` | **能**，`RAG_TOP_K_RESULTS` |
| 知识页 | `agent/tools.py` `WIKI_SEARCH_LIMIT = 2` | `_search()` 里直接读模块常量，没有注入点 | **不能**，改它要动代码 |

这对 R+ 对照臂的含义：

- W 臂每轮拿到 6 条教材 + 2 页知识页；R 臂只有 6 条教材。两臂的证据总量不等，
  W 臂如果赢了，分不清是「知识页有用」还是「多给了两段证据」。
- R+ 对照臂用 `RAG_TOP_K_RESULTS=8` 起一个第三实例即可，不需要改代码——
  8 条教材 + 0 页知识页，证据条数与 W 臂对齐。
- 「知识页给 0 席」的反向对照不用改代码：R 臂课程不开 `wiki_enabled`，
  `search_wiki` 本来就返回空。
- 另有一条会影响两臂可比性的：`rag_min_rerank_score`（`RAG_MIN_RERANK_SCORE`，默认 0.3）
  是按教材片段标定的，现在也在管知识页这种概括语言的门槛。真模型下知识页正文有实义，
  未必触发，但**这是一个未标定的旋钮，两臂比出来的差异里有它一份**。
