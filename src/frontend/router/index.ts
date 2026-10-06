// S01 只注册骨架占位路由；业务路由在 S06 按其阶段文档实现。

import { createRouter, createWebHistory } from 'vue-router';

import HomeView from '../pages/HomeView.vue';

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      name: 'home',
      component: HomeView,
    },
  ],
});

export default router;
