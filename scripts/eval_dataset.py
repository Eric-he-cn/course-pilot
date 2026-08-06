#!/usr/bin/env python3
"""数据集评测：固定样本跑真实链路，评答案、检索来源与工作流程（架构 §16.2 的 full regression 层）。

和另外三个脚本的分工：`benchmark.py` / `e2e_*.py` 只断言结构化行为，`evaluate.py` 从 trace
抽样打分但没有参考答案。这里是唯一有标注的一层——标准回答要点、正确页码、工作流程约束。

三个维度里两个是确定性的：检索来源比标注页码，工作流程比 must_call / must_not_call /
调用次数上限。只有答案正确性和流程合理性用 judge，可以 --no-judge 关掉。

用法（另起一套实例，别对着开发库跑）：
    CP_PORT_OFFSET=3 STORAGE_DATA_DIR=testdata/eval ./scripts/dev.sh
    .venv/bin/python scripts/eval_dataset.py --base http://127.0.0.1:8003 --data-dir testdata/eval
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from e2e_fixture import SLICES, SOURCES, cut, fetch  # noqa: E402
from e2e_journey import Turn  # noqa: E402

DATASET = ROOT / "evals" / "dataset.yaml"
FIXTURES = ROOT / "testdata" / "fixtures"
USER = "eval"
_SOURCE_BY_KEY = {source.key: source for source in SOURCES}

_ANSWER_PROMPT = """你是学习助手的离线评审。只依据给出的材料判断，不要补充自己的知识。

标注给出了这道题的参考要点。逐条判断助手的回答是否覆盖了该要点（表述可以完全不同，
只看意思对不对），再判断有没有出现被禁止的说法。

只输出 JSON：
{"covered": [true, false, ...], "forbidden_hit": ["原文片段", ...], "reason": "一句话"}
covered 的长度必须与要点条数一致。"""

_WORKFLOW_PROMPT = """你是学习助手的离线评审，只看它这一轮的工具调用路径合不合理。

已知这个产品的工具：search_materials（检索教材）、list_materials、concept_search、get_plan、
plan_update、get_archive、emit_evidence、artifact_read、artifact_append、note_read、note_write、
web_search、web_fetch、calculator、use_skill、memory_patch、ask_user。

判断路径有没有绕：有没有查了又查同一件事、有没有调了对这个问题无用的工具、
有没有本该一次拿到的信息分成多次取。**工具用得少不等于好**，该查的没查是更大的问题。

