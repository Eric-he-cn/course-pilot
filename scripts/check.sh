#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${project_dir}/.venv/bin/python"

if [[ ! -x "${python_bin}" ]]; then
  echo "Missing .venv. Follow the setup steps in README.md first." >&2
  exit 1
fi

cd "${project_dir}"
PYTHONPATH=backend "${python_bin}" -m pytest -q tests/backend backend/tests
"${python_bin}" -m compileall -q backend

cd "${project_dir}/frontend"
pnpm run typecheck
pnpm run build
