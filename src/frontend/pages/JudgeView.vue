<!--
  S03-07 界面骨架：上传 → 预检 → 开始判别（整批）。
  S06-05 改造：**删除单条判别与模拟/真实开关**（`POST /api/judge` 已删除，判一条由
  整批 + 记录详情取代，S06 阶段文档 §0.2 第 1、7 项）。

  模式条改为**只读指示**：原来那个开关只影响单条判别，整批跑什么模式由 worker 的
  `--mode` 启动参数决定，开关会让人误以为能控制整批。现在显示的是**事实**——
  最近一次 worker 的模式与已发生的模拟调用次数，取自最近一个批次的详情
  （`execution_control.last_worker` 与 `call_statistics.*.simulated`）。

  本页只展示后端返回的状态与计数，不推导业务类别；预检的 `records[].record_key`
  是给人看的「编号或来源行」，只用于预览，不是判别选择键。
-->
<script setup lang="ts">
import { ElMessage } from 'element-plus';
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';

import {
  ApiError,
  createRun,
  fetchRun,
  fetchRuns,
  fetchSamples,
  newIdempotencyKey,
  sampleDownloadUrl,
  uploadWorkbook,
  validateUpload,
} from '../api/client';
import type { RunDetail, SampleInfo, UploadData, ValidationData } from '../types/api';

const router = useRouter();
const upload = ref<UploadData | null>(null);
const selectedSheet = ref<string | null>(null);
const validation = ref<ValidationData | null>(null);
const startingBatch = ref(false);
const hint = ref<{ kind: 'error' | 'info'; text: string } | null>(null);
const uploading = ref(false);
const validating = ref(false);
const fileInput = ref<HTMLInputElement | null>(null);
const samples = ref<SampleInfo[]>([]);
const selectedSample = ref<string>('');

/** 最近一次 worker 的归属与整批模式；取自**最近一个批次**的详情。 */
const workerRun = ref<RunDetail | null>(null);
/** `unknown`＝还没读或读失败；`none`＝库里一个批次都没有。 */
const workerState = ref<'loading' | 'unknown' | 'none' | 'known'>('loading');
const workerHint = ref<string | null>(null);

const precheckPassed = computed(() => validation.value?.status === 'passed');
const canStartBatch = computed(() => precheckPassed.value && !startingBatch.value);
const currentSample = computed(
  () => samples.value.find((sample) => sample.name === selectedSample.value) ?? null,
);

const lastWorker = computed(() => workerRun.value?.execution_control.last_worker ?? null);

/** 模式文案；`--mode` 只接受 mock／real（worker.py 的 `MODES`）。 */
const workerModeText = computed<string>(() => {
  const mode = lastWorker.value?.mode;
  if (mode === 'mock') return '模拟模式';
  if (mode === 'real') return '真实模式';
  return mode ?? '未记录';
});
const workerIsReal = computed(() => lastWorker.value?.mode === 'real');

/** 含模拟调用 N/M 次：两个阶段的 `simulated` 与 `attempts` 各自相加。 */
const simulatedCalls = computed(() => {
  const stats = workerRun.value?.call_statistics;
  if (!stats) return { simulated: 0, attempts: 0 };
  return {
    simulated: stats.stage1.simulated + stats.stage2.simulated,
    attempts: stats.stage1.attempts + stats.stage2.attempts,
  };
});

/**
 * 读 worker 事实：先取最近一个批次，再读它的详情。
 *
 * 一个批次都没有时读不到 `execution_control`（它是批次详情里的字段），此时**不猜**
 * 模式，只说明「还没有记录可读」。失败也只提示，不影响本页的上传与预检。
 */
async function loadWorkerFact(): Promise<void> {
  workerState.value = 'loading';
  workerHint.value = null;
  try {
    const page = await fetchRuns(1, 1, null);
    const newest = page.items[0];
    if (!newest) {
      workerRun.value = null;
      workerState.value = 'none';
      return;
    }
    workerRun.value = await fetchRun(newest.run_id);
    workerState.value = 'known';
  } catch (error) {
    workerRun.value = null;
    workerState.value = 'unknown';
    workerHint.value =
      error instanceof ApiError ? `[${error.code}] ${error.message}` : String(error);
  }
}

onMounted(async () => {
  // 样例清单取不到不影响使用：仍可上传自己准备的文件。
  try {
    samples.value = await fetchSamples();
    selectedSample.value = samples.value[0]?.name ?? '';
  } catch {
    samples.value = [];
  }
  await loadWorkerFact();
});

function downloadSample(): void {
  if (!currentSample.value) return;
  // 浏览器直接下载文件本体；下载后请回到本页上传该文件。
  window.location.assign(sampleDownloadUrl(currentSample.value.name));
}

