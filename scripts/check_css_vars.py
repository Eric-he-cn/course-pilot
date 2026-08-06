#!/usr/bin/env python3
"""CSS 变量门：styles.css 里引用的每个自定义属性都必须先定义。

`.choice:hover` 曾经引用过一个不存在的 `--primary`，浏览器把整条声明按
invalid 处理、回落到 currentColor，于是 hover 变黑而不是变绿——坏得像正常，
所以一直没被发现。这道门专防这类笔误。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

TARGET = Path("frontend/src/styles.css")
COMPONENTS = Path("frontend/src")   # 变量也会在 JSX 内联样式里被引用


def blank(text: str) -> str:
    """抹成等长空格，行号不变。"""
    return re.sub(r"[^\n]", " ", text)


def strip_noise(css: str) -> str:
    """抹掉注释、字符串字面量与 url() 的内容，保住行号。

    注释不抹的话，说明性注释里提一句 var(--x) 就会误报成缺陷。
    字符串不抹的话，`content:"{"` 这类花括号会把下面的深度算错——而且是双向的：
    `content:"}"` 能让深度掉成负数，于是 @media 里的 :root 被当成顶层收下，
    正是这道门要防的那种「坏得像正常」。
    """
    css = re.sub(r"/\*.*?\*/", lambda m: blank(m.group()), css, flags=re.S)
    css = re.sub(r"'[^'\n]*'|\"[^\"\n]*\"", lambda m: blank(m.group()), css)
    return re.sub(r"url\([^)\n]*\)", lambda m: blank(m.group()), css)


def top_level_root(css: str) -> str:
    """所有顶层 :root 块的内容拼在一起。

    三处都得考虑：
    - 嵌在 `@media print { :root { … } }` 里的定义在别的条件下并不生效，引用它的地方
      会重演 --primary 那种坏得像正常的症状，所以只认花括号深度 0 的块；
    - 块尾要按花括号配对找，`find("}")` 遇到 CSS 嵌套（`:root { &:hover{…} }`）会停在
      内层块的收尾，把大半定义漏掉；
    - 可能有多个 :root，也可能写成 `:root,body{…}` 这样的选择器组。
    """
    grouped = re.compile(r"(^|,)\s*:root\b[^,]*(,|$)")
    blocks: list[str] = []
    depth = 0
    start = 0
    index = 0
    while index < len(css):
        char = css[index]
        if char == "{":
            depth += 1
            if depth == 1 and grouped.search(css[start:index].strip()):
                inner, cursor = 1, index + 1
                while cursor < len(css) and inner:
                    inner += (css[cursor] == "{") - (css[cursor] == "}")
                    cursor += 1
                blocks.append(css[index + 1 : cursor - 1])
            start = index + 1
        elif char == "}":
            depth = max(0, depth - 1)   # 夹在 0 以上：别让计数错位把 at-rule 里的块当成顶层
            start = index + 1
        index += 1
    return "\n".join(blocks)


def strip_ts_noise(source: str) -> str:
    """抹掉 .tsx 的注释与字符串，保住行号。

    不抹的话，注释里提一句 `var(--x)` 就能凭空造出一个引用，让「没人用」那半失效。
    """
    source = re.sub(r"/\*.*?\*/", lambda m: blank(m.group()), source, flags=re.S)
    source = re.sub(r"(?<![:\w])//.*$", lambda m: blank(m.group()), source, flags=re.M)
    return source


def jsx_refs(source: str) -> tuple[set[str], set[str]]:
    """.tsx 里引用的变量：(完整名, 动态前缀)。

    组件会动态拼名字（`var(--stack-${index})`），静态取不到全名，只能按前缀放过。
    前缀必须至少有一个字符——写 `*` 的话 `var(--${name})` 会捕获到空的 `"--"`，
    而每个 token 都以 `--` 开头，整个「定义了但没人用」检查就永久变绿了。
    """
    clean = strip_ts_noise(source)
    exact = set(re.findall(r"var\(\s*(--[\w-]+)\s*[,)]", clean))
    prefix = {name for name in re.findall(r"var\(\s*(--[\w-]+)\$\{", clean)}
    return exact, prefix


def main() -> int:
    css = strip_noise(TARGET.read_text(encoding="utf-8"))
    defined = set(re.findall(r"(?:^|[;{}])\s*(--[\w-]+)\s*:", top_level_root(css), flags=re.M))
    used: dict[str, int] = {}
    for number, line in enumerate(css.splitlines(), 1):
        for name in re.findall(r"var\(\s*(--[\w-]+)", line):
            used.setdefault(name, number)

    # JSX 是这个项目里的一等引用点（7 个 token 只在 .tsx 里用），所以两个方向都要查它：
    # 既算「有人用」，也要求它引用的变量已定义。
    jsx_exact: dict[str, Path] = {}
    jsx_prefix: set[str] = set()
    for path in sorted(COMPONENTS.rglob("*.tsx")):
        exact, prefix = jsx_refs(path.read_text(encoding="utf-8"))
        for name in exact:
            jsx_exact.setdefault(name, path)
        jsx_prefix |= prefix

    missing = [(f"{TARGET}:{number}", name) for name, number in sorted(used.items()) if name not in defined]
    missing += [(str(path), name) for name, path in sorted(jsx_exact.items()) if name not in defined]
    for where, name in missing:
        print(f"{where} 引用了未定义的变量 {name}", file=sys.stderr)

    def referenced(name: str) -> bool:
        return name in used or name in jsx_exact or any(name.startswith(p) for p in jsx_prefix)

    unused = sorted(name for name in defined if not referenced(name))
    for name in unused:
        print(f"{TARGET} 定义了但没人用的变量 {name}", file=sys.stderr)

    if missing or unused:
        return 1
    only_jsx = sorted(name for name in defined if name not in used)
    print(f"CSS 变量门通过：{len(defined)} 个定义，{len(used)} 个在样式里被引用，"
          f"{len(only_jsx)} 个只在组件里用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
