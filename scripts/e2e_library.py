"""端到端：资料库那条链路。建课 → 上传 → 索引 → 开知识页 → 构建 → 读回。

与 e2e_journey / e2e_multiturn 的分工：那两个从头到尾都在 sessions/turns 里，
这一条一句话都不问。界面上「解析到 Wiki」那个按钮走的就是这里，此前没有自动化点过它。

判据落在**分片**上，不落在页码上：几段各查几页就能凑满整本书，页码粒度看不出漏读。

已知坑：上传只落盘，索引要另起 `POST /materials/{id}/index` 再轮询 job，
漏了这步教材会一直停在 `index_status: uploaded`，后面全都静默地查不到东西。

用法（`--data-dir` 要和实例的 `STORAGE_DATA_DIR` 是同一个，脚本会直接读那份 SQLite）：

    CP_PORT_OFFSET=5 STORAGE_DATA_DIR=testdata/lib5 ./scripts/dev.sh
    .venv/bin/python scripts/e2e_library.py --base http://127.0.0.1:8005 --data-dir testdata/lib5
    .venv/bin/python scripts/e2e_library.py --ui --web http://127.0.0.1:5178   # 再点一遍界面按钮

成本：知识页每页一次模型调用。默认给两份十来页的切片和孤儿清理那段的两份小教材真写页，
实测 26 次，`--ui` 再加 2 次；大切片只索引不写页，「不漏」直接调 `plan_sections` 这个
纯函数验，一次调用都不花。`--build-big` 才会真给大切片写页（实测 51 次），跑之前先算页数，
超过 `--max-model-calls` 就不跑。

每段开头都会先把同名课程删掉重建，所以重复跑不会踩上一次的残留。
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "testdata" / "fixtures"
sys.path.insert(0, str(ROOT / "backend"))

from core.identity import sole_workspace  # noqa: E402
from core.store import SQLiteStore  # noqa: E402
from modules.knowledge.repository import KnowledgeRepository  # noqa: E402
from modules.knowledge.wiki import WIKI_MAX_NODES, plan_sections  # noqa: E402

# 大切片：d2l 第 4 章，66 页 99 条书签 88 个概念，节点上限与树深度都压得到。
BIG = "深度学习-多层感知机.pdf"
# 有书签的小切片：十来页，够生成多层结构，真写页也只要十几次调用。
OUTLINED = "深度学习-批量规范化.pdf"
# 原书零书签：走按分片顺序切段那条路，多数真实教材（讲义、扫描件）都在这条路上。
FLAT = "os-cpu-scheduling.pdf"

# 孤儿清理与格式那两段用手写的小教材：一份一页，一次构建两次调用，跑得起。
LIMIT_MD = ("# 极限\n\n极限描述函数在某一点附近的趋势：自变量趋近某个值时，函数值稳定地靠近一个数。\n"
            "判断极限是否存在要看左右极限相不相等。\n")
CONTINUITY_MD = ("# 连续性\n\n连续性建立在极限之上：函数在某点连续，指的是这一点的极限值等于函数值。\n"
                 "间断点分成可去、跳跃与无穷三类。\n")
SCHEDULING_MD = ("# CPU 调度\n\n先来先服务按到达顺序执行，长作业排在前面时会产生护航效应。\n"
                 "时间片轮转把 CPU 按固定时长切给每个任务，响应时间变好，周转时间变差。\n")

results: list[tuple[str, bool, str]] = []
model_calls = 0


# ---- HTTP ----

def call(base: str, path: str, payload: dict | None = None, method: str | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{base}/api/v2{path}", data=data, method=method or ("POST" if data is not None else "GET"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=900) as response:
        body = response.read().decode()
    return json.loads(body) if body else {}


def call_error(base: str, path: str, payload: dict | None = None) -> tuple[int, str]:
    """期望失败的请求。错误体里 error 在顶层、detail 是消息串，两种形状都认。"""
    try:
        call(base, path, payload)
    except urllib.error.HTTPError as error:
        body = json.loads(error.read().decode() or "{}")
        nested = body.get("error") if isinstance(body.get("error"), dict) else body
        return error.code, str(nested.get("code", ""))
    return 200, ""


def upload_bytes(base: str, course_id: str, name: str, payload: bytes, content_type: str) -> dict:
    boundary = "----coursepilot-library"
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'.encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        payload, b"\r\n", f"--{boundary}--\r\n".encode(),
    ])
    request = urllib.request.Request(
        f"{base}/api/v2/courses/{course_id}/materials", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(request, timeout=900) as response:
        return json.loads(response.read().decode())


def upload(base: str, course_id: str, path: Path) -> dict:
    return upload_bytes(base, course_id, path.name, path.read_bytes(), "application/pdf")


def docx_bytes(paragraphs: list[str]) -> bytes:
    """最小可解析的 docx。后端拆 OOXML 用的是标准库，这里同样不引第三方依赖。"""
    namespace = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml",
                         f'<?xml version="1.0"?><w:document {namespace}><w:body>{body}</w:body></w:document>')
    return buffer.getvalue()


def wait_job(base: str, job_id: str, *, timeout: int = 1800) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = call(base, f"/jobs/{job_id}")
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(2)
    return {"status": "timeout", "error_message": f"{timeout}s 内没进终态"}


def check(name: str, condition: bool, detail: str = "") -> bool:
    results.append((name, bool(condition), detail))
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f" — {detail}" if detail and not condition else ""))
    return bool(condition)


def info(text: str) -> None:
    print(f"  INFO  {text}")


# ---- 落库读回 ----

def workspace(data_dir: Path) -> Path:
    return sole_workspace(data_dir)


def db(data_dir: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(workspace(data_dir) / "coursepilot.db", timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def repository(data_dir: Path) -> KnowledgeRepository:
    """读概念树与分片走真实的仓储查询，别在脚本里另抄一份 SQL——那样两边会各自漂。"""
    return KnowledgeRepository(SQLiteStore(workspace(data_dir) / "coursepilot.db"))


def fresh_course(base: str, name: str) -> dict:
    """同名课程先删掉再建：删课会连教材、分片和知识页目录一起清，脚本才能重复跑。"""
    for course in call(base, "/courses"):
        if course["name"] == name:
            call(base, f"/courses/{course['id']}", None, method="DELETE")
    return call(base, "/courses", {"name": name})


def index_material(base: str, material: dict, label: str) -> dict:
    job = wait_job(base, call(base, f"/materials/{material['id']}/index", {})["id"])
    if job["status"] != "completed":
        raise SystemExit(f"{label} 索引失败：{job.get('error_message')}")
    return material


def install(base: str, course_id: str, filename: str) -> dict:
    """上传 + 索引。上传只落盘，不另起索引作业教材会停在 uploaded。"""
    return index_material(base, upload(base, course_id, FIXTURES / filename), filename)


def install_bytes(base: str, course_id: str, name: str, payload: bytes, content_type: str) -> dict:
    return index_material(base, upload_bytes(base, course_id, name, payload, content_type), name)


def build_wiki(base: str, material_id: str, label: str) -> dict[str, int]:
    """跑一次知识页构建，返回覆盖率提示解析出来的字段，并记账模型调用。

    一页一次调用，所以 `written` 就是这次花掉的次数。`empty` 那几条分两种情形
    （证据为空压根没调、调了模型没吐字），分不出来，出现了就单独报一句。
    """
    global model_calls
    job = wait_job(base, call(base, f"/materials/{material_id}/wiki", {})["id"])
    summary = job.get("error_message") or ""
    if job["status"] != "completed":
        raise SystemExit(f"{label} 知识页构建失败：{summary}")
    fields = coverage_fields(summary)
    model_calls += fields.get("written", 0)
    info(f"{label} {summary}")
    if fields.get("empty"):
        info(f"  另有 {fields['empty']} 页无产出，其中调用了模型的那几次没计进账")
    return fields


def coverage_fields(summary: str) -> dict[str, int]:
    if not summary.startswith("wiki_coverage "):
        return {}
    out = {}
    for item in summary.split()[1:]:
        key, _, value = item.partition("=")
        if value.isdigit():
            out[key] = int(value)
    return out


def wiki_documents(base: str, course_id: str) -> dict[str, str]:
    """整门课的知识页原文（含 frontmatter）。source_refs 记着这一页读了哪些分片。"""
    return {page["concept_id"]: call(base, f"/courses/{course_id}/wiki/{page['concept_id']}")["content"]
            for page in call(base, f"/courses/{course_id}/wiki")["pages"]}


def frontmatter(raw: str) -> dict[str, str]:
    head = raw.split("\n---\n", 1)[0]
    return {match.group(1): match.group(2).strip()
            for match in re.finditer(r"^([a-z_]+):[ \t]*(.*)$", head, re.MULTILINE)}


def source_refs(raw: str) -> list[str]:
    """frontmatter 里这一页读过的出处。叶子页是「教材.pdf p.3 #chunk_xxx」，中间页是子页名。"""
    block = raw.split("source_refs:", 1)[1].split("\n---\n", 1)[0] if "source_refs:" in raw else ""
    return [line.strip()[2:] for line in block.splitlines() if line.strip().startswith("- ")]


