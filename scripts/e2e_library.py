"""端到端：资料库那条链路。建课 → 上传 → 索引 → 开知识页 → 构建 → 读回。

与 e2e_journey / e2e_multiturn 的分工：那两个从头到尾都在 sessions/turns 里，
这一条一句话都不问。界面上「解析到 Wiki」那个按钮走的就是这里，此前没有自动化点过它。

判据落在**分片**上，不落在页码上：几段各查几页就能凑满整本书，页码粒度看不出漏读。

已知坑：上传只落盘，索引要另起 `POST /materials/{id}/index` 再轮询 job，
漏了这步教材会一直停在 `index_status: uploaded`，后面全都静默地查不到东西。

用法（`--data-dir` 要和实例的 `STORAGE_DATA_DIR` 是同一个，脚本会直接读那份 SQLite）：

    CP_PORT_OFFSET=5 STORAGE_DATA_DIR=testdata/lib5 ./scripts/dev.sh
    .venv/bin/python scripts/e2e_library.py --base http://127.0.0.1:8005 --data-dir testdata/lib5

成本：知识页每页一次模型调用。默认只给两份十来页的切片真写页，实测 21 次；
大切片只索引不写页，「不漏」直接调 `plan_sections` 这个纯函数验，一次调用都不花。
`--build-big` 才会真给大切片写页（实测 51 次），跑之前先算页数，
超过 `--max-model-calls` 就不跑。

每段开头都会先把同名课程删掉重建，所以重复跑不会踩上一次的残留。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
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


def upload(base: str, course_id: str, path: Path) -> dict:
    boundary = "----coursepilot-library"
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
        b"Content-Type: application/pdf\r\n\r\n",
        path.read_bytes(), b"\r\n", f"--{boundary}--\r\n".encode(),
    ])
    request = urllib.request.Request(
        f"{base}/api/v2/courses/{course_id}/materials", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(request, timeout=900) as response:
        return json.loads(response.read().decode())


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


def install(base: str, course_id: str, filename: str) -> dict:
    """上传 + 索引。上传只落盘，不另起索引作业教材会停在 uploaded。"""
    material = upload(base, course_id, FIXTURES / filename)
    job = wait_job(base, call(base, f"/materials/{material['id']}/index", {})["id"])
    if job["status"] != "completed":
        raise SystemExit(f"{filename} 索引失败：{job.get('error_message')}")
    return material


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
    for cap in (4, 8, 20, WIKI_MAX_NODES):
        read, total, stats = chunks_read_by_plan(data_dir, material_id, max_nodes=cap)
        check(f"节点上限 {cap}：分片一个不漏", read == total and stats["dropped"] == 0,
              f"读到 {read}/{total}，dropped={stats['dropped']}")
        info(f"  上限 {cap} → {stats['sections']} 页，候选 {stats['candidates']}，"
             f"并掉/砍掉 {stats['capped']}")

    sections, _stats = plan_from_db(data_dir, material_id, max_nodes=WIKI_MAX_NODES)
    check("上限之下确实被压过", len(sections) == WIKI_MAX_NODES,
          f"{len(sections)} 页，没顶到上限就测不到截断")
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
            check(f"老形态 · 节点上限 {cap}：分片一个不漏", read == total and stats["dropped"] == 0,
                  f"读到 {read}/{total}，dropped={stats['dropped']}")
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

    check("覆盖率提示可解析", {"concepts", "pages", "written", "skipped", "merged", "dropped"} <= set(fields),
          str(fields))
    check("没有内容被丢掉", fields.get("dropped") == 0, str(fields))
    check("确实写出了页", fields.get("written", 0) > 0, str(fields))

    documents = wiki_documents(base, course["id"])
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
    check("没有内容被丢掉", fields.get("dropped") == 0, str(fields))

    documents = wiki_documents(base, course["id"])
    levels = {int(frontmatter(raw).get("level") or 0) for cid, raw in documents.items() if cid != "index"}
    check("没有书签就平铺", levels <= {0}, f"层级 {sorted(levels)}")
    covered = set().union(*(read_chunk_ids(raw) for raw in documents.values())) if documents else set()
    total = material_chunk_ids(data_dir, material["id"])
    check("每个分片都被某一页读到", covered >= total,
          f"读到 {len(covered & total)}/{len(total)}，漏 {sorted(total - covered)[:5]}")


def stage_build_big(base: str, data_dir: Path, course_id: str, material_id: str, budget: int) -> None:
    """可选：真给大切片写页。默认不跑——一次几十次模型调用。"""
    print("\n[7] 大切片真实构建（--build-big）")
    sections, _stats = plan_from_db(data_dir, material_id, max_nodes=WIKI_MAX_NODES)
    planned = len(sections) + 1
    info(f"预计 {planned} 次模型调用（{len(sections)} 页 + 课程首页）")
    if planned > budget:
        info(f"超过 --max-model-calls={budget}，跳过。要跑就把上限调高。")
        return

    fields = build_wiki(base, material_id, BIG)
    check("大切片构建完成且没丢内容", fields.get("dropped") == 0 and fields.get("written", 0) > 0, str(fields))

    documents = wiki_documents(base, course_id)
    covered = set().union(*(read_chunk_ids(raw) for raw in documents.values())) if documents else set()
    total = material_chunk_ids(data_dir, material_id)
    check("大切片每个分片都被读到", covered >= total,
          f"读到 {len(covered & total)}/{len(total)}")


def run(base: str, data_dir: Path, *, build_big: bool, budget: int) -> None:
    big_course, big_material = stage_index(base, data_dir)
    stage_no_loss(data_dir, big_material)
    stage_legacy_shape(data_dir, big_material)
    course_id, material_id = stage_build(base, data_dir)
    stage_incremental(base, course_id, material_id)
    stage_flat(base, data_dir)
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
    args = parser.parse_args()
    data_dir = ROOT / args.data_dir if not Path(args.data_dir).is_absolute() else Path(args.data_dir)

    missing = [name for name in (BIG, OUTLINED, FLAT) if not (FIXTURES / name).is_file()]
    if missing:
        print(f"缺少切片教材 {missing}，先跑 scripts/e2e_fixture.py 生成", file=sys.stderr)
        return 2

    try:
        run(args.base, data_dir, build_big=args.build_big, budget=args.max_model_calls)
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
