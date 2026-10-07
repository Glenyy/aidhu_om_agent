<!--
  S07-03：评估结果页（**只读**）。

  这一页把**已经算完、已经落库**的评估摊开给人看，七个区块对应 S07 阶段文档
  §2.1：划分清单与每类数量、混淆矩阵、逐类指标、达标判定、逐条对照、冻结清单、
  评估历史与报告下载。

  三条边界在这一页上是硬性的，改代码时别松：

  1. **不能在这发起评估**：计算入口只有 CLI `evaluate`（plan/08）。这一页连写请求
     都没有，接口层也没有对应的 POST。页面上的命令只是**给人照抄的文本**。
  2. **不能在这改标签**：人工修订是 S08 的事，而且按 plan/04「人工修订不计入 agent
     预测」——若允许网页改标签再重算，评估的分母就不是模型的实际表现了。
  3. **不推导业务结论**：标签、指标、达标判定一律照后端返回值显示；页面自己只做
     矩阵的**行/列合计**这种纯算术，并用它和 `counts.scored`（有合法预测的条数，
     与矩阵同一口径）对照（对不上就说明页面画错了，此时必须显出来，不能默默算完）。
     对照**不要**改用 `metrics.scored_total`：那是「有效核定」总数，含漏评，必然多出
     漏评那几条，会把正确的矩阵报成「对不上」。

  历史是列表：同一批次可以用不同划分评多次，每次都留痕。当前看哪一份写在地址栏的
  `eid` 里，省略时取最新一份——这样「把链接发给别人」看到的还是同一份评估。
-->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';

import {
  ApiError,
  fetchEvaluation,
  fetchEvaluationRecords,
  fetchRunEvaluations,
} from '../api/client';
import {
  EVALUATION_AGREE_VALUES,
  EVALUATION_PAGE_SIZES,
  RECORD_LABELS,
  RECORD_PAGE_SIZES,
  RECORD_STATUSES,
} from '../constants';
import type {
  EvaluationDetail,
  EvaluationListPage,
  EvaluationRecordItem,
  EvaluationRecordListPage,
  EvaluationSplit,
  EvaluationSummary,
} from '../types/api';

const route = useRoute();
const router = useRouter();
const runId = computed(() => String(route.params.runId));

const historyPage = ref<EvaluationListPage | null>(null);
const historyHint = ref<string | null>(null);
const historyLoading = ref(false);

const detail = ref<EvaluationDetail | null>(null);
const detailHint = ref<string | null>(null);
const detailLoading = ref(false);

const recordsPage = ref<EvaluationRecordListPage | null>(null);
const recordHint = ref<string | null>(null);
const recordLoading = ref(false);

// ------------------------------------------------------------------ query 往返

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

function queryBool(raw: unknown): boolean | null {
  const value = queryText(raw);
  if (value === 'true') return true;
  if (value === 'false') return false;
  return null;
}

function queryPageSize(raw: unknown, allowed: readonly number[], fallback: number): number {
  const value = queryInt(raw, fallback);
  return allowed.includes(value) ? value : fallback;
}

/** 当前查看的评估；省略时取历史里最新的那一份。 */
const selectedId = computed<string | null>(
  () => queryText(route.query.eid) ?? historyPage.value?.items[0]?.evaluation_id ?? null,
);

/** 「是否一致」的三态：未给＝不筛、`true`／`false` 作为布尔发给后端。 */
function queryAgree(raw: unknown): boolean | null {
  const value = queryEnum(raw, EVALUATION_AGREE_VALUES);
  if (value === 'true') return true;
  if (value === 'false') return false;
  return null;
}

const recordQuery = computed(() => ({
  page: queryInt(route.query.epage, 1),
  pageSize: queryPageSize(route.query.esize, RECORD_PAGE_SIZES, 50),
  agree: queryAgree(route.query.eagree),
  status: queryEnum(route.query.estatus, RECORD_STATUSES),
  recordId: queryText(route.query.erid),
}));

function recordQueryString(next: {
  page: number;
  pageSize: number;
  agree: boolean | null;
  status: string | null;
  recordId: string | null;
}): string {
  const query: Record<string, string> = {};
  if (next.page > 1) query.epage = String(next.page);
  if (next.pageSize !== 50) query.esize = String(next.pageSize);
  if (next.agree !== null) query.eagree = String(next.agree);
  if (next.status) query.estatus = next.status;
  if (next.recordId) query.erid = next.recordId;
  return new URLSearchParams(query).toString();
}