function fail(error: unknown): void {
  if (error instanceof ApiError) {
    hint.value = { kind: 'error', text: `[${error.code}] ${error.message}` };
  } else {
    hint.value = { kind: 'error', text: String(error) };
  }
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MiB`;
}

function pickFile(): void {
  fileInput.value?.click();
}

async function onFileChange(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = '';
  if (!file) return;

  hint.value = null;
  uploading.value = true;
  upload.value = null;
  validation.value = null;
  try {
    const data = await uploadWorkbook(file);
    upload.value = data;
    selectedSheet.value = data.suggested_sheet;
    hint.value = {
      kind: 'info',
      text: `上传完成。上传成功不代表内容通过预检。请确认工作表后点击「预检」。`,
    };
  } catch (error) {
    fail(error);
  } finally {
    uploading.value = false;
  }
}

async function runValidate(): Promise<void> {
  if (!upload.value) return;
  hint.value = null;
  validating.value = true;
  validation.value = null;
  try {
    validation.value = await validateUpload(upload.value.upload_id, selectedSheet.value);
  } catch (error) {
    fail(error);
  } finally {
    validating.value = false;
  }
}

/**
 * 开始整批判别：从这次预检创建一个批次并跳到批次详情。
 *
 * 创建是写操作、且会产生模型调用，因此带 `Idempotency-Key`（plan/08 §4）。请求
 * 失败时**不重试**：界面把错误如实显示，用户再点一次会生成新键——这是有意的新动作，
 * 而不是「不确定结果」的重复提交。
 */
async function startBatch(): Promise<void> {
  if (!validation.value) return;
  hint.value = null;
  startingBatch.value = true;
  try {
    const created = await createRun(validation.value.validation_id, newIdempotencyKey());
    ElMessage.success(
      created.reused
        ? `该请求此前已提交，沿用批次 ${created.run_id}`
        : `已创建批次 ${created.run_id}，判别任务 ${created.job_id} 已入队`,
    );
    // 批次只入队；实际执行由 worker 完成，因此跳到详情页看进度。
    await router.push(`/runs/${created.run_id}`);
  } catch (error) {
    fail(error);
  } finally {
    startingBatch.value = false;
  }
}

function openNewestRun(): void {
  const run = workerRun.value;
  if (run) void router.push(`/runs/${run.run_id}`);
}
</script>

<template>
  <div class="judge-page">
    <!-- 1. 执行模式：只读指示，取值来自最近一个批次的详情 -->
    <el-card
      class="mode-bar"
      :class="{ 'mode-bar--real': workerIsReal, 'mode-bar--mock': !workerIsReal }"
    >
      <div class="mode-bar__row">
        <div class="mode-bar__labels">
          <strong>整批执行模式（只读）</strong>
          <el-tag v-if="workerState === 'known'" :type="workerIsReal ? 'danger' : 'info'" effect="dark">
            {{ workerModeText }}
          </el-tag>
          <el-tag v-else type="warning" effect="dark">未记录</el-tag>
        </div>
        <el-button :loading="workerState === 'loading'" @click="loadWorkerFact">刷新模式</el-button>
      </div>

      <p class="muted mode-bar__line">
        整批模式由 <span class="mono">worker</span> 的启动参数
        <span class="mono">--mode</span> 决定，页面<strong>不可切换</strong>（原来的模拟/真实开关只影响
        已被删除的单条判别，留着会让人误以为能控制整批）。
      </p>

      <template v-if="workerState === 'known' && lastWorker">
        <p class="muted mode-bar__line">
          最近一次 worker：<span class="mono">{{ lastWorker.worker_id }}</span
          >｜上线 {{ lastWorker.started_at ?? '—' }}｜模式 {{ lastWorker.mode ?? '—' }}
          ｜本批次含模拟调用 {{ simulatedCalls.simulated }} / {{ simulatedCalls.attempts }} 次
        </p>
        <p class="muted mode-bar__line">
          取自批次 <el-link type="primary" @click="openNewestRun">{{ workerRun?.run_id }}</el-link>
          （{{ workerRun?.status }}）。批次一起跑就是这个模式。
        </p>
        <el-alert
          v-if="workerIsReal"
          type="warning"
          :closable="false"
          show-icon
          title="最近一次 worker 跑的是真实模式：整批判别会调用真实模型服务并产生费用。"
        />
      </template>

      <template v-else-if="workerState === 'known'">
        <el-alert
          type="info"
          :closable="false"
          show-icon
          title="本机还没有 worker 上线记录"
          description="批次会排队，但不会有任何记录被判别。启动 `python -m aidhu_om_agent worker --mode mock` 后才开始执行；排队不是失败。"
        />
      </template>

      <el-alert
        v-else-if="workerState === 'none'"
        type="info"
        :closable="false"
        show-icon
        title="还没有任何批次，因此没有 worker 模式可读"
        description="模式取自批次详情里的 execution_control；建一个批次（并启动 worker）后这里就会显示实际模式。"
      />

      <el-alert
        v-else-if="workerState === 'unknown'"
        type="error"
        :closable="false"
        show-icon
        title="没有读到 worker 模式（不影响上传与预检）"
        :description="workerHint ?? ''"
      />
    </el-card>

    <el-alert
      v-if="hint"
      class="hint"
      :type="hint.kind === 'error' ? 'error' : 'info'"
      :closable="false"
      show-icon
      :title="hint.text"
    />

    <!-- 2. 上传与选表 -->
    <el-card class="panel">
      <template #header>第 1 步：上传 Excel 并选择工作表</template>
      <input
        ref="fileInput"
        class="hidden-input"
        type="file"
        accept=".xlsx"
        @change="onFileChange"
      />
      <el-button type="primary" :loading="uploading" @click="pickFile">选择 .xlsx 文件</el-button>
      <span class="muted">只接受 .xlsx；上传只保存文件，不调用模型。</span>

      <div class="sample-row">
        <span>没有文件？下载一份合成样例：</span>
        <el-select v-model="selectedSample" placeholder="选择样例" class="sheet-select" :disabled="!samples.length">
          <el-option
            v-for="sample in samples"
            :key="sample.name"
            :label="sample.filename"
            :value="sample.name"
          />
        </el-select>
        <el-button :disabled="!currentSample" @click="downloadSample">下载示例文件</el-button>
        <span v-if="currentSample" class="muted">
          {{ currentSample.description }}（{{ currentSample.record_count }} 条记录）
        </span>
        <span v-else class="muted">样例清单不可用，请自行准备 .xlsx。</span>
      </div>
      <p class="muted sample-note">
        样例是程序现场生成的合成数据，不含任何真实业务资料；下载后请用上方「选择 .xlsx 文件」上传。
      </p>

      <el-table v-if="upload" class="upload-table" :data="[upload]" border>
        <el-table-column prop="original_filename" label="文件名" />
        <el-table-column label="大小" width="120">
          <template #default="{ row }">{{ formatBytes(row.size_bytes) }}</template>
        </el-table-column>
        <el-table-column prop="sha256" label="sha256" min-width="280" />
      </el-table>

      <div v-if="upload" class="sheet-row">
        <span>工作表：</span>
        <el-select v-model="selectedSheet" placeholder="请选择工作表" class="sheet-select">
          <el-option
            v-for="sheet in upload.sheets"
            :key="sheet.name"
            :label="sheet.visible ? sheet.name : `${sheet.name}（隐藏）`"
            :value="sheet.name"
          />
        </el-select>
        <span v-if="upload.suggested_sheet" class="muted">
          建议：{{ upload.suggested_sheet }}
        </span>
        <el-button type="primary" :loading="validating" :disabled="!selectedSheet" @click="runValidate">
          预检
        </el-button>
      </div>
    </el-card>

    <!-- 3. 预检结果：同时用于复验 S02 -->
    <el-card v-if="validation" class="panel">
      <template #header>
        第 2 步：预检结果（S02 复验）
        <el-tag :type="precheckPassed ? 'success' : 'danger'" class="header-tag">
          {{ precheckPassed ? 'passed' : 'blocked' }}
        </el-tag>
      </template>

      <p class="muted">
        validation_id：{{ validation.validation_id }}｜工作表：{{ validation.sheet_name ?? '未确定' }}
        ｜输入合同版本：{{ validation.input_contract_version }}
      </p>
      <p class="muted">
        文件 sha256：{{ validation.file_sha256 }}
        <template v-if="upload">
          ｜与上传时一致：<el-tag
            :type="upload.sha256 === validation.file_sha256 ? 'success' : 'danger'"
            size="small"
          >
            {{ upload.sha256 === validation.file_sha256 ? '是' : '否' }}
          </el-tag>
        </template>
      </p>

      <el-table :data="[validation.counts]" border class="counts-table">
        <el-table-column prop="total" label="总记录" />
        <el-table-column prop="valid" label="有效" />
        <el-table-column prop="input_invalid" label="输入失败" />
        <el-table-column prop="skipped_blank_rows" label="跳过空行" />
      </el-table>

      <el-alert
        v-if="validation.blockers.length"
        type="error"
        :closable="false"
        show-icon
        class="blocker-alert"
        title="批次阻断（不调用模型，需先修正输入后重新预检）"
      >
        <ul>
          <li v-for="blocker in validation.blockers" :key="blocker.code">
            [{{ blocker.code }}] {{ blocker.message }}
          </li>
        </ul>
      </el-alert>

      <div v-if="validation.row_errors.length" class="sub-block">
        <h4>输入失败记录（{{ validation.row_errors.length }} 条）</h4>
        <el-table :data="validation.row_errors" border>
          <el-table-column prop="source_row" label="来源行" width="100" />
          <el-table-column prop="record_id" label="编号" width="140" />
          <el-table-column prop="reason" label="原因" />
        </el-table>
      </div>

      <div v-if="precheckPassed" class="sub-block batch-block">
        <h4>开始整批判别（S04）</h4>
        <el-button
          type="primary"
          :disabled="!canStartBatch"
          :loading="startingBatch"
          @click="startBatch"
        >
          开始判别（整批）
        </el-button>
        <span class="muted">
          为本次预检创建一个批次（共 {{ validation.counts.total ?? 0 }} 条记录，含输入失败的
          {{ validation.counts.input_invalid ?? 0 }} 条）。创建只<strong>入队</strong>，不会立即调用模型：
          需要有一个运行中的 worker 才会真正执行。
        </span>
        <el-alert
          v-if="workerState === 'known' && !lastWorker"
          class="batch-worker-note"
          type="info"
          :closable="false"
          show-icon
          title="还没有 worker 上线记录"
          description="现在创建批次只会排队，不会开始判别（排队不是失败）。请另开一个终端运行 python -m aidhu_om_agent worker --mode mock。"
        />
        <p class="muted batch-note">
          批次模式由 worker 的启动参数决定，不在本页切换；整批进度、恢复、调用统计、记录列表与
          导出都在「批次」页查看。单条记录的证据（含被校验拒绝的原始输出）在记录详情页。
        </p>
      </div>

      <div v-if="validation.warnings.length" class="sub-block">
        <h4>提示（{{ validation.warnings.length }} 条）</h4>
        <ul>
          <li v-for="warning in validation.warnings" :key="warning.code">
            [{{ warning.code }}] {{ warning.message }}
          </li>
        </ul>
      </div>

      <div class="sub-block">
        <h4>有效记录预览（{{ validation.records.length }} 条）</h4>
        <p class="muted preview-note">
          「编号或来源行」是给人看的可读键，只用于预览；判别的选择键是批次里程序生成的记录键，
          点开批次详情里的记录列表即可看到。
        </p>
        <el-empty v-if="!validation.records.length" description="没有有效记录" />
        <el-table v-else :data="validation.records" border max-height="320">
          <el-table-column label="编号或来源行" width="160">
            <template #default="{ row }">{{ row.record_key }}</template>
          </el-table-column>
          <el-table-column prop="record_id" label="编号" width="140" />
          <el-table-column prop="source_row" label="来源行" width="100" />
          <el-table-column prop="ref_count" label="资料数" width="100" />
          <el-table-column label="问题摘要" min-width="280">
            <template #default="{ row }">{{ row.q_preview ?? '（无 q）' }}</template>
          </el-table-column>
        </el-table>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.judge-page {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.mode-bar--mock {
  border-left: 6px solid var(--el-color-info);
}

.mode-bar--real {
  border-left: 6px solid var(--el-color-danger);
}

.mode-bar__row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
}

.mode-bar__labels {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.mode-bar__line {
  margin: 0.5rem 0 0;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
}

.panel :deep(.el-card__header) {
  font-weight: 600;
}

.header-tag {
  margin-left: 0.5rem;
}

.hidden-input {
  display: none;
}

.muted {
  color: var(--el-text-color-secondary);
  margin-left: 0.5rem;
}

.upload-table,
.counts-table,
.sheet-row,
.sample-row,
.sub-block {
  margin-top: 1rem;
}

.sample-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
}

.sample-note,
.preview-note {
  margin: 0.5rem 0 0;
  font-size: 0.85rem;
  margin-left: 0;
}

.batch-block :deep(.el-button) {
  margin-right: 0.5rem;
}

.batch-note,
.batch-worker-note {
  margin: 0.5rem 0 0;
  font-size: 0.85rem;
}

.batch-worker-note {
  width: 100%;
}

.sheet-select {
  width: 220px;
}

.sub-block h4 {
  margin: 1rem 0 0.5rem;
}

.blocker-alert ul {
  margin: 0.25rem 0;
  padding-left: 1.2rem;
}
</style>
