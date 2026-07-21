#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${project_dir}/.venv/bin/python"

if [[ ! -x "${python_bin}" ]]; then
  echo "Missing .venv. Follow the setup steps in README.md first." >&2
  exit 1
fi
if ! command -v pnpm >/dev/null 2>&1; then
  echo "pnpm is required. Install it before starting the Demo." >&2
  exit 1
fi

"${python_bin}" -m uvicorn app.main:app --app-dir "${project_dir}/backend" --host 127.0.0.1 --port 8000 &
backend_pid=$!
cleanup() {
  kill "${backend_pid}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

cd "${project_dir}/frontend"
pnpm dev --host 127.0.0.1 --port 5173
