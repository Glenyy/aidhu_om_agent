<!--
  S04-07：批次详情（最小形态）。

  展示后端返回的状态、计数、进度、当前任务、调用统计、失败摘要与 allowed_actions，
  不推导业务结论。两个必须说清楚的地方：

  1. **模拟标识**：调用统计里只要出现过模拟调用，就显著标注——模拟结果不能当作
     模型质量证据（review-and-handoff 规则 §4.3）。
  2. **worker 状态措辞**：只报「最近一次登记的 worker 与上线时间」，**不据时间戳
     断言进程已退出**（S04-07 定稿要点）。worker 可能正在处理一条长请求。

  轮询约 2 秒，批次进入终态后停止。
-->
<script setup lang="ts">
import { ElMessage } from 'element-plus';
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import { ApiError, fetchRun, newIdempotencyKey, resumeRun } from '../api/client';
import type { ResumeResult, RunDetail } from '../types/api';

const TERMINAL_STATUS = new Set(['completed', 'partial_failed', 'failed']);
const POLL_INTERVAL_MS = 2000;

const route = useRoute();
const router = useRouter();
const runId = computed(() => String(route.params.runId));

const detail = ref<RunDetail | null>(null);
const hint = ref<string | null>(null);
const loading = ref(false);
const submitting = ref(false);
/** 最近一次恢复入队的响应；界面据此显示选中数/重开数，与提交时的复查同源。 */
const lastResume = ref<ResumeResult | null>(null);

let pollTimer: number | null = null;

const actions = computed(() => detail.value?.allowed_actions ?? null);
const paused = computed(
  () => detail.value?.execution_control.model_dispatch_paused ?? false,
);
/**
 * 是否还在刷新。`interrupted` 不是「跑完了」而是「停下来了」——必须继续轮询，
 * 否则新 worker 启动后完成的中断恢复不会反映到页面上。
 */
const shouldPoll = computed(() => {
  const current = detail.value;
  if (!current) return false;
  if (current.active_job) return true;
  return !TERMINAL_STATUS.has(current.status);
});

const simulatedAttempts = computed(() => {
  const stats = detail.value?.call_statistics;
  if (!stats) return 0;
  return stats.stage1.simulated + stats.stage2.simulated;
});

const totalAttempts = computed(() => {
  const stats = detail.value?.call_statistics;
  if (!stats) return 0;
  return stats.stage1.attempts + stats.stage2.attempts;
});

