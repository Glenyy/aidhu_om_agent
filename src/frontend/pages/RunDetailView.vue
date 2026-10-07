<!--
  S04-07：批次详情（最小形态）＋ S05-05：导出区块 ＋ S06-03：收尾摘要与失败数
  ＋ S06-04：记录列表（分页 + 筛选 + 进单条详情）＋ S06-05：失败摘要可直达详情。

  展示后端返回的状态、计数、进度、当前任务、调用统计、失败摘要与 allowed_actions，
  不推导业务结论。三个必须说清楚的地方：

  1. **模拟标识**：调用统计里只要出现过模拟调用，就显著标注——模拟结果不能当作
     模型质量证据（review-and-handoff 规则 §4.3）。
  2. **worker 状态措辞**：只报「最近一次登记的 worker 与上线时间」，**不据时间戳
     断言进程已退出**（S04-07 定稿要点）。worker 可能正在处理一条长请求。
  3. **导出（S05-05）**：按钮与禁用原因来自 `allowed_actions.can_export`；快照信息
     来自 `latest_export.captured_*`，未捕获就说「尚未捕获快照」，不拿别的数字顶替；
     `counts_at_capture.remaining` 不为 0 时必须明示这不是完整成功批次的导出。

  轮询约 2 秒，批次进入终态后停止；**导出未完成时导出区块单独继续轮询**，直到两份
  文件可下载（plan/10 §9）。
-->
<script setup lang="ts">
import { ElMessage } from 'element-plus';
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import {
  ApiError,
  artifactDownloadUrl,
  createExport,
  fetchExports,
  fetchRecords,
  fetchRun,
  newIdempotencyKey,
  resumeRun,
} from '../api/client';
import { RECORD_LABELS, RECORD_PAGE_SIZES, RECORD_STATUSES } from '../constants';
import type {
  ExportCreated,
  ExportListPage,
  ResumeResult,
  RunDetail,
  RunExport,
  RunRecordListItem,
  RunRecordListPage,
} from '../types/api';

const TERMINAL_STATUS = new Set(['completed', 'partial_failed', 'failed']);
const POLL_INTERVAL_MS = 2000;
/** 导出还在队列里或正在写文件；两种状态都要继续轮询。 */
const EXPORT_ACTIVE = new Set(['queued', 'running']);

const route = useRoute();
const router = useRouter();
const runId = computed(() => String(route.params.runId));

const detail = ref<RunDetail | null>(null);
const hint = ref<string | null>(null);
const loading = ref(false);
const submitting = ref(false);
/** 最近一次恢复入队的响应；界面据此显示选中数/重开数，与提交时的复查同源。 */
const lastResume = ref<ResumeResult | null>(null);

const exportPage = ref<ExportListPage | null>(null);
const exportHint = ref<string | null>(null);
const exportLoading = ref(false);
const exportSubmitting = ref(false);
/** 最近一次手动导出入队的响应（只表示已入队）。 */
const lastExport = ref<ExportCreated | null>(null);

let pollTimer: number | null = null;
let exportTimer: number | null = null;

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

const exportsList = computed<RunExport[]>(() => exportPage.value?.items ?? []);

/**
 * 收尾摘要（S06-03）。
 *
 * `partial_failed` **不是错误**，它是「已收尾但没有全部成功」。S06-02 起后端不再为它
 * 写 `last_error`，摘要由 `counts` 与 `failure_summary` 承载；这里把这句话显示出来，
 * 免得用户看到「部分失败」却找不到失败几条。
 */
const settlementNotice = computed<string | null>(() => {
  const current = detail.value;
  if (!current || current.status !== 'partial_failed') return null;
  return `本批次已收尾但没有全部成功：技术失败 ${current.counts.failed} 条、输入失败 ${current.counts.input_invalid} 条。明细见下方「失败记录摘要」。`;
});

/**
 * 旧批次里的 `last_error` 是 `{code: PARTIAL_FAILED, failed_count}` 这种**收尾摘要**。
 * 存量数据不迁移（S06 阶段文档 §0.3 第 8 项），所以这里单独识别它，措辞与真错误分开。
 */
