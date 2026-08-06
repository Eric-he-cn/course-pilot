#!/usr/bin/env python
"""多教材 Wiki e2e 评测 · 跑一臂。

一门课上传 slices/ 下的 4 份互有重合的教材切片，逐题提问并把每轮的结构化结果落成 JSONL。
两臂只差一件事：W 臂给这门课开知识页并逐份构建，R 臂完全不碰。判定全在 judge.py 里离线做，
这个脚本不判对错——跑一次真模型很贵，判据改一版就重跑一遍是不可接受的。

语料（切片与题目集）不在仓库里，怎么准备见 scripts/e2e_wiki/README.md。

用法（每臂各起一套独立实例，别对着开发库跑）：
    STORAGE_DATA_DIR=testdata/e2e-wiki/data-R .venv/bin/python -m uvicorn app.main:app \
        --app-dir backend --host 127.0.0.1 --port 8002
    .venv/bin/python scripts/e2e_wiki/run_arm.py --arm R --base http://127.0.0.1:8002 \
        --data-dir testdata/e2e-wiki/data-R \
        --dataset testdata/e2e-wiki/e2e_wiki_dataset.yaml --slices testdata/e2e-wiki/slices \
        --repeat 3 --out testdata/e2e-wiki/out/R.jsonl

--resume 跳过 out 里已经跑成的 (sample_id, run)，中断后接着跑。
每题一个新会话：同一个会话里连问 20 题会让后面的题读到前面的回答。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import Client, HttpError, Turn  # noqa: E402

COURSE_NAME = "深度学习综合"


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


# ── 建课与教材 ────────────────────────────────────────────────────────────────

def ensure_course(client: Client, name: str) -> dict:
    for course in client.call("/courses"):
        if course["name"] == name:
            log(f"课程已存在：{name} ({course['id']})")
            return course
    course = client.call("/courses", {"name": name})
    log(f"新建课程：{name} ({course['id']})")
    return course


def ensure_materials(client: Client, course_id: str, slices: Path) -> list[dict]:
    """上传并索引 4 份切片。按文件名幂等：已经索引好的不重传也不重建。"""
    files = sorted(slices.glob("*.pdf"))
    if not files:
        raise SystemExit(f"{slices} 下没有 PDF")
    existing = {item["filename"]: item for item in client.call(f"/courses/{course_id}/materials")}
    for path in files:
        material = existing.get(path.name)
        if material is None:
            material = client.upload(course_id, path)
            log(f"上传 {path.name} → {material['id']}")
        if material.get("index_status") == "indexed":
            log(f"已索引 {path.name}：chunks={material.get('chunk_count')} "
                f"embedded={material.get('embedded_count')}")
            continue
        job = client.call(f"/materials/{material['id']}/index", {})
        log(f"索引 {path.name} job={job['id']} …")
        done = client.wait_job(job["id"])
        if done.get("status") != "completed":
            raise SystemExit(f"{path.name} 索引失败：{done.get('status')} {done.get('error')}")

    result = [item for item in client.call(f"/courses/{course_id}/materials")
              if item["filename"] in {path.name for path in files}]
    for item in result:
        if item.get("index_status") != "indexed" or not (item.get("chunk_count") or 0):
            raise SystemExit(f"{item['filename']} 索引状态异常：{item}")
    log("教材就绪：" + " · ".join(f"{item['filename']} chunks={item['chunk_count']}"
                                 f"/emb={item['embedded_count']}" for item in result))
    return result


# ── W 臂：知识页 ──────────────────────────────────────────────────────────────

def build_wiki(client: Client, course: dict, materials: list[dict], *, budget: int,
               report: Path, limit: int = 0) -> dict:
    """开知识页并逐份构建。每份的 wiki_coverage 汇总串原样记下来给 probe.py 对账。

    limit>0 只建前几份，冒烟用——正式评测必须四份都建，少一份就等于换了一门课。
    """
    client.call(f"/courses/{course['id']}", {"wiki_enabled": True}, method="PATCH")
    log("已开启知识页")
    if limit:
        materials = materials[:limit]
        log(f"! --wiki-limit {limit}：只建 {[item['filename'] for item in materials]}，这不是完整的 W 臂")

    estimates: dict[str, dict] = {}
    total_calls = 0
    for material in materials:
        estimate = client.call(f"/materials/{material['id']}/wiki/estimate")
        estimates[material["filename"]] = estimate
        total_calls += int(estimate.get("calls") or 0)
        log(f"预估 {material['filename']}：pages={estimate['pages']} calls={estimate['calls']} "
            f"outline={estimate.get('outline')} sections={estimate.get('sections')} "
            f"candidates={estimate.get('candidates')} merged={estimate.get('merged')}")
    log(f"四份合计预估 {total_calls} 次模型调用")
    if budget >= 0 and total_calls > budget:
        raise SystemExit(f"预估 {total_calls} 次调用 > --wiki-budget {budget}，不构建。"
                         f"确认要花这笔就把预算调大。")

    builds: dict[str, dict] = {}
    for material in materials:
        job = client.call(f"/materials/{material['id']}/wiki", {})
        log(f"构建 {material['filename']} job={job['id']} …")
        started = time.monotonic()
        done = client.wait_job(job["id"])
        elapsed = round(time.monotonic() - started, 1)
        builds[material["filename"]] = {
            "material_id": material["id"], "job_id": job["id"], "status": done.get("status"),
            # 完成时 error 字段装的是 `wiki_coverage k=v ...` 汇总串（见 wiki.coverage_summary）
            "coverage": done.get("error"), "elapsed_seconds": elapsed,
        }
        log(f"  {done.get('status')} {elapsed}s · {done.get('error')}")
        if done.get("status") != "completed":
            raise SystemExit(f"{material['filename']} 知识页构建失败：{done.get('error')}")

    pages = client.call(f"/courses/{course['id']}/wiki").get("pages", [])
    payload = {
        "course_id": course["id"], "course_name": course["name"],
        "estimates": estimates, "builds": builds,
        "listed_pages": len(pages),
        "listed": [{"concept_id": page["concept_id"], "concept_name": page["concept_name"],
                    "level": page["level"], "order": page["order"], "chars": page["chars"]}
                   for page in pages],
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"知识页 {len(pages)} 页，构建报告写到 {report}")
    return payload


# ── 逐题提问 ──────────────────────────────────────────────────────────────────

def done_keys(out: Path) -> set[tuple[str, int]]:
    """out 里已经跑成的 (sample_id, run)。失败的那些不算，重跑时会再试一次。"""
    if not out.is_file():
        return set()
    keys: set[tuple[str, int]] = set()
    for line in out.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("ok"):
            keys.add((record.get("sample_id"), int(record.get("run", 0))))
    return keys


def ask_sample(client: Client, course_id: str, sample: dict, run: int, arm: str,
               raw_dir: Path) -> dict:
    session = client.call("/sessions", {"scope_mode": "course", "course_id": course_id})
    tag = f"{arm}-{sample['id']}-r{run}"
    base = {
        "arm": arm, "sample_id": sample["id"], "kind": sample["kind"], "topic": sample.get("topic"),
        "run": run, "question": sample["question"], "session_id": session["id"],
        "asked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    started = time.monotonic()
    try:
        raw = client.turn(session["id"], sample["question"], tag)
    except (HttpError, OSError) as error:
        return {**base, "ok": False, "error": f"{type(error).__name__}: {error}",
                "elapsed_ms": int((time.monotonic() - started) * 1000)}
    elapsed = int((time.monotonic() - started) * 1000)

    turn = Turn(raw)
    if turn.bad_frames:
        # 吞掉解析错误就等于把「这一轮到底发生了什么」丢掉，原文留档，判据那边照常算。
        dump = raw_dir / f"{tag}.sse"
        raw_dir.mkdir(parents=True, exist_ok=True)
        dump.write_text(raw, encoding="utf-8")
        log(f"  ! {len(turn.bad_frames)} 帧没解析出来，原文 → {dump}")

    failure = turn.failed
    context = turn.context_usage()
    resolution = turn.named("course_resolution")
    record = {
        **base,
        "ok": failure is None and bool(turn.named("turn_completed")),
        "elapsed_ms": elapsed,
        "answer_text": turn.answer,
        "finish_reason": turn.finish_reason,
        "citations": turn.citations(),
        "tool_calls": [item["name"] for item in turn.tool_calls()],
        "tool_calls_detail": turn.tool_calls(),
        "usage": turn.usage(),
        "context_usage": context,
        "course_resolution": resolution[0] if resolution else None,
        "event_counts": {name: sum(1 for event, _ in turn.events if event == name)
                         for name in sorted({event for event, _ in turn.events})},
        "bad_frames": turn.bad_frames or None,
    }
    if failure is not None:
        record["error"] = f"turn_failed: {failure.get('error_code')}"
    return record


def run(args: argparse.Namespace) -> int:
    dataset = yaml.safe_load(Path(args.dataset).read_text(encoding="utf-8"))
    samples = dataset["samples"]
    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        samples = [item for item in samples if item["id"] in wanted]
    if args.limit:
        samples = samples[: args.limit]
    if not samples:
        raise SystemExit("题目集筛完是空的")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    raw_dir = out.parent / f"{out.stem}-raw"
    skip = done_keys(out) if args.resume else set()
    if not args.resume and out.is_file():
        raise SystemExit(f"{out} 已存在。要接着跑加 --resume，要重跑先把它挪走。")

    client = Client(args.base, user=args.user, timeout=args.timeout)
    health = client.call("/health")
    log(f"后端 {args.base} · llm={health.get('llm', {}).get('mode')} "
        f"model={health.get('llm', {}).get('model')} web={health.get('web', {}).get('configured')}")

    course = ensure_course(client, args.course_name)
    materials = ensure_materials(client, course["id"], Path(args.slices))

    if args.arm == "W":
        report = out.parent / f"{out.stem}-wiki.json"
        # 已经建过就别再走一遍：构建本身按 source_hash 幂等（一次都不会调模型），
        # 但重写报告会把「这一批实际写了多少页」换成一排 skipped，probe 就对不出真账了。
        if report.is_file() and not args.force_wiki:
            log(f"知识页已建过，沿用 {report}（要重建加 --force-wiki）")
        else:
            build_wiki(client, course, materials, budget=args.wiki_budget,
                       report=report, limit=args.wiki_limit)
    else:
        if course.get("wiki_enabled"):
            raise SystemExit("R 臂的课程上开着知识页——这一臂必须是关的，换一个数据目录重来。")
        log("R 臂：不开知识页")
    if args.setup_only:
        log("--setup-only，不提问")
        return 0

    total = len(samples) * args.repeat
    counted = fresh = skipped = failures = 0
    with out.open("a", encoding="utf-8") as sink:
        for run_index in range(1, args.repeat + 1):
            for sample in samples:
                counted += 1
                if (sample["id"], run_index) in skip:
                    skipped += 1
                    log(f"({counted}/{total}) 跳过 {sample['id']} run={run_index}（已有）")
                    continue
                fresh += 1
                log(f"({counted}/{total}) {sample['id']} run={run_index} {sample['kind']}")
                record = ask_sample(client, course["id"], sample, run_index, args.arm, raw_dir)
                sink.write(json.dumps(record, ensure_ascii=False) + "\n")
                sink.flush()
                if not record.get("ok"):
                    failures += 1
                    log(f"  FAIL {record.get('error')}")
                else:
                    kinds = [item["kind"] for item in record["citations"]]
                    log(f"  {len(record['answer_text'])} 字 · 引用 {len(kinds)} 条"
                        f"（material={kinds.count('material')} wiki={kinds.count('wiki')}）"
                        f" · 工具 {record['tool_calls']}")

    log(f"完成：共 {total} 轮 · 新跑 {fresh} · 跳过 {skipped} · 失败 {failures} → {out}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arm", required=True, choices=["R", "W"], help="R=关知识页 W=开知识页")
    parser.add_argument("--base", required=True, help="后端地址，例如 http://127.0.0.1:8002")
    parser.add_argument("--data-dir", required=True,
                        help="这一臂的 STORAGE_DATA_DIR。脚本只用它做记录与自检，不直接写")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--slices", required=True)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--out", required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题（冒烟用）")
    parser.add_argument("--only", default="", help="只跑这些 id，逗号分隔")
    parser.add_argument("--user", default="local")
    parser.add_argument("--course-name", default=COURSE_NAME)
    parser.add_argument("--wiki-budget", type=int, default=400,
                        help="W 臂四份合计的模型调用上限，超了就不建；-1 表示不设限")
    parser.add_argument("--wiki-limit", type=int, default=0,
                        help="只给前 N 份教材建知识页（冒烟用，0=全部）")
    parser.add_argument("--force-wiki", action="store_true",
                        help="即使 <out>-wiki.json 已存在也重建一遍知识页")
    parser.add_argument("--timeout", type=int, default=1200, help="单个 HTTP 请求的超时（秒）")
    parser.add_argument("--setup-only", action="store_true", help="只建课/索引/建知识页，不提问")
    args = parser.parse_args()

    # 这一臂的数据目录必须和别人分开：两臂共用一个目录时 R 臂会读到 W 臂建的知识页。
    data_dir = Path(args.data_dir)
    log(f"数据目录（由后端进程的 STORAGE_DATA_DIR 决定，这里只记录）：{data_dir}")
    try:
        return run(args)
    except HttpError as error:
        log(f"HTTP 错误：{error}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
