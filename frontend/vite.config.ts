import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => ({
  plugins: [react()],
  server: {
    proxy: {
      // 代理目标可覆盖：便于把前端指向独立后端实例做验证，不影响开发库。
      '/api': { target: loadEnv(mode, '.', 'VITE_').VITE_PROXY_TARGET || 'http://127.0.0.1:8000', changeOrigin: true },
    },
    // 浏览器端到端测试要把本地教材注入上传控件，只额外开放 fixture 目录。
    fs: { allow: ['.', '../data/e2e-fixtures'] },
  },
}))
