"""后端产出的 i18n key 必须在前端字典里有对应条目。

后端每轮上屏的文案（工具结果 summary、上下文段标签）以 key 的形式发给前端，
前端查字典渲染。加了 key 忘了加翻译，界面就会掉回中文兜底而不报错——这道门把它变成失败。

    PYTHONPATH=backend .venv/bin/python scripts/check_i18n_keys.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend" / "modules" / "agent"
DICTIONARY = ROOT / "frontend" / "src" / "i18n.ts"

# 后端只有这几个文件产出面向用户的 key
SOURCES = ("tools.py", "service.py", "context.py")
KEY_PATTERN = re.compile(r'["\'](summary\.[a-z0-9_]+|context\.segment\.[a-z0-9_]+)["\']')


DICTIONARY_HEAD = re.compile(r"(?m)^const (zh|en)\b")


def collect(paths: list[Path]) -> dict[str, set[str]]:
    """key -> 出现在哪些文件。正则扫字面量而不解析 AST：key 常常先赋给局部变量
    再传进构造函数，AST 只认关键字实参会漏掉那些。"""
    found: dict[str, set[str]] = {}
    for path in paths:
        for key in KEY_PATTERN.findall(path.read_text(encoding="utf-8")):
            found.setdefault(key, set()).add(path.name)
    return found


def collect_per_dictionary() -> dict[str, set[str]]:
    """zh 与 en 两份分开扫。整个文件一起扫是不够的：漏在 zh 里的 key，en 那份仍会
    让它出现在文件里；而这类 key 只被 tOr 动态调用，TypeScript 也检查不到。"""
    src = DICTIONARY.read_text(encoding="utf-8")
    heads = [(match.group(1), match.start()) for match in DICTIONARY_HEAD.finditer(src)]
    bounds = [(name, start, heads[i + 1][1] if i + 1 < len(heads) else len(src))
              for i, (name, start) in enumerate(heads)]
    return {name: set(KEY_PATTERN.findall(src[start:end])) for name, start, end in bounds}


def main() -> int:
    missing_files = [name for name in SOURCES if not (BACKEND / name).exists()]
    if missing_files or not DICTIONARY.exists():
        print(f"找不到要对账的文件：{missing_files or DICTIONARY}", file=sys.stderr)
        return 2

    backend = collect([BACKEND / name for name in SOURCES])
    dictionaries = collect_per_dictionary()
    if set(dictionaries) != {"zh", "en"}:
        print(f"在 {DICTIONARY.name} 里没找到 zh 与 en 两份字典，找到的是 {sorted(dictionaries)}", file=sys.stderr)
        return 2

    problems = []
    for name, keys in sorted(dictionaries.items()):
        for key in sorted(backend.keys() - keys):
            problems.append(f"后端产出 {key}（{'、'.join(sorted(backend[key]))}），{name} 字典里没有")
    for key in sorted(set.union(*dictionaries.values()) - backend.keys()):
        problems.append(f"前端字典有 {key}，后端已经不产出了")

    for line in problems:
        print(line, file=sys.stderr)
    if problems:
        print(f"\n共 {len(problems)} 处不一致。后端 {len(backend)} 个 key。", file=sys.stderr)
        return 1
    print(f"i18n key 对账通过：{len(backend)} 个 × zh/en 两份")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
