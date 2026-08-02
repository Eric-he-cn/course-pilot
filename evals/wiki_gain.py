#!/usr/bin/env python3
"""知识页收益的 A/B 测量：两侧只差「这门课有没有知识页」。

为什么不直接用 scripts/eval_dataset.py：那边缺第三个判据。召回量的是引用了哪几页教材，
而知识页是转述稿、按设计不产生教材页码，收益必须绕道才显形。这里补上事实锚点
（evals/wiki_anchors.yaml），直接判「远处那一页的事实有没有出现在回答正文里」。

两侧靠用户隔离：每个用户一份数据目录，同一套实例上跑得了 A 和 B，模型、教材、样本全同。

用法（另起实例，别对着开发库跑）：
    CP_PORT_OFFSET=4 STORAGE_DATA_DIR=testdata/gain4 ./scripts/dev.sh

    python evals/wiki_gain.py setup    --user gain-a
    python evals/wiki_gain.py setup    --user gain-b
    python evals/wiki_gain.py wiki     --user gain-b --estimate-only   # 先看账单
    python evals/wiki_gain.py wiki     --user gain-b
    python evals/wiki_gain.py run      --user gain-a --tag A1 --out scratchpad/A1.jsonl
    python evals/wiki_gain.py report   --a A1.jsonl --a A2.jsonl --a A3.jsonl \
                                       --b B1.jsonl --b B2.jsonl --b B3.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from e2e_fixture import SLICES  # noqa: E402
from e2e_journey import Turn  # noqa: E402

DATASET = ROOT / "evals" / "dataset.yaml"
ANCHORS = ROOT / "evals" / "wiki_anchors.yaml"
FIXTURES = ROOT / "testdata" / "fixtures"
CATEGORIES = ("global_synthesis", "cited_qa")

_DOCS_BY_COURSE: dict[str, set[str]] = {}
for _spec in SLICES:
    _DOCS_BY_COURSE.setdefault(_spec.course, set()).add(_spec.out)


# ---------------------------------------------------------------- HTTP


def call(base: str, user: str, path: str, payload: dict | None = None, method: str | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{base}/api/v2{path}", data=data, method=method or ("POST" if data else "GET"),
        headers={"Content-Type": "application/json", "X-CoursePilot-User": urllib.parse.quote(user)},
    )
    with urllib.request.urlopen(request, timeout=900) as response:
        body = response.read().decode()
    return json.loads(body) if body else {}


def ask(base: str, user: str, session_id: str, message: str, tag: str) -> Turn:
    payload = json.dumps({"message": message, "client_request_id": tag}).encode()
    request = urllib.request.Request(
        f"{base}/api/v2/sessions/{session_id}/turns", data=payload, method="POST",
        headers={"Content-Type": "application/json", "X-CoursePilot-User": urllib.parse.quote(user)},
    )
    with urllib.request.urlopen(request, timeout=1800) as response:
        return Turn(response.read().decode())


# ---------------------------------------------------------------- 装课


def verify_materials(expected: dict[str, str]) -> list[str]:
    problems = []
    for name, digest in expected.items():
        path = FIXTURES / name
        if not path.is_file():
            problems.append(f"{name} 不在 {FIXTURES}")
        elif (actual := hashlib.sha256(path.read_bytes()).hexdigest()) != digest:
            problems.append(f"{name} 的 sha256 变了：标注按 {digest[:12]}…，实际 {actual[:12]}…")
    return problems


def _upload_and_wait(base: str, user: str, course_id: str, path: Path) -> None:
    boundary = "----coursepilot-gain"
    body = b"".join([
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n".encode(),
        b"Content-Type: application/pdf\r\n\r\n", path.read_bytes(), f"\r\n--{boundary}--\r\n".encode(),
    ])
    request = urllib.request.Request(
        f"{base}/api/v2/courses/{course_id}/materials", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "X-CoursePilot-User": urllib.parse.quote(user)},
    )
    with urllib.request.urlopen(request, timeout=900) as response:
        created = json.loads(response.read().decode())
    material_id = created.get("material", {}).get("id") or created.get("id")
    job = call(base, user, f"/materials/{material_id}/index", {})
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        status = call(base, user, f"/jobs/{job['id']}")["status"]
        if status == "completed":
            return
        if status == "failed":
            raise SystemExit(f"{path.name} 索引失败")
        time.sleep(3)
    raise SystemExit(f"{path.name} 索引超时")


def setup(base: str, user: str) -> dict[str, str]:
    existing = {item["name"]: item["id"] for item in call(base, user, "/courses")}
    ids = {}
    for spec in SLICES:
        if spec.course not in ids:
            ids[spec.course] = existing.get(spec.course) or call(base, user, "/courses", {"name": spec.course})["id"]
    for spec in SLICES:
        course_id = ids[spec.course]
        already = {item.get("filename") or item.get("name")
                   for item in call(base, user, f"/courses/{course_id}/materials")}
        if spec.out in already:
            continue
        print(f"  [{user}] 装 {spec.out} → {spec.course}", flush=True)
        _upload_and_wait(base, user, course_id, FIXTURES / spec.out)
    return ids


def _materials(base: str, user: str, ids: dict[str, str]) -> list[tuple[str, str, str]]:
    """(course_name, material_id, filename)，只要样本用到的那五份切片。"""
    wanted = {spec.out for spec in SLICES}
    out = []
    for course, course_id in ids.items():
        for item in call(base, user, f"/courses/{course_id}/materials"):
            name = item.get("filename") or item.get("name")
            if name in wanted:
                out.append((course, item["id"], name))
    return sorted(out, key=lambda row: row[2])


def wiki(base: str, user: str, *, estimate_only: bool) -> None:
    ids = setup(base, user)
    for course, course_id in ids.items():
        call(base, user, f"/courses/{course_id}", {"wiki_enabled": True}, method="PATCH")
        print(f"  [{user}] {course} wiki_enabled=True", flush=True)
    rows = _materials(base, user, ids)
    total_calls = total_pages = 0
    print("\n构建前的账单（离线算，不调模型）：")
    for course, material_id, name in rows:
        est = call(base, user, f"/materials/{material_id}/wiki/estimate")
        pages, calls = est.get("pages") or est.get("page_count") or 0, est.get("llm_calls") or est.get("calls") or 0
        total_pages, total_calls = total_pages + pages, total_calls + calls
        print(f"  {name:<28} 预计页数 {pages:<4} 模型调用 {calls:<4} {json.dumps(est, ensure_ascii=False)}")
    print(f"\n合计：预计 {total_pages} 页、{total_calls} 次模型调用")
    if estimate_only:
        return
    started = time.monotonic()
    for course, material_id, name in rows:
        job = call(base, user, f"/materials/{material_id}/wiki", {})
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            state = call(base, user, f"/jobs/{job['id']}")
            if state["status"] == "completed":
                print(f"  {name} 完成 {json.dumps(state.get('result') or {}, ensure_ascii=False)[:200]}", flush=True)
                break
            if state["status"] == "failed":
                raise SystemExit(f"{name} 知识页构建失败：{state}")
            time.sleep(5)
        else:
            raise SystemExit(f"{name} 知识页构建超时")
    print(f"\n知识页构建耗时 {time.monotonic() - started:.0f} 秒")
    for course, course_id in ids.items():
        pages = call(base, user, f"/courses/{course_id}/wiki")["pages"]
        print(f"  {course}：{len(pages)} 页")


# ---------------------------------------------------------------- 判据


def material_refs(turn: Turn) -> list[tuple[str, int]]:
    return [(item.get("document") or "", item["page"]) for item in turn.named("citation")
            if item.get("kind", "material") == "material" and isinstance(item.get("page"), int)]


def wiki_ref_names(turn: Turn) -> list[str]:
    return sorted({item.get("concept_name") or item.get("concept_id") or "?"
                   for item in turn.named("citation") if item.get("kind") == "wiki"})


def executed_tools(turn: Turn) -> list[str]:
    """按 tool_result 数：种子检索只发 result 不发 call。"""
    return [item["name"] for item in turn.named("tool_result")]


def score_retrieval(sample: dict, refs: list[tuple[str, int]]) -> dict:
    spec = sample["expected"]["citations"]
    must, ok = set(spec.get("must_include_pages") or []), set(spec.get("acceptable_pages") or [])
    target = spec.get("document")
    seen = {page for document, page in refs if not target or document == target}
    out_of_course = sorted({document for document, _ in refs if document
                            and document not in _DOCS_BY_COURSE.get(sample["course"], set())})
    return {
        "recall": len(must & seen) / len(must) if must else (0.0 if seen and not ok else 1.0),
        "precision": len(seen & (must | ok)) / len(seen) if seen else (1.0 if not must else 0.0),
        "forbidden_pages": sorted(seen & set(spec.get("forbidden_pages") or [])),
        "out_of_course": out_of_course,
        "cited": sorted(f"{document} p.{page}" for document, page in set(refs)),
    }


def _normalize(answer: str) -> str:
    """PDF 与模型输出都可能在词中间插空白；比对前压掉，免得 'NF 4' 匹配不上 'NF4'。"""
    return re.sub(r"[ \t ]+", " ", answer).lower()


def score_anchor(spec: dict, answer: str) -> dict:
    text = _normalize(answer)
    hits = [p for p in spec["patterns"] if re.search(p, text, re.IGNORECASE)]
    need = spec.get("match", "any")
    ok = len(hits) >= int(need.split(">=")[1]) if need.startswith("count>=") else bool(hits)
    return {"name": spec["name"], "hit": ok, "matched": hits, "rule": need}


# ---------------------------------------------------------------- 跑


def run(base: str, user: str, tag: str, samples: list[dict], workers: int) -> list[dict]:
    ids = {item["name"]: item["id"] for item in call(base, user, "/courses")}
    anchors = {row["id"]: row for row in yaml.safe_load(ANCHORS.read_text(encoding="utf-8"))["anchors"]}

    def one(index_sample: tuple[int, dict]) -> dict:
        index, sample = index_sample
        started = time.monotonic()
        session_id = call(base, user, "/sessions",
                          {"scope_mode": "course", "course_id": ids[sample["course"]]})["id"]
        turn = ask(base, user, session_id, sample["question"], f"{tag}-{sample['id']}")
        tools = executed_tools(turn)
        entry = {
            "tag": tag, "user": user, "id": sample["id"], "category": sample["category"],
            "retrieval": score_retrieval(sample, material_refs(turn)),
            "tools": tools,
            "wiki_read": tools.count("wiki_read"),
            "wiki_index": tools.count("wiki_index"),
            "wiki_cited": wiki_ref_names(turn),
            "answer": turn.answer,
            "answer_chars": len(turn.answer),
            "seconds": round(time.monotonic() - started, 1),
        }
        if (spec := anchors.get(sample["id"])):
            entry["anchor"] = score_anchor(spec["primary"], turn.answer)
            entry["anchor2"] = score_anchor(spec["secondary"], turn.answer)
            if "strict" in spec:  # 主锚点可能被页面标题满足时，加严的那一条
                entry["anchor_strict"] = score_anchor(spec["strict"], turn.answer)
        mark = "" if "anchor" not in entry else f" 锚点 {'HIT ' if entry['anchor']['hit'] else 'miss'}"
        print(f"[{tag} {index}/{len(samples)}] {sample['id']:<12} 召回 {entry['retrieval']['recall']:.2f} "
              f"精确 {entry['retrieval']['precision']:.2f} wiki_read {entry['wiki_read']} "
              f"wiki引用 {len(entry['wiki_cited'])} 工具 {len(tools)}{mark} {entry['seconds']}s", flush=True)
        return entry

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, enumerate(samples, 1)))
    return sorted(results, key=lambda row: row["id"])


# ---------------------------------------------------------------- 报表


def _mean(values) -> float:
    values = list(values)
    return statistics.mean(values) if values else 0.0


def report(groups: dict[str, list[list[dict]]]) -> None:
    """groups: {'A': [第1遍, 第2遍, 第3遍], 'B': [...]}，每遍是逐条结果。"""
    print("\n" + "=" * 78)
    print("一、两类样本 × 三个维度（每格是「各遍的最小 ~ 最大（各遍均值）」）")
    print("=" * 78)
    header = f"{'':<18}{'A 侧（无知识页）':<30}{'B 侧（有知识页）':<30}"
    print(header)
    for category in CATEGORIES:
        print(f"\n[{category}]")
        for label, pick in (("检索召回", lambda r: r["retrieval"]["recall"]),
                            ("引用精确", lambda r: r["retrieval"]["precision"]),
                            ("事实锚点命中率", None),
                            ("wiki_read 次数", lambda r: r["wiki_read"]),
                            ("知识页引用条数", lambda r: len(r["wiki_cited"]))):
            cells = []
            for side in ("A", "B"):
                per_round = []
                for rows in groups.get(side, []):
                    subset = [r for r in rows if r["category"] == category]
                    if not subset:
                        continue  # 只跑了一类样本的补充遍：别把它当成这一类的 0 分
                    if label == "事实锚点命中率":
                        vals = [1.0 if r["anchor"]["hit"] else 0.0 for r in subset if "anchor" in r]
                        per_round.append(_mean(vals) if vals else None)
                    else:
                        per_round.append(_mean(pick(r) for r in subset))
                per_round = [v for v in per_round if v is not None]
                if not per_round:
                    cells.append("—")
                    continue
                if label in ("wiki_read 次数", "知识页引用条数"):
                    cells.append(f"{min(per_round):.2f} ~ {max(per_round):.2f} （{_mean(per_round):.2f}）")
                else:
                    cells.append(f"{min(per_round):.3f} ~ {max(per_round):.3f} （{_mean(per_round):.3f}）")
            print(f"  {label:<16}{cells[0]:<30}{cells[1]:<30}")

    print("\n" + "=" * 78)
    print("二、global_synthesis 逐条：事实锚点两侧各命中几遍")
    print("=" * 78)
    spec_by_id = {row["id"]: row for row in yaml.safe_load(ANCHORS.read_text(encoding="utf-8"))["anchors"]}
    for sid in sorted(spec_by_id):
        spec = spec_by_id[sid]
        line = {}
        for side in ("A", "B"):
            rounds = groups.get(side, [])
            got = [next((r for r in rows if r["id"] == sid), None) for rows in rounds]
            line[side] = (sum(1 for r in got if r and r["anchor"]["hit"]),
                          sum(1 for r in got if r and r["anchor2"]["hit"]),
                          len([r for r in got if r]))
        a, b = line["A"], line["B"]
        print(f"\n  {sid}  锚点「{spec['primary']['name']}」（只在 {spec['document']} p.{spec['primary']['only_on_page']}）")
        print(f"    {spec['primary']['fact']}")
        print(f"    主锚点  A {a[0]}/{a[2]} 遍命中    B {b[0]}/{b[2]} 遍命中")
        print(f"    次锚点（{spec['secondary']['name']}）  A {a[1]}/{a[2]}    B {b[1]}/{b[2]}")
        if "strict" in spec:
            strict = {}
            for side in ("A", "B"):
                got = [next((r for r in rows if r["id"] == sid), None) for rows in groups.get(side, [])]
                got = [r for r in got if r and "anchor_strict" in r]
                strict[side] = (sum(1 for r in got if r["anchor_strict"]["hit"]), len(got))
            print(f"    加严锚点（{spec['strict']['name']}，标题满足不了）  "
                  f"A {strict['A'][0]}/{strict['A'][1]}    B {strict['B'][0]}/{strict['B'][1]}")

    print("\n" + "=" * 78)
    print("三、知识页实际被用了几次（B 侧逐条，各遍合计）")
    print("=" * 78)
    ids = sorted({r["id"] for rows in groups.get("B", []) for r in rows})
    print(f"  {'样本':<12}{'wiki_read':<12}{'wiki_index':<12}{'知识页引用':<12}{'引到的页'}")
    for sid in ids:
        rows = [r for rounds in groups.get("B", []) for r in rounds if r["id"] == sid]
        names = sorted({n for r in rows for n in r["wiki_cited"]})
        print(f"  {sid:<12}{sum(r['wiki_read'] for r in rows):<12}{sum(r['wiki_index'] for r in rows):<12}"
              f"{sum(len(r['wiki_cited']) for r in rows):<12}{'、'.join(names)[:70]}")

    print("\n" + "=" * 78)
    print("四、cited_qa 有没有变差（逐条，各遍均值）")
    print("=" * 78)
    ids = sorted({r["id"] for rows in groups.get("A", []) for r in rows if r["category"] == "cited_qa"})
    print(f"  {'样本':<10}{'A 召回':<10}{'B 召回':<10}{'A 精确':<10}{'B 精确':<10}{'B wiki引用'}")
    for sid in ids:
        cell = {}
        for side in ("A", "B"):
            rows = [r for rounds in groups.get(side, []) for r in rounds if r["id"] == sid]
            cell[side] = (_mean(r["retrieval"]["recall"] for r in rows),
                          _mean(r["retrieval"]["precision"] for r in rows),
                          sum(len(r["wiki_cited"]) for r in rows))
        print(f"  {sid:<10}{cell['A'][0]:<10.3f}{cell['B'][0]:<10.3f}"
              f"{cell['A'][1]:<10.3f}{cell['B'][1]:<10.3f}{cell['B'][2]}")

    print("\n" + "=" * 78)
    print("五、一条教材引用都没有的轮次（召回口径的分母塌成 0 就出在这里）")
    print("=" * 78)
    for side in ("A", "B"):
        for category in CATEGORIES:
            rows = [r for rounds in groups.get(side, []) for r in rounds if r["category"] == category]
            empty = [f"{r['tag']}/{r['id']}" for r in rows if not r["retrieval"]["cited"]]
            print(f"  {side} 侧 {category:<18} {len(empty)}/{len(rows)} 轮" + (f"  {empty}" if empty else ""))

    print("\n" + "=" * 78)
    print("六、越界与禁页（两侧都要为 0，否则上面的数字不可信）")
    print("=" * 78)
    for side in ("A", "B"):
        bad = [(r["tag"], r["id"], r["retrieval"]["out_of_course"], r["retrieval"]["forbidden_pages"])
               for rows in groups.get(side, []) for r in rows
               if r["retrieval"]["out_of_course"] or r["retrieval"]["forbidden_pages"]]
        print(f"  {side} 侧：{len(bad)} 条" + ("" if not bad else f" {bad}"))


# ---------------------------------------------------------------- CLI


def load_samples() -> list[dict]:
    data = yaml.safe_load(DATASET.read_text(encoding="utf-8"))
    if problems := verify_materials(data.get("materials", {})):
        raise SystemExit("教材校验失败：" + "；".join(problems))
    return [s for s in data["samples"] if s["category"] in CATEGORIES]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("setup", "wiki", "run", "report"))
    # 用 --a / --b 分组，不用裸 -- 分隔：argparse 会把第一个 -- 吃掉，两侧文件会全落进 A。
    parser.add_argument("--a", action="append", default=[], help="report 用：A 侧（无知识页）的一遍结果，可重复")
    parser.add_argument("--b", action="append", default=[], help="report 用：B 侧（有知识页）的一遍结果，可重复")
    parser.add_argument("--base", default="http://127.0.0.1:8004")
    parser.add_argument("--user", default="")
    parser.add_argument("--tag", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--estimate-only", action="store_true")
    parser.add_argument("--only", default="", help="只跑这些 id 或 category，逗号分隔")
    args = parser.parse_args()

    if args.command == "report":
        # 回答原文存在结果文件里，锚点每次出报表都按当前 yaml 重算：
        # 改判据不用重跑模型，也不会出现「表里的数字和 yaml 里的正则对不上」。
        anchors = {row["id"]: row for row in yaml.safe_load(ANCHORS.read_text(encoding="utf-8"))["anchors"]}

        def rounds(names: list[str]) -> list[list[dict]]:
            out = []
            for name in names:
                rows = [json.loads(line) for line in Path(name).read_text(encoding="utf-8").splitlines() if line]
                for row in rows:
                    if not (spec := anchors.get(row["id"])):
                        continue
                    row["anchor"] = score_anchor(spec["primary"], row["answer"])
                    row["anchor2"] = score_anchor(spec["secondary"], row["answer"])
                    if "strict" in spec:
                        row["anchor_strict"] = score_anchor(spec["strict"], row["answer"])
                out.append(rows)
            return out
        if not args.a or not args.b:
            raise SystemExit("report 要 --a 与 --b 各至少一份")
        report({"A": rounds(args.a), "B": rounds(args.b)})
        return 0

    if not args.user:
        raise SystemExit("要 --user")
    if args.command == "setup":
        setup(args.base, args.user)
        return 0
    if args.command == "wiki":
        wiki(args.base, args.user, estimate_only=args.estimate_only)
        return 0

    samples = load_samples()
    if only := {item.strip() for item in args.only.split(",") if item.strip()}:
        samples = [s for s in samples if s["id"] in only or s["category"] in only]
    results = run(args.base, args.user, args.tag or args.user, samples, args.workers)
    if args.out:
        Path(args.out).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in results), encoding="utf-8")
        print(f"\n逐条结果 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
