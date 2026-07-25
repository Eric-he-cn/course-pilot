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

# 端口被占用时必须失败：残留的旧后端会静默接管请求，让人误以为在测新代码。
for port in 8000 5173; do
  if lsof -ti "tcp:${port}" >/dev/null 2>&1; then
    echo "端口 ${port} 已被占用（可能是残留的旧服务进程）。" >&2
    echo "先停掉它再启动：lsof -ti tcp:${port} | xargs kill" >&2
    exit 1
  fi
done

# --reload：后端改动自动生效，避免改完代码忘重启、对着旧进程调试。
"${python_bin}" -m uvicorn app.main:app --app-dir "${project_dir}/backend" --host 127.0.0.1 --port 8000 --reload --reload-dir "${project_dir}/backend" &
backend_pid=$!
cleanup() {
  kill "${backend_pid}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

cd "${project_dir}/frontend"
pnpm dev --host 127.0.0.1 --port 5173
