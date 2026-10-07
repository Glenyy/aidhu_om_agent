<!--
  S03-07 界面骨架：上传 → 预检 → 判一条（单页）。

  范围限于 S03：批次列表/详情、批量进度、恢复、导出与下载属 S06。
  模拟模式为默认；真实模式需要二次确认，且结果区会显著标注。
  本页只展示后端返回的状态、标签与证据，不推导业务类别。
-->
<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus';
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';

import {
  ApiError,
  createRun,
  fetchJob,
  fetchSamples,
  judgeRecord,
  newIdempotencyKey,
  sampleDownloadUrl,
  uploadWorkbook,
  validateUpload,
} from '../api/client';
import type {
  JudgeMode,
  JobPayload,
  JobStatus,
  SampleInfo,
  UploadData,
  ValidationData,
} from '../types/api';

const TERMINAL_STATUS = new Set(['completed', 'partial_failed', 'failed']);
const POLL_INTERVAL_MS = 2000;
const TICK_INTERVAL_MS = 1000;

const router = useRouter();
const mode = ref<JudgeMode>('mock');
const upload = ref<UploadData | null>(null);
const selectedSheet = ref<string | null>(null);
const validation = ref<ValidationData | null>(null);
const selectedRecordKey = ref<string | null>(null);
const job = ref<JobPayload | null>(null);
const hint = ref<{ kind: 'error' | 'info'; text: string } | null>(null);
const uploading = ref(false);
const validating = ref(false);
const submitting = ref(false);
const startingBatch = ref(false);
const fileInput = ref<HTMLInputElement | null>(null);
const samples = ref<SampleInfo[]>([]);
const selectedSample = ref<string>('');

let pollTimer: number | null = null;
let tickTimer: number | null = null;
// 真实模式下单条判别可能跑数分钟，本地每秒刷新已用时间，避免看起来像卡死。
const nowTick = ref(Date.now());

const isMock = computed(() => mode.value === 'mock');
const running = computed(() => job.value !== null && !TERMINAL_STATUS.has(job.value.status));

/** 已用毫秒：终态用后端给的最终值，运行中按开始时间本地累加。 */
const elapsedMs = computed<number | null>(() => {
  const current = job.value;
  if (!current) return null;
  if (TERMINAL_STATUS.has(current.status)) return current.elapsed_ms;
  if (!current.started_at) return null;
  const started = Date.parse(current.started_at);
  if (Number.isNaN(started)) return null;
  return Math.max(0, nowTick.value - started);
});