const legacySettlementError = computed(
  () => detail.value?.last_error?.code === 'PARTIAL_FAILED',
);

// ---------------------------------------- S06-04：记录列表（分页 + 筛选，plan/08 §6）

const recordsPage = ref<RunRecordListPage | null>(null);
const recordHint = ref<string | null>(null);
const recordLoading = ref(false);
/** 生成当前这一页时的批次 revision；下一次批次轮询拿到新 revision 就重取本页。 */
let recordsRevision: number | null = null;

function queryText(raw: unknown): string | null {
  const value = Array.isArray(raw) ? raw[0] : raw;
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

function queryInt(raw: unknown, fallback: number): number {
  const value = Number(queryText(raw));
  return Number.isInteger(value) && value >= 1 ? value : fallback;
}

/** 只接受后端认得的枚举值：非法值当作「没有这个筛选」，不发出去吃 422。 */
function queryEnum(raw: unknown, allowed: readonly string[]): string | null {
  const value = queryText(raw);
  return value !== null && allowed.includes(value) ? value : null;
}

/** 「是否需复核」的三态：未给＝不筛、`true`／`false` 作为布尔发给后端。 */
function queryBool(raw: unknown): boolean | null {
  const value = queryText(raw);
  if (value === 'true') return true;
  if (value === 'false') return false;
  return null;
}

function queryRecordPageSize(raw: unknown): number {
  const value = queryInt(raw, 50);
  return (RECORD_PAGE_SIZES as readonly number[]).includes(value) ? value : 50;
}

// 同批次列表页：筛选与页码写在 URL query 里，刷新与分享后仍然生效。
const recordQuery = computed(() => ({
  page: queryInt(route.query.rpage, 1),
  pageSize: queryRecordPageSize(route.query.rsize),
  label: queryEnum(route.query.rlabel, RECORD_LABELS),
  reviewRequired: queryBool(route.query.rreview),
  status: queryEnum(route.query.rstatus, RECORD_STATUSES),
  recordId: queryText(route.query.rid),
}));

const records = computed<RunRecordListItem[]>(() => recordsPage.value?.items ?? []);
const recordFiltering = computed(
  () =>
    recordQuery.value.label !== null ||
    recordQuery.value.reviewRequired !== null ||
    recordQuery.value.status !== null ||
    recordQuery.value.recordId !== null,
);

async function loadRecords(): Promise<void> {
  recordLoading.value = true;
  try {
    recordsPage.value = await fetchRecords(runId.value, recordQuery.value);
    recordsRevision = recordsPage.value.revision;
    recordHint.value = null;
  } catch (error) {
    recordHint.value =
      error instanceof ApiError ? `[${error.code}] ${error.message}` : String(error);
  } finally {
    recordLoading.value = false;
  }
}

function recordQueryString(next: {
  page: number;
  pageSize: number;
  label: string | null;
  reviewRequired: boolean | null;
  status: string | null;
  recordId: string | null;
}): string {
  const query: Record<string, string> = {};
  if (next.page > 1) query.rpage = String(next.page);
  if (next.pageSize !== 50) query.rsize = String(next.pageSize);
  if (next.label) query.rlabel = next.label;
  if (next.reviewRequired !== null) query.rreview = String(next.reviewRequired);
  if (next.status) query.rstatus = next.status;
  if (next.recordId) query.rid = next.recordId;
  return new URLSearchParams(query).toString();
}

/** 合并进当前 URL query（其它 query 键原样保留），只有变化时才导航。 */
function applyRecordQuery(patch: Partial<typeof recordQuery.value>): void {
  const next = { ...recordQuery.value, ...patch };
  const target = recordQueryString(next);
  if (target === recordQueryString(recordQuery.value)) return;
  void router.replace({ query: Object.fromEntries(new URLSearchParams(target)) });
}

/** 换筛选条件回到第 1 页：停在第 5 页筛出 1 条会让用户以为「没有结果」。 */
function onRecordFilterChange(patch: Partial<typeof recordQuery.value>): void {
  applyRecordQuery({ ...patch, page: 1 });
}

function onRecordPageChange(value: number): void {
  applyRecordQuery({ page: value });
}

function onRecordPageSizeChange(value: number): void {
  applyRecordQuery({ page: 1, pageSize: queryRecordPageSize(value) });
}

/**
 * 打开单条记录详情。**带上当前的记录筛选**（`rpage`／`rlabel`／`rstatus`…），
 * 这样详情页的「返回批次详情」能回到同一个筛选与页码，分享出去的链接也是同一条
 * （S06 阶段文档 §2.1 第 10 项）。query 键都是记录列表自己的，跨页不会互相干扰。
 */
function openRecord(recordKey: string): void {
  void router.push({ path: `/runs/${runId.value}/records/${recordKey}`, query: route.query });
}

/** 一键跳到「失败记录」视图：只改记录列表自己的筛选键，其它 query 保留。 */
function showFailedRecords(): void {
  applyRecordQuery({ page: 1, status: 'failed' });
}

/** 同上，筛出输入失败的行（`input_invalid` 与 `failed` 是两种成因，分开筛）。 */
function showInputInvalidRecords(): void {
  applyRecordQuery({ page: 1, status: 'input_invalid' });
}

function onRecordRowClick(row: RunRecordListItem): void {
  openRecord(row.record_key);
}

/** 下拉的 change 在清空时可能给 `null` 或空串，统一当成「没有这个筛选」。 */
function onLabelFilterChange(value: string | null): void {
  onRecordFilterChange({ label: value ? value : null });
}

function onStatusFilterChange(value: string | null): void {
  onRecordFilterChange({ status: value ? value : null });
}

function onReviewFilterChange(value: string | null): void {
  onRecordFilterChange({
    reviewRequired: value === 'true' ? true : value === 'false' ? false : null,
  });
}

/** 编号按**精确匹配**筛（plan/08 §6）；空串表示不筛。 */
function onRecordIdFilterChange(value: string): void {
  const text = (value ?? '').trim();
  onRecordFilterChange({ recordId: text ? text : null });
}

function clearRecordFilters(): void {
  onRecordFilterChange({
    page: 1,
    label: null,
    status: null,
    reviewRequired: null,
    recordId: null,
  });
}

function recordStatusTagType(status: string): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'completed') return 'success';
  if (status === 'failed') return 'danger';
  if (status === 'input_invalid') return 'warning';
  return 'info';
}