function stopPolling(): void {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function load(): Promise<void> {
  loading.value = true;
  try {
    detail.value = await fetchRun(runId.value);
    hint.value = null;
  } catch (error) {
    hint.value =
      error instanceof ApiError ? `[${error.code}] ${error.message}` : String(error);
    stopPolling();
  } finally {
    loading.value = false;
  }
  if (shouldPoll.value) {
    if (pollTimer === null) {
      pollTimer = window.setInterval(() => void load(), POLL_INTERVAL_MS);
    }
  } else {
    stopPolling();
  }
}

onMounted(() => void load());
onBeforeUnmount(stopPolling);
// 从列表跳到另一个批次时组件被复用，参数变化要重新加载并重开轮询。
watch(runId, () => {
  detail.value = null;
  lastResume.value = null;
  stopPolling();
  void load();
});

async function doResume(retryFailed: boolean): Promise<void> {
  if (submitting.value) return;
  submitting.value = true;
  hint.value = null;
  try {
    // 一次用户动作生成一个键；后端据此保证重复提交不会排进两个任务。
    const result = await resumeRun(runId.value, retryFailed, newIdempotencyKey());
    lastResume.value = result;
    ElMessage.success(
      retryFailed
        ? `已入队重试失败项：选中 ${result.selected_records} 条，计划重开 ${result.renewed_campaigns} 轮预算`
        : `已入队恢复：选中 ${result.selected_records} 条，计划重开 ${result.renewed_campaigns} 轮预算`,
    );
    await load();
  } catch (error) {
    hint.value =
      error instanceof ApiError ? `[${error.code}] ${error.message}` : String(error);
  } finally {
    submitting.value = false;
  }
}

function statusTagType(status: string): 'success' | 'warning' | 'danger' | 'info' | 'primary' {
  if (status === 'completed') return 'success';
  if (status === 'partial_failed') return 'warning';
  if (status === 'failed') return 'danger';
  if (status === 'running') return 'primary';
  return 'info';
}

function stageText(stage: string | null): string {
  if (stage === 'stage1') return '阶段一：资料分析';
  if (stage === 'stage2') return '阶段二：答案判别';
  return '—';
}
</script>

<template>
  <div class="detail-page">
    <el-card class="panel">
      <template #header>
        <div class="header-row">
          <span>批次详情</span>
          <div>
            <el-tag v-if="detail" :type="statusTagType(detail.status)" size="large">
              {{ detail.status }}
            </el-tag>
            <el-button class="refresh" :loading="loading" @click="load">立即刷新</el-button>
            <el-button @click="router.push('/runs')">返回列表</el-button>
          </div>
        </div>
      </template>

      <el-alert v-if="hint" type="error" :closable="false" show-icon :title="hint" />
      <el-empty v-if="!detail && !loading" description="没有读到批次详情" />

      <template v-if="detail">
        <p class="muted">
          批次 <strong class="mono">{{ detail.run_id }}</strong>｜来源文件
          {{ detail.original_filename }}（工作表 {{ detail.sheet_name }}）｜revision
          {{ detail.revision }}
        </p>
        <p class="muted">
          创建 {{ detail.created_at }}｜开始 {{ detail.started_at ?? '—' }}｜结束
          {{ detail.finished_at ?? '—' }}
        </p>
        <p v-if="!shouldPoll" class="muted">批次已进入终态，页面已停止刷新。</p>
        <p v-else class="muted">页面每约 2 秒刷新一次；刷新浏览器不会丢失批次。</p>

        <el-progress
          class="progress"
          :percentage="detail.progress_percent"
          :stroke-width="18"
          text-inside
        />
        <p class="muted">
          进度 = processed / total = {{ detail.counts.processed }} / {{ detail.counts.total }}。
          已结束的批次不一定 100%：失败与输入失败记录不计为成功。
        </p>

        <el-table class="counts" :data="[detail.counts]" border>
          <el-table-column prop="total" label="total" />
          <el-table-column prop="valid" label="valid" />
          <el-table-column prop="input_invalid" label="input_invalid" />
          <el-table-column prop="classified" label="classified" />
          <el-table-column prop="failed" label="failed" />
          <el-table-column prop="remaining" label="remaining" />
          <el-table-column prop="processed" label="processed" />
          <el-table-column prop="review_required" label="review_required" />
        </el-table>

        <el-alert
          v-if="detail.last_error"
          class="block"
          type="warning"
          :closable="false"
          show-icon
          :title="`上次结束时的错误：${detail.last_error.code ?? ''} ${
            detail.last_error.message ?? ''
          }`"
        />
      </template>
    </el-card>

    <template v-if="detail">
      <!-- 当前任务与最近任务 -->
      <el-card class="panel">
        <template #header>当前任务与最近任务</template>
        <p class="muted">
          活跃判别任务：{{ detail.active_job ? detail.active_job.job_id : '无（队列里没有排队或运行中的判别任务）' }}
        </p>
        <p v-if="detail.active_job" class="muted">
          状态 {{ detail.active_job.status }}｜当前记录
          <span class="mono">{{ detail.active_job.current_record_key ?? '—' }}</span>｜当前阶段
          {{ stageText(detail.active_job.current_stage) }}
        </p>
        <el-table :data="detail.recent_jobs" border>
          <el-table-column prop="job_id" label="任务" min-width="240" />
          <el-table-column prop="mode" label="来源" width="110" />
          <el-table-column prop="status" label="状态" width="130" />
          <el-table-column prop="started_at" label="开始" width="180" />
          <el-table-column prop="finished_at" label="结束" width="180" />
          <el-table-column label="错误" min-width="200">
            <template #default="{ row }">
              {{ row.error ? `${row.error.code}：${row.error.message}` : '—' }}
            </template>
          </el-table-column>
        </el-table>

        <h4>worker 状态</h4>
        <p class="muted">
          <template v-if="detail.execution_control.last_worker">
            最近一次登记：worker
            <span class="mono">{{ detail.execution_control.last_worker.worker_id }}</span>
            （模式 {{ detail.execution_control.last_worker.mode ?? '未知' }}，上线时间
            {{ detail.execution_control.last_worker.started_at ?? '—' }}，登记于
            {{ detail.execution_control.runtime_updated_at }}）。
          </template>
          <template v-else>还没有 worker 登记过（runtime_state 为空）。</template>
        </p>
        <p class="muted">
          本页<strong>不根据时间戳判断 worker 是否退出</strong>：worker 可能正在处理一条长
          请求，也可能已经停止而排队任务原样保留。请以终端的 worker 窗口为准。
        </p>
        <el-alert
          v-if="paused"
          type="error"
          :closable="false"
          show-icon
          title="判别派发已被暂停（系统性故障）：排队任务会保留但不会被消费，界面按钮也会被禁用。"
        >
          <p v-if="detail.execution_control.pause_reason">
            {{ detail.execution_control.pause_reason.code }}：{{
              detail.execution_control.pause_reason.message
            }}
          </p>
        </el-alert>
      </el-card>

      <!-- 调用统计：恢复前后核对阶段一没有被重复调用 -->
      <el-card class="panel">
        <template #header>
          调用统计
          <el-tag v-if="simulatedAttempts > 0" type="warning" class="header-tag">
            含模拟调用（{{ simulatedAttempts }} / {{ totalAttempts }} 次），不是真实模型结果
          </el-tag>
        </template>
        <el-table :data="[
          { stage: '阶段一', ...detail.call_statistics.stage1 },
          { stage: '阶段二', ...detail.call_statistics.stage2 },
        ]" border>
          <el-table-column prop="stage" label="阶段" width="120" />
          <el-table-column prop="attempts" label="尝试总数" width="120" />
          <el-table-column prop="succeeded" label="成功" width="100" />
          <el-table-column prop="failed" label="失败" width="100" />
          <el-table-column prop="unknown_after_interrupt" label="中断未知" width="120" />
          <el-table-column prop="simulated" label="其中模拟" width="120" />
        </el-table>
        <p class="muted">
          「中断未知」是已占位但结果未提交的调用：它**仍然占用预算**。
          恢复前后对比这里的阶段一数字：数字不变、只有阶段二增长，说明检查点被复用，
          阶段一没有被重复调用。
        </p>
      </el-card>

      <!-- 失败摘要 + allowed_actions -->
      <el-card class="panel">
        <template #header>
          失败记录摘要
          <el-tag v-if="detail.failure_summary.count" type="danger" class="header-tag">
            {{ detail.failure_summary.count }} 条
          </el-tag>
        </template>
        <el-empty
          v-if="!detail.failure_summary.count"
          description="没有失败记录（输入失败的行单列，不在这里）"
        />
        <template v-else>
          <el-table :data="detail.failure_summary.items" border>
            <el-table-column prop="order_index" label="顺序" width="80" />
            <el-table-column label="编号" width="140">
              <template #default="{ row }">{{ row.record_id ?? '（无编号）' }}</template>
            </el-table-column>
            <el-table-column prop="source_row" label="来源行" width="100" />
            <el-table-column prop="failure_stage" label="失败阶段" width="110" />
            <el-table-column prop="code" label="错误码" width="170" />
            <el-table-column label="可重试" width="100">
              <template #default="{ row }">{{ row.retryable ? '是' : '否' }}</template>
            </el-table-column>
            <el-table-column prop="message" label="说明" min-width="260" />
          </el-table>
          <p class="muted">
            这里只列编号、失败阶段、错误码与说明，**不含证据正文**；单条详情属 S06。
          </p>
        </template>
        <p v-if="detail.failure_summary.truncated" class="muted">
          只列出前 {{ detail.failure_summary.items.length }} 条，共
          {{ detail.failure_summary.count }} 条。
        </p>
      </el-card>

      <el-card class="panel">
        <template #header>允许的操作（allowed_actions）</template>
        <el-table :data="[detail.allowed_actions]" border class="actions-table">
          <el-table-column label="恢复（继续剩余预算）" width="180">
            <template #default="{ row }">{{ row.can_resume ? '可用' : '不可用' }}</template>
          </el-table-column>
          <el-table-column label="重试失败项（重开预算）" width="200">
            <template #default="{ row }">{{ row.can_retry_failed ? '可用' : '不可用' }}</template>
          </el-table-column>
          <el-table-column label="仅需收尾" width="120">
            <template #default="{ row }">{{ row.finalization_required ? '是' : '否' }}</template>
          </el-table-column>
          <el-table-column label="恢复选中/重开" width="160">
            <template #default="{ row }">{{ row.selected_records }} / {{ row.renewed_campaigns }}</template>
          </el-table-column>
          <el-table-column label="重试选中/重开" width="160">
            <template #default="{ row }">{{ row.retry_failed_selected ?? 0 }} / {{ row.retry_failed_renewed ?? 0 }}</template>
          </el-table-column>
          <el-table-column label="预算耗尽跳过" width="140">
            <template #default="{ row }">{{ row.skipped_budget_exhausted }}</template>
          </el-table-column>
          <el-table-column label="需新批次跳过" width="140">
            <template #default="{ row }">{{ row.skipped_needs_new_batch }}</template>
          </el-table-column>
        </el-table>

        <p v-if="actions?.disabled_reason" class="muted">
          当前不可恢复的原因：{{ actions.disabled_reason }}
        </p>

        <div class="action-row">
          <el-button
            type="primary"
            :disabled="!actions?.can_resume || submitting"
            :loading="submitting"
            @click="doResume(false)"
          >
            恢复（不重开预算）
          </el-button>
          <el-button
            :disabled="!actions?.can_retry_failed || submitting"
            :loading="submitting"
            @click="doResume(true)"
          >
            重试失败项（重开预算）
          </el-button>
          <span class="muted">
            点击只表示<strong>已入队</strong>：真正执行需要一个正在运行的 worker。
          </span>
        </div>

        <el-alert
          v-if="lastResume"
          class="block"
          type="info"
          :closable="false"
          show-icon
          :title="`上次提交：选中 ${lastResume.selected_records} 条，计划重开 ${lastResume.renewed_campaigns} 轮，预算耗尽跳过 ${lastResume.skipped_budget_exhausted} 条，需新批次跳过 ${lastResume.skipped_needs_new_batch} 条，仅收尾=${lastResume.finalize_only}，派发暂停=${lastResume.dispatch_paused}`"
        />
      </el-card>

      <el-card class="panel">
        <template #header>模型与版本快照（创建批次时冻结）</template>
        <el-table :data="Object.entries(detail.model_config).map(([stage, value]) => ({ stage, value: JSON.stringify(value) }))" border>
          <el-table-column prop="stage" label="分组" width="140" />
          <el-table-column prop="value" label="内容" />
        </el-table>
        <p class="muted">
          快照只含模型标识与已验证的非敏感参数；<strong>凭据只以「是否存在」的形式出现</strong>，
          不写入页面、日志或版本库。
        </p>
        <el-table :data="Object.entries(detail.versions).map(([name, value]) => ({ name, value: String(value) }))" border class="block">
          <el-table-column prop="name" label="版本项" width="200" />
          <el-table-column prop="value" label="值" />
        </el-table>
      </el-card>
    </template>
  </div>
</template>

<style scoped>
.detail-page {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
}

.header-tag,
.refresh {
  margin-left: 0.5rem;
}

.muted {
  color: var(--el-text-color-secondary);
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
}

.progress,
.counts,
.block,
.action-row {
  margin-top: 1rem;
}

.action-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
}
</style>