def read_chunk_ids(raw: str) -> set[str]:
    return {match.group(1) for ref in source_refs(raw) if (match := re.search(r"#(\S+)", ref))}


def material_chunk_ids(data_dir: Path, material_id: str) -> set[str]:
    with db(data_dir) as connection:
        return {row["id"] for row in connection.execute(
            "SELECT id FROM chunks WHERE material_id = ? AND source_kind = 'chunk'", (material_id,))}


def plan_from_db(data_dir: Path, material_id: str, *, max_nodes: int):
    store = repository(data_dir)
    return plan_sections(
        material_id=material_id,
        concepts=store.list_material_concept_tree(material_id=material_id),
        chunks=store.list_material_chunks(material_id=material_id),
        max_nodes=max_nodes,
    )


def chunks_read_by_plan(data_dir: Path, material_id: str, *, max_nodes: int) -> tuple[int, int, dict]:
    sections, stats = plan_from_db(data_dir, material_id, max_nodes=max_nodes)
    read = {chunk["id"] for section in sections for chunk in section.chunks}
    total = len(repository(data_dir).list_material_chunks(material_id=material_id))
    return len(read), total, {**stats, "sections": len(sections)}


# ---- 各段 ----

def stage_index(base: str, data_dir: Path) -> tuple[str, str]:
    """大切片：建课 → 上传 → 索引。这一段不花模型额度，向量是本地算的。"""
    print("\n[1] 建课、上传、索引（大切片）")
    course = fresh_course(base, "深度学习·多层感知机（e2e）")
    material = upload(base, course["id"], FIXTURES / BIG)
    listed = next(item for item in call(base, f"/courses/{course['id']}/materials")
                  if item["id"] == material["id"])
    check("上传只落盘，索引没自动跑", listed["index_status"] == "uploaded", listed["index_status"])

    status, code = call_error(base, f"/materials/{material['id']}/wiki", {})
    check("课程没开知识页就建会被挡住", (status, code) == (409, "feature_disabled"), f"{status} {code}")
    patched = call(base, f"/courses/{course['id']}", {"wiki_enabled": True}, method="PATCH")
    check("课程开关打得开", patched["wiki_enabled"] is True, str(patched))
    status, code = call_error(base, f"/materials/{material['id']}/wiki", {})
    check("没索引就建知识页会被挡住", (status, code) == (409, "material_not_indexed"), f"{status} {code}")

    job = wait_job(base, call(base, f"/materials/{material['id']}/index", {})["id"])
    check("索引作业跑完", job["status"] == "completed", str(job.get("error_message")))
    listed = next(item for item in call(base, f"/courses/{course['id']}/materials")
                  if item["id"] == material["id"])
    check("索引产出切块与向量",
          (listed.get("chunk_count") or 0) > 0 and (listed.get("embedded_count") or 0) > 0,
          f"chunks={listed.get('chunk_count')} embedded={listed.get('embedded_count')}")

    tree = repository(data_dir).list_material_concept_tree(material_id=material["id"])
    levels = sorted({row["level"] for row in tree if row["level"] is not None})
    check("书签的层级落进了概念表", len(levels) >= 3 and any(row["parent_id"] for row in tree),
          f"层级 {levels}，有父节点的 {sum(1 for row in tree if row['parent_id'])} 条")
    info(f"{BIG}：{listed.get('chunk_count')} 个分片，{len(tree)} 个概念")
    return course["id"], material["id"]


