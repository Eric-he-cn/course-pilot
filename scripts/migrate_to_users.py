"""把旧布局的数据迁进按用户隔离的目录。

改造前所有数据直接放在 data/ 下；现在每个用户一份 data/users/<workspace_id>/。
不迁移的话打开是空的——数据还在原地，只是服务端不再从那里读。

    .venv/bin/python scripts/migrate_to_users.py --dry-run   # 先看清单
    .venv/bin/python scripts/migrate_to_users.py
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from core.identity import workspace_id  # noqa: E402

# 库要连 WAL 的边车文件一起搬：只搬主文件会丢掉已提交事务的尾部。
ITEMS = (
    "coursepilot.db", "coursepilot.db-wal", "coursepilot.db-shm",
    "materials", "notes", "wiki", "traces", "courses", "user.md",
)


def instance_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v2/health", timeout=2):
            return True
    except (urllib.error.URLError, OSError):
        return False


def repoint_material_paths(database: Path, old_root: Path, new_root: Path) -> int:
    """materials.storage_path 存的是绝对路径，搬完必须改，否则重建索引与 Wiki 找不到文件。"""
    if not database.is_file():
        return 0
    connection = sqlite3.connect(database)
    try:
        changed = connection.execute(
            "UPDATE materials SET storage_path = replace(storage_path, ?, ?) WHERE storage_path LIKE ?",
            (str(old_root), str(new_root), f"{old_root}%"),
        ).rowcount
        connection.commit()
    finally:
        connection.close()
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--user", default="local", help="旧数据归给哪个用户名（默认 local，与 COURSEPILOT_DEFAULT_USER 一致）")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = (Path(__file__).resolve().parent.parent / args.data_dir).resolve()
    target = root / "users" / workspace_id(args.user)
    present = [name for name in ITEMS if (root / name).exists()]
    if not present:
        print(f"{root} 下没有旧布局的数据，无需迁移。")
        return 0

    print(f"用户名 {args.user!r} → {target.relative_to(root.parent)}")
    for name in present:
        exists = "（目标已存在，跳过）" if (target / name).exists() else ""
        print(f"  {name}{exists}")
    if args.dry_run:
        print("\n--dry-run：没有改动任何文件。")
        return 0

    if instance_running(args.port):
        # rename 在 POSIX 上不会失败，老进程握着旧 inode，之后的写入会静默落进被搬走的文件里。
        print(f"\n检测到 127.0.0.1:{args.port} 上有实例在跑。先停掉它再迁移，否则数据会一分为二。")
        return 2

    target.mkdir(parents=True, exist_ok=True)
    moved = []
    for name in present:
        destination = target / name
        if destination.exists():
            continue
        shutil.move(str(root / name), str(destination))
        moved.append(name)
    changed = repoint_material_paths(target / "coursepilot.db", root, target)
    print(f"\n已迁移 {len(moved)} 项：{'、'.join(moved) or '无'}")
    print(f"教材路径改写 {changed} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