/** 合并进当前 URL query（`eid` 等其它键原样保留），只有变化时才导航。 */
function applyRecordQuery(patch: Partial<typeof recordQuery.value>): void {
  const next = { ...recordQuery.value, ...patch };
  if (recordQueryString(next) === recordQueryString(recordQuery.value)) return;
  void router.replace({ query: { ...route.query, ...Object.fromEntries(new URLSearchParams(recordQueryString(next))) } });
}

/** 换筛选条件回到第 1 页：停在第 5 页筛出 1 条会让用户以为「没有结果」。 */
function onRecordFilterChange(patch: Partial<typeof recordQuery.value>): void {
  applyRecordQuery({ ...patch, page: 1 });
}

/** 下拉的 change 在清空时可能给 `null` 或空串，统一当成「没有这个筛选」。 */
function onAgreeFilterChange(value: string | null): void {
  onRecordFilterChange({ agree: value === 'true' ? true : value === 'false' ? false : null });
}

function onStatusFilterChange(value: string | null): void {
  onRecordFilterChange({ status: value ? value : null });
}

/** 编号按**精确匹配**筛（plan/08 §6）；空串表示不筛。 */
function onRecordIdFilterChange(value: string): void {
  const text = (value ?? '').trim();
  onRecordFilterChange({ recordId: text ? text : null });
}

function clearRecordFilters(): void {
  onRecordFilterChange({ page: 1, agree: null, status: null, recordId: null });
}

function onRecordPageChange(value: number): void {
  applyRecordQuery({ page: value });
}

function onRecordPageSizeChange(value: number): void {
  applyRecordQuery({ page: 1, pageSize: queryPageSize(value, RECORD_PAGE_SIZES, 50) });
}

function onRecordRowClick(row: EvaluationRecordItem): void {
  openRecord(row.record_key);
}

// ---------------------------------------------------------------------- 读取

async function loadHistory(): Promise<void> {
  historyLoading.value = true;
  try {
    historyPage.value = await fetchRunEvaluations(
      runId.value,
      1,
      queryPageSize(route.query.hsize, EVALUATION_PAGE_SIZES, 20),
    );
    historyHint.value = null;
  } catch (error) {
    historyHint.value =
      error instanceof ApiError ? `[${error.code}] ${error.message}` : String(error);
  } finally {
    historyLoading.value = false;
  }
}

async function loadDetail(): Promise<void> {
  const target = selectedId.value;
  if (!target) {
    detail.value = null;
    return;
  }
  detailLoading.value = true;
  try {
    detail.value = await fetchEvaluation(target);
    detailHint.value = null;
  } catch (error) {
    detail.value = null;
    detailHint.value =
      error instanceof ApiError ? `[${error.code}] ${error.message}` : String(error);
  } finally {
    detailLoading.value = false;
  }
}

async function loadRecords(): Promise<void> {
  const target = selectedId.value;
  if (!target) {
    recordsPage.value = null;
    return;
  }
  recordLoading.value = true;
  try {
    recordsPage.value = await fetchEvaluationRecords(target, recordQuery.value);
    recordHint.value = null;
  } catch (error) {
    recordHint.value =
      error instanceof ApiError ? `[${error.code}] ${error.message}` : String(error);
  } finally {
    recordLoading.value = false;
  }
}

async function loadAll(): Promise<void> {
  await loadHistory();
  await loadDetail();
  await loadRecords();
}

onMounted(() => void loadAll());
// 换批次（组件被复用）时全部重来，别把上一个批次的评估留在页面上。
watch(runId, () => void loadAll());
// `eid` 变化（点历史里的「查看」）→ 换一份详情与逐条对照。
watch(() => route.query.eid, () => {
  void loadDetail();
  void loadRecords();
});
// 逐条对照的筛选与页码写在 URL 里，改了就重新取这一页（浏览器前进/后退也一样）。
watch(
  () => [route.query.epage, route.query.esize, route.query.eagree, route.query.estatus, route.query.erid],
  () => void loadRecords(),
);

// -------------------------------------------------------------- 矩阵、判定展示

const history = computed<EvaluationSummary[]>(() => historyPage.value?.items ?? []);
const records = computed<EvaluationRecordItem[]>(() => recordsPage.value?.items ?? []);