/** 有导出还在排队或正在生成文件——导出的单独轮询只在这种时候开着。 */
const exportInFlight = computed(() =>
  exportsList.value.some((item: RunExport) => EXPORT_ACTIVE.has(item.job_status ?? '')),
);

/** 最近一次导出的未处理数；null 表示还没捕获快照，0 表示当时全都处理完了。 */
const exportRemaining = computed(() => {
  const counts = detail.value?.latest_export?.counts_at_capture;
  return counts ? counts.remaining : null;
});

function stopPolling(): void {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
}

function stopExportPolling(): void {
  if (exportTimer !== null) {
    window.clearInterval(exportTimer);
    exportTimer = null;
  }
}

/**
 * 导出区块的轮询，与批次轮询**分开**：批次进入终态后批次轮询会停，但终态那次自动
 * 导出还要几十毫秒到几秒才写完，界面必须继续跟到两份文件都出来（plan/10 §9）。
 */
function syncExportPolling(): void {
  if (exportInFlight.value) {
    if (exportTimer === null) {
      exportTimer = window.setInterval(() => void loadExports(), POLL_INTERVAL_MS);
    }
  } else {
    stopExportPolling();
  }
}

async function loadExports(): Promise<void> {
  exportLoading.value = true;
  try {
    exportPage.value = await fetchExports(runId.value, 1, 50);
    exportHint.value = null;
  } catch (error) {
    exportHint.value =
      error instanceof ApiError ? `[${error.code}] ${error.message}` : String(error);
    stopExportPolling();
  } finally {
    exportLoading.value = false;
  }
  syncExportPolling();
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
  // 导出区块自己轮询时不必在这里重复拉一次；只有在它停下来时才顺手同步，
  // 以免批次刚跑完、自动导出刚排上那一刻界面还显示「还没排过导出」。
  if (!exportInFlight.value) {
    await loadExports();
  }
  // 记录列表按 `revision` 变化刷新（plan/08 §6 结尾）：翻页期间批次又判了几条时，
  // 这一页会跟着更新，而不是把不同轮次的记录拼在一张表里。
  if (detail.value && detail.value.revision !== recordsRevision) {
    await loadRecords();
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
onBeforeUnmount(() => {
  stopPolling();
  stopExportPolling();
});
// 从列表跳到另一个批次时组件被复用，参数变化要重新加载并重开轮询。
watch(runId, () => {
  detail.value = null;
  lastResume.value = null;
  exportPage.value = null;
  lastExport.value = null;
  recordsPage.value = null;
  recordsRevision = null;
  stopPolling();
  stopExportPolling();
  void load();
});
// 筛选与页码写在 URL query 里：改变时重新取这一页（浏览器前进/后退也一样）。
watch(() => route.query, () => void loadRecords());

async function doExport(): Promise<void> {
  if (exportSubmitting.value) return;
  exportSubmitting.value = true;
  exportHint.value = null;
  try {
    // 一次用户动作生成一个键：网络结果不确定时用同一个键重试不会排进两份导出。
    lastExport.value = await createExport(runId.value, newIdempotencyKey());
    ElMessage.success(
      lastExport.value.reused
        ? `命中幂等键：沿用已入队的导出 ${lastExport.value.export_id}`
        : `已入队导出 ${lastExport.value.export_id}；文件由 worker 生成，完成后这里出现下载链接`,
    );
    await loadExports();
    await load();
  } catch (error) {
    exportHint.value =
      error instanceof ApiError ? `[${error.code}] ${error.message}` : String(error);
  } finally {
    exportSubmitting.value = false;
  }
}

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

        <!--
          收尾摘要与错误分开显示（S06-03）：
          ① 新批次：`partial_failed` 没有 `last_error`，摘要由 counts/failure_summary 承载；
          ② 旧批次：库里可能已有 `{code: PARTIAL_FAILED, failed_count}`，原样显示但标明口径；
          ③ 真错误（系统性故障）：照旧报错，并带暂停派发标志。
        -->
        <el-alert
          v-if="settlementNotice && !legacySettlementError"
          class="block"
          type="warning"
          :closable="false"
          show-icon
          title="收尾摘要"
        >
          <p class="settlement-text">{{ settlementNotice }}</p>
          <!-- 摘要里的两个数字各自可一键筛出（§2.1 第 15 项）。 -->
          <div class="settlement-actions">
            <el-button v-if="detail.counts.failed" size="small" @click="showFailedRecords">
              在记录列表里只看技术失败（{{ detail.counts.failed }}）
            </el-button>
            <el-button
              v-if="detail.counts.input_invalid"
              size="small"
              @click="showInputInvalidRecords"
            >
              只看输入失败（{{ detail.counts.input_invalid }}）
            </el-button>
          </div>
        </el-alert>
        <el-alert
          v-if="legacySettlementError"
          class="block"
          type="warning"
          :closable="false"
          show-icon
          title="上次结束时的收尾摘要（旧批次记录）"
          :description="`${detail.last_error?.code ?? ''} ${
            detail.last_error?.message ?? ''
          }——这是早期版本为「部分失败」留下的收尾摘要，不是执行错误；失败明细看下方「失败记录摘要」。`"
        />
        <el-alert
          v-else-if="detail.last_error"
          class="block"
          type="error"
          :closable="false"
          show-icon
          :title="`上次结束时的错误：${detail.last_error.code ?? ''} ${
            detail.last_error.message ?? ''
          }`"
        />
      </template>
    </el-card>

    <template v-if="detail">
      <!-- 记录列表（S06-04）：分页 + 筛选，条件写在 URL query 里 -->
      <el-card class="panel">
        <template #header>
          <div class="header-row">
            <span>记录列表</span>
            <div>
              <el-tag v-if="recordsPage" type="info" size="small">
                共 {{ recordsPage.total }} 条，revision {{ recordsPage.revision }}
              </el-tag>
              <el-button class="refresh" :loading="recordLoading" @click="loadRecords">
                刷新记录
              </el-button>
            </div>
          </div>
        </template>

        <el-alert v-if="recordHint" type="error" :closable="false" show-icon :title="recordHint" />

        <div class="filter-row">
          <el-select
            class="filter-select"
            :model-value="recordQuery.label"
            clearable
            placeholder="全部标签"
            @change="onLabelFilterChange"
          >
            <el-option v-for="label in RECORD_LABELS" :key="label" :label="label" :value="label" />
          </el-select>

          <el-select
            class="filter-select"
            :model-value="recordQuery.status"
            clearable
            placeholder="全部状态"
            @change="onStatusFilterChange"
          >
            <el-option
              v-for="status in RECORD_STATUSES"
              :key="status"
              :label="status"
              :value="status"
            />
          </el-select>

          <el-select
            class="filter-select"
            :model-value="
              recordQuery.reviewRequired === null ? null : String(recordQuery.reviewRequired)
            "
            clearable
            placeholder="是否需复核"
            @change="onReviewFilterChange"
          >
            <el-option label="需人工复核" value="true" />
            <el-option label="不需人工复核" value="false" />
          </el-select>

          <el-input
            class="filter-input"
            :model-value="recordQuery.recordId ?? ''"
            placeholder="编号（精确匹配）"
            clearable
            @change="onRecordIdFilterChange"
          />

          <el-button v-if="recordFiltering" @click="clearRecordFilters">清除筛选</el-button>
        </div>

        <p class="muted">
          筛选条件写在地址栏里（<span class="mono">rlabel／rstatus／rreview／rid</span>），
          刷新或把链接发给别人仍是这组筛选；多个条件同时生效是「且」的关系。
          列表<strong>不含回答与资料原文</strong>，点「查看详情」看完整输入与两阶段证据。
          「序号」是人读序号（1..N，按输入顺序），存储层的 <span class="mono">order_index</span>
          从 0 起，两者差 1；导出里的序号同样从 1 起。
        </p>

        <el-empty
          v-if="!records.length && !recordLoading"
          :description="recordFiltering ? '这条筛选下没有记录：清除筛选可看全部' : '这个批次还没有记录'"
        />

        <el-table v-else :data="records" border @row-click="onRecordRowClick">
          <el-table-column label="序号" width="80">
            <template #default="{ row }">{{ row.order_index + 1 }}</template>
          </el-table-column>
          <el-table-column label="编号" width="140">
            <template #default="{ row }">{{ row.record_id ?? '（无编号）' }}</template>
          </el-table-column>
          <el-table-column prop="source_row" label="来源行" width="100" />
          <el-table-column prop="q_preview" label="问题（摘要）" min-width="260" />
          <el-table-column label="状态" width="130">
            <template #default="{ row }">
              <el-tag :type="recordStatusTagType(row.status)" size="small">{{ row.status }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="标签" width="200">
            <template #default="{ row }">{{ row.label ?? '—' }}</template>
          </el-table-column>
          <el-table-column label="需复核" width="100">
            <template #default="{ row }">
              {{ row.review_required === null ? '—' : row.review_required ? '是' : '否' }}
            </template>
          </el-table-column>
          <el-table-column label="失败" min-width="200">
            <template #default="{ row }">
              {{ row.failure ? `${row.failure.stage}／${row.failure.code}` : '—' }}
            </template>
          </el-table-column>
          <el-table-column label="操作" width="120">
            <template #default="{ row }">
              <el-link type="primary" @click.stop="openRecord(row.record_key)">查看详情</el-link>
            </template>
          </el-table-column>
        </el-table>

        <el-pagination
          v-if="recordsPage"
          class="pager"
          background
          layout="prev, pager, next, sizes, total"
          :current-page="recordsPage.page"
          :page-size="recordsPage.page_size"
          :page-sizes="[...RECORD_PAGE_SIZES]"
          :total="recordsPage.total"
          @current-change="onRecordPageChange"
          @size-change="onRecordPageSizeChange"
        />
      </el-card>

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
          <div class="header-row">
            <span>
              失败记录摘要
              <el-tag v-if="detail.failure_summary.count" type="danger" class="header-tag">
                {{ detail.failure_summary.count }} 条
              </el-tag>
            </span>
            <el-button v-if="detail.failure_summary.count" @click="showFailedRecords">
              在记录列表里只看失败项
            </el-button>
          </div>
        </template>
        <el-empty
          v-if="!detail.failure_summary.count"
          description="没有失败记录（输入失败的行单列，不在这里）"
        />
        <template v-else>
          <el-table :data="detail.failure_summary.items" border>
            <el-table-column label="序号" width="80">
              <template #default="{ row }">{{ row.order_index + 1 }}</template>
            </el-table-column>
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
            <el-table-column label="详情" width="100" fixed="right">
              <template #default="{ row }">
                <el-link type="primary" @click.stop="openRecord(row.record_key)">查看详情</el-link>
              </template>
            </el-table-column>
          </el-table>
          <p class="muted">
            这里只列编号、失败阶段、错误码与说明，<strong>不含证据正文</strong>；点「查看详情」到记录详情页看
            完整输入、两阶段结果与<strong>被校验拒绝的原始输出</strong>。
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

      <!-- 导出（S05-05）：发起一份、看状态与历史、下载两份文件 -->
      <el-card class="panel">
        <template #header>
          <div class="header-row">
            <span>导出（Excel + JSONL）</span>
            <div>
              <el-tag v-if="exportInFlight" type="primary" size="small">
                导出进行中，约 2 秒刷新
              </el-tag>
              <el-tag v-else type="info" size="small">没有进行中的导出，已停止刷新</el-tag>
              <el-button class="refresh" :loading="exportLoading" @click="loadExports">
                刷新导出
              </el-button>
            </div>
          </div>
        </template>

        <el-alert v-if="exportHint" type="error" :closable="false" show-icon :title="exportHint" />

        <p class="muted">
          一次导出生成<strong>两份</strong>文件：Excel（分类结果、复核清单、失败清单、运行概况）
          与 JSONL（两阶段明细）。点击只表示<strong>已入队</strong>，文件由 worker 认领时才生成；
          快照也在那时捕获，所以任意批次状态都可以导出，导出内容可能含未处理记录。
        </p>

        <div class="action-row">
          <el-button
            type="primary"
            :disabled="!actions?.can_export || exportSubmitting"
            :loading="exportSubmitting"
            @click="doExport"
          >
            导出一份（Excel + JSONL）
          </el-button>
          <span v-if="actions?.export_disabled_reason" class="muted">
            {{ actions.export_disabled_reason }}
          </span>
          <span v-else class="muted">
            导出与判别互不阻塞：判别正在跑时导出的是一份当前进度的快照。
          </span>
        </div>

        <el-alert
          v-if="lastExport"
          class="block"
          type="info"
          :closable="false"
          show-icon
          :title="`上次提交：export_id ${lastExport.export_id}，job_id ${lastExport.job_id}${
            lastExport.reused ? '（命中幂等键，沿用第一次的入队结果）' : ''
          }`"
        />

        <template v-if="detail.latest_export">
          <h4>最近一次导出</h4>
          <p class="muted">
            导出 <span class="mono">{{ detail.latest_export.export_id }}</span>｜来源
            {{ detail.latest_export.source === 'automatic' ? '自动（批次终态时排入）' : '手动' }}｜状态
            {{ detail.latest_export.job_status ?? '—' }}｜排队于
            {{ detail.latest_export.created_at }}
          </p>
          <p v-if="detail.latest_export.captured_at" class="muted">
            快照捕获于 {{ detail.latest_export.captured_at }}（批次 revision
            {{ detail.latest_export.run_revision }}，当时状态
            {{ detail.latest_export.run_status_at_capture }}）
          </p>
          <p v-else class="muted">
            <strong>尚未捕获快照</strong>：导出任务还在队列里（或被暂停的派发挡着），
            需要有一个正在运行的 worker 认领它。
          </p>

          <el-alert
            v-if="exportRemaining"
            class="block"
            type="warning"
            :closable="false"
            show-icon
            :title="`这份导出含未处理记录（remaining = ${exportRemaining}）：它不是一份完整成功批次的导出，不能当作「全部判完」的证据。`"
          />
          <el-alert
            v-if="detail.latest_export.job_status === 'failed'"
            class="block"
            type="error"
            :closable="false"
            show-icon
            :title="`最近一次导出失败：${
              detail.latest_export.error?.code ?? 'EXPORT_FAILED'
            } ${detail.latest_export.error?.message ?? ''}`"
          >
            <p>
              失败只影响这一次导出：<strong>分类结果与批次状态都没有被改动</strong>。
              重新导出会生成新的 export_id 与新文件，不覆盖原文件，也不覆盖人工已改的文件。
            </p>
          </el-alert>
        </template>
        <el-empty v-else description="这个批次还没有排过导出" />

        <h4>导出历史（{{ exportPage?.total ?? 0 }} 次）</h4>
        <el-empty v-if="!exportsList.length" description="还没有导出记录" />
        <el-table v-else :data="exportsList" border>
          <el-table-column prop="created_at" label="排队时间" width="180" />
          <el-table-column label="来源" width="90">
            <template #default="{ row }">
              {{ row.source === 'automatic' ? '自动' : '手动' }}
            </template>
          </el-table-column>
          <el-table-column label="状态" width="120">
            <template #default="{ row }">
              <el-tag :type="statusTagType(row.job_status ?? '')" size="small">
                {{ row.job_status ?? '—' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="快照（捕获时间 / revision / 当时状态）" width="300">
            <template #default="{ row }">
              <template v-if="row.captured_at">
                {{ row.captured_at }} / {{ row.run_revision }} /
                {{ row.run_status_at_capture }}
              </template>
              <template v-else>尚未捕获快照</template>
            </template>
          </el-table-column>
          <el-table-column label="未处理" width="90">
            <template #default="{ row }">
              {{ row.counts_at_capture ? row.counts_at_capture.remaining : '—' }}
            </template>
          </el-table-column>
          <el-table-column label="文件" min-width="380">
            <template #default="{ row }">
              <div v-if="row.artifacts.length">
                <div v-for="artifact in row.artifacts" :key="artifact.artifact_id">
                  <el-link
                    type="primary"
                    :href="artifactDownloadUrl(artifact.artifact_id)"
                    target="_blank"
                  >
                    {{ artifact.download_name }}
                  </el-link>
                  <span class="muted">
                    （{{ artifact.size_bytes }} 字节，sha256
                    {{ artifact.sha256.slice(0, 12) }}…）
                  </span>
                </div>
              </div>
              <span v-else-if="row.job_status === 'failed'" class="muted">
                导出失败：{{ row.error?.code ?? 'EXPORT_FAILED' }}
                {{ row.error?.message ?? '' }}——分类结果未受影响，可重新导出
              </span>
              <span v-else class="muted">尚未生成（导出任务还没跑完）</span>
            </template>
          </el-table-column>
        </el-table>
        <p class="muted">
          下载由浏览器直接处理：Excel 用表格软件打开后，四张表的行序与「序号」列可直接
          和 JSONL 逐条对齐；「运行概况」表写明未处理数与被截断单元格的位置。文件落在
          <span class="mono">outputs/&lt;批次&gt;/&lt;导出&gt;/</span>，每次导出各占一个目录。
        </p>
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

.settlement-text {
  margin: 0;
}

.settlement-actions {
  margin-top: 0.5rem;
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