def stage_no_loss(data_dir: Path, material_id: str) -> None:
    """判据一：这份教材的每个分片都要被某个 section 读到，任何节点上限下都是。

    这一段是纯函数验证——`plan_sections` 决定了哪些原文会进知识页，
    真写页只是把它读到的内容交给模型，漏不漏在切段这一步就定死了。
    """
    print("\n[2] 不漏：大切片在各档节点上限下都不漏分片")
    sections, _stats = plan_from_db(data_dir, material_id, max_nodes=WIKI_MAX_NODES)
    for cap in (4, 8, 20, WIKI_MAX_NODES):
        read, total, stats = chunks_read_by_plan(data_dir, material_id, max_nodes=cap)
        check(f"节点上限 {cap}：分片一个不漏", read == total, f"读到 {read}/{total}")
        check(f"节点上限 {cap}：页数没超过上限", stats["sections"] <= cap, f"切出 {stats['sections']} 页")
        info(f"  上限 {cap} → {stats['sections']} 页，候选 {stats['candidates']}，"
             f"并掉/砍掉 {stats['capped']}")

    # 低上限那几档才是截断路径的判据。默认上限是跑飞的兜底，顶到它反而说明它压了教材结构。
    check("低上限那几档确实砍到了东西", len(sections) > 20,
          f"这份教材自然切出 {len(sections)} 页，不超过 20 就压不到截断")
    check("默认上限装得下这份教材", len(sections) < WIKI_MAX_NODES,
          f"{len(sections)} 页顶到了 {WIKI_MAX_NODES}，默认值在替教材决定该分几节")
    check("树是多层的", len({section.level for section in sections}) >= 2,
          str(sorted({section.level for section in sections})))


