// S01 前端构建配置。
// root 指向 src/frontend，构建产物输出到项目根 dist/frontend。
// /api 代理到本地后端；业务 API 在 S06 实现。

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
