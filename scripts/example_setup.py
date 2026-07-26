"""给新用户准备一份能直接玩的示例工作区：建课、下载公开教材切片、建索引。

跑完用 `--user` 指定的用户名（默认 example）登录，就有带教材和索引的课程可以提问。

教材不进仓库——都是有版权的公开教材，脚本只是替你从各自官网下载并切出一章，
和你自己去官网下一份没有区别。source PDF 缓存在 testdata/fixtures/source/，重跑不会重下。

用法（先让服务跑起来）：
    ./scripts/dev.sh
    .venv/bin/python scripts/example_setup.py            # 一门课，下载约 120 KB
    .venv/bin/python scripts/example_setup.py --all      # 四门课，首次要下约 70 MB
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from e2e_fixture import SLICES, SOURCES, cut, fetch  # noqa: E402

FIXTURES = ROOT / "testdata" / "fixtures"
# 默认只装这一份：切片和 source 都是最小的，几秒就能跑完一轮完整流程。
QUICK = "os-cpu-scheduling.pdf"


def call(base: str, path: str, user: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{base}/api/v2{path}", data=data, method="POST" if data else "GET",
        headers={"Content-Type": "application/json", "X-CoursePilot-User": urllib.parse.quote(user)},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        body = response.read().decode()
    return json.loads(body) if body else {}


def upload(base: str, course_id: str, path: Path, user: str) -> dict:
    boundary = "----coursepilot-example"
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
        b"Content-Type: application/pdf\r\n\r\n",
        path.read_bytes(), b"\r\n", f"--{boundary}--\r\n".encode(),
    ])
    request = urllib.request.Request(
        f"{base}/api/v2/courses/{course_id}/materials", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "X-CoursePilot-User": urllib.parse.quote(user)},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.loads(response.read().decode())


def course_id_for(base: str, name: str, user: str) -> str:
    """课程重名会被拒，所以重跑时复用已有的那门课。"""
    for course in call(base, "/courses", user):
        if course["name"] == name:
            return course["id"]
    return call(base, "/courses", user, {"name": name})["id"]


def wait_for_job(base: str, job_id: str, user: str, timeout: float = 900) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = call(base, f"/jobs/{job_id}", user)
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(1)
    raise SystemExit(f"索引任务 {job_id} 超时（{timeout} 秒）")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--user", default="example", help="示例数据落在哪个用户名下")
    parser.add_argument("--all", action="store_true", help="装全部四门课（首次下载约 70 MB）")
    args = parser.parse_args()

    try:
        health = call(args.base, "/health", args.user)
    except (urllib.error.URLError, OSError) as error:
        print(f"连不上 {args.base}：{error}\n先另开一个终端跑 ./scripts/dev.sh", file=sys.stderr)
        return 2

    wanted = [s for s in SLICES if args.all or s.out == QUICK]
    needed = {s.source for s in wanted}
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / "source").mkdir(exist_ok=True)

    print(f"准备 {len(wanted)} 份教材切片…")
    downloads = {s.key: fetch(s, FIXTURES / "source") for s in SOURCES if s.key in needed}
    for spec in wanted:
        cut(downloads[spec.source], spec, FIXTURES)

    print(f"\n写入用户 {args.user} 的工作区：")
    jobs = []
    for spec in wanted:
        course = course_id_for(args.base, spec.course, args.user)
        existing = [m for m in call(args.base, f"/courses/{course}/materials", args.user)
                    if m["filename"] == spec.out and m["index_status"] == "indexed"]
        if existing:
            print(f"  [{spec.course}] {spec.out} 已在库且已索引，跳过")
            continue
        material = upload(args.base, course, FIXTURES / spec.out, args.user)
        job = call(args.base, f"/materials/{material['id']}/index", args.user, {})
        jobs.append((spec, job["id"]))
        print(f"  [{spec.course}] {spec.out} 已上传，索引中…")

    if "bge" not in str(health.get("rag", {}).get("backend", "")):
        print("\n注意：语义检索没启用，现在是纯关键词匹配。装上 sentence-transformers 后重建索引可以改善。")

    failed = []
    for spec, job_id in jobs:
        job = wait_for_job(args.base, job_id, args.user)
        status = "完成" if job["status"] == "completed" else f"失败（{job.get('error')}）"
        print(f"  [{spec.course}] {spec.out} 索引{status}")
        if job["status"] != "completed":
            failed.append(spec.out)

    print(f"\n用 {args.user} 这个用户名登录 http://127.0.0.1:5173 就能看到这些课程。")
    print("可以试着问：")
    for spec in wanted:
        print(f"  [{spec.course}] {spec.anchors[0] if spec.anchors else spec.course} 是什么意思？")
    print("\n教材来源：")
    for source in SOURCES:
        if source.key in needed:
            print(f"  {source.note}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