/** 行 = 人工真值、列 = agent 预测，顺序按后端的三分类顺序。 */
const confusionRows = computed(() => {
  const matrix = detail.value?.metrics.confusion;
  if (!matrix) return [];
  return RECORD_LABELS.map((gold) => {
    const cells = RECORD_LABELS.map((predicted) => matrix[gold]?.[predicted] ?? 0);
    return { gold, cells, total: cells.reduce((sum, cell) => sum + cell, 0) };
  });
});

const confusionColumnTotals = computed(() =>
  RECORD_LABELS.map((_, index) =>
    confusionRows.value.reduce((sum, row) => sum + row.cells[index], 0),
  ),
);

/**
 * 矩阵总和。它必须等于**有合法预测**的条数：**对不上就说明画错了**。
 *
 * 这里不自动纠错、也不隐藏：界面把两个数并排显示，对不上时打红字。指标表已经
 * 由后端算好，页面若悄悄补一个「看得出来对不上」的矩阵，就成了在页面上编数据。
 */
const confusionGrandTotal = computed(() =>
  confusionRows.value.reduce((sum, row) => sum + row.total, 0),
);

/**
 * 拿来对照的「有预测」条数。
 *
 * 用后端落库时的 `counts.scored`（口径是「有效核定 − 漏评」，与矩阵同一公式），
 * **不是** `metrics.scored_total`——后者是「有效核定」总数，把技术失败与未跑完的
 * 漏评也算在内，拿它对照会必然差出漏评那几条，进而把正确的矩阵报成「对不上」。
 */
const confusionScoredCount = computed<number | null>(
  () => detail.value?.counts.scored ?? null,
);
const confusionConsistent = computed(
  () =>
    confusionScoredCount.value !== null &&
    confusionGrandTotal.value === confusionScoredCount.value,
);

/** 逐类指标表；顺序与三分类一致，不按 F1 排序（排序会掩盖少样本类）。 */
const perClassRows = computed(() => {
  const cells = detail.value?.metrics.per_class;
  if (!cells) return [];
  return RECORD_LABELS.map((label) => cells[label]).filter((cell) => cell !== undefined);
});

/** 本次评的是哪一侧、共几条；与划分清单里那两个总数同一个来源。 */
const selectedSideCount = computed<number | null>(() => {
  const info = detail.value?.split_info;
  if (!info) return null;
  return info.split === 'calibration' ? info.calibration : info.holdout;
});

/** 人工排除的编号与理由；没记理由的照实写「未记理由」，不替它编一个。 */
const excludedRows = computed(() => {
  const info = detail.value?.split_info;
  if (!info) return [];
  return info.excluded_ids.map((id) => ({
    id,
    reason: info.excluded_reasons[id] ?? '（未记理由）',
  }));
});

function thresholdTagType(passed: boolean | null): 'success' | 'danger' | 'info' {
  if (passed === true) return 'success';
  if (passed === false) return 'danger';
  return 'info';
}

function thresholdText(passed: boolean | null): string {
  if (passed === true) return '达标';
  if (passed === false) return '未达标';
  return '不可计算';
}

function labelCounts(counts: Record<string, number> | undefined): string {
  if (!counts) return '—';
  const parts = RECORD_LABELS.map((label) => `${label} ${counts[label] ?? 0}`);
  return parts.join('｜');
}

function agreeText(agree: boolean | null): string {
  if (agree === null) return '无预测';
  return agree ? '一致' : '不一致';
}

function agreeTagType(agree: boolean | null): 'success' | 'danger' | 'info' {
  if (agree === null) return 'info';
  return agree ? 'success' : 'danger';
}

function splitText(split: EvaluationSplit): string {
  return split === 'calibration' ? '校准集' : '保留集';
}

/** 这一份评估对应的 CLI 命令；**只是给人照抄的文本**，页面上没有执行入口。 */
const cliHint = computed(() => {
  const current = detail.value;
  if (!current) return null;
  return [
    'python -m aidhu_om_agent evaluate',
    `  --run-id ${current.run_id}`,
    `  --gold <核定文件.xlsx>`,
    `  --split-manifest <划分清单.json>`,
    `  --split ${current.split}`,
  ].join(' \\\n');
});

