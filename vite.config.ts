// 前端构建配置（S01 建立，S03-07 起接入真实接口）。
// root 指向 src/frontend，构建产物输出到项目根 dist/frontend（由 `serve` 直接托管）。
// dev 时 /api 代理到本地后端；后端用 `python -m aidhu_om_agent serve` 启动。

import vue from '@vitejs/plugin-vue';
import { defineConfig } from 'vite';

export default defineConfig({
  root: 'src/frontend',
  plugins: [vue()],
  build: {
    outDir: '../../dist/frontend',
    emptyOutDir: true,
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
});