def stage_legacy_shape(data_dir: Path, material_id: str) -> None:
    """判据三：层级三列加上之前索引的旧库，构建仍然不漏内容。

    老库的概念行没有 level/parent_id/ordinal，`plan_sections` 取不到目录，
    改走按分片顺序切段那条路。作者拿真书踩到的就是这个形态。
    """
    print("\n[3] 老数据形态：层级三列为空时不漏")
    with db(data_dir) as connection:
        saved = [dict(row) for row in connection.execute(
            "SELECT id, level, parent_id, ordinal FROM concepts WHERE material_id = ?", (material_id,))]
        connection.execute(
            "UPDATE concepts SET level = NULL, parent_id = NULL, ordinal = NULL WHERE material_id = ?",
            (material_id,))
        connection.commit()
    try:
        tree = repository(data_dir).list_material_concept_tree(material_id=material_id)
        check("三列确实清空了", all(row["level"] is None for row in tree), f"{len(tree)} 个概念")
        for cap in (4, WIKI_MAX_NODES):
            read, total, stats = chunks_read_by_plan(data_dir, material_id, max_nodes=cap)
            check(f"老形态 · 节点上限 {cap}：分片一个不漏", read == total, f"读到 {read}/{total}")
            info(f"  上限 {cap} → {stats['sections']} 页（没有目录，按分片顺序切段）")
    finally:
        with db(data_dir) as connection:
            for row in saved:
                connection.execute("UPDATE concepts SET level = ?, parent_id = ?, ordinal = ? WHERE id = ?",
                                   (row["level"], row["parent_id"], row["ordinal"], row["id"]))
            connection.commit()
    restored = repository(data_dir).list_material_concept_tree(material_id=material_id)
    check("测完把三列还原了", any(row["level"] is not None for row in restored),
          "还原失败会让后面的判据测的是另一个形态")


def stage_build(base: str, data_dir: Path) -> tuple[str, str]:
    """真写页：有书签的小切片，验层级、覆盖率提示、分片覆盖与读回。"""
    print("\n[4] 构建知识页并读回（有书签的小切片）")
    course = fresh_course(base, "深度学习·批量规范化（e2e）")
    call(base, f"/courses/{course['id']}", {"wiki_enabled": True}, method="PATCH")
    material = install(base, course["id"], OUTLINED)
    fields = build_wiki(base, material["id"], OUTLINED)

    check("覆盖率提示可解析",
          {"concepts", "pages", "written", "skipped", "merged", "pruned"} <= set(fields), str(fields))
    check("确实写出了页", fields.get("written", 0) > 0, str(fields))

    documents = wiki_documents(base, course["id"])
    check("报出的页数与真正落盘的页数一致", fields.get("pages") == len(documents),
          f"提示说 {fields.get('pages')} 页，读得回来 {len(documents)} 页")
    check("生成了课程首页", "index" in documents, str(sorted(documents)[:5]))
    levels = {int(frontmatter(raw).get("level") or 0) for cid, raw in documents.items() if cid != "index"}
    check("有书签的教材生成多层结构", max(levels, default=0) >= 1, f"层级 {sorted(levels)}")

    branches = {cid for cid in documents
                if any(frontmatter(other).get("parent_id") == cid for other in documents.values())}
    check("生成了带子页的中间页", bool(branches), str(sorted(documents)[:5]))
    bad = [cid for cid in branches if any(re.search(r"p\.\d+", ref) for ref in source_refs(documents[cid]))]
    check("中间页记的是子页不是页码", not bad, f"这几页的出处里出现了教材页码：{bad}")

    covered = set().union(*(read_chunk_ids(raw) for raw in documents.values())) if documents else set()
    total = material_chunk_ids(data_dir, material["id"])
    check("每个分片都被某一页读到", covered >= total,
          f"读到 {len(covered & total)}/{len(total)}，漏 {sorted(total - covered)[:5]}")

    body = call(base, f"/courses/{course['id']}/wiki/index")["content"]
    check("首页读得回来且带目录", "## 全部页面" in body, body[:120])
    with db(data_dir) as connection:
        wiki_rows = connection.execute(
            "SELECT count(*) c FROM chunks WHERE course_id = ? AND source_kind = 'wiki'",
            (course["id"],)).fetchone()["c"]
    check("知识页进了检索库（可被引用）", wiki_rows >= len(documents) - 1, f"{wiki_rows} 行")
    return course["id"], material["id"]


