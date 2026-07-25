import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
    // 浏览器端到端测试要把本地教材注入上传控件，只额外开放 fixture 目录。
    fs: { allow: ['.', '../data/e2e-fixtures'] },
  },
})
