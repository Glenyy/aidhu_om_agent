<!--
  S06-04：单条记录详情（独立路由 `/runs/:runId/records/:recordKey`）。

  为什么是独立页而不是抽屉：可深链接、可刷新、可把某一条发给别人核对；抽屉一刷新
  就丢上下文（S06 阶段文档 §0.2 第 2 项）。

  只展示后端返回的内容：
  - **输入快照**（建批次时留下的 13 列原文）——输入失败行也在，含失败原因；
  - **两个阶段的结果原文**：阶段一的资料充分性与逐字摘录、阶段二的标签／理由／
    复核项与「阶段一更正」，都按原文展示，页面不重排、不推断标签；
  - **调用尝试**：第几次、耗时、是否模拟、错误码；**只有被校验拒绝的那几次**带
    被拒正文（`raw_output`），这是 S03 返工成果在批次路径上的承接；
  - 不显示模型推理链、请求头与 SDK 调试堆栈。

  轮询约 2 秒；记录进入终态（完成／失败／输入失败）后停止。
-->

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import { ApiError, fetchRecord } from '../api/client';
import type { AttemptSummary, RunRecordDetail } from '../types/api';

const POLL_INTERVAL_MS = 2000;
/** 记录终态：到这里就不会再自己变了（重试失败项会另开一轮，此时状态回到 pending）。 */
const TERMINAL_STATUS = new Set(['completed', 'failed', 'input_invalid']);
/** 输入列的展示顺序：与输入合同一致，不是按对象的键序。 */
const INPUT_FIELDS = ['编号', 'q', 'a', ...Array.from({ length: 10 }, (_, i) => `ref${i + 1}`)];

const route = useRoute();
const router = useRouter();
const runId = computed(() => String(route.params.runId));
const recordKey = computed(() => String(route.params.recordKey));

const record = ref<RunRecordDetail | null>(null);
const hint = ref<string | null>(null);
const loading = ref(false);

let pollTimer: number | null = null;

const shouldPoll = computed(
  () => record.value !== null && !TERMINAL_STATUS.has(record.value.status),
);

const simulatedAttempts = computed(
  () => record.value?.attempt_summary.filter((entry) => entry.simulated).length ?? 0,
);

/** 被校验拒绝的那几次尝试：只有它们带正文，界面上单独分组展示。 */
const rejectedAttempts = computed(
  () => record.value?.attempt_summary.filter((entry) => entry.raw_output) ?? [],
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
    record.value = await fetchRecord(runId.value, recordKey.value);
    hint.value = null;
  } catch (error) {
    hint.value = error instanceof ApiError ? `[${error.code}] ${error.message}` : String(error);
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
watch([runId, recordKey], () => {
  record.value = null;
  stopPolling();
  void load();
});

function statusTagType(status: string): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'completed') return 'success';
  if (status === 'failed') return 'danger';
  if (status === 'input_invalid') return 'warning';
  return 'info';
}

/** 尝试的结果名与后端 `agent/pipeline.py` 的口径一致；未知值原样显示。 */
function outcomeText(outcome: string): string {
  if (outcome === 'ok') return '成功';
  if (outcome === 'validation_error') return '被校验拒绝';
  if (outcome === 'model_error') return '调用失败';
  if (outcome === 'unknown_after_interrupt') return '中断未知（仍占预算）';
  return outcome;
}

function attemptLabel(entry: AttemptSummary): string {
  return `${entry.stage === 'stage1' ? '阶段一' : '阶段二'} 第 ${entry.attempt} 次`;
}

function inputValue(field: string): string | null {
  return record.value?.input?.[field] ?? null;
}

/**
 * 回批次详情：**把 URL 上带过来的记录筛选原样带回去**（`openRecord` 从批次页带过来
 * 的就是这些键），所以返回后仍是同一个筛选与页码，不会把用户丢回未筛选的第 1 页
 * （S06 阶段文档 §2.1 第 10 项）。
 */
function backToRun(): void {
  void router.push({ path: `/runs/${runId.value}`, query: route.query });
}
</script>