def stage_incremental(base: str, course_id: str, material_id: str) -> None:
    """判据五：证据没变就不重写。原地再建一次，一次模型调用都不该发生。"""
    print("\n[5] 增量：证据未变就不重写")
    before = model_calls
    fields = build_wiki(base, material_id, "重建 " + OUTLINED)
    check("一页都没重写", fields.get("written") == 0, str(fields))
    check("全部命中已有页", fields.get("skipped", 0) > 0, str(fields))
    check("没有额外的模型调用", model_calls == before, f"多花了 {model_calls - before} 次")
    check("页还在", len(call(base, f"/courses/{course_id}/wiki")["pages"]) == fields.get("pages"),
          str(fields))


def stage_flat(base: str, data_dir: Path) -> None:
    """判据二的另一半：原书零书签的教材平铺，且整条链路不崩。"""
    print("\n[6] 无书签教材：平铺且不漏")
    course = fresh_course(base, "操作系统·CPU 调度（e2e）")
    call(base, f"/courses/{course['id']}", {"wiki_enabled": True}, method="PATCH")
    material = install(base, course["id"], FLAT)

    tree = repository(data_dir).list_material_concept_tree(material_id=material["id"])
    check("这份教材确实没有目录书签", all(row["level"] is None for row in tree),
          f"{len(tree)} 个概念，带层级的 {sum(1 for row in tree if row['level'] is not None)} 个")

    fields = build_wiki(base, material["id"], FLAT)
    check("没有书签也写出了页", fields.get("written", 0) >= 2, str(fields))

    documents = wiki_documents(base, course["id"])
    check("报出的页数与真正落盘的页数一致", fields.get("pages") == len(documents),
          f"提示说 {fields.get('pages')} 页，读得回来 {len(documents)} 页")
    levels = {int(frontmatter(raw).get("level") or 0) for cid, raw in documents.items() if cid != "index"}
    check("没有书签就平铺", levels <= {0}, f"层级 {sorted(levels)}")
    covered = set().union(*(read_chunk_ids(raw) for raw in documents.values())) if documents else set()
    total = material_chunk_ids(data_dir, material["id"])
    check("每个分片都被某一页读到", covered >= total,
          f"读到 {len(covered & total)}/{len(total)}，漏 {sorted(total - covered)[:5]}")


