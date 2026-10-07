// S03-07 起首页为判别页（上传 → 预检 → 开始判别）；S06-05 起首页不再有「判一条」。
// S04-07 增加最小批次面：/runs 列表与 /runs/{run_id} 详情。
// S06-04 增加单条记录详情：/runs/{run_id}/records/{record_key}。
// S07-03 增加只读评估页：/runs/{run_id}/evaluation（看哪一份由 query `eid` 决定）。
//
// 用 history 模式：后端 `_mount_frontend` 会把未知路径回落到 index.html，
// 因此直接刷新 /runs/{id}（含记录详情这样的深链接）也能打开
// （S04-07 的「重启后刷新仍在」依赖这一点，S06-06 复验同一条回落）。

import { createRouter, createWebHistory } from 'vue-router';

import EvaluationView from '../pages/EvaluationView.vue';
import JudgeView from '../pages/JudgeView.vue';
import RecordDetailView from '../pages/RecordDetailView.vue';
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
    {
      // `recordKey` 是内部 UUID（不是预检返回的可读编号），所以放在批次后面做子路径；
      // 独立成页而不是抽屉：可深链接、可刷新（S06 阶段文档 §0.2 第 2 项）。
      path: '/runs/:runId/records/:recordKey',
      name: 'record-detail',
      component: RecordDetailView,
    },
    {
      // 评估是**批次的属性**（同一批次可以评多次），所以挂在批次下面；
      // 具体看哪一份由 query `eid` 决定，省略时取最新一份（S07-03）。
      path: '/runs/:runId/evaluation',
      name: 'run-evaluation',
      component: EvaluationView,
    },
  ],
});

export default router;
