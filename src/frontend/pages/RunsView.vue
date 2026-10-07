<!--
  S04-07：批次列表。S06-03 补上**筛选与分页控件**（接口本来就支持，界面此前没有）。

  筛选条件与页码写进 **URL query**：刷新、收藏、把链接发给别人之后仍然生效；
  浏览器前进/后退也能回到上一种筛选。列表只展示后端返回的状态与计数，不推导结论；
  轮询约 2 秒，**当前页全部批次都进终态后停止**。

  导出不放在列表页：一份导出属于某个批次，界面入口在批次详情页（S05-05）。列表页
  只把这件事在说明里讲清楚，不放按钮——列表上的按钮会让人以为「导出整张表」。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import { ApiError, fetchRuns } from '../api/client';
import type { RunListItem, RunListPage } from '../types/api';

//: 批次终态；`interrupted` 也是终态——它要等新 worker 启动或用户点恢复才会改变。
const TERMINAL_STATUS = new Set(['completed', 'partial_failed', 'failed', 'interrupted']);
//: 与后端 `GET /api/runs` 的 `status` 取值一致（plan/08 §5）。
const STATUS_OPTIONS = [
  'queued',
  'running',
  'completed',
  'partial_failed',
  'failed',
  'interrupted',
] as const;
//: 后端分页默认 50、上限 100（plan/08 §5）；这里只提供这三档，不做任意输入。
const PAGE_SIZES = [20, 50, 100] as const;
const DEFAULT_PAGE_SIZE = 50;
const POLL_INTERVAL_MS = 2000;

const route = useRoute();
const router = useRouter();
const page = ref<RunListPage | null>(null);
const loading = ref(false);
const hint = ref<string | null>(null);

let pollTimer: number | null = null;

function firstValue(raw: unknown): unknown {
  return Array.isArray(raw) ? raw[0] : raw;
}

function queryPage(raw: unknown): number {
  const value = Number(firstValue(raw));
  return Number.isInteger(value) && value >= 1 ? value : 1;
}

function queryPageSize(raw: unknown): number {
  const value = Number(firstValue(raw));
  return (PAGE_SIZES as readonly number[]).includes(value) ? value : DEFAULT_PAGE_SIZE;
}

function queryStatus(raw: unknown): string | null {
  const value = firstValue(raw);
  return typeof value === 'string' && (STATUS_OPTIONS as readonly string[]).includes(value)
    ? value
    : null;
}

// URL query 是**唯一真源**：控件改的是它，读列表用的也是它，两者不会各存一份。
const currentPage = computed(() => queryPage(route.query.page));
const pageSize = computed(() => queryPageSize(route.query.page_size));
const statusFilter = computed(() => queryStatus(route.query.status));

const runs = computed<RunListItem[]>(() => page.value?.items ?? []);
const anyRunning = computed(() => runs.value.some((run) => !TERMINAL_STATUS.has(run.status)));