function openRecord(recordKey: string): void {
  // 带上本页的 query（含 `eid` 与逐条对照的筛选），返回时回到同一份评估的同一页。
  void router.push({ path: `/runs/${runId.value}/records/${recordKey}`, query: route.query });
}

function viewEvaluation(evaluationId: string): void {
  // 换一份评估：逐条对照的筛选清空（不同评估的编号集合可能完全不同）。
  void router.replace({
    query: { ...route.query, eid: evaluationId, epage: undefined, erid: undefined, eagree: undefined, estatus: undefined },
  });
}

/**
 * 把内嵌的报告正文存成本地文件。
 *
 * 报告不是「已登记产物」，走不了 `/api/artifacts/.../download`；正文已经在手上，
 * 用 Blob 存盘即可，不必为此新增接口。
 */
function downloadReport(): void {
  const current = detail.value;
  if (!current?.report_text) return;
  const textName = current.report_files.find((item) => item.kind === 'text')?.name;
  const blob = new Blob([current.report_text], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = textName ?? `${current.evaluation_id}-report.md`;
  link.click();
  URL.revokeObjectURL(url);
}
</script>

<template>
  <div class="evaluation-page">
    <el-card class="panel">
      <template #header>
        <div class="header-row">
          <span>批次评估（只读）</span>
          <div>
            <el-button class="refresh" :loading="historyLoading" @click="loadAll">
              立即刷新
            </el-button>
            <el-button @click="router.push(`/runs/${runId}`)">返回批次详情</el-button>
          </div>
        </div>
      </template>

      <p class="muted">
        批次 <strong class="mono">{{ runId }}</strong>。本页<strong>只读</strong>：
        不触发计算、不调用模型、不编辑标签。评估由命令行产生（见下方命令），
        结果落库后这里能看到；同一批次可以用不同划分评多次，每次都留痕。
      </p>
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        title="指标只说明这个批次在指定划分上的表现，不代替业务验收。保留集成绩才是质量口径；校准集是拿来调规则用的。"
      />

      <el-alert v-if="historyHint" class="block" type="error" :closable="false" show-icon :title="historyHint" />
    </el-card>

    <!-- 7. 评估历史（同一批次的多份评估） -->
    <el-card class="panel">
      <template #header>
        <div class="header-row">
          <span>评估历史</span>
          <el-tag v-if="historyPage" type="info" size="small">
            共 {{ historyPage.total }} 份
          </el-tag>
        </div>
      </template>

      <el-empty
        v-if="!history.length && !historyLoading"
        description="这个批次还没有评估记录：评估由命令行 evaluate 产生，网页不能发起"
      />

      <template v-else>
        <el-table :data="history" border>
          <el-table-column prop="evaluation_id" label="评估" min-width="240" />
          <el-table-column label="划分" width="100">
            <template #default="{ row }">{{ splitText(row.split) }}</template>
          </el-table-column>
          <el-table-column prop="created_at" label="评估时间" width="200" />
          <el-table-column prop="gold_filename" label="核定文件" min-width="180" />
          <el-table-column label="有效/评分/排除" width="150">
            <template #default="{ row }">
              {{ row.valid_count }} / {{ row.scored_count }} / {{ row.excluded_count }}
            </template>
          </el-table-column>
          <el-table-column label="Macro-F1" width="180">
            <template #default="{ row }">{{ row.macro_f1 ?? '—' }}</template>
          </el-table-column>
          <el-table-column label="达标判定" width="110">
            <template #default="{ row }">
              <el-tag :type="row.passed ? 'success' : 'danger'" size="small">
                {{ row.passed ? '全部达标' : '未全部达标' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="130">
            <template #default="{ row }">
              <el-link type="primary" @click="viewEvaluation(row.evaluation_id)">查看</el-link>
            </template>
          </el-table-column>
        </el-table>
        <p class="muted">
          「达标判定」看的是全部检查：只要有一项未达标或不可计算，这里就不是「全部达标」。
          <strong>不可计算（分母为 0）不算通过。</strong>
        </p>
      </template>
    </el-card>

    <el-alert v-if="detailHint" type="error" :closable="false" show-icon :title="detailHint" />
    <el-empty
      v-if="!detail && !detailLoading && history.length"
      description="没有读到这份评估的详情"
    />

    <template v-if="detail">
      <!-- 概要：锁定「这是哪一批、哪一侧、什么版本」 -->
      <el-card class="panel">
        <template #header>
          <div class="header-row">
            <span>评估概要</span>
            <div>
              <el-tag size="small" type="primary">{{ splitText(detail.split) }}</el-tag>
              <el-button class="refresh" :disabled="!detail.report_text" @click="downloadReport">
                下载报告（{{ detail.report_files.find((item) => item.kind === 'text')?.name }}）
              </el-button>
            </div>
          </div>
        </template>

        <p class="muted">
          评估 <strong class="mono">{{ detail.evaluation_id }}</strong>｜于
          {{ detail.created_at }} 生成｜来源批次
          {{ detail.run ? detail.run.source_filename : '—' }}（状态
          {{ detail.run ? detail.run.status : '—' }}，revision
          {{ detail.run ? detail.run.revision : '—' }}）
        </p>
        <p class="muted">
          核定文件 {{ detail.gold.filename }}（数据版本 {{ detail.gold.data_version }}，
          合同版本 {{ detail.gold.contract_version }}，sha256
          <span class="mono">{{ detail.gold.sha256_prefix }}…</span>）｜划分清单
          {{ detail.manifest.filename }}（版本 {{ detail.manifest.version }}，sha256
          <span class="mono">{{ detail.manifest.sha256_prefix }}…</span>）
        </p>
        <p class="muted">
          参与口径：有效核定 <strong>{{ detail.counts.valid }}</strong> 条 → 有合法预测
          <strong>{{ detail.counts.scored }}</strong> 条（漏评
          <strong>{{ detail.counts.missing }}</strong> 条）｜人工排除
          {{ detail.counts.excluded }} 条
        </p>

        <el-table class="block" :data="[detail.metrics]" border>
          <el-table-column label="总体一致率" min-width="200">
            <template #default="{ row }">{{ row.agreement.display }}</template>
          </el-table-column>
          <el-table-column label="Macro-F1" min-width="200">
            <template #default="{ row }">{{ row.macro_f1.display }}</template>
          </el-table-column>
          <el-table-column label="非正确类误判正确率" min-width="220">
            <template #default="{ row }">{{ row.wrong_as_correct.display }}</template>
          </el-table-column>
          <el-table-column label="分类完成率" min-width="200">
            <template #default="{ row }">{{ row.completion.display }}</template>
          </el-table-column>
          <el-table-column label="复核比例" min-width="200">
            <template #default="{ row }">{{ row.review_ratio.display }}</template>
          </el-table-column>
        </el-table>
        <p class="muted">
          每个指标都带分子与分母。<strong>分母为 0 记「不可计算」</strong>，不当作 0。
          一致率的分母是「有预测的条数」，完成率的分母是「有效核定的条数」。
        </p>
      </el-card>

      <!-- 4. 达标判定 -->
      <el-card class="panel">
        <template #header>达标判定</template>
        <el-table :data="detail.thresholds" border>
          <el-table-column prop="name" label="检查项" width="200" />
          <el-table-column prop="requirement" label="要求" width="160" />
          <el-table-column prop="actual" label="实际" min-width="220" />
          <el-table-column label="结论" width="120">
            <template #default="{ row }">
              <el-tag :type="thresholdTagType(row.passed)" size="small">
                {{ thresholdText(row.passed) }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>
        <p class="muted">
          「每类样本数 ≥ 5」是<strong>保留集</strong>的门槛：校准集每类只有 1 条也属正常，
          因此这一项在校准集上通常不达标，报告里照实显示，不据此判断质量。
        </p>

        <h4>技术失败与漏评</h4>
        <p class="muted">
          没有合法预测的记录<strong>不进混淆矩阵</strong>，在这里单列：
          技术失败 <strong>{{ detail.metrics.failed_ids.length }}</strong> 条、未跑完
          <strong>{{ detail.metrics.unfinished_ids.length }}</strong> 条。
          它们拉低分类完成率，但不会被算成「判错」。
        </p>
        <p v-if="detail.metrics.failed_ids.length" class="muted mono">
          技术失败：{{ detail.metrics.failed_ids.join('、') }}
        </p>
        <p v-if="detail.metrics.unfinished_ids.length" class="muted mono">
          未跑完：{{ detail.metrics.unfinished_ids.join('、') }}
        </p>
      </el-card>

      <!-- 2. 混淆矩阵 -->
      <el-card class="panel">
        <template #header>
          <div class="header-row">
            <span>混淆矩阵（行 = 人工真值，列 = agent 预测）</span>
            <el-tag :type="confusionConsistent ? 'success' : 'danger'" size="small">
              合计 {{ confusionGrandTotal }} / 有预测 {{ confusionScoredCount ?? '—' }}
            </el-tag>
          </div>
        </template>

        <el-table :data="confusionRows" border>
          <el-table-column label="人工真值 ↓ ／ 预测 →" min-width="220">
            <template #default="{ row }">
              <span class="mono">{{ row.gold }}</span>
            </template>
          </el-table-column>
          <el-table-column v-for="label in RECORD_LABELS" :key="label" :label="label" min-width="170">
            <template #default="{ row }">{{ row.cells[RECORD_LABELS.indexOf(label)] }}</template>
          </el-table-column>
          <el-table-column label="行合计" width="110">
            <template #default="{ row }">{{ row.total }}</template>
          </el-table-column>
        </el-table>
        <el-table class="block" :data="[{ cells: confusionColumnTotals, total: confusionGrandTotal }]" border>
          <el-table-column label="列合计" min-width="220" />
          <el-table-column v-for="label in RECORD_LABELS" :key="label" :label="label" min-width="170">
            <template #default="{ row }">{{ row.cells[RECORD_LABELS.indexOf(label)] }}</template>
          </el-table-column>
          <el-table-column label="总计" width="110">
            <template #default="{ row }">{{ row.total }}</template>
          </el-table-column>
        </el-table>

        <p v-if="confusionConsistent" class="muted">
          矩阵总和 {{ confusionGrandTotal }} 与有预测条数一致。对角线是判对的，
          「人工真值 = 未检索到正确资料／检索到正确资料但回答错误」却落在
          「回答正确」列的那一格，就是<strong>非正确类误判正确</strong>。
        </p>
        <el-alert
          v-else
          class="block"
          type="error"
          :closable="false"
          show-icon
          title="矩阵总和与有预测条数对不上：以报告 JSON 里的数字为准，并把这处差异反馈给开发者。"
        />
      </el-card>

      <!-- 3. 逐类指标 -->
      <el-card class="panel">
        <template #header>逐类指标</template>
        <el-table :data="perClassRows" border>
          <el-table-column prop="label" label="人工类别" min-width="220" />
          <el-table-column prop="tp" label="TP" width="80" />
          <el-table-column prop="fp" label="FP" width="80" />
          <el-table-column prop="fn" label="FN" width="80" />
          <el-table-column prop="support" label="该类条数" width="110" />
          <el-table-column label="精确率" min-width="200">
            <template #default="{ row }">{{ row.precision.display }}</template>
          </el-table-column>
          <el-table-column label="召回率" min-width="200">
            <template #default="{ row }">{{ row.recall.display }}</template>
          </el-table-column>
          <el-table-column label="F1" min-width="200">
            <template #default="{ row }">{{ row.f1.display }}</template>
          </el-table-column>
        </el-table>
        <p class="muted">
          Macro-F1 是三个 F1 的算术平均（不按条数加权）。精确率的分母是「被判成该类的条数」，
          召回率的分母是「该类人工条数」；任一为 0 时该项记「不可计算」。
        </p>
      </el-card>

      <!-- 1. 划分清单与每类数量 -->
      <el-card class="panel">
        <template #header>
          <div class="header-row">
            <span>划分清单与每类数量</span>
            <el-tag size="small" type="info">种子 {{ detail.split_info.seed }}</el-tag>
          </div>
        </template>

        <el-table :data="[detail.split_info]" border>
          <el-table-column prop="calibration" label="校准集条数" width="140" />
          <el-table-column prop="holdout" label="保留集条数" width="140" />
          <el-table-column label="校准集每类" min-width="320">
            <template #default="{ row }">{{ labelCounts(row.calibration_by_label) }}</template>
          </el-table-column>
          <el-table-column label="保留集每类" min-width="320">
            <template #default="{ row }">{{ labelCounts(row.holdout_by_label) }}</template>
          </el-table-column>
        </el-table>

        <p class="muted">
          划分按固定种子与「校准约 {{ detail.split_info.calibration_target }} 条、
          其余为保留集、按三类比例最大余数分配」的规则生成，因此同一份核定文件
          每次生成的划分完全一致。两集不交叉、合起来就是全部有效编号。
        </p>
        <p class="muted">
          强制进校准集的编号（早期调试用过的样本，不能进入未使用的保留集）：
          <span class="mono">{{ detail.split_info.forced_calibration.join('、') || '无' }}</span>
        </p>
        <p class="muted">
          本次评的是<strong>{{ splitText(detail.split) }}</strong>，共
          <span class="mono">{{ selectedSideCount }}</span> 条；编号见下方逐条对照。
        </p>

        <h4>被排除的编号</h4>
        <el-empty
          v-if="!detail.split_info.excluded_ids.length"
          description="没有排除项：全部有效编号都进了划分"
        />
        <el-table v-else :data="excludedRows" border>
          <el-table-column prop="id" label="编号" width="140" />
          <el-table-column prop="reason" label="排除理由" min-width="320" />
        </el-table>
      </el-card>

      <!-- 5. 逐条对照 -->
      <el-card class="panel">
        <template #header>
          <div class="header-row">
            <span>逐条对照（人工标签 vs agent 原预测）</span>
            <el-tag v-if="recordsPage" type="info" size="small">
              共 {{ recordsPage.total }} 条
            </el-tag>
          </div>
        </template>

        <el-alert v-if="recordHint" type="error" :closable="false" show-icon :title="recordHint" />

        <div class="filter-row">
          <el-select
            class="filter-select"
            :model-value="recordQuery.agree === null ? null : String(recordQuery.agree)"
            clearable
            placeholder="全部（是否一致）"
            @change="onAgreeFilterChange"
          >
            <el-option label="一致" value="true" />
            <el-option label="不一致" value="false" />
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

          <el-input
            class="filter-input"
            :model-value="recordQuery.recordId ?? ''"
            placeholder="编号（精确匹配）"
            clearable
            @change="onRecordIdFilterChange"
          />

          <el-button
            v-if="recordQuery.agree !== null || recordQuery.status || recordQuery.recordId"
            @click="clearRecordFilters"
          >
            清除筛选
          </el-button>
        </div>

        <p class="muted">
          筛选条件写在地址栏里（<span class="mono">eagree／estatus／erid</span>），刷新或把链接
          发给别人仍是这组筛选。「不一致」只筛<strong>有预测却判错</strong>的行；
          <strong>没有预测的行</strong>（技术失败、没跑完）在这里显示为「无预测」，
          要用「状态」筛选单独看——两类混在一起会把「没判出来」看成「判错了」。
          「评的是哪一侧」由上面的划分清单决定：换校准／保留要点评估历史里的「查看」。
        </p>

        <el-empty
          v-if="!records.length && !recordLoading"
          :description="recordQuery.agree !== null || recordQuery.status || recordQuery.recordId ? '这条筛选下没有记录：清除筛选可看全部' : '这份评估没有逐条记录'"
        />

        <el-table v-else :data="records" border @row-click="onRecordRowClick">
          <el-table-column prop="record_id" label="编号" width="140" />
          <el-table-column prop="gold_label" label="人工标签" min-width="220" />
          <el-table-column label="agent 原预测" min-width="220">
            <template #default="{ row }">{{ row.agent_label ?? '—（无预测）' }}</template>
          </el-table-column>
          <el-table-column label="是否一致" width="110">
            <template #default="{ row }">
              <el-tag :type="agreeTagType(row.agree)" size="small">{{ agreeText(row.agree) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="需复核" width="100">
            <template #default="{ row }">
              {{ row.review_required === null ? '—' : row.review_required ? '是' : '否' }}
            </template>
          </el-table-column>
          <el-table-column prop="record_status" label="记录状态" width="130" />
          <el-table-column prop="failure_stage" label="失败阶段" width="110">
            <template #default="{ row }">{{ row.failure_stage ?? '—' }}</template>
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
        <p class="muted">
          评的是 <strong>agent 的原预测</strong>（记录列表里的标签）：即使人工修订过，
          这里的对照也仍然用原预测，因为评估要衡量的是模型表现。
          复核标记不会把记录剔出评分，只体现在「复核比例」上。
        </p>
      </el-card>

      <!-- 6. 冻结清单与校准记录 -->
      <el-card class="panel">
        <template #header>冻结版本清单</template>
        <el-table
          :data="[
            { item: '程序版本', value: detail.freeze.program },
            { item: '阶段一提示词', value: detail.freeze.stage1_prompt_version },
            { item: '阶段二提示词', value: detail.freeze.stage2_prompt_version },
            { item: '阶段一 schema', value: detail.freeze.stage1_schema_version },
            { item: '阶段二 schema', value: detail.freeze.stage2_schema_version },
            { item: '输入合同', value: detail.freeze.input_contract_version },
            { item: '冻结时间', value: detail.freeze.frozen_at },
          ]"
          border
        >
          <el-table-column prop="item" label="项" width="180" />
          <el-table-column prop="value" label="值" min-width="320" />
        </el-table>

        <h4>数据与划分的版本</h4>
        <p class="muted">
          核定文件 <span class="mono">{{ detail.freeze.gold.filename }}</span>（数据版本
          {{ detail.freeze.gold.data_version }}，合同 {{ detail.freeze.gold.contract_version }}，
          sha256 <span class="mono">{{ detail.freeze.gold.sha256 }}</span>）；
          划分清单 <span class="mono">{{ detail.freeze.manifest.filename }}</span>
          （版本 {{ detail.freeze.manifest.version }}，种子 {{ detail.freeze.manifest.seed }}，
          sha256 <span class="mono">{{ detail.freeze.manifest.sha256 }}</span>）。
          清单记的核定文件摘要与本次一致：<strong>{{ detail.freeze.manifest.gold_sha256_match ? '是' : '否' }}</strong>
          ——不一致时评估会先被拒绝，不会算出一份对错版本的结论。
        </p>

        <h4>模型配置快照</h4>
        <p class="muted">
          与批次详情页的 <span class="mono">model_config</span> <strong>同一来源</strong>，
          两处不一致就说明快照取错了。这里<strong>不含任何凭据</strong>。
        </p>
        <pre class="json-block">{{ JSON.stringify(detail.freeze.model_config, null, 2) }}</pre>

        <h4>校准记录</h4>
        <p class="muted">
          本阶段的校修订与依据（`data/evaluation/calibration-report-v1.md`）属于
          <strong>S07-04</strong>：那一步需要在校准集上跑真实模型，**须另行授权**，
          目前尚未开始。因此这里还没有可展开的修订条目——这是真实的空缺，
          不是页面没做。
        </p>

        <h4>报告文件</h4>
        <el-table :data="detail.report_files" border>
          <el-table-column prop="name" label="文件名" min-width="320" />
          <el-table-column prop="kind" label="类型" width="100" />
          <el-table-column label="大小" width="120">
            <template #default="{ row }">{{ row.size_bytes === null ? '—' : `${row.size_bytes} 字节` }}</template>
          </el-table-column>
          <el-table-column label="是否存在" width="110">
            <template #default="{ row }">
              <el-tag :type="row.exists ? 'success' : 'danger'" size="small">
                {{ row.exists ? '是' : '否' }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>
        <p class="muted">
          文件落在数据目录的 <span class="mono">evaluation/reports/</span> 下
          （本机路径不在页面上显示）。文本报告可直接用上方「下载报告」存盘；
          JSON 报告与 Markdown 报告内容同源，JSON 更适合逐条核对。
        </p>
      </el-card>

      <!-- 计算入口只有 CLI：这一页把命令显示出来，但**不提供执行按钮** -->
      <el-card class="panel">
        <template #header>这一份评估是怎么来的</template>
        <p class="muted">
          命令行入口（网页不发起计算，也不调用模型；本页没有对应的按钮）：
        </p>
        <pre class="json-block">{{ cliHint }}</pre>
        <p class="muted">
          退出码：<span class="mono">0</span> 成功／<span class="mono">1</span> 配置或环境问题／
          <span class="mono">2</span> 参数或状态不适用（例如编号集合与划分不一致、批次还没跑完）。
          评估<strong>不依赖 worker</strong>、<strong>零模型调用</strong>，读的是批次里已有的原预测。
        </p>
      </el-card>
    </template>
  </div>
</template>

<style scoped>
.evaluation-page {
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

.block {
  margin-top: 1rem;
}

.filter-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
  margin: 0.75rem 0;
}

.filter-select {
  width: 200px;
}

.filter-input {
  width: 220px;
}

.pager {
  margin-top: 0.75rem;
}

.json-block {
  margin: 0.5rem 0 1rem;
  padding: 0.75rem;
  overflow-x: auto;
  background: var(--el-fill-color-light);
  border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 0.85rem;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
