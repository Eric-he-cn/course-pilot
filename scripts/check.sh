#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${project_dir}/.venv/bin/python"

if [[ ! -x "${python_bin}" ]]; then
  echo "Missing .venv. Follow the setup steps in README.md first." >&2
  exit 1
fi

cd "${project_dir}"
PYTHONPATH=backend "${python_bin}" -m pytest -q tests/backend
"${python_bin}" -m compileall -q backend

# 界面文案全部走 i18n：剥掉注释后源码里不该再有中文，漏替换的调用点靠这道门发现。
# 字典本身除外——它的 zh 那半就是中文。
"${python_bin}" - <<'PY'
import re
import sys
from pathlib import Path

bad: list[str] = []
for path in sorted(Path("frontend/src").rglob("*.ts*")):
    if path.name == "i18n.ts":
        continue
    src = path.read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)        # 块注释与 JSX 注释
    src = re.sub(r"(?m)(^|\s)//.*$", r"\1", src)           # 行注释
    src = re.sub(r"/\[[^\]\n]*\][a-z]*", "", src)          # 正则字符类：全角标点是数据清洗，不是文案
    bad += [f"{path.name}:{n} 文案没走 i18n：{line.strip()[:120]}"
            for n, line in enumerate(src.splitlines(), 1)
            if re.search(r"[　-〿一-鿿！-｠]", line)]
for line in bad:
    print(line, file=sys.stderr)
sys.exit(1 if bad else 0)
PY

"${python_bin}" scripts/check_i18n_keys.py

cd "${project_dir}/frontend"
pnpm run typecheck
pnpm run build