def stage_orphans(base: str) -> None:
    """孤儿页清理 + 跨教材首页 + 连点两次。三件事共用一门装了两份教材的课。

    `WikiStore.prune` 的三条分支此前只有单测，所有跑法里 `pruned` 都是 0；`_write_index`
    跨教材聚合那条路也没跑过——每门课只装过一份教材，首页看着很对，其实只有那一份在里面。
    """
    global model_calls
    print("\n[7] 孤儿页清理、跨教材首页、连点两次")
    course = fresh_course(base, "知识页孤儿清理（e2e）")
    call(base, f"/courses/{course['id']}", {"wiki_enabled": True}, method="PATCH")
    doomed = install_bytes(base, course["id"], "极限.md", LIMIT_MD.encode(), "text/markdown")
    build_wiki(base, doomed["id"], "极限.md")
    kept = install_bytes(base, course["id"], "连续性.md", CONTINUITY_MD.encode(), "text/markdown")

    # 连点两次：两个作业都建出来再一起等，第二个必须全部命中已有页。
    jobs = [call(base, f"/materials/{kept['id']}/wiki", {})["id"] for _ in range(2)]
    finished = [wait_job(base, job_id) for job_id in jobs]
    rounds = [coverage_fields(job.get("error_message") or "") for job in finished]
    model_calls += sum(item.get("written", 0) for item in rounds)
    check("连点两次都跑到了终态", all(job["status"] == "completed" for job in finished),
          str([job["status"] for job in finished]))
    check("第一次真的写了页", rounds[0].get("written", 0) > 0, str(rounds[0]))
    check("第二次全部命中已有页", rounds[1].get("written") == 0 and rounds[1].get("skipped", 0) > 0,
          str(rounds[1]))

    documents = wiki_documents(base, course["id"])
    names = {page["concept_id"]: page["concept_name"]
             for page in call(base, f"/courses/{course['id']}/wiki")["pages"]}
    owners = {cid: frontmatter(raw).get("material_id", "") for cid, raw in documents.items()}
    doomed_pages = {cid for cid, owner in owners.items() if owner == doomed["id"]}
    kept_pages = {cid for cid, owner in owners.items() if owner == kept["id"]}
    check("两份教材各自写出了页", bool(doomed_pages) and bool(kept_pages), str(owners))

    index = documents["index"]
    top = [cid for cid, raw in documents.items()
           if cid != "index" and int(frontmatter(raw).get("level") or 0) == 0]
    missing = [names[cid] for cid in top if names[cid] not in index]
    check("课程首页列出了两份教材的顶层页", not missing, f"目录里少了 {missing}")
    check("首页读的是全部顶层页", len(source_refs(index)) == len(top),
          f"{source_refs(index)}，顶层页 {len(top)} 个")

    call(base, f"/materials/{doomed['id']}", None, method="DELETE")
    rebuilt = build_wiki(base, kept["id"], "删掉一份教材后重建")
    left = set(wiki_documents(base, course["id"]))
    check("被删教材的页真的没了", not doomed_pages & left, str(sorted(doomed_pages & left)))
    check("留下的教材的页一页不少", kept_pages <= left, str(sorted(kept_pages - left)))
    check("课程首页还在", "index" in left, str(sorted(left)[:5]))
    check("清掉的页数如实报出", rebuilt.get("pruned") == len(doomed_pages),
          f"报了 pruned={rebuilt.get('pruned')}，实际该清 {len(doomed_pages)} 页")


