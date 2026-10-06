// 前端入口：S01 只验证工具链（Vue + Router + Element Plus 可加载、可构建）。
// 上传、进度、结果等业务页面在 S06 实现。

import { createApp } from 'vue';
import ElementPlus from 'element-plus';
import 'element-plus/dist/index.css';

import App from './App.vue';
import router from './router';
import './styles/main.css';

createApp(App).use(router).use(ElementPlus).mount('#app');
