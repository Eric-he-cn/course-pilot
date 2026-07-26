#!/usr/bin/env python3
"""事件流回放：掌握度算法改动后，用历史证据事件对比新旧曲线（策划书 §6）。

用法：
  改动算法前  python scripts/replay_mastery.py --data-dir testdata/e2e --save baseline.json
  改动算法后  python scripts/replay_mastery.py --data-dir testdata/e2e --compare baseline.json

回放只读 evidence_events，不写任何数据；投影表本身可以随时用 rebuild 重建。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from modules.learning.mastery import ALGORITHM_VERSION, mastery_score, replay  # noqa: E402


def curves(database: Path) -> dict[str, dict]:
    """每个概念按事件逐条重放，得到 (事件序号 → 掌握度) 曲线。"""
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT e.concept_id, COALESCE(c.name, '(已删除概念)') AS name, e.kind, e.payload_json, e.created_at"
        " FROM evidence_events e LEFT JOIN concepts c ON c.id = e.concept_id"
        " WHERE e.concept_id IS NOT NULL ORDER BY e.concept_id, e.created_at ASC, e.rowid ASC"
    ).fetchall()
    connection.close()

    grouped: dict[str, list[dict]] = {}
    names: dict[str, str] = {}
    for row in rows:
        names[row["concept_id"]] = row["name"]
        grouped.setdefault(row["concept_id"], []).append(
            {"kind": row["kind"], "payload": json.loads(row["payload_json"] or "{}"), "created_at": row["created_at"]}
        )

    now = datetime.now(timezone.utc)
    result: dict[str, dict] = {}
    for concept_id, events in grouped.items():
        points = []
        for index in range(1, len(events) + 1):
            state = replay(events[:index])
            points.append({"n": index, "bkt_p": round(state.bkt_p, 4), "score": mastery_score(state, at=now)})
        final = replay(events)
        result[concept_id] = {
            "name": names[concept_id], "events": len(events), "curve": points,
            "final_bkt_p": round(final.bkt_p, 4), "final_score": mastery_score(final, at=now),
        }
    return result


def user_database(data_dir: Path) -> Path:
    """库在 <data>/users/<user_id>/ 下。指向 <data>/coursepilot.db 会拿到一个不存在或空的库。"""
    candidates = sorted(Path(data_dir).glob("users/*/coursepilot.db"))
    if not candidates:
        raise SystemExit(f"{data_dir} 下没有找到用户库")
    if len(candidates) > 1:
        raise SystemExit(f"{data_dir} 下有多个用户库，用 --data-dir 指定到一个：{[str(p) for p in candidates]}")
    return candidates[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--save", default="", help="把当前算法的曲线存为基线")
    parser.add_argument("--compare", default="", help="与基线对比，报告变化")
    args = parser.parse_args()

    database = user_database(Path(args.data_dir))
    current = curves(database)
    print(f"算法版本 {ALGORITHM_VERSION}，回放 {len(current)} 个概念、{sum(item['events'] for item in current.values())} 条事件")
    for concept_id, item in sorted(current.items(), key=lambda pair: pair[1]["name"]):
        trail = " → ".join(str(point["bkt_p"]) for point in item["curve"])
        print(f"  {item['name'][:34]:36} P: {trail}  最终掌握度={item['final_score']}")

    if args.save:
        Path(args.save).write_text(json.dumps({"algorithm_version": ALGORITHM_VERSION, "curves": current}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n基线已写入 {args.save}")

    if args.compare:
        baseline = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        print(f"\n对比基线（{baseline['algorithm_version']} → {ALGORITHM_VERSION}）：")
        changed, data_moved = 0, 0
        for concept_id, item in current.items():
            old = baseline["curves"].get(concept_id)
            if old is None:
                print(f"  新增概念 {item['name']}（数据变化，不参与算法对比）")
                data_moved += 1
                continue
            if old["events"] != item["events"]:
                # 事件数变了说明期间又产生了新证据，这种差异不能算作算法影响。
                print(f"  {item['name'][:30]:32} 事件数 {old['events']} → {item['events']}，数据已变化，不可比")
                data_moved += 1
                continue
            if old["final_bkt_p"] != item["final_bkt_p"] or old["final_score"] != item["final_score"]:
                print(f"  {item['name'][:30]:32} P {old['final_bkt_p']} → {item['final_bkt_p']}，掌握度 {old['final_score']} → {item['final_score']}")
                changed += 1
        missing = set(baseline["curves"]) - set(current)
        for concept_id in missing:
            print(f"  基线里有但现在没有：{baseline['curves'][concept_id]['name']}")
        comparable = len(current) - data_moved
        print(f"\n  可比概念 {comparable} 个：其中 {changed} 个曲线因算法改动而变化")
        if data_moved:
            print(f"  {data_moved} 个概念的事件数变了（新证据），已排除；要纯比算法就在数据静止时重存基线")
        if missing:
            print(f"  {len(missing)} 个概念在当前库里缺失")
        if comparable and changed == 0:
            print("  相同事件流下算法输出完全一致")


if __name__ == "__main__":
    main()
