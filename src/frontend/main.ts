// 前端入口（S01 建立）。S03-07 起首页为 JudgeView（上传 → 预检 → 判一条）；
// 批次列表、详情与导出页面在 S06 实现。

import { createApp } from 'vue';
import ElementPlus from 'element-plus';
import 'element-plus/dist/index.css';

import App from './App.vue';
import router from './router';
import './styles/main.css';

createApp(App).use(router).use(ElementPlus).mount('#app');
