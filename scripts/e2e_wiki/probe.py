#!/usr/bin/env python
"""W 臂的三条前置检查。全部绿了才值得把 20 题 × 3 轮跑完。

    .venv/bin/python scripts/e2e_wiki/probe.py \
        --wiki-json testdata/e2e-wiki/out/W-wiki.json --jsonl testdata/e2e-wiki/out/W.jsonl \
        --dataset testdata/e2e-wiki/e2e_wiki_dataset.yaml \
        [--data-dir testdata/e2e-wiki/data-W] [--base http://127.0.0.1:8003]

1. 页数对账 —— 每份教材：估算页数 == 构建汇总里的 pages，且 written+skipped == pages、
   empty == 0、outline 没退回概念表。课程级：sum(pages) - (份数-1) == 落盘页数
   （课程首页每份构建都会写一次，但只存一份）。任何一处差不为 0 都要报出来。

2. 知识页来源分布 —— 抽 3 条 cross_source 题，看每轮的知识页引用来自哪几份教材。
   两个席位被同一本书垄断，跨教材这件事就没在检索层面发生。
   来源取自引用里挂着的 sources（转述时依据的教材页）；没有时按 --data-dir 读页面
   frontmatter 的 material_id 兜底，两条路都拿不到就明说缺数据。

3. 知识页证据段 —— context_usage 里 context.segment.wiki_evidence 是否非零。
   这一段是 0 表示知识页正文压根没进上下文，那时候比两臂比的不是知识页。

缺数据就报「缺数据」，不推断、不当成通过。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import Client, HttpError  # noqa: E402

results: list[tuple[str, str, str]] = []  # (状态, 名称, 说明)


def record(status: str, name: str, detail: str = "") -> None:
    results.append((status, name, detail))
    print(f"  {status:<8}{name}" + (f" — {detail}" if detail else ""))


def parse_coverage(text: str | None) -> dict[str, object]:
    """`wiki_coverage concepts=7 pages=8 written=8 ... outline=material` → dict。"""
    if not text or "wiki_coverage" not in text:
        return {}
    out: dict[str, object] = {}
    for token in text.split("wiki_coverage", 1)[1].split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        out[key] = int(value) if value.lstrip("-").isdigit() else value
    return out


# ── 1. 页数对账 ───────────────────────────────────────────────────────────────

def check_pages(wiki: dict) -> None:
    print("\n[1] 页数对账")
    estimates, builds = wiki.get("estimates") or {}, wiki.get("builds") or {}
    if not builds:
        record("缺数据", "页数对账", "W-wiki.json 里没有 builds，先用 run_arm.py --arm W 建一遍")
        return

    total_pages = 0
    for name, build in sorted(builds.items()):
        coverage = parse_coverage(build.get("coverage"))
        estimate = estimates.get(name) or {}
        if not coverage:
            record("缺数据", f"{name} 的 wiki_coverage", f"job 汇总串解析不出：{build.get('coverage')!r}")
            continue
        pages = int(coverage.get("pages") or 0)
        total_pages += pages
        want = int(estimate.get("pages") or 0)
        record("PASS" if pages == want else "FAIL", f"{name} 估算页数 == 构建页数",
               "" if pages == want else f"估算 {want}，构建 {pages}，差 {pages - want}")
        parts = int(coverage.get("written") or 0) + int(coverage.get("skipped") or 0)
        record("PASS" if parts == pages else "FAIL", f"{name} written+skipped == pages",
               "" if parts == pages else f"{parts} != {pages}")
        empty = int(coverage.get("empty") or 0)
        record("PASS" if empty == 0 else "FAIL", f"{name} 没有空页",
               "" if empty == 0 else f"empty={empty}，这些节生成失败了")
        outline = coverage.get("outline")
        record("PASS" if outline == "material" else "FAIL", f"{name} 目录来自教材",
               "" if outline == "material" else f"outline={outline}，退回概念表了（层级降质）")
        merged = int(coverage.get("merged") or 0)
        pruned = int(coverage.get("pruned") or 0)
        print(f"          concepts={coverage.get('concepts')} pages={pages} "
              f"merged={merged} pruned={pruned} outline={outline} "
              f"{build.get('elapsed_seconds')}s")

    listed = wiki.get("listed_pages")
    count = len(builds)
    if listed is None or not count:
        record("缺数据", "课程级页数对账", "W-wiki.json 里没有 listed_pages")
        return
    # 课程首页是课程级的一页，每份教材构建都会写它一次，落盘只有一份
    expected = total_pages - (count - 1)
    record("PASS" if expected == listed else "FAIL", "课程级：sum(pages)-(份数-1) == 落盘页数",
           f"sum={total_pages} 份数={count} 预期 {expected}，落盘 {listed}，差 {listed - expected}")


# ── 2. 知识页来源分布 ─────────────────────────────────────────────────────────

def frontmatter_materials(data_dir: Path | None, course_id: str) -> dict[str, str]:
    """concept_id → material_id，读 <data>/users/<ws>/wiki/<course_id>/**/*.md 的 frontmatter。"""
    if data_dir is None:
        return {}
    roots = [path for path in data_dir.glob("users/*/wiki") if path.is_dir()]
    if (data_dir / "wiki").is_dir():
        roots.append(data_dir / "wiki")
    mapping: dict[str, str] = {}
    for root in roots:
        for path in (root / course_id).rglob("*.md") if (root / course_id).is_dir() else []:
            head = path.read_text(encoding="utf-8", errors="replace")[:800]
            concept = re.search(r"^concept_id:[ \t]*(\S+)$", head, re.MULTILINE)
            material = re.search(r"^material_id:[ \t]*(\S*)$", head, re.MULTILINE)
            if concept:
                mapping[concept.group(1)] = (material.group(1) if material else "") or "（未记归属）"
    return mapping


def check_sources(records: list[dict], samples: dict, *, wiki: dict, data_dir: Path | None,
                  client: Client | None, want: int) -> None:
    print(f"\n[2] 知识页来源分布（抽 {want} 条 cross_source 题）")
    cross = [item for item in records
             if samples.get(item.get("sample_id"), {}).get("kind") == "cross_source" and item.get("ok")]
    picked_ids = []
    for item in cross:
        if item["sample_id"] not in picked_ids:
            picked_ids.append(item["sample_id"])
        if len(picked_ids) >= want:
            break
    if not picked_ids:
        record("缺数据", "知识页来源分布", "jsonl 里没有成功跑完的 cross_source 轮次")
        return

    course_id = wiki.get("course_id") or ""
    by_concept = frontmatter_materials(data_dir, course_id)
    material_names = {info["material_id"]: name
                      for name, info in (wiki.get("builds") or {}).items() if info.get("material_id")}

    def documents_of(citation: dict) -> list[str]:
        docs = citation.get("source_documents") or []
        if docs:
            return docs
        concept = citation.get("concept_id") or ""
        if concept in by_concept:
            return [material_names.get(by_concept[concept], by_concept[concept])]
        if client is not None and course_id and concept:
            try:
                payload = client.call(f"/courses/{course_id}/wiki/{concept}/sources")
            except HttpError:
                return []
            return sorted({item.get("document") for item in payload.get("anchors") or []
                           if item.get("document")})
        return []

    turns = [item for item in cross if item["sample_id"] in picked_ids]
    spread = Counter()
    monopoly = unknown = with_wiki = 0
    for item in turns:
        wiki_cites = [c for c in item["citations"] if c.get("kind") == "wiki"]
        docs_per_cite = [documents_of(c) for c in wiki_cites]
        flat = sorted({doc for docs in docs_per_cite for doc in docs})
        spread.update(flat)
        if wiki_cites:
            with_wiki += 1
        if not flat and wiki_cites:
            unknown += 1
        if len(wiki_cites) >= 2 and len(flat) == 1:
            monopoly += 1
        print(f"          {item['sample_id']} run={item['run']}：知识页 {len(wiki_cites)} 条 → "
              f"{flat or '（来源未知）'}")

    record("PASS" if with_wiki else "FAIL", "抽样轮次里有知识页引用",
           f"{with_wiki}/{len(turns)} 轮" + ("" if with_wiki else "，知识页压根没被检索到"))
    if unknown:
        record("缺数据", "知识页引用的来源教材",
               f"{unknown} 条引用既没挂 sources、也没从 --data-dir / --base 查到归属")
    if with_wiki:
        record("PASS" if monopoly == 0 else "注意", "两个知识页席位不被单本垄断",
               f"{monopoly}/{with_wiki} 轮里两席来自同一本书" if monopoly else "")
        print(f"          来源分布：" + "、".join(f"{doc}×{n}" for doc, n in spread.most_common()))


# ── 3. 知识页证据段 ───────────────────────────────────────────────────────────

def check_evidence_segment(records: list[dict]) -> None:
    print("\n[3] 上下文里的知识页证据段")
    ok = [item for item in records if item.get("ok")]
    with_event = [item for item in ok if item.get("context_usage")]
    if not with_event:
        record("缺数据", "wiki_evidence 段",
               "所有轮次都没有 context_usage 事件，这条 probe 没有数据源")
        return
    def segment(item: dict, key: str) -> int:
        # 事件只报 tokens>0 的分段，所以「找不到」就是 0
        found = next((seg for seg in (item["context_usage"].get("segments") or [])
                      if seg.get("label_key") == key), None)
        return int(found["tokens"]) if found else 0

    tokens = [segment(item, "context.segment.wiki_evidence") for item in with_event]
    directory = [segment(item, "context.segment.wiki") for item in with_event]
    nonzero = [value for value in tokens if value > 0]
    record("PASS" if nonzero else "FAIL", "wiki_evidence 段非零",
           f"{len(nonzero)}/{len(tokens)} 轮非零，"
           f"中位 {sorted(nonzero)[len(nonzero) // 2] if nonzero else 0} token"
           if nonzero else "全部为 0：知识页正文没进上下文")
    # 知识页目录是另一条路：它注入系统提示，跟检索到没检索到无关，两个数分开看
    hits = [value for value in directory if value > 0]
    print(f"          知识页目录段（注入系统提示）：{len(hits)}/{len(directory)} 轮非零，"
          f"中位 {sorted(hits)[len(hits) // 2] if hits else 0} token")
    if len(with_event) < len(ok):
        record("注意", "部分轮次没有 context_usage 事件", f"{len(ok) - len(with_event)}/{len(ok)} 轮缺")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wiki-json", required=True, help="run_arm.py --arm W 写出的 <out>-wiki.json")
    parser.add_argument("--jsonl", required=True, help="W 臂的 jsonl")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--data-dir", help="W 臂的 STORAGE_DATA_DIR，用来读知识页 frontmatter 兜底")
    parser.add_argument("--base", help="后端地址，用来查 wiki 页的教材出处兜底")
    parser.add_argument("--user", default="local")
    parser.add_argument("--cross-samples", type=int, default=3)
    args = parser.parse_args()

    wiki = json.loads(Path(args.wiki_json).read_text(encoding="utf-8"))
    samples = {item["id"]: item
               for item in yaml.safe_load(Path(args.dataset).read_text(encoding="utf-8"))["samples"]}
    records = [json.loads(line) for line in Path(args.jsonl).read_text(encoding="utf-8").splitlines()
               if line.strip()]
    client = Client(args.base, user=args.user) if args.base else None
    data_dir = Path(args.data_dir) if args.data_dir else None

    check_pages(wiki)
    check_sources(records, samples, wiki=wiki, data_dir=data_dir, client=client,
                  want=args.cross_samples)
    check_evidence_segment(records)

    counts = Counter(status for status, _, _ in results)
    print(f"\n{counts['PASS']} 通过 · {counts['FAIL']} 失败 · {counts['缺数据']} 缺数据 · "
          f"{counts['注意']} 需留意")
    for status, name, detail in results:
        if status != "PASS":
            print(f"  {status} {name} — {detail}")
    return 1 if counts["FAIL"] or counts["缺数据"] else 0


if __name__ == "__main__":
    sys.exit(main())
