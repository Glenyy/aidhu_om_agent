// S03-07 起首页为判别页（上传 → 预检 → 判一条）；
// S04-07 增加最小批次面：/runs 列表与 /runs/{run_id} 详情。
// 筛选分页、记录列表与证据详情、导出与下载仍属 S06。
//
// 用 history 模式：后端 `_mount_frontend` 会把未知路径回落到 index.html，
// 因此直接刷新 /runs/{id} 也能打开（S04-07 的「重启后刷新仍在」依赖这一点）。

import { createRouter, createWebHistory } from 'vue-router';

import JudgeView from '../pages/JudgeView.vue';
import RunDetailView from '../pages/RunDetailView.vue';
import RunsView from '../pages/RunsView.vue';

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      name: 'judge',
      component: JudgeView,
    },
    {
      path: '/runs',
      name: 'runs',
      component: RunsView,
    },
    {
      path: '/runs/:runId',
      name: 'run-detail',
      component: RunDetailView,
    },
  ],
});

export default router;
