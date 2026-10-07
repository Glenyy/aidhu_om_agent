<!--
  S04-07：批次列表（最小形态，不做筛选分页——那是 S06）。

  列表只展示后端返回的状态与计数，不推导任何结论；轮询约 2 秒，**全部批次都进入
  终态后停止**。服务重启后刷新本页，批次仍在，因为批次、记录与计数都在 SQLite 里。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';

import { ApiError, fetchRuns } from '../api/client';
import type { RunListItem, RunListPage } from '../types/api';

//: 批次终态；`interrupted` 也是终态——它要等新 worker 启动或用户点恢复才会改变。
const TERMINAL_STATUS = new Set(['completed', 'partial_failed', 'failed', 'interrupted']);
const POLL_INTERVAL_MS = 2000;

const router = useRouter();
const page = ref<RunListPage | null>(null);
const loading = ref(false);
const hint = ref<string | null>(null);

let pollTimer: number | null = null;

const runs = computed<RunListItem[]>(() => page.value?.items ?? []);
const anyRunning = computed(() =>
  runs.value.some((run) => !TERMINAL_STATUS.has(run.status)),
);

function stopPolling(): void {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function load(): Promise<void> {
  loading.value = true;
  try {
    page.value = await fetchRuns(1, 50);
    hint.value = null;
  } catch (error) {
    hint.value =
      error instanceof ApiError ? `[${error.code}] ${error.message}` : String(error);
    stopPolling();
  } finally {
    loading.value = false;
  }
  // 有批次仍在跑就继续轮询；全部终态就停下，避免页面空转。
  if (anyRunning.value) {
    if (pollTimer === null) {
      pollTimer = window.setInterval(() => void load(), POLL_INTERVAL_MS);
    }
  } else {
    stopPolling();
  }
}

onMounted(() => void load());
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
</script>

<template>
  <div class="runs-page">
    <el-card class="panel">
      <template #header>
        <div class="header-row">
          <span>批次列表（S04-07）</span>
          <div>
            <el-tag v-if="anyRunning" type="primary" size="small">有批次进行中，约 2 秒刷新</el-tag>
            <el-tag v-else type="info" size="small">全部批次已结束，已停止刷新</el-tag>
            <el-button class="refresh" :loading="loading" @click="load">立即刷新</el-button>
          </div>
        </div>
      </template>

      <el-alert v-if="hint" type="error" :closable="false" show-icon :title="hint" />

      <p class="muted">
        共 {{ page?.total ?? 0 }} 个批次（第 {{ page?.page ?? 1 }} 页，每页
        {{ page?.page_size ?? 50 }} 条）。筛选分页、记录列表与导出属 S06。
      </p>

      <el-empty v-if="!runs.length && !loading" description="还没有批次：到「判一条」页上传文件、预检通过后点「开始判别（整批）」" />

      <el-table v-else :data="runs" border @row-click="(row: RunListItem) => openRun(row.run_id)">
        <el-table-column label="批次标识" min-width="240">
          <template #default="{ row }">
            <el-link type="primary" @click.stop="openRun(row.run_id)">{{ row.run_id }}</el-link>
          </template>
        </el-table-column>
        <el-table-column prop="original_filename" label="来源文件" min-width="200" />
        <el-table-column label="状态" width="140">
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

.refresh {
  margin-left: 0.5rem;
}

.muted {
  color: var(--el-text-color-secondary);
}

.runs-page :deep(.el-table__row) {
  cursor: pointer;
}
</style>