function stopPolling(): void {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function load(): Promise<void> {
  loading.value = true;
  try {
    page.value = await fetchRuns(currentPage.value, pageSize.value, statusFilter.value);
    hint.value = null;
  } catch (error) {
    hint.value = error instanceof ApiError ? `[${error.code}] ${error.message}` : String(error);
    stopPolling();
  } finally {
    loading.value = false;
  }
  // 有批次仍在跑就继续轮询；当前页全终态就停下，避免页面空转。
  if (anyRunning.value) {
    if (pollTimer === null) {
      pollTimer = window.setInterval(() => void load(), POLL_INTERVAL_MS);
    }
  } else {
    stopPolling();
  }
}

/** 只把非默认值写进 URL：默认页保持 `/runs` 这样的干净地址。 */
function queryOf(nextPage: number, nextSize: number, nextStatus: string | null): string {
  const query: Record<string, string> = {};
  if (nextPage > 1) query.page = String(nextPage);
  if (nextSize !== DEFAULT_PAGE_SIZE) query.page_size = String(nextSize);
  if (nextStatus) query.status = nextStatus;
  return new URLSearchParams(query).toString();
}

function applyQuery(nextPage: number, nextSize: number, nextStatus: string | null): void {
  const target = queryOf(nextPage, nextSize, nextStatus);
  if (target === queryOf(currentPage.value, pageSize.value, statusFilter.value)) {
    return; // URL 没变：路由不会重新触发，也就没有要重新加载的东西
  }
  // 只由 URL 变化驱动加载（下面的 watch），控件本身不直接请求——否则会打两次。
  void router.replace({ query: Object.fromEntries(new URLSearchParams(target)) });
}

/** 换筛选条件必须回到第 1 页：停在第 5 页筛出 1 条会让用户以为「没有结果」。 */
function onStatusChange(value: string | null): void {
  stopPolling();
  applyQuery(1, pageSize.value, value ?? null);
}

function onPageChange(value: number): void {
  applyQuery(value, pageSize.value, statusFilter.value);
}

function onPageSizeChange(value: number): void {
  applyQuery(1, queryPageSize(value), statusFilter.value);
}

// 首次进入、深链接、浏览器前进/后退都走这里；控件改动经 URL 后同样落到这里。
watch(() => route.query, () => void load(), { immediate: true });
onBeforeUnmount(stopPolling);

function openRun(runId: string): void {
  void router.push(`/runs/${runId}`);
}

function statusTagType(status: string): 'success' | 'warning' | 'danger' | 'info' | 'primary' {
  if (status === 'completed') return 'success';
  if (status === 'partial_failed') return 'warning';
  if (status === 'failed') return 'danger';
  if (status === 'running') return 'primary';
  return 'info';
}

function statusText(status: string): string {
  if (status === 'queued') return '排队中';
  if (status === 'running') return '判别中';
  if (status === 'completed') return '已完成';
  if (status === 'partial_failed') return '部分失败';
  if (status === 'failed') return '失败';
  if (status === 'interrupted') return '已中断';
  return status;
}
</script>

<template>
  <div class="runs-page">
    <el-card class="panel">
      <template #header>
        <div class="header-row">
          <span>批次列表</span>
          <div>
            <el-tag v-if="anyRunning" type="primary" size="small">有批次进行中，约 2 秒刷新</el-tag>
            <el-tag v-else type="info" size="small">当前页已无进行中的批次，已停止刷新</el-tag>
            <el-button class="refresh" :loading="loading" @click="load">立即刷新</el-button>
          </div>
        </div>
      </template>

      <el-alert v-if="hint" type="error" :closable="false" show-icon :title="hint" />

      <div class="filter-row">
        <span class="muted">按状态筛选：</span>
        <el-select
          class="status-select"
          :model-value="statusFilter"
          clearable
          placeholder="全部状态"
          @change="onStatusChange"
        >
          <el-option
            v-for="status in STATUS_OPTIONS"
            :key="status"
            :label="`${statusText(status)}（${status}）`"
            :value="status"
          />
        </el-select>
        <span class="muted">
          筛选条件写在地址栏里，刷新或把链接发给别人仍然是这个筛选。
        </span>
      </div>

      <p class="muted">
        共 {{ page?.total ?? 0 }} 个批次（第 {{ page?.page ?? 1 }} 页，每页
        {{ page?.page_size ?? DEFAULT_PAGE_SIZE }} 条）。点开批次可发起导出并下载
        Excel 与 JSONL（S05）、查看记录列表与单条证据详情。
      </p>

      <el-empty
        v-if="!runs.length && !loading"
        :description="
          statusFilter
            ? `没有状态为 ${statusFilter} 的批次：清除筛选可看全部`
            : '还没有批次：到首页上传文件、预检通过后点「开始判别（整批）」'
        "
      />

      <el-table v-else :data="runs" border @row-click="(row: RunListItem) => openRun(row.run_id)">
        <el-table-column label="批次标识" min-width="240">
          <template #default="{ row }">
            <el-link type="primary" @click.stop="openRun(row.run_id)">{{ row.run_id }}</el-link>
          </template>
        </el-table-column>
        <el-table-column prop="original_filename" label="来源文件" min-width="200" />
        <el-table-column label="状态" width="160">
          <template #default="{ row }">
            <el-tag :type="statusTagType(row.status)" size="small">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="计数（已判/失败/输入失败/总）" width="220">
          <template #default="{ row }">
            {{ row.counts.classified }} / {{ row.counts.failed }} /
            {{ row.counts.input_invalid }} / {{ row.counts.total }}
          </template>
        </el-table-column>
        <el-table-column label="进度" width="140">
          <template #default="{ row }">{{ row.progress_percent.toFixed(1) }}%</template>
        </el-table-column>
        <el-table-column label="revision" width="100">
          <template #default="{ row }">{{ row.revision }}</template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建时间" width="190" />
      </el-table>

      <el-pagination
        v-if="page"
        class="pager"
        background
        layout="prev, pager, next, sizes, total"
        :current-page="page.page"
        :page-size="page.page_size"
        :page-sizes="[...PAGE_SIZES]"
        :total="page.total"
        @current-change="onPageChange"
        @size-change="onPageSizeChange"
      />
    </el-card>
  </div>
</template>

<style scoped>
.header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
}

.filter-row {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  flex-wrap: wrap;
  margin-bottom: 0.75rem;
}

.status-select {
  width: 220px;
}

.refresh {
  margin-left: 0.5rem;
}

.pager {
  margin-top: 0.75rem;
}

.muted {
  color: var(--el-text-color-secondary);
}

.runs-page :deep(.el-table__row) {
  cursor: pointer;
}
</style>
