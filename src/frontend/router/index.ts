// S03-07：首页为判别页（上传 → 预检 → 判一条）。
// 批次列表、详情与导出路由在 S06 按其阶段文档补充。

import { createRouter, createWebHistory } from 'vue-router';

import JudgeView from '../pages/JudgeView.vue';

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      name: 'judge',
      component: JudgeView,
    },
  ],
});

export default router;