def stage_formats(base: str) -> None:
    """非 PDF 走真实的 multipart 上传。md / txt / docx 此前只在解析层的单测里进过，
    上传 → 索引 → 检索这条链路没人走——落盘后缀、content-type、提取分支哪一处不对，
    用户看到的都是一份空教材。"""
    print("\n[8] 非 PDF 上传：md / txt / docx")
    course = fresh_course(base, "非 PDF 格式（e2e）")
    files = [
        ("讲义.md", SCHEDULING_MD.encode(), "text/markdown", "护航效应"),
        ("讲义.txt", "时间片轮转把 CPU 按固定时长切给每个任务。".encode(), "text/plain", "时间片轮转"),
        ("讲义.docx", docx_bytes(["抢占式调度允许高优先级任务打断当前任务。"]),
         "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "抢占式调度"),
    ]
    for name, payload, content_type, needle in files:
        # 这一段的判据就是「索引成不成」，所以不走 install_bytes 的硬退出，失败要记成一条红的。
        material = upload_bytes(base, course["id"], name, payload, content_type)
        job = wait_job(base, call(base, f"/materials/{material['id']}/index", {})["id"])
        listed = next(item for item in call(base, f"/courses/{course['id']}/materials")
                      if item["id"] == material["id"])
        check(f"{name} 索引完成并切出了块",
              job["status"] == "completed" and (listed.get("chunk_count") or 0) > 0,
              f"{job['status']} {job.get('error_message')} chunks={listed.get('chunk_count')}")
        hits = call(base, f"/courses/{course['id']}/knowledge/search", {"query": needle})
        check(f"{name} 的正文检索得到", bool(hits) and hits[0]["material_name"] == name,
              str(hits[:1]))

    refused = upload_status(base, course["id"], "讲义.epub", b"whatever", "application/epub+zip")
    check("不支持的格式挡在上传这一步", refused == 422, f"HTTP {refused}")
    check("被拒的文件没留在资料库", len(call(base, f"/courses/{course['id']}/materials")) == len(files),
          str([item["filename"] for item in call(base, f"/courses/{course['id']}/materials")]))


def upload_status(base: str, course_id: str, name: str, payload: bytes, content_type: str) -> int:
    """期望失败的上传。成功了就把 201 报回去，让判据自己红。"""
    try:
        upload_bytes(base, course_id, name, payload, content_type)
    except urllib.error.HTTPError as error:
        return error.code
    return 201


LOGIN_SNIPPET = """
user => {
  if (!document.querySelector('.login-card')) return
  const input = document.querySelector('.login-card input')
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, user)
  input.dispatchEvent(new Event('input', { bubbles: true }))
  document.querySelector('.login-submit').click()
}
"""

_CLICK_TEXT = """
labels => {
  const hit = [...document.querySelectorAll('button')].find(node => labels.some(l => node.textContent.includes(l)))
  if (!hit) throw new Error('找不到按钮：' + labels.join(' / '))
  hit.click()
  return hit.textContent.trim()
}
"""


def stage_ui(base: str, data_dir: Path, web: str, user: str, shots: Path) -> None:
    """在界面上真点一遍「解析目录结构」和「解析到 Wiki」。

    自动化一直打的是 API，这两个按钮本身没人点过：前端少传一个 id、类名改掉、
    确认框的按钮失效，接口全绿也没人发现。用无头 playwright，不用 Browser pane——
    面板未布局时视口是 0x0，量什么都不可信。
    """
    global model_calls
    print("\n[9] 界面按钮（--ui）")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        check("playwright 可用", False,
              ".venv/bin/python -m pip install playwright && .venv/bin/python -m playwright install chromium")
        return

    course = fresh_course(base, "界面按钮（e2e）")
    material = install_bytes(base, course["id"], "调度讲义.md", SCHEDULING_MD.encode(), "text/markdown")
    shots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        try:
            _drive_library_ui(page, data_dir, web, user, course, material, shots)
        except Exception as error:  # noqa: BLE001 - 等不到元素就是这一段没走完，记成一条失败
            check("界面这一段走完了", False, f"{type(error).__name__}: {str(error).splitlines()[0]}")
        finally:
            page.screenshot(path=str(shots / "ui-final.png"))
            browser.close()
    info(f"截图在 {shots}")


def _drive_library_ui(page, data_dir: Path, web: str, user: str, course: dict, material: dict, shots: Path) -> None:
    global model_calls

    def click(*labels: str) -> str:
        return page.evaluate(_CLICK_TEXT, list(labels))

    page.goto(web, wait_until="networkidle")
    page.wait_for_timeout(900)
    page.evaluate(LOGIN_SNIPPET, user)
    page.wait_for_timeout(1600)
    click(course["name"])
    page.wait_for_timeout(500)
    click("知识库")
    page.wait_for_selector(".tabs")

    # ---- 目录结构：预告 → 确认 ----
    click("概念目录")
    page.wait_for_selector(".concept-tree")
    label = click("重新解析", "解析目录结构")
    page.wait_for_selector(".ocr-card")
    page.wait_for_selector(".ocr-card .danger-actions .primary-button:not([disabled])", timeout=30000)
    preview = page.locator(".ocr-card").inner_text()
    page.screenshot(path=str(shots / "ui-structure-preview.png"))
    check("目录结构按钮打开了影响预告", "个概念" in preview, f"按钮写着「{label}」，预告是 {preview[:80]!r}")
    check("预告说清了掌握度与错题会不会受影响",
          "掌握度" in preview or "不受影响" in preview, preview[:120])

    click("确认重建")
    page.wait_for_selector(".ocr-card", state="detached", timeout=30000)
    page.wait_for_timeout(600)
    done = page.locator(".help-note").first.inner_text() if page.locator(".help-note").count() else ""
    check("确认之后界面报出了重算结果", "已重算" in done, done[:120])

    # ---- 知识页：开开关 → 看账单 → 解析到 Wiki ----
    click("Wiki 知识页")
    page.wait_for_selector(".wiki-card")
    if not page.locator(".wiki-card .switch.on").count():
        page.click(".wiki-card .switch")
    page.wait_for_selector(".wiki-card .material-row", timeout=30000)
    # 账单是异步算的，先显示「正在估算…」。等到真出数再读，否则读到的是占位文案。
    page.wait_for_function(
        "() => { const n = document.querySelector('.wiki-card .wiki-coverage');"
        " return n && n.textContent.includes('预计') }", timeout=60000)
    estimate = page.locator(".wiki-card .wiki-coverage").first.inner_text()
    check("界面上先给出了构建账单", "预计" in estimate and "次模型调用" in estimate, estimate[:120])
    page.screenshot(path=str(shots / "ui-wiki-estimate.png"))

    page.click(".wiki-card .material-row .ghost-button")
    # 页面列表要等作业跑完才出现，这一等同时验了「点了按钮 → 起了作业 → 轮询 → 刷新列表」整条路
    page.wait_for_selector(".concept-tree .tree-open", timeout=600000)
    page.wait_for_timeout(500)
    titles = page.locator(".concept-tree .tree-open").all_inner_texts()
    check("点按钮之后界面上出现了知识页", len(titles) >= 2, str(titles))
    page.screenshot(path=str(shots / "ui-wiki-pages.png"))

    page.click(".concept-tree .tree-open")
    page.wait_for_selector(".note-viewer", timeout=30000)
    body = page.locator(".note-viewer").inner_text()
    check("知识页点开能看到正文", len(body) > 80 and "---" not in body[:20], body[:80])
    page.screenshot(path=str(shots / "ui-wiki-page-open.png"))

    written = sum(coverage_fields(row["error_message"] or "").get("written", 0)
                  for row in wiki_jobs(data_dir, material["id"]))
    model_calls += written
    info(f"界面这一段写了 {written} 页")


def wiki_jobs(data_dir: Path, material_id: str) -> list:
    """这份教材的知识页作业。作业是界面自己起的，脚本拿不到 id，只能回库里查。"""
    with db(data_dir) as connection:
        return connection.execute(
            "SELECT error_message FROM jobs WHERE material_id = ? AND type = 'wiki'", (material_id,)).fetchall()


def stage_build_big(base: str, data_dir: Path, course_id: str, material_id: str, budget: int) -> None:
    """可选：真给大切片写页。默认不跑——一次几十次模型调用。"""
    print("\n[10] 大切片真实构建（--build-big）")
    sections, _stats = plan_from_db(data_dir, material_id, max_nodes=WIKI_MAX_NODES)
    planned = len(sections) + 1
    info(f"预计 {planned} 次模型调用（{len(sections)} 页 + 课程首页）")
    if planned > budget:
        info(f"超过 --max-model-calls={budget}，跳过。要跑就把上限调高。")
        return

    fields = build_wiki(base, material_id, BIG)
    check("大切片构建完成", fields.get("written", 0) > 0, str(fields))

    documents = wiki_documents(base, course_id)
    check("报出的页数与真正落盘的页数一致", fields.get("pages") == len(documents),
          f"提示说 {fields.get('pages')} 页，读得回来 {len(documents)} 页")
    covered = set().union(*(read_chunk_ids(raw) for raw in documents.values())) if documents else set()
    total = material_chunk_ids(data_dir, material_id)
    check("大切片每个分片都被读到", covered >= total,
          f"读到 {len(covered & total)}/{len(total)}")


def run(base: str, data_dir: Path, *, build_big: bool, budget: int,
        ui: str = "", user: str = "local", shots: Path | None = None) -> None:
    big_course, big_material = stage_index(base, data_dir)
    stage_no_loss(data_dir, big_material)
    stage_legacy_shape(data_dir, big_material)
    course_id, material_id = stage_build(base, data_dir)
    stage_incremental(base, course_id, material_id)
    stage_flat(base, data_dir)
    stage_orphans(base)
    stage_formats(base)
    if ui:
        stage_ui(base, data_dir, ui, user, shots or (data_dir / "ui-shots"))
    if build_big:
        stage_build_big(base, data_dir, big_course, big_material, budget)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8005")
    parser.add_argument("--data-dir", default="testdata/lib5")
    parser.add_argument("--build-big", action="store_true",
                        help="也给大切片真写页，几十次模型调用，默认不跑")
    parser.add_argument("--max-model-calls", type=int, default=60,
                        help="--build-big 的预算上限，预计超过就不跑")
    parser.add_argument("--ui", action="store_true", help="再用无头浏览器点一遍界面上那两个按钮")
    parser.add_argument("--web", default="", help="前端地址，默认按 --base 的端口偏移推算")
    parser.add_argument("--user", default="local", help="界面用哪个用户名登录")
    args = parser.parse_args()
    data_dir = ROOT / args.data_dir if not Path(args.data_dir).is_absolute() else Path(args.data_dir)
    web = args.web or f"http://127.0.0.1:{5173 + int(args.base.rsplit(':', 1)[-1]) - 8000}"

    missing = [name for name in (BIG, OUTLINED, FLAT) if not (FIXTURES / name).is_file()]
    if missing:
        print(f"缺少切片教材 {missing}，先跑 scripts/e2e_fixture.py 生成", file=sys.stderr)
        return 2

    try:
        run(args.base, data_dir, build_big=args.build_big, budget=args.max_model_calls,
            ui=web if args.ui else "", user=args.user)
    except urllib.error.HTTPError as error:
        print(f"\nHTTP 错误：{error.code} {error.read().decode()[:300]}")
        results.append(("链路未走完", False, f"HTTP {error.code}"))
    except Exception as error:  # noqa: BLE001 - 脚本要把异常也算作失败
        print(f"\n中断：{type(error).__name__} {error}")
        results.append(("链路未走完", False, str(error)))

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} 通过，模型调用 {model_calls} 次")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAIL {name} — {detail}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