const elapsedText = computed<string>(() => {
  const value = elapsedMs.value;
  if (value === null) return '尚未开始';
  const totalSeconds = Math.floor(value / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  if (minutes < 60) return `${minutes} 分 ${String(totalSeconds % 60).padStart(2, '0')} 秒`;
  return `${Math.floor(minutes / 60)} 小时 ${String(minutes % 60).padStart(2, '0')} 分`;
});

/** 运行中显示「第 N 次尝试」：同一次判别最多尝试 3 次，失败会重试。 */
const attemptText = computed<string>(() =>
  job.value?.current_attempt ? `第 ${job.value.current_attempt} 次尝试` : '',
);
const precheckPassed = computed(() => validation.value?.status === 'passed');
const canJudge = computed(
  () => precheckPassed.value && selectedRecordKey.value !== null && !submitting.value && !running.value,
);
/** 整批判别：预检通过即可，与单条判别互不影响（整批走批次接口与 worker）。 */
const canStartBatch = computed(() => precheckPassed.value && !startingBatch.value);
const currentSample = computed(
  () => samples.value.find((sample) => sample.name === selectedSample.value) ?? null,
);

onMounted(async () => {
  // 样例清单取不到不影响使用：仍可上传自己准备的文件。
  try {
    samples.value = await fetchSamples();
    selectedSample.value = samples.value[0]?.name ?? '';
  } catch {
    samples.value = [];
  }
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

function stopPolling(): void {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
  stopTicker();
}

function startTicker(): void {
  stopTicker();
  nowTick.value = Date.now();
  tickTimer = window.setInterval(() => {
    nowTick.value = Date.now();
  }, TICK_INTERVAL_MS);
}

function stopTicker(): void {
  if (tickTimer !== null) {
    window.clearInterval(tickTimer);
    tickTimer = null;
  }
}

function startPolling(jobId: string): void {
  stopPolling();
  startTicker();
  pollTimer = window.setInterval(() => {
    void pollOnce(jobId);
  }, POLL_INTERVAL_MS);
}

async function pollOnce(jobId: string): Promise<JobPayload | null> {
  try {
    const payload = await fetchJob(jobId);
    job.value = payload;
    if (TERMINAL_STATUS.has(payload.status)) {
      stopPolling();
      // 终态用后端给的最终耗时，不再依赖本地时钟。
      nowTick.value = Date.now();
    }
    return payload;
  } catch (error) {
    stopPolling();
    fail(error);
    return null;
  }
}

onBeforeUnmount(stopPolling);

async function onModeChange(value: string | number | boolean): Promise<void> {
  if (value !== true) {
    mode.value = 'mock';
    return;
  }
  try {
    await ElMessageBox.confirm(
      '真实模式会调用真实模型服务并产生费用，单条判别可能需要数分钟。\n请确认你已在本机配置服务地址与凭据。',
      '切换到真实模式',
      { type: 'warning', confirmButtonText: '确认切换', cancelButtonText: '保持模拟' },
    );
    mode.value = 'real';
  } catch {
    mode.value = 'mock';
  }
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
  selectedRecordKey.value = null;
  job.value = null;
  stopPolling();
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
  selectedRecordKey.value = null;
  job.value = null;
  stopPolling();
  try {
    validation.value = await validateUpload(upload.value.upload_id, selectedSheet.value);
  } catch (error) {
    fail(error);
  } finally {
    validating.value = false;
  }
}

async function runJudge(): Promise<void> {
  if (!validation.value || selectedRecordKey.value === null) return;
  hint.value = null;
  submitting.value = true;
  job.value = null;
  try {
    const created = await judgeRecord(
      validation.value.validation_id,
      selectedRecordKey.value,
      mode.value,
    );
    ElMessage.success(`已提交判别任务（${mode.value === 'mock' ? '模拟' : '真实'}模式）`);
    const payload = await pollOnce(created.job_id);
    if (payload && !TERMINAL_STATUS.has(payload.status)) startPolling(created.job_id);
  } catch (error) {
    fail(error);
  } finally {
    submitting.value = false;
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

function sufficiencyText(value: string): string {
  if (value === 'sufficient') return '资料充分';
  if (value === 'insufficient') return '资料不足';
  if (value === 'uncertain') return '适用性不确定';
  return value;
}

// 任务终态时后端会把 current_stage 置空（services/judging.py）。此时若仍显示“等待中”
// 会与左侧的状态列自相矛盾，故按状态区分：仅在非终态、且尚未进入任何阶段时才叫“等待中”。
function stageText(stage: string | null, status: JobStatus): string {
  if (stage === 'stage1') return '阶段一：资料分析';
  if (stage === 'stage2') return '阶段二：答案判别';
  if (TERMINAL_STATUS.has(status)) return '无（任务已结束）';
  return '等待中';
}
</script>

<template>
  <div class="judge-page">
    <!-- 1. 模式条：常驻可见，模拟为默认 -->
    <el-card class="mode-bar" :class="{ 'mode-bar--mock': isMock, 'mode-bar--real': !isMock }">
      <div class="mode-bar__row">
        <div class="mode-bar__labels">
          <strong>判别模式</strong>
          <el-switch
            :model-value="!isMock"
            active-text="真实模式"
            inactive-text="模拟模式"
            @change="onModeChange"
          />
        </div>
        <el-tag v-if="isMock" type="info" effect="dark" size="large">
          模拟模式（默认）：不会调用真实模型，结果仅供交互验证
        </el-tag>
        <el-tag v-else type="danger" effect="dark" size="large">
          真实模式：会调用真实模型服务并产生费用
        </el-tag>
      </div>
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
        <p class="muted batch-note">
          批次模式由 worker 决定（模拟或真实），不在本页切换；整批进度、恢复与调用统计在
          「批次」页查看。
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
        <h4>有效记录（{{ validation.records.length }} 条）</h4>
        <el-empty v-if="!validation.records.length" description="没有有效记录" />
        <el-radio-group v-else v-model="selectedRecordKey" class="record-list">
          <el-radio
            v-for="record in validation.records"
            :key="record.record_key"
            :value="record.record_key"
            border
          >
            {{ record.record_key }}（第 {{ record.source_row }} 行，{{ record.ref_count }} 份资料）——
            {{ record.q_preview ?? '（无 q）' }}
          </el-radio>
        </el-radio-group>
      </div>
    </el-card>

    <!-- 4. 判一条 -->
    <el-card v-if="validation" class="panel">
      <template #header>
        第 3 步：判一条
        <el-tag v-if="isMock" type="info" class="header-tag">模拟模式</el-tag>
      </template>

      <el-button type="primary" :disabled="!canJudge" :loading="running || submitting" @click="runJudge">
        {{ isMock ? '用模拟模式判这一条' : '用真实模式判这一条（产生费用）' }}
      </el-button>
      <span class="muted">当前选择：{{ selectedRecordKey ?? '未选择记录' }}</span>

      <div v-if="job" class="result">
        <el-alert
          v-if="isMock"
          type="warning"
          :closable="false"
          show-icon
          title="模拟结果，非真实模型输出：以下标签与证据由确定性合成响应生成，不能作为模型质量证据。"
        />
        <p class="muted job-line">
          任务 {{ job.job_id }}｜状态：{{ job.status }}｜当前阶段：{{
            stageText(job.current_stage, job.status)
          }}<template v-if="attemptText">（{{ attemptText }}）</template>｜已用：{{ elapsedText }}
        </p>
        <p v-if="running" class="muted slow-note">
          真实模型单次调用可能需要 1 分钟以上；同一阶段最多尝试 3 次，因此单条判别可能持续数分钟。
          页面会自动轮询，无需保持请求连接。
        </p>

        <el-alert
          v-if="job.error"
          type="error"
          :closable="false"
          show-icon
          :title="`${job.error.code}：${job.error.message}`"
        />

        <template v-if="job.result">
          <p>
            <strong>最终标签：</strong>
            <el-tag v-if="job.result.label" type="success" size="large">{{ job.result.label }}</el-tag>
            <el-tag v-else type="danger" size="large">无标签（技术失败，不填造判断）</el-tag>
          </p>

          <el-alert
            v-if="job.result.failure"
            type="error"
            :closable="false"
            show-icon
            :title="`${job.result.failure.stage} 失败（${job.result.failure.code}，尝试 ${job.result.failure.attempt_count} 次）：${job.result.failure.message}`"
          />

          <template v-if="job.result.stage2">
            <h4>阶段二：答案判别</h4>
            <p>理由：{{ job.result.stage2.reason }}</p>
            <p>充分性：{{ sufficiencyText(job.result.stage2.evidence_sufficiency) }}</p>
            <p>
              需人工复核：
              <el-tag :type="job.result.stage2.review_required ? 'warning' : 'info'">
                {{ job.result.stage2.review_required ? '是' : '否' }}
              </el-tag>
              <span v-if="job.result.stage2.review_reasons.length">
                —— {{ job.result.stage2.review_reasons.join('；') }}
              </span>
            </p>
            <div v-if="job.result.stage2.issues.length">
              <h5>关键问题</h5>
              <ul>
                <li v-for="(issue, index) in job.result.stage2.issues" :key="index">{{ issue }}</li>
              </ul>
            </div>
            <div v-if="job.result.stage2.stage1_corrections.length">
              <h5>对阶段一的更正</h5>
              <ul>
                <li
                  v-for="(correction, index) in job.result.stage2.stage1_corrections"
                  :key="index"
                >
                  {{ correction.corrected_item }}——{{ correction.reason }}
                  （影响最终分类：{{ correction.affects_classification ? '是' : '否' }}）
                </li>
              </ul>
            </div>
          </template>

          <template v-if="job.result.stage1">
            <h4>阶段一：资料分析（原结果，未被更正覆盖）</h4>
            <p>充分性：{{ sufficiencyText(job.result.stage1.evidence_sufficiency) }}</p>
            <p>核心作答要点：{{ job.result.stage1.required_points.join('；') || '（无）' }}</p>
            <p>
              资料缺口：{{ job.result.stage1.missing_information.join('；') || '（无）' }}
            </p>
            <p>资料冲突：{{ job.result.stage1.conflicts.join('；') || '（无）' }}</p>
            <h5>证据（ref 编号 + 原文摘录）</h5>
            <el-empty
              v-if="!job.result.stage1.evidence.length"
              description="阶段一没有给出证据（资料不足时允许）"
            />
            <ul v-else class="evidence">
              <li v-for="(item, index) in job.result.stage1.evidence" :key="index">
                <el-tag size="small">{{ item.ref_id }}</el-tag>
                <blockquote>{{ item.quote }}</blockquote>
              </li>
            </ul>
          </template>

          <h4>调用摘要</h4>
          <el-table :data="job.result.attempts" border>
            <el-table-column type="expand">
              <template #default="{ row }">
                <div class="attempt-detail">
                  <p v-if="row.error_message">
                    <strong>校验问题：</strong>{{ row.error_message }}
                  </p>
                  <template v-if="row.raw_output">
                    <p>
                      <strong>被拒的原始输出</strong>
                      <span v-if="row.raw_output_truncated" class="muted">
                        （过长已截断，保留头尾）
                      </span>
                      <span class="muted">——仅模型输出正文，不含推理过程。</span>
                    </p>
                    <pre class="raw-output">{{ row.raw_output }}</pre>
                  </template>
                  <p v-else class="muted">
                    本次尝试没有留存原始输出（只有被校验拒绝的尝试会留存）。
                  </p>
                </div>
              </template>
            </el-table-column>
            <el-table-column prop="stage" label="阶段" width="90" />
            <el-table-column prop="attempt" label="第几次" width="90" />
            <el-table-column prop="outcome" label="结果" width="140" />
            <el-table-column prop="model" label="模型" width="160" />
            <el-table-column label="模拟" width="70">
              <template #default="{ row }">{{ row.simulated ? '是' : '否' }}</template>
            </el-table-column>
            <el-table-column prop="latency_ms" label="耗时(ms)" width="100" />
            <el-table-column prop="error_code" label="错误码" width="160" />
          </el-table>
        </template>
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

.sample-note {
  margin: 0.5rem 0 0;
  font-size: 0.85rem;
}

.batch-block :deep(.el-button) {
  margin-right: 0.5rem;
}

.batch-note {
  margin: 0.5rem 0 0;
  font-size: 0.85rem;
}

.sheet-select {
  width: 220px;
}

.sub-block h4,
.result h4 {
  margin: 1rem 0 0.5rem;
}

.sub-block h5,
.result h5 {
  margin: 0.75rem 0 0.25rem;
}

.record-list {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 0.4rem;
}

.record-list :deep(.el-radio) {
  margin-right: 0;
}

.evidence blockquote {
  margin: 0.25rem 0 0.5rem;
  padding: 0.4rem 0.6rem;
  border-left: 3px solid var(--el-border-color);
  white-space: pre-wrap;
  word-break: break-word;
}

.blocker-alert ul,
.result ul {
  margin: 0.25rem 0;
  padding-left: 1.2rem;
}

.result {
  margin-top: 1rem;
}

.job-line {
  font-variant-numeric: tabular-nums;
}

.slow-note {
  margin-top: -0.5rem;
}

.attempt-detail {
  padding: 0.5rem 1rem;
}

.attempt-detail p {
  margin: 0.25rem 0;
}

/* 被拒的原始输出可能很长：等宽、只读、可滚动，避免撑破表格。 */
.raw-output {
  margin: 0.25rem 0 0.5rem;
  padding: 0.5rem;
  max-height: 20rem;
  overflow: auto;
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
