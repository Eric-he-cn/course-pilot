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

# 两个人（或两个 agent）同时开发时各起一套：CP_PORT_OFFSET=10 就是 8010 / 5183。
# 数据也要分开，否则两套服务写同一个 SQLite：STORAGE_DATA_DIR=data/wip-b ./scripts/dev.sh
offset="${CP_PORT_OFFSET:-0}"
backend_port=$((8000 + offset))
frontend_port=$((5173 + offset))

# 端口被占用时必须失败：残留的旧后端会静默接管请求，让人误以为在测新代码。
for port in "${backend_port}" "${frontend_port}"; do
  if lsof -ti "tcp:${port}" >/dev/null 2>&1; then
    echo "端口 ${port} 已被占用（可能是残留的旧服务进程）。" >&2
    echo "先停掉它：lsof -ti tcp:${port} | xargs kill" >&2
    echo "或者换一套端口：CP_PORT_OFFSET=10 ./scripts/dev.sh" >&2
    exit 1
  fi
done

# --reload：后端改动自动生效，避免改完代码忘重启、对着旧进程调试。
"${python_bin}" -m uvicorn app.main:app --app-dir "${project_dir}/backend" --host 127.0.0.1 --port "${backend_port}" --reload --reload-dir "${project_dir}/backend" &
backend_pid=$!
cleanup() {
  kill "${backend_pid}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

cd "${project_dir}/frontend"
# 前端要知道后端换了端口，否则代理仍然指向 8000
VITE_PROXY_TARGET="http://127.0.0.1:${backend_port}" pnpm dev --host 127.0.0.1 --port "${frontend_port}"