给 1-5 分（5=路径干净，3=有一两步多余，1=明显原地打转），只输出 JSON：
{"efficiency": n, "reason": "一句话"}"""


def call(base: str, path: str, payload: dict | None = None, method: str | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{base}/api/v2{path}", data=data, method=method or ("POST" if data else "GET"),
        headers={"Content-Type": "application/json", "X-CoursePilot-User": urllib.parse.quote(USER)},
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        body = response.read().decode()
    return json.loads(body) if body else {}


def ask(base: str, session_id: str, message: str, tag: str) -> Turn:
    payload = json.dumps({"message": message, "client_request_id": tag}).encode()
    request = urllib.request.Request(
        f"{base}/api/v2/sessions/{session_id}/turns", data=payload, method="POST",
        headers={"Content-Type": "application/json", "X-CoursePilot-User": urllib.parse.quote(USER)},
    )
    with urllib.request.urlopen(request, timeout=900) as response:
        return Turn(response.read().decode())


def verify_materials(expected: dict[str, str]) -> list[str]:
    """教材内容变了，标注的页码就是错的。这时候要报错，不能给出一个基于错页码的结论。"""
    problems = []
    for name, digest in expected.items():
        path = FIXTURES / name
        if not path.is_file():
            problems.append(f"{name} 不在 {FIXTURES}，先跑 scripts/e2e_fixture.py 生成切片")
        elif (actual := hashlib.sha256(path.read_bytes()).hexdigest()) != digest:
            problems.append(f"{name} 的 sha256 变了：标注按 {digest[:12]}…，实际 {actual[:12]}…")
    return problems


def prepare(base: str, courses: set[str]) -> dict[str, str]:
    """建课并装教材。已经装过的跳过，脚本要能重复跑。"""
    existing = {item["name"]: item["id"] for item in call(base, "/courses")}
    ids = {}
    for name in sorted(courses):
        ids[name] = existing.get(name) or call(base, "/courses", {"name": name})["id"]
    for spec in SLICES:
        if spec.course not in ids:
            continue
        course_id = ids[spec.course]
        already = {item.get("filename") or item.get("name") for item in call(base, f"/courses/{course_id}/materials")}
        if spec.out in already:
            continue
        path = FIXTURES / spec.out
        if not path.is_file():
            cut(fetch(_SOURCE_BY_KEY[spec.source], FIXTURES / "source"), spec, FIXTURES)
        print(f"  装 {spec.out} → {spec.course}")
        _upload_and_wait(base, course_id, path)
    return ids


def _upload_and_wait(base: str, course_id: str, path: Path) -> None:
    """上传只是落盘，索引要另起一个 job。首次跑要等本地嵌入模型加载，实测能到一分钟。"""
    boundary = "----coursepilot-eval"
    body = b"".join([
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n".encode(),
        b"Content-Type: application/pdf\r\n\r\n", path.read_bytes(), f"\r\n--{boundary}--\r\n".encode(),
    ])
    request = urllib.request.Request(
        f"{base}/api/v2/courses/{course_id}/materials", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "X-CoursePilot-User": urllib.parse.quote(USER)},
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        created = json.loads(response.read().decode())
    material_id = created.get("material", {}).get("id") or created.get("id")
    job = call(base, f"/materials/{material_id}/index", {})
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        status = call(base, f"/jobs/{job['id']}")["status"]
        if status == "completed":
            return
        if status == "failed":
            raise SystemExit(f"{path.name} 索引失败")
        time.sleep(3)
    raise SystemExit(f"{path.name} 索引超时")


_DOCS_BY_COURSE: dict[str, set[str]] = {}
for _spec in SLICES:
    _DOCS_BY_COURSE.setdefault(_spec.course, set()).add(_spec.out)


def cited_refs(turn: Turn) -> list[tuple[str, int]]:
    """(文档, 页码)。一门课可能有多份切片，页码各自从 1 开始，只比页号会串。

    只数教材引用：知识页是转述稿、没有页码，混进来会把按标注页码算的精确度拉低，
    这一期前后的数字就不可比了。它单独计数，见 wiki_refs。
    """
    return [(item.get("document") or "", item["page"]) for item in turn.named("citation")
            if item.get("kind", "material") == "material" and isinstance(item.get("page"), int)]


def wiki_refs(turn: Turn) -> list[str]:
    """这一轮引到了哪几页知识页。单独一列，不参与召回与精确。"""
    return sorted({item.get("concept_name") or item.get("concept_id") or "?"
                   for item in turn.named("citation") if item.get("kind") == "wiki"})


def score_retrieval(sample: dict, refs: list[tuple[str, int]]) -> dict:
    spec = sample["expected"]["citations"]
    must, ok = set(spec.get("must_include_pages") or []), set(spec.get("acceptable_pages") or [])
    forbidden, target = set(spec.get("forbidden_pages") or []), spec.get("document")
    seen = {page for document, page in refs if not target or document == target}
    # 越界不靠标注：固定课程的会话引到别门课的切片就是越界。通用会话按问题解析课程，不判。
    out_of_course = sorted({document for document, _ in refs if document
                            and sample.get("scope", "course") == "course"
                            and document not in _DOCS_BY_COURSE.get(sample["course"], set())})
    return {
        # 没有 must 的样本（教材里本来就没有）按「引了不该引的才算错」判
        "recall": len(must & seen) / len(must) if must else (0.0 if seen and not ok else 1.0),
        "precision": len(seen & (must | ok)) / len(seen) if seen else (1.0 if not must else 0.0),
        "forbidden_pages": sorted(seen & forbidden),
        "out_of_course": out_of_course,
        "cited": sorted(f"{document} p.{page}" for document, page in set(refs)),
    }


def executed_tools(turn: Turn) -> list[str]:
    """按 tool_result 数，不按 tool_call：种子检索只发 result 不发 call，
    只看 call 会把「靠种子检索拿到证据」误判成一次都没查。"""
    return [item["name"] for item in turn.named("tool_result")]


def score_workflow(sample: dict, tools: list[str]) -> dict:
    spec = sample["expected"]["workflow"]
    used = set(tools)
    order_broken = []
    for earlier, later in spec.get("required_order") or []:
        if later in tools and (earlier not in tools or tools.index(earlier) > tools.index(later)):
            order_broken.append(f"{earlier}→{later}")
    limit = spec.get("max_tool_calls")
    return {
        "missing": [name for name in spec.get("must_call") or [] if name not in used],
        "forbidden_used": [name for name in spec.get("must_not_call") or [] if name in used],
        "order_broken": order_broken,
        "calls": len(tools),
        "over_limit": bool(limit and len(tools) > limit),
        "sequence": tools,
    }


def probe(base: str, samples: list[dict], only: set[str]) -> int:
    """只查检索、不跑模型：核对标注的页码到底能不能被检索到。

    标注页没出现在检索结果里，两种可能都值得看——页码标错了，或者检索本身召回不足。
    分不开的时候看 snippet：内容对得上就是召回问题，对不上就是标注问题。
    """
    ids = {item["name"]: item["id"] for item in call(base, "/courses")}
    suspicious = 0
    for sample in samples:
        if only and sample["id"] not in only and sample["category"] not in only:
            continue
        spec = sample["expected"]["citations"]
        must = spec.get("must_include_pages") or []
        if not must:
            continue
        hits = call(base, f"/courses/{ids[sample['course']]}/knowledge/search",
                    {"query": sample["question"], "limit": 8})
        found = {(hit["material_name"], hit["page"]) for hit in hits}
        target = spec.get("document")
        missing = [page for page in must if (target, page) not in found] if target else \
                  [page for page in must if page not in {p for _, p in found}]
        mark = "ok" if not missing else f"标注 p.{missing} 没被检索到"
        if missing:
            suspicious += 1
        print(f"{sample['id']:<16} {mark}")
        if missing:
            for hit in hits[:3]:
                print(f"    实际命中 {hit['material_name']} p.{hit['page']} "
                      f"score={hit['score']:.3f}：{hit['text'][:70].replace(chr(10), ' ')}")
    print(f"\n可疑标注 {suspicious} 条。逐条看上面的 snippet 判断是标错了还是检索没召回。")
    return suspicious


def judge(chat, prompt: str, case: str) -> dict:
    from contracts.llm import ChatDelta, ChatMessage
    parts = [item.text for item in chat.chat(
        messages=[ChatMessage(role="system", content=prompt), ChatMessage(role="user", content=case)],
    ) if isinstance(item, ChatDelta)]
    raw = "".join(parts).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < 0:
        return {"error": "judge 未返回 JSON", "raw": raw[:200]}
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return {"error": "judge JSON 解析失败", "raw": raw[:200]}


def build_chat():
    from adapters.llm import OpenAICompatibleChat, ResponsesApiChat
    from core.settings import Settings
    settings = Settings.from_environment()
    if not settings.remote_llm_configured:
        raise SystemExit("judge 需要配置 TEXT_API_KEY / TEXT_BASE_URL / TEXT_MODEL，或加 --no-judge")
    adapter = ResponsesApiChat if settings.text_protocol == "responses" else OpenAICompatibleChat
    return adapter(
        api_key=settings.text_api_key, base_url=settings.text_base_url, model=settings.text_model,
        provider=settings.text_provider, extra_body=settings.text_extra_body,
        total_timeout_seconds=settings.llm_total_timeout_seconds,
    )


def run(base: str, samples: list[dict], chat, only: set[str]) -> list[dict]:
    prepare(base, {sample["course"] for sample in samples})
    results = []
    for index, sample in enumerate(samples, 1):
        if only and sample["id"] not in only and sample["category"] not in only:
            continue
        scope = sample.get("scope", "course")
        payload = {"scope_mode": "general"} if scope == "general" else {"scope_mode": "course", "course_id": None}
        if scope != "general":
            course_id = {item["name"]: item["id"] for item in call(base, "/courses")}[sample["course"]]
            payload["course_id"] = course_id
        session_id = call(base, "/sessions", payload)["id"]
        turn = ask(base, session_id, sample["question"], f"eval-{sample['id']}")

        retrieval = score_retrieval(sample, cited_refs(turn))
        workflow = score_workflow(sample, executed_tools(turn))
        entry = {"id": sample["id"], "category": sample["category"], "retrieval": retrieval,
                 "workflow": workflow, "wiki_cited": wiki_refs(turn), "answer_chars": len(turn.answer)}
        if chat is not None:
            points = sample["expected"]["answer_points"]
            case = (f"学生提问：\n{sample['question']}\n\n参考要点：\n"
                    + "\n".join(f"{i}. {p}" for i, p in enumerate(points, 1))
                    + f"\n\n被禁止的说法：\n{sample['expected'].get('forbidden_claims') or '（无）'}"
                    + f"\n\n助手回答：\n{turn.answer[:3000]}")
            entry["answer"] = judge(chat, _ANSWER_PROMPT, case)
            entry["efficiency"] = judge(chat, _WORKFLOW_PROMPT,
                                        f"提问：{sample['question']}\n\n工具调用序列：{executed_tools(turn)}")
        results.append(entry)
        _print_one(index, len(samples), sample, entry)
    return results


def _print_one(index: int, total: int, sample: dict, entry: dict) -> None:
    retrieval, workflow = entry["retrieval"], entry["workflow"]
    flags = []
    if workflow["missing"]:
        flags.append(f"缺 {','.join(workflow['missing'])}")
    if workflow["forbidden_used"]:
        flags.append(f"越界 {','.join(workflow['forbidden_used'])}")
    if workflow["over_limit"]:
        flags.append(f"超限 {workflow['calls']}")
    if workflow["order_broken"]:
        flags.append(f"顺序 {','.join(workflow['order_broken'])}")
    if retrieval["forbidden_pages"]:
        flags.append(f"引了禁页 {retrieval['forbidden_pages']}")
    if retrieval["out_of_course"]:
        flags.append(f"越界引了 {','.join(retrieval['out_of_course'])}")
    covered = entry.get("answer", {}).get("covered")
    answer_note = f" 要点 {sum(bool(x) for x in covered)}/{len(covered)}" if isinstance(covered, list) else ""
    status = "FAIL " + " · ".join(flags) if flags else "ok"
    wiki = entry.get("wiki_cited") or []
    print(f"[{index}/{total}] {sample['id']:<20} 召回 {retrieval['recall']:.2f} "
          f"精确 {retrieval['precision']:.2f} 知识页 {len(wiki)} 工具 {workflow['calls']}{answer_note}  {status}")


def summarize(results: list[dict]) -> None:
    if not results:
        print("没有样本被执行")
        return
    hard_fail = [r for r in results if r["workflow"]["missing"] or r["workflow"]["forbidden_used"]
                 or r["workflow"]["over_limit"] or r["workflow"]["order_broken"]
                 or r["retrieval"]["forbidden_pages"] or r["retrieval"]["out_of_course"]]
    print("\n" + "=" * 64)
    print(f"样本 {len(results)} 条，硬约束失败 {len(hard_fail)} 条")
    print(f"检索召回 {statistics.mean(r['retrieval']['recall'] for r in results):.3f}  "
          f"精确 {statistics.mean(r['retrieval']['precision'] for r in results):.3f}"
          "（只算教材引用，标注比的就是页码）")
    # 知识页引用没有页码，参与不了页码判据，单独一列看它被用了多少。
    wiki_counts = [len(r.get("wiki_cited") or []) for r in results]
    print(f"知识页引用 {sum(wiki_counts)} 条，出现在 {sum(1 for n in wiki_counts if n)}/{len(results)} 条样本")
    covered = [(sum(bool(x) for x in r["answer"]["covered"]), len(r["answer"]["covered"]))
               for r in results if isinstance(r.get("answer", {}).get("covered"), list)]
    if covered:
        print(f"答案要点覆盖 {sum(c for c, _ in covered)}/{sum(t for _, t in covered)}")
    efficiency = [r["efficiency"]["efficiency"] for r in results
                  if isinstance(r.get("efficiency", {}).get("efficiency"), int | float)]
    if efficiency:
        print(f"流程合理性 {statistics.mean(efficiency):.2f}/5（judge，小样本下噪声大，看趋势不看绝对值）")
    by_category: dict[str, list[dict]] = {}
    for item in results:
        by_category.setdefault(item["category"], []).append(item)
    print("\n按类别：")
    for name, items in sorted(by_category.items()):
        failed = sum(1 for i in items if i in hard_fail)
        print(f"  {name:<18} {len(items)} 条，硬约束失败 {failed}")
    if hard_fail:
        print("\n失败明细：")
        for item in hard_fail:
            print(f"  {item['id']}: 工具={item['workflow']['sequence']} 引用页={item['retrieval']['cited']}"
                  f" 知识页={item.get('wiki_cited') or []}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8003")
    parser.add_argument("--dataset", default=str(DATASET))
    parser.add_argument("--no-judge", action="store_true", help="只跑确定性维度，不调 judge 模型")
    parser.add_argument("--probe", action="store_true", help="只查检索核对标注页码，不跑模型")
    parser.add_argument("--only", default="", help="只跑这些 id 或 category，逗号分隔")
    parser.add_argument("--out", default="", help="把逐条结果写成 JSONL")
    args = parser.parse_args()

    data = yaml.safe_load(Path(args.dataset).read_text(encoding="utf-8"))
    samples = data["samples"]
    if problems := verify_materials(data.get("materials", {})):
        for line in problems:
            print(f"教材校验失败：{line}", file=sys.stderr)
        return 2

    only = {item.strip() for item in args.only.split(",") if item.strip()}
    if args.probe:
        prepare(args.base, {sample["course"] for sample in samples})
        return 1 if probe(args.base, samples, only) else 0
    chat = None if args.no_judge else build_chat()
    try:
        results = run(args.base, samples, chat, only)
    except urllib.error.URLError as error:
        print(f"连不上 {args.base}：{error}", file=sys.stderr)
        return 2
    finally:
        if chat is not None:
            chat.close()
    summarize(results)
    if args.out:
        Path(args.out).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in results), encoding="utf-8")
        print(f"\n逐条结果 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