<template>
  <div class="record-page">
    <el-card class="panel">
      <template #header>
        <div class="header-row">
          <span>记录详情</span>
          <div>
            <el-tag v-if="record" :type="statusTagType(record.status)" size="large">
              {{ record.status }}
            </el-tag>
            <el-button class="refresh" :loading="loading" @click="load">立即刷新</el-button>
            <el-button @click="backToRun">返回批次详情</el-button>
          </div>
        </div>
      </template>

      <el-alert v-if="hint" type="error" :closable="false" show-icon :title="hint" />
      <el-empty v-if="!record && !loading" description="没有读到这条记录" />

      <template v-if="record">
        <p class="muted">
          批次 <strong class="mono">{{ record.run_id }}</strong>｜记录键
          <span class="mono">{{ record.record_key }}</span>｜编号
          {{ record.record_id ?? '（无编号）' }}｜来源行 {{ record.source_row }}｜序号
          {{ record.order_index + 1 }}（存储层 <span class="mono">order_index</span>
          {{ record.order_index }}，从 0 起）｜revision {{ record.revision }}
        </p>
        <p class="muted">
          创建 {{ record.created_at }}｜最后更新 {{ record.updated_at }}
        </p>
        <p v-if="!shouldPoll" class="muted">这条记录已进入终态，页面已停止刷新。</p>
        <p v-else class="muted">
          记录还没判完（{{ record.status }}），页面每约 2 秒刷新一次。
        </p>

        <el-alert
          v-if="simulatedAttempts > 0"
          class="block"
          type="warning"
          :closable="false"
          show-icon
          title="本记录的调用含模拟结果，不是真实模型输出"
          :description="`${simulatedAttempts} / ${record.attempt_summary.length} 次调用是模拟的；模拟结果不能当作模型质量证据。`"
        />

        <h4>输入快照</h4>
        <el-table :data="INPUT_FIELDS.map((field) => ({ field, value: inputValue(field) }))" border>
          <el-table-column prop="field" label="列" width="120" />
          <el-table-column label="原文" min-width="420">
            <template #default="{ row }">
              <span v-if="row.value === null" class="muted">（空）</span>
              <span v-else class="preserve">{{ row.value }}</span>
            </template>
          </el-table-column>
        </el-table>

        <el-alert
          v-if="record.input_error"
          class="block"
          type="warning"
          :closable="false"
          show-icon
          title="这条记录的输入不可用，没有送去判别"
          :description="record.input_error.reason"
        />

        <h4>结论</h4>
        <p class="muted">
          标签：<strong>{{ record.label ?? '（无标签）' }}</strong>｜需人工复核：{{
            record.review_required === null ? '—' : record.review_required ? '是' : '否'
          }}
        </p>
        <el-alert
          v-if="record.failure"
          class="block"
          type="error"
          :closable="false"
          show-icon
          title="技术失败（无标签）"
          :description="`${record.failure.stage}／${record.failure.code}：${record.failure.message}（共 ${record.failure.attempt_count} 次尝试，本预算内可重试：${record.failure.retryable ? '是' : '否'}）`"
        />
      </template>
    </el-card>

    <template v-if="record">
      <!-- 阶段一：资料分析 -->
      <el-card class="panel">
        <template #header>阶段一：资料分析</template>
        <el-empty v-if="!record.stage1" description="没有阶段一结果（未执行或未提交）" />
        <template v-else>
          <p class="muted">
            资料充分性：<strong>{{ record.stage1.evidence_sufficiency }}</strong>
          </p>
          <p class="preserve">{{ record.stage1.reason }}</p>

          <h4>必需要点</h4>
          <ul class="plain">
            <li v-for="(item, index) in record.stage1.required_points" :key="index">{{ item }}</li>
            <li v-if="!record.stage1.required_points.length" class="muted">（无）</li>
          </ul>

          <h4>缺失信息</h4>
          <ul class="plain">
            <li v-for="(item, index) in record.stage1.missing_information" :key="index">
              {{ item }}
            </li>
            <li v-if="!record.stage1.missing_information.length" class="muted">（无）</li>
          </ul>

          <h4>资料冲突</h4>
          <ul class="plain">
            <li v-for="(item, index) in record.stage1.conflicts" :key="index">{{ item }}</li>
            <li v-if="!record.stage1.conflicts.length" class="muted">（无）</li>
          </ul>

          <h4>逐字摘录证据</h4>
          <el-table :data="record.stage1.evidence" border>
            <el-table-column prop="ref_id" label="来源" width="100" />
            <el-table-column label="摘录" min-width="420">
              <template #default="{ row }">
                <span class="preserve">{{ row.quote }}</span>
              </template>
            </el-table-column>
          </el-table>
        </template>
      </el-card>

      <!-- 阶段二：答案判别 -->
      <el-card class="panel">
        <template #header>阶段二：答案判别（含阶段一更正）</template>
        <el-empty v-if="!record.stage2" description="没有阶段二结果（未执行或未提交）" />
        <template v-else>
          <p class="muted">
            标签：<strong>{{ record.stage2.label }}</strong>｜资料充分性：{{
              record.stage2.evidence_sufficiency
            }}｜需人工复核：{{ record.stage2.review_required ? '是' : '否' }}
          </p>
          <p class="preserve">{{ record.stage2.reason }}</p>

          <h4>需人工复核的原因</h4>
          <ul class="plain">
            <li v-for="(item, index) in record.stage2.review_reasons" :key="index">{{ item }}</li>
            <li v-if="!record.stage2.review_reasons.length" class="muted">（无）</li>
          </ul>

          <h4>发现的问题</h4>
          <ul class="plain">
            <li v-for="(item, index) in record.stage2.issues" :key="index">{{ item }}</li>
            <li v-if="!record.stage2.issues.length" class="muted">（无）</li>
          </ul>

          <h4>逐字摘录证据</h4>
          <el-table :data="record.stage2.evidence" border>
            <el-table-column prop="ref_id" label="来源" width="100" />
            <el-table-column label="摘录" min-width="420">
              <template #default="{ row }">
                <span class="preserve">{{ row.quote }}</span>
              </template>
            </el-table-column>
          </el-table>

          <h4>阶段一更正</h4>
          <el-empty
            v-if="!record.stage2.stage1_corrections.length"
            description="没有更正（阶段一结论被原样采用）"
          />
          <div v-for="(item, index) in record.stage2.stage1_corrections" :key="index" class="block">
            <p class="muted">
              更正项：<strong>{{ item.corrected_item }}</strong>｜是否影响分类：{{
                item.affects_classification ? '是' : '否'
              }}
            </p>
            <p class="preserve">{{ item.reason }}</p>
            <el-table :data="item.evidence" border>
              <el-table-column prop="ref_id" label="来源" width="100" />
              <el-table-column label="摘录" min-width="420">
                <template #default="{ row }">
                  <span class="preserve">{{ row.quote }}</span>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </template>
      </el-card>

      <!-- 调用尝试：第几次、耗时、模拟与否、被拒原文 -->
      <el-card class="panel">
        <template #header>
          调用尝试
          <el-tag v-if="record.attempt_summary.length" type="info" class="header-tag">
            共 {{ record.attempt_summary.length }} 次
          </el-tag>
        </template>
        <el-empty v-if="!record.attempt_summary.length" description="还没有调用过模型" />
        <template v-else>
          <el-table :data="record.attempt_summary" border>
            <el-table-column label="阶段/第几次" width="130">
              <template #default="{ row }">{{ attemptLabel(row) }}</template>
            </el-table-column>
            <el-table-column label="结果" width="170">
              <template #default="{ row }">{{ outcomeText(row.outcome) }}</template>
            </el-table-column>
            <el-table-column label="模型" width="180">
              <template #default="{ row }">{{ row.model ?? '—' }}</template>
            </el-table-column>
            <el-table-column label="耗时(ms)" width="110">
              <template #default="{ row }">{{ row.latency_ms ?? '—' }}</template>
            </el-table-column>
            <el-table-column label="模拟" width="80">
              <template #default="{ row }">
                <el-tag v-if="row.simulated" type="warning" size="small">模拟</el-tag>
                <span v-else>否</span>
              </template>
            </el-table-column>
            <el-table-column label="错误" min-width="220">
              <template #default="{ row }">
                {{ row.error_code ? `${row.error_code}：${row.error_message ?? ''}` : '—' }}
              </template>
            </el-table-column>
          </el-table>

          <h4>被校验拒绝的原始输出</h4>
          <p class="muted">
            只留存<strong>被校验拒绝</strong>的那几次的最终正文（不是推理链）；成功尝试不留正文。
            {{ rejectedAttempts.length }} 次可查看。
          </p>
          <el-empty
            v-if="!rejectedAttempts.length"
            description="这条记录没有被拒的原始输出"
          />
          <el-collapse v-else>
            <el-collapse-item
              v-for="entry in rejectedAttempts"
              :key="`${entry.stage}-${entry.attempt}`"
              :title="`${attemptLabel(entry)}（${entry.error_code ?? '无错误码'}）${
                entry.raw_output_truncated ? '［已截断，保头尾］' : ''
              }`"
            >
              <pre class="raw">{{ entry.raw_output }}</pre>
            </el-collapse-item>
          </el-collapse>
        </template>
      </el-card>
    </template>
  </div>
</template>

<style scoped>
.record-page {
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

.mono {
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
}

.refresh {
  margin-left: 0.5rem;
}

.header-tag {
  margin-left: 0.5rem;
}

.block {
  margin-top: 0.75rem;
}

.muted {
  color: var(--el-text-color-secondary);
}

.preserve {
  white-space: pre-wrap;
  word-break: break-word;
}

.plain {
  margin: 0.25rem 0 0.75rem;
  padding-left: 1.25rem;
}

.raw {
  margin: 0;
  padding: 0.75rem;
  background: var(--el-fill-color-light);
  white-space: pre-wrap;
  word-break: break-word;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}

.record-page :deep(.el-table__row) {
  cursor: default;
}
</style>
